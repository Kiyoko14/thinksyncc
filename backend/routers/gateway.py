from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from services.domain_service import get_workspace_by_domain
from services.port_allocator import get_port

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gateway"])

_SUPPORTED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _extract_subdomain(host: str) -> str | None:
    host = (host or "").split(":")[0].lower().strip()
    parts = host.split(".")
    if len(parts) >= 3:
        return parts[0]
    return None


@router.api_route(
    "/gateway/{path:path}",
    methods=list(_SUPPORTED_METHODS),
    include_in_schema=False,
)
async def proxy_request(path: str, request: Request) -> Response:
    host_header = request.headers.get("host", "")
    subdomain = _extract_subdomain(host_header)

    if not subdomain:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or invalid Host header — cannot resolve workspace"},
        )

    workspace_id = get_workspace_by_domain(subdomain)
    if not workspace_id:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "error": f"No workspace found for subdomain '{subdomain}'"},
        )

    port = get_port(workspace_id)
    if not port:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "Workspace has no allocated port"},
        )

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body()
    headers = _forward_headers(request)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
    except httpx.ConnectError:
        logger.warning("Gateway: upstream unreachable at port %d for workspace %s", port, workspace_id)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Upstream workspace is not reachable"},
        )
    except Exception as exc:
        logger.exception("Gateway: unexpected proxy error for workspace %s: %s", workspace_id, exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "Proxy error"},
        )

    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
