from __future__ import annotations

import asyncio
import logging

import httpx

from services.port_allocator import (
    _ACTIVE_SET,
    check_port_consistency,
    mark_workspace_health,
    remove_from_active,
)
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

_PING_INTERVAL = 30
_PING_TIMEOUT = 5.0
_EVICT_AFTER_FAILURES = 5

_detected_endpoints: dict[str, str] = {}
_unhealthy_streak: dict[str, int] = {}


async def _ping_workspace(workspace_id: str, port: int) -> None:
    detected = _detected_endpoints.get(workspace_id)

    async with httpx.AsyncClient(timeout=_PING_TIMEOUT) as client:
        if detected == "health":
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
                healthy = resp.status_code < 500
            except Exception:
                healthy = False

        elif detected == "root":
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/")
                healthy = resp.status_code < 500
            except Exception:
                healthy = False

        else:
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
                if resp.status_code < 500:
                    _detected_endpoints[workspace_id] = "health"
                    healthy = True
                else:
                    _detected_endpoints[workspace_id] = "root"
                    healthy = resp.status_code < 500
            except (httpx.ConnectError, httpx.TimeoutException):
                try:
                    resp = await client.get(f"http://127.0.0.1:{port}/")
                    healthy = resp.status_code < 500
                    _detected_endpoints[workspace_id] = "root"
                except Exception:
                    healthy = False
            except Exception:
                healthy = False

    mark_workspace_health(workspace_id, healthy=healthy)

    if healthy:
        _unhealthy_streak.pop(workspace_id, None)
    else:
        streak = _unhealthy_streak.get(workspace_id, 0) + 1
        _unhealthy_streak[workspace_id] = streak
        if streak >= _EVICT_AFTER_FAILURES:
            logger.warning(
                "Health checker: workspace %s unhealthy for %d consecutive checks — evicting from active set",
                workspace_id, streak,
            )
            remove_from_active(workspace_id)
            _unhealthy_streak.pop(workspace_id, None)
            _detected_endpoints.pop(workspace_id, None)


async def _collect_active_workspaces() -> list[tuple[str, int]]:
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
