from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError
from starlette.responses import Response

import json
import logging

from core.config import get_settings
from routers import agents, auth, chat, commands, deployments, health, jobs, servers, workspaces, ws

settings = get_settings()
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    # Disable interactive docs in production to reduce attack surface.
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _redact(obj: object) -> object:
    sensitive = {"password", "ssh_password", "ssh_key", "access_token", "authorization"}
    if isinstance(obj, dict):
        redacted: dict[str, object] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in sensitive:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(value)
        return redacted
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


@app.middleware("http")
async def log_http_requests(request: Request, call_next):  # type: ignore[override]
    # Log only JSONish bodies; keep size bounded.
    raw_body = await request.body()
    body_preview = raw_body[:2048]
    parsed_body: object | None = None
    if body_preview:
        try:
            parsed_body = json.loads(body_preview.decode("utf-8"))
        except Exception:
            parsed_body = None

    logger.info(
        "[http] request | method=%s | path=%s | query=%s | body=%s",
        request.method,
        request.url.path,
        request.url.query,
        _redact(parsed_body) if parsed_body is not None else body_preview.decode("utf-8", "replace"),
    )

    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001
        logger.exception("[http] exception | method=%s | path=%s", request.method, request.url.path)
        raise

    # Capture response body for logging; rebuild response.
    resp_chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        resp_chunks.append(chunk)
    resp_body = b"".join(resp_chunks)
    logger.info(
        "[http] response | method=%s | path=%s | status=%s | body=%s",
        request.method,
        request.url.path,
        response.status_code,
        resp_body[:2048].decode("utf-8", "replace"),
    )

    return Response(
        content=resp_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )

# Health check lives at the root level.
app.include_router(health.router)

# Application API routes.
app.include_router(auth.router)
app.include_router(servers.router)
app.include_router(commands.router)
app.include_router(workspaces.router)
app.include_router(chat.router)
app.include_router(deployments.router)
app.include_router(agents.router)
app.include_router(jobs.router)
app.include_router(ws.router)


def _api_error_code(exc: APIError) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code.upper()

    first_arg = exc.args[0] if exc.args else None
    if isinstance(first_arg, dict):
        raw_code = first_arg.get("code")
        if isinstance(raw_code, str):
            return raw_code.upper()

    return ""


@app.exception_handler(APIError)
async def handle_postgrest_error(_: Request, exc: APIError) -> JSONResponse:
    code = _api_error_code(exc)
    message = str(exc) or "PostgREST error"
    if code == "22P02":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": {"code": code, "message": message}},
        )
    if code in {"42501", "23503"}:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": {"code": code, "message": message}},
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": {"code": code or "DB_ERROR", "message": message}},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.DEBUG,
    )
