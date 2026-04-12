from __future__ import annotations

import logging

import redis
import redis.asyncio as aioredis

from core.config import get_settings

logger = logging.getLogger(__name__)

_sync_client: redis.Redis | None = None
_async_client: aioredis.Redis | None = None


class RedisService:
    @staticmethod
    def get_sync_client() -> redis.Redis | None:
        global _sync_client
        settings = get_settings()
        if not settings.REDIS_URL:
            return None
        if _sync_client is None:
            try:
                _sync_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            except Exception as exc:
                logger.warning("Failed to initialize sync Redis client: %s", exc)
                return None
        return _sync_client

    @staticmethod
    def get_async_client() -> aioredis.Redis | None:
        global _async_client
        settings = get_settings()
        if not settings.REDIS_URL:
            return None
        if _async_client is None:
            try:
                _async_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            except Exception as exc:
                logger.warning("Failed to initialize async Redis client: %s", exc)
                return None
        return _async_client
