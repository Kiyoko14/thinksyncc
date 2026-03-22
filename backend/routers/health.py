from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    return {
        "status": "ok",
        "service": "ThinkSync API",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
