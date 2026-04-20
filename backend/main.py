from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError
from starlette.responses import Response

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any

from core.config import get_settings
from routers import agents, auth, chat, commands, deployments, gateway, health, jobs, servers, workspaces, ws
from services.health_checker import run_health_check_loop, run_startup_consistency_check
from services.http_client import close_http_client, init_http_client

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application):  # type: ignore[type-arg]
    await init_http_client()
    await run_startup_consistency_check()
    task = asyncio.create_task(run_health_check_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_http_client()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
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
app.include_router(gateway.router)


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


def _stringify_error_detail(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        if isinstance(detail.get("message"), str) and detail["message"].strip():
            return detail["message"].strip()
        if isinstance(detail.get("error"), str) and detail["error"].strip():
            return detail["error"].strip()
        if isinstance(detail.get("detail"), str) and detail["detail"].strip():
            return detail["detail"].strip()
        try:
            return json.dumps(detail, ensure_ascii=False)
        except Exception:
            return str(detail)
    if isinstance(detail, list):
        try:
            return json.dumps(detail, ensure_ascii=False)
        except Exception:
            return str(detail)
    return str(detail)


def _error_payload(message: str, *, request: Request | None = None, code: str | None = None, exc: Exception | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": message or "Unknown error"}
    if code:
        payload["code"] = code
    if request is not None:
        payload["path"] = request.url.path
    if settings.DEBUG and exc is not None:
        payload["exception_type"] = type(exc).__name__
        payload["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return payload


@app.exception_handler(APIError)
async def handle_postgrest_error(request: Request, exc: APIError) -> JSONResponse:
    code = _api_error_code(exc)
    message = _stringify_error_detail(getattr(exc, "args", [""])[0] if getattr(exc, "args", None) else exc)
    if code == "22P02":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_payload(message or "Invalid request data.", request=request, code=code, exc=exc),
        )
    if code in {"42501", "23503"}:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=_error_payload(message or "Access denied.", request=request, code=code, exc=exc),
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_payload(message or "Database error", request=request, code=code or "DB_ERROR", exc=exc),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=_error_payload(_stringify_error_detail(exc.errors()), request=request, code="INVALID_REQUEST", exc=exc),
    )


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    message = _stringify_error_detail(exc.detail)
    code = str(exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(message, request=request, code=code, exc=exc),
    )


@app.exception_handler(Exception)
async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_payload(f"{type(exc).__name__}: {exc}", request=request, code="INTERNAL_ERROR", exc=exc),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.DEBUG,
    )
