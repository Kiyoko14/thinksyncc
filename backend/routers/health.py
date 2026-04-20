from datetime import datetime, timezone

from fastapi import APIRouter

from services.logger import METRICS

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    return {
        "status": "ok",
        "service": "ThinkSync API",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/metrics")
async def metrics() -> dict:
    return METRICS.snapshot().to_public()
