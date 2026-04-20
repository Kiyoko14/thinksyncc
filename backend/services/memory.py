from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from services.redis_service import RedisService

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MemoryConfig:
    key_prefix: str = "agent_memory"
    max_items: int = 50
    ttl_seconds: int = 60 * 60 * 24


class MemoryStore:
    """Persistent, lightweight agent memory backed by Redis (best-effort)."""

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self._config = config or MemoryConfig()

    def _key(self, *, user_id: str, workspace_id: str) -> str:
        return f"{self._config.key_prefix}:{user_id}:{workspace_id}"

    async def load(self, *, user_id: str, workspace_id: str, limit: int = 12) -> list[dict[str, Any]]:
        redis = RedisService.get_async_client()
        if redis is None:
            return []
        try:
            raw_items = await redis.lrange(self._key(user_id=user_id, workspace_id=workspace_id), -limit, -1)
        except Exception as exc:
            logger.warning("Memory load failed: %s", exc)
            return []

        items: list[dict[str, Any]] = []
        for raw in raw_items or []:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    items.append(payload)
            except Exception:
                continue
        return items

    async def append(self, *, user_id: str, workspace_id: str, item: dict[str, Any]) -> None:
        redis = RedisService.get_async_client()
        if redis is None:
            return

        settings = get_settings()
        ttl = int(settings.REDIS_CHAT_MEMORY_TTL_SECONDS or self._config.ttl_seconds)
        max_items = int(self._config.max_items)
        payload = dict(item)
        payload.setdefault("timestamp", _now_iso())

        try:
            key = self._key(user_id=user_id, workspace_id=workspace_id)
            await redis.rpush(key, json.dumps(payload))
            await redis.ltrim(key, -max_items, -1)
            if ttl > 0:
                await redis.expire(key, ttl)
        except Exception as exc:
            logger.warning("Memory append failed: %s", exc)

