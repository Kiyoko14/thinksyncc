from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=30.0,
)


async def init_http_client() -> None:
    global _client
    _client = httpx.AsyncClient(limits=_LIMITS, timeout=None)
    logger.info("HTTP client pool initialised (max=%d keepalive=%d)",
                _LIMITS.max_connections, _LIMITS.max_keepalive_connections)


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("HTTP client pool closed")


def get_http_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialised")
    return _client
