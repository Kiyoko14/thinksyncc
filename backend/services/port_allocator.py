from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status

from services.redis_service import RedisService

logger = logging.getLogger(__name__)

PORT_MIN = 3000
PORT_MAX = 8000

_FREE_SET = "ports:free"
_USED_SET = "ports:used"


def _ws_port_key(workspace_id: str) -> str:
    return f"ws:{workspace_id}:port"


def _ws_type_key(workspace_id: str) -> str:
    return f"ws:{workspace_id}:type"


_ALLOC_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then return existing end
local sz = redis.call('SCARD', KEYS[2])
if tonumber(sz) == 0 then return false end
local port = redis.call('SPOP', KEYS[2])
if not port then return false end
redis.call('SET', KEYS[1], port)
redis.call('SADD', KEYS[3], port)
return port
"""


def _initialize_pool_if_needed(r) -> None:
    if r.exists(_FREE_SET) or r.exists(_USED_SET):
        return
    pipeline = r.pipeline()
    pipeline.sadd(_FREE_SET, *[str(p) for p in range(PORT_MIN, PORT_MAX + 1)])
    pipeline.execute()
    logger.info("Port pool initialised: %d–%d", PORT_MIN, PORT_MAX)


def allocate_port(workspace_id: str) -> int:
    r = RedisService.get_sync_client()
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "REDIS_UNAVAILABLE", "message": "Redis is required for port allocation"},
        )

    _initialize_pool_if_needed(r)

    port_key = _ws_port_key(workspace_id)

    raw = r.eval(_ALLOC_SCRIPT, 3, port_key, _FREE_SET, _USED_SET)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "NO_PORTS_AVAILABLE", "message": "No free ports available"},
        )

    port = int(raw)
    logger.info("Allocated port %d for workspace %s", port, workspace_id)
    return port


def get_port(workspace_id: str) -> Optional[int]:
    r = RedisService.get_sync_client()
    if r is None:
        return None
    raw = r.get(_ws_port_key(workspace_id))
    return int(raw) if raw is not None else None


def set_workspace_type(workspace_id: str, workspace_type: str) -> None:
    r = RedisService.get_sync_client()
    if r is None:
        return
    r.set(_ws_type_key(workspace_id), workspace_type.lower().strip())


def get_workspace_type(workspace_id: str) -> Optional[str]:
    r = RedisService.get_sync_client()
    if r is None:
        return None
    return r.get(_ws_type_key(workspace_id))


def release_port(workspace_id: str) -> None:
    r = RedisService.get_sync_client()
    if r is None:
        return

    port_key = _ws_port_key(workspace_id)
    raw = r.get(port_key)
    if raw is None:
        return

    port = int(raw)
    pipeline = r.pipeline()
    pipeline.delete(port_key)
    pipeline.srem(_USED_SET, str(port))
    pipeline.sadd(_FREE_SET, str(port))
    pipeline.execute()

    logger.info("Released port %d for workspace %s", port, workspace_id)
