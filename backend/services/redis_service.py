from __future__ import annotations

import logging
from urllib.parse import quote

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from core.config import get_settings

logger = logging.getLogger(__name__)


def _normalize_userinfo(url: str) -> str:
    """
    Percent-encode unsafe characters inside the userinfo portion of a Redis URL
    so urllib's parser does not misread the host:port boundary. Required for
    real-world Redis passwords containing characters like '>', '<', '@', etc.
    """
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    head = url[: scheme_sep + 3]
    rest = url[scheme_sep + 3 :]
    at_idx = rest.rfind("@")
    if at_idx == -1:
        return url
    userinfo = rest[:at_idx]
    hostpart = rest[at_idx:]
    if ":" in userinfo:
        user, password = userinfo.split(":", 1)
        userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
    else:
        userinfo = quote(userinfo, safe="")
    return f"{head}{userinfo}{hostpart}"


def _validated_url() -> str:
    settings = get_settings()
    url = settings.REDIS_URL
    if not url or not url.startswith(("redis://", "rediss://")):
        raise RuntimeError("Invalid REDIS_URL. Must use redis:// or rediss://")
    return _normalize_userinfo(url)


_REDIS_URL = _validated_url()

_sync_client: Redis = Redis.from_url(_REDIS_URL, decode_responses=True)
_async_client: AsyncRedis = AsyncRedis.from_url(_REDIS_URL, decode_responses=True)


def init_redis() -> None:
    _sync_client.ping()


async def init_async_redis() -> None:
    await _async_client.ping()


class RedisService:
    @staticmethod
    def get_sync_client() -> Redis:
        return _sync_client

    @staticmethod
    def get_async_client() -> AsyncRedis:
        return _async_client


def get_sync_client() -> Redis:
    return _sync_client


def get_async_client() -> AsyncRedis:
    return _async_client
