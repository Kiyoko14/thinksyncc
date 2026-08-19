"""GitHub App webhook endpoint (Part 1 — production-ready).

POST /github-app/webhook

Auth: NONE (GitHub calls this). Authenticity is established by the
``X-Hub-Signature-256`` HMAC over the raw request body (verified in the
service layer). This endpoint is intentionally NOT protected by
``get_current_user`` — it is a machine-to-machine callback from GitHub.

The endpoint reads the RAW body (before any JSON parsing) so the HMAC is
computed over the exact bytes GitHub signed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.github_webhook_service import WebhookError, process_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github-app", tags=["github-app-webhook"])


@router.post("/webhook")
async def github_webhook(request: Request) -> JSONResponse:
    # Read the RAW body first — the HMAC must be over the exact signed bytes.
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    # Parse JSON defensively; a malformed body must not 500.
    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        # Signature is verified inside process_webhook; but if the body isn't
        # valid JSON we still want a clean 400 (after signature check).
        payload = {}

    try:
        result = await process_webhook(
            raw_body=raw_body,
            signature_header=signature_header,
            event_type=event_type,
            delivery_id=delivery_id,
            payload=payload,
        )
    except WebhookError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )
    except Exception as exc:  # noqa: BLE001
        # Last-resort isolation: never leak internals; log and 500.
        logger.exception("[webhook] unexpected error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"code": "WEBHOOK_INTERNAL_ERROR", "message": "Internal webhook error."},
        )

    return JSONResponse(status_code=200, content=result)
