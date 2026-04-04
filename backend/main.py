from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError

from core.config import get_settings
from routers import agents, auth, chat, commands, deployments, health, servers, workspaces

settings = get_settings()

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

# Health check lives at the root level (no versioning needed).
app.include_router(health.router)

# Versioned API routes.
app.include_router(auth.router, prefix="/api/v1")
app.include_router(servers.router, prefix="/api/v1")
app.include_router(commands.router, prefix="/api/v1")
app.include_router(workspaces.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(deployments.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")


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
    if code == "22P02":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Invalid request data"},
        )
    if code in {"42501", "23503"}:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Access denied"},
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Database operation failed"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.DEBUG,
    )
