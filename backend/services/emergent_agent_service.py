import logging
from typing import Any

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


class EmergentE1Service:
    """Legacy E1 service (DEPRECATED). Use Forge v2 pipeline via AgentService."""

    @staticmethod
    async def run(*args, **kwargs) -> Any:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Emergent E1 is disabled. Use the unified Forge v2 pipeline.",
        )

    @staticmethod
    async def get_plan(*args, **kwargs) -> Any:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Emergent E1 is disabled. Use the unified Forge v2 pipeline.",
        )
