from __future__ import annotations

import asyncio
import logging

import httpx

from services.port_allocator import (
    _ACTIVE_SET,
    check_port_consistency,
    get_port,
    mark_workspace_health,
)
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

_PING_INTERVAL = 30
_PING_TIMEOUT = 5.0


async def _ping_workspace(workspace_id: str, port: int) -> None:
    health_url = f"http://127.0.0.1:{port}/health"
    fallback_url = f"http://127.0.0.1:{port}/"
    healthy = False
    try:
        async with httpx.AsyncClient(timeout=_PING_TIMEOUT) as client:
            try:
                resp = await client.get(health_url)
                healthy = resp.status_code < 500
            except (httpx.ConnectError, httpx.TimeoutException):
                resp = await client.get(fallback_url)
                healthy = resp.status_code < 500
    except Exception:
        healthy = False
    mark_workspace_health(workspace_id, healthy=healthy)


async def _collect_active_workspaces() -> list[tuple[str, int]]:
    """
    Read from ws:active set — O(n) where n = active workspaces only,
    instead of scanning all ws:*:port keys.
    """
    r = RedisService.get_async_client()
    if r is None:
        return []
    results: list[tuple[str, int]] = []
    try:
        members = await r.smembers(_ACTIVE_SET)
        for workspace_id in members:
            raw_port = await r.get(f"ws:{workspace_id}:port")
            if raw_port is not None:
                results.append((workspace_id, int(raw_port)))
    except Exception as exc:
        logger.warning("Health checker: failed reading active workspaces — %s", exc)
    return results


async def run_health_check_loop() -> None:
    logger.info("Health checker: background loop started (interval=%ds)", _PING_INTERVAL)
    while True:
        try:
            workspaces = await _collect_active_workspaces()
            if workspaces:
                await asyncio.gather(
                    *(_ping_workspace(ws_id, port) for ws_id, port in workspaces),
                    return_exceptions=True,
                )
                logger.debug("Health checker: checked %d active workspace(s)", len(workspaces))
        except Exception as exc:
            logger.warning("Health checker: loop iteration error — %s", exc)
        await asyncio.sleep(_PING_INTERVAL)


async def run_startup_consistency_check() -> None:
    workspaces = await _collect_active_workspaces()
    for ws_id, _ in workspaces:
        check_port_consistency(ws_id)
    if workspaces:
        logger.info("Startup consistency check: verified %d active workspace(s)", len(workspaces))
