from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import defaultdict

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from services.domain_service import get_workspace_by_domain
from services.http_client import get_http_client
from services.port_allocator import get_port, get_workspace_health, mark_workspace_health
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gateway"])

_SUPPORTED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

_ALLOWED_HOST_SUFFIX = "thinksync.art"

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]{1,10}-[a-z0-9]{6}$")
_RESERVED = {"app", "api", "www"}

_RATE_LIMIT_MAX = 100
_RATE_LIMIT_WINDOW = 60

_PER_WS_CONCURRENCY = 10
_ws_semaphores: defaultdict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(_PER_WS_CONCURRENCY)
)

_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local uid = ARGV[4]
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
redis.call('ZADD', key, now, uid)
redis.call('EXPIRE', key, window + 1)
local count = redis.call('ZCARD', key)
if tonumber(count) > tonumber(limit) then return 1 else return 0 end
"""

_UNSAFE_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_UNSAFE_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def _forward_request_headers(request: Request, request_id: str) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _UNSAFE_REQUEST_HEADERS
    }
    headers["x-request-id"] = request_id
    return headers


def _forward_response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _UNSAFE_RESPONSE_HEADERS
    }


def _extract_subdomain(host: str) -> str | None:
    bare = host.split(":")[0].lower().strip()
    if not bare.endswith(_ALLOWED_HOST_SUFFIX):
        return None
    without_base = bare[: -(len(_ALLOWED_HOST_SUFFIX))].rstrip(".")
    if not without_base:
        return None
    first = without_base.split(".")[0].strip()
    return first if first else None


def _resolve_timeout(path: str) -> float:
    clean = path.lstrip("/")
    if clean.startswith("api"):
        return 15.0
    return 5.0


async def _is_rate_limited(workspace_id: str, client_ip: str) -> bool:
    r = RedisService.get_async_client()
    if r is None:
        raise RuntimeError("Rate limiter unavailable")
    key = f"rate:sw:{workspace_id}:{client_ip}"
    now = time.time()
    uid = f"{now:.6f}-{uuid.uuid4().hex[:8]}"
    result = await r.eval(
        _SLIDING_WINDOW_SCRIPT, 1,
        key,
        str(now), str(_RATE_LIMIT_WINDOW), str(_RATE_LIMIT_MAX), uid,
    )
    return bool(result)


@router.api_route(
    "/{path:path}",
    methods=list(_SUPPORTED_METHODS),
    include_in_schema=False,
)
async def proxy_request(path: str, request: Request) -> Response:
    request_id = str(uuid.uuid4())
    host_header = request.headers.get("host", "")
    subdomain = _extract_subdomain(host_header)

    if not subdomain:
        return JSONResponse(status_code=404, content={"error": "No subdomain"})

    if subdomain in _RESERVED:
        return JSONResponse(status_code=404, content={"error": "Reserved domain"})

    if not _SUBDOMAIN_RE.match(subdomain):
        return JSONResponse(status_code=404, content={"error": "Invalid subdomain"})

    try:
        workspace_id = get_workspace_by_domain(subdomain)
    except Exception:
        return JSONResponse(status_code=503, content={"error": "Routing unavailable"})

    if not workspace_id:
        return JSONResponse(status_code=404, content={"error": "Workspace not found"})

    try:
        health = get_workspace_health(workspace_id)
    except Exception:
        return JSONResponse(status_code=503, content={"error": "Health lookup failed"})

    if health == "unhealthy":
        return JSONResponse(status_code=503, content={"error": "Workspace unavailable"})

    try:
        port = get_port(workspace_id)
    except Exception:
        return JSONResponse(status_code=503, content={"error": "Port lookup failed"})

    if not port:
        return JSONResponse(status_code=500, content={"error": "Port missing"})

    logger.info("GW route | subdomain=%s ws=%s port=%s", subdomain, workspace_id, port)

    client_ip = request.client.host if request.client else "unknown"
    try:
        limited = await _is_rate_limited(workspace_id, client_ip)
    except Exception:
        return JSONResponse(status_code=503, content={"error": "Rate limiter down"})

    if limited:
        logger.warning(
            "rid=%s Gateway: rate limit exceeded | ip=%s workspace_id=%s subdomain=%s",
            request_id, client_ip, workspace_id, subdomain,
        )
        return JSONResponse(
            status_code=429,
            content={"status": "error", "error": "Too many requests — rate limit exceeded"},
        )
    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body()
    headers = _forward_request_headers(request, request_id)
    timeout = _resolve_timeout(path)
    semaphore = _ws_semaphores[workspace_id]

    if semaphore.locked():
        return JSONResponse(
            status_code=429,
            content={"error": "Too many concurrent requests"}
        )

    try:
        async with semaphore:
            upstream = await get_http_client().request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                timeout=timeout,
            )
    except httpx.TimeoutException:
        mark_workspace_health(workspace_id, healthy=False)
        logger.warning(
            "rid=%s Gateway: timeout | subdomain=%s workspace_id=%s port=%d timeout=%.1fs",
            request_id, subdomain, workspace_id, port, timeout,
        )
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Upstream workspace timed out"},
        )
    except httpx.ConnectError:
        mark_workspace_health(workspace_id, healthy=False)
        logger.warning(
            "rid=%s Gateway: connect error | subdomain=%s workspace_id=%s port=%d",
            request_id, subdomain, workspace_id, port,
        )
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Upstream workspace is not reachable"},
        )
    except Exception as exc:
        mark_workspace_health(workspace_id, healthy=False)
        logger.exception(
            "rid=%s Gateway: unexpected error | subdomain=%s workspace_id=%s port=%d error=%s",
            request_id, subdomain, workspace_id, port, exc,
        )
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Proxy error"},
        )

    mark_workspace_health(workspace_id, healthy=True)

    logger.info(
        "rid=%s Gateway: ok | subdomain=%s workspace_id=%s port=%d status=%d timeout=%.1fs",
        request_id, subdomain, workspace_id, port, upstream.status_code, timeout,
    )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_forward_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )
