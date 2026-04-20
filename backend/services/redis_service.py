from __future__ import annotations

import logging
from urllib.parse import urlparse, unquote

import redis
import redis.asyncio as aioredis

from core.config import get_settings

logger = logging.getLogger(__name__)

_sync_client: redis.Redis | None = None
_async_client: aioredis.Redis | None = None


def _build_async_client(url: str) -> aioredis.Redis:
    """
    Build an async Redis client from a URL string.
    Handles rediss:// (TLS) URLs including those with tokens/passwords that
    contain characters that trip up the standard from_url parser.
    """
    parsed = urlparse(url)
    use_ssl = parsed.scheme in ("rediss", "https")

    host = parsed.hostname or "localhost"

    try:
        port: int = parsed.port or (6380 if use_ssl else 6379)
    except ValueError:
        port = 6380 if use_ssl else 6379

    # netloc format: user:password@host:port  — when port is unparseable the
    # password may contain a colon that shifted the parsing boundary.
    # Re-parse manually as a fallback.
    if not parsed.hostname:
        try:
            netloc = parsed.netloc
            at_idx = netloc.rfind("@")
            hostport = netloc[at_idx + 1:] if at_idx != -1 else netloc
            userinfo = netloc[:at_idx] if at_idx != -1 else ""
            colon_idx = hostport.rfind(":")
            if colon_idx != -1:
                host = hostport[:colon_idx]
                candidate = hostport[colon_idx + 1:]
                port = int(candidate) if candidate.isdigit() else port
            else:
                host = hostport
            if userinfo:
                colon_ui = userinfo.find(":")
                if colon_ui != -1:
                    parsed = parsed._replace(
                        username=unquote(userinfo[:colon_ui]),
                        password=unquote(userinfo[colon_ui + 1:]),
                    )
        except Exception:
            pass

    password = unquote(parsed.password) if parsed.password else None
    username = unquote(parsed.username) if parsed.username else None
    db_part = (parsed.path or "/0").lstrip("/")
    db = int(db_part) if db_part.isdigit() else 0

    return aioredis.Redis(
        host=host,
        port=port,
        password=password,
        username=username,
        db=db,
        ssl=use_ssl,
        ssl_cert_reqs=None if use_ssl else None,
        decode_responses=True,
    )


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
            url = settings.REDIS_URL
            try:
                _async_client = _build_async_client(url)
            except Exception as exc:
                logger.warning("Failed to initialize async Redis client: %s", exc)
                return None
        return _async_client
