from __future__ import annotations

import asyncio
import logging

import httpx

from services.port_allocator import check_port_consistency, mark_workspace_health
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

_PING_INTERVAL = 30
_PING_TIMEOUT = 5.0
_KEY_PATTERN = "ws:*:port"


async def _ping_workspace(workspace_id: str, port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    try:
        async with httpx.AsyncClient(timeout=_PING_TIMEOUT) as client:
            resp = await client.get(url)
        healthy = resp.status_code < 500
    except Exception:
        healthy = False
    mark_workspace_health(workspace_id, healthy=healthy)


async def _collect_workspace_ids() -> list[tuple[str, int]]:
    r = RedisService.get_async_client()
    if r is None:
        return []
    results: list[tuple[str, int]] = []
    try:
        async for key in r.scan_iter(_KEY_PATTERN):
            raw_port = await r.get(key)
            if raw_port is None:
                continue
            parts = key.split(":")
            if len(parts) >= 3:
                workspace_id = parts[1]
                results.append((workspace_id, int(raw_port)))
    except Exception as exc:
        logger.warning("Health checker: failed scanning workspaces — %s", exc)
    return results


async def run_health_check_loop() -> None:
    logger.info("Health checker: background loop started (interval=%ds)", _PING_INTERVAL)
    while True:
        try:
            workspaces = await _collect_workspace_ids()
            if workspaces:
                await asyncio.gather(
                    *(_ping_workspace(ws_id, port) for ws_id, port in workspaces),
                    return_exceptions=True,
                )
                logger.debug("Health checker: checked %d workspace(s)", len(workspaces))
        except Exception as exc:
            logger.warning("Health checker: loop iteration error — %s", exc)
        await asyncio.sleep(_PING_INTERVAL)


async def run_startup_consistency_check() -> None:
    workspaces = await _collect_workspace_ids()
    for ws_id, _ in workspaces:
        check_port_consistency(ws_id)
    if workspaces:
        logger.info("Startup consistency check: verified %d workspace(s)", len(workspaces))
