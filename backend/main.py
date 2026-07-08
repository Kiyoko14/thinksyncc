from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError
from starlette.responses import Response
from dotenv import load_dotenv
load_dotenv()

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


async def _run_startup_diagnostics() -> dict[str, Any]:
    """Run startup diagnostics and report actionable issues.

    Checks:
      - Redis connectivity (sync + async)
      - Database schema: new tables exist
      - Migration status: critical columns exist
    """
    from services.redis_service import get_async_client, get_sync_client
    from core.database import get_supabase

    diagnostics: dict[str, Any] = {
        "redis_sync": {"ok": False, "message": "Not checked"},
        "redis_async": {"ok": False, "message": "Not checked"},
        "db_tables": {"ok": False, "missing": []},
        "db_columns": {"ok": False, "missing": []},
        "ready": False,
    }

    # Redis sync
    try:
        get_sync_client().ping()
        diagnostics["redis_sync"] = {"ok": True, "message": "Connected"}
    except Exception as exc:
        diagnostics["redis_sync"] = {"ok": False, "message": f"Failed: {exc}"}
        logger.warning("Redis sync unavailable: %s", exc)

    # Redis async
    try:
        await get_async_client().ping()
        diagnostics["redis_async"] = {"ok": True, "message": "Connected"}
    except Exception as exc:
        diagnostics["redis_async"] = {"ok": False, "message": f"Failed: {exc}"}
        logger.warning("Redis async unavailable: %s", exc)

    # Database tables
    required_tables = [
        "job_steps",
        "job_decisions",
        "job_retries",
        "job_execution_details",
    ]
    missing_tables: list[str] = []
    for table in required_tables:
        try:
            get_supabase().table(table).select("id", count="exact").limit(0).execute()
        except Exception as exc:
            missing_tables.append(table)
            logger.warning("Missing table %s: %s", table, exc)
    diagnostics["db_tables"] = {"ok": not missing_tables, "missing": missing_tables}

    # Database columns
    required_columns = [
        ("jobs", "deleted_at"),
        ("jobs", "recoverable"),
        ("jobs", "recovery_reason"),
        ("job_events", "trace_id"),
    ]
    missing_columns: list[str] = []
    for table, column in required_columns:
        try:
            get_supabase().table(table).select(column).limit(1).execute()
        except Exception as exc:
            missing_columns.append(f"{table}.{column}")
            logger.warning("Missing column %s.%s: %s", table, column, exc)
    diagnostics["db_columns"] = {"ok": not missing_columns, "missing": missing_columns}

    diagnostics["ready"] = (
        diagnostics["redis_sync"]["ok"]
        and not missing_tables
        and not missing_columns
    )

    if not diagnostics["ready"]:
        logger.warning("Startup diagnostics: %s", json.dumps(diagnostics, default=str))
    else:
        logger.info("Startup diagnostics: all checks passed")

    return diagnostics


@asynccontextmanager
async def lifespan(application):  # type: ignore[type-arg]
    diagnostics = await _run_startup_diagnostics()
    await init_http_client()
    await run_startup_consistency_check()

    # Sprint 3A.2 / 3A.3 — Task 1+3: fail fast if APPROVAL_RESUME_SECRET is missing
    from core.config import get_settings as _get_settings
    from models.approval import ApprovalConfigurationError
    _settings = _get_settings()
    if not getattr(_settings, "APPROVAL_RESUME_SECRET", None):
        raise ApprovalConfigurationError(
            "APPROVAL_RESUME_SECRET is not set. "
            "Set it to a long random secret in your .env file. "
            "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )

    # Sprint 3B.2 — Objective 3: startup verification
    from services.conversation_reliability import StartupVerifier, StartupVerificationError
    try:
        await StartupVerifier.verify()
    except StartupVerificationError:
        raise  # let the typed exception propagate unchanged
    except Exception as exc:
        raise StartupVerificationError([str(exc)]) from exc

    # Health check loop
    health_task = asyncio.create_task(run_health_check_loop())

    # Worker recovery loop — detect stale jobs and dead workers
    from services.worker_service import WorkerService
    async def _recovery_loop() -> None:
        consecutive_failures = 0
        while True:
            try:
                await asyncio.sleep(60)
                # Run sync DB calls in thread pool to avoid blocking
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, WorkerService.recover_stale_jobs)
                await loop.run_in_executor(None, WorkerService.cleanup_dead_workers)
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error(
                    "[recovery_loop] error (consecutive=%s): %s",
                    consecutive_failures,
                    exc,
                    exc_info=(consecutive_failures >= 3),
                )
    recovery_task = asyncio.create_task(_recovery_loop())

    yield
    health_task.cancel()
    recovery_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass
    try:
        await recovery_task
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




# keyin qolganlar
app.include_router(health.router)
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
