from __future__ import annotations

import logging
from typing import Optional

from services.redis_service import RedisService

logger = logging.getLogger(__name__)


def _ws_domain_key(workspace_id: str) -> str:
    return f"ws:{workspace_id}:domain"


def _domain_lookup_key(subdomain: str) -> str:
    return f"ws_domain:{subdomain.lower().strip()}"


def assign_domain(workspace_id: str, subdomain: str) -> str:
    r = RedisService.get_sync_client()
    if r is None:
        raise RuntimeError("Redis unavailable")

    clean = subdomain.lower().strip()
    domain_key = _ws_domain_key(workspace_id)
    lookup_key = _domain_lookup_key(clean)

    existing_domain = r.get(domain_key)
    if existing_domain is not None:
        return existing_domain

    owner = r.get(lookup_key)
    if owner is not None and owner != workspace_id:
        raise ValueError(
            f"Subdomain '{clean}' is already assigned to workspace {owner}"
        )

    pipeline = r.pipeline()
    pipeline.set(domain_key, clean)
    pipeline.set(lookup_key, workspace_id)
    pipeline.execute()

    logger.info("Assigned subdomain '%s' to workspace %s", clean, workspace_id)
    return clean


def get_workspace_by_domain(subdomain: str) -> Optional[str]:
    r = RedisService.get_sync_client()
    if r is None:
        raise RuntimeError("Redis unavailable")
    return r.get(_domain_lookup_key(subdomain.lower().strip()))


def get_domain(workspace_id: str) -> Optional[str]:
    r = RedisService.get_sync_client()
    if r is None:
        return None
    return r.get(_ws_domain_key(workspace_id))
