from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from services.domain_service import get_workspace_by_domain
from services.port_allocator import check_port_consistency, get_port, mark_workspace_health
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gateway"])

_SUPPORTED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

_ALLOWED_HOST_SUFFIX = "thinksync.art"

_RATE_LIMIT_MAX = 100
_RATE_LIMIT_WINDOW = 60

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


def _forward_request_headers(request: Request) -> dict[str, str]:
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _UNSAFE_REQUEST_HEADERS
    }


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
    without_base = bare[: -(len(_ALLOWED_HOST_SUFFIX))]
    without_base = without_base.rstrip(".")
    if not without_base:
        return None
    return without_base.split(".")[0]


async def _is_rate_limited(client_ip: str) -> bool:
    r = RedisService.get_async_client()
    if r is None:
        return False
    key = f"rate:{client_ip}"
    try:
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, _RATE_LIMIT_WINDOW)
        return int(count) > _RATE_LIMIT_MAX
    except Exception as exc:
        logger.warning("Rate limit check failed for %s: %s", client_ip, exc)
        return False


@router.api_route(
    "/gateway/{path:path}",
    methods=list(_SUPPORTED_METHODS),
    include_in_schema=False,
)
async def proxy_request(path: str, request: Request) -> Response:
    host_header = request.headers.get("host", "")
    bare_host = host_header.split(":")[0].lower().strip()

    if not bare_host.endswith(_ALLOWED_HOST_SUFFIX):
        logger.warning("Gateway: rejected invalid host '%s'", bare_host)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid host — only thinksync.art subdomains are accepted"},
        )

    subdomain = _extract_subdomain(host_header)
    if not subdomain:
        logger.warning("Gateway: could not extract subdomain from host '%s'", bare_host)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or invalid Host header — cannot resolve workspace"},
        )

    client_ip = request.client.host if request.client else "unknown"
    if await _is_rate_limited(client_ip):
        logger.warning("Gateway: rate limit exceeded for ip=%s subdomain=%s", client_ip, subdomain)
        return JSONResponse(
            status_code=429,
            content={"status": "error", "error": "Too many requests — rate limit exceeded"},
        )

    workspace_id = get_workspace_by_domain(subdomain)
    if not workspace_id:
        logger.warning("Gateway: no workspace for subdomain='%s'", subdomain)
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error": f"No workspace found for subdomain '{subdomain}'"},
        )

    check_port_consistency(workspace_id)

    port = get_port(workspace_id)
    if not port:
        logger.error(
            "Gateway: workspace_id=%s subdomain=%s has no allocated port",
            workspace_id, subdomain,
        )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Workspace has no allocated port"},
        )

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body()
    headers = _forward_request_headers(request)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
    except httpx.TimeoutException:
        mark_workspace_health(workspace_id, healthy=False)
        logger.warning(
            "Gateway: timeout | subdomain=%s workspace_id=%s port=%d",
            subdomain, workspace_id, port,
        )
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Upstream workspace timed out"},
        )
    except httpx.ConnectError:
        mark_workspace_health(workspace_id, healthy=False)
        logger.warning(
            "Gateway: connect error | subdomain=%s workspace_id=%s port=%d",
            subdomain, workspace_id, port,
        )
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Upstream workspace is not reachable"},
        )
    except Exception as exc:
        mark_workspace_health(workspace_id, healthy=False)
        logger.exception(
            "Gateway: unexpected error | subdomain=%s workspace_id=%s port=%d error=%s",
            subdomain, workspace_id, port, exc,
        )
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Proxy error"},
        )

    mark_workspace_health(workspace_id, healthy=True)

    logger.info(
        "Gateway: proxied | subdomain=%s workspace_id=%s port=%d status=%d",
        subdomain, workspace_id, port, upstream.status_code,
    )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_forward_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )
