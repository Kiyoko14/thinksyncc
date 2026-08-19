"""Structured Output abstraction.

This module is the SINGLE internal boundary between the planner (and any other
structured-LLM caller) and the underlying provider.

Design contract (per the provider-independence refactor):

* Callers request *structured output* by passing a JSON schema + a logical
  ``role``. They NEVER pass ``response_format``, NEVER choose a model, and NEVER
  see which provider/transport produced the result.
* Model selection lives ONLY in configuration (``Settings.resolve_model``).
* Provider-specific behaviour is isolated here:
    - If the provider supports native structured output (OpenAI-style
      ``response_format={"type": "json_object"}``) it is used.
    - If the provider rejects it (e.g. a SiliconFlow model without JSON mode),
      the transport transparently falls back to prompt-constrained output:
      the schema is injected into the prompt and the raw text is parsed.
  The caller cannot tell which path was taken.
* This module does NOT execute tools, does NOT know orchestration, and does NOT
  know the planner. It only obtains structured content, parses it, validates
  the required keys, and returns a dict.

Provider pluggability: to add a future provider, extend the transport
(``_parse_once`` / ``_dispatch``) — no planner or caller changes required.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import HTTPException, status
from openai import AsyncOpenAI

from core.config import get_settings

logger = logging.getLogger(__name__)

# Substrings that indicate a provider rejected the OpenAI-style JSON mode.
_FORMAT_UNSUPPORTED_HINTS = (
    "json mode",
    "json_mode",
    "response_format",
    "20024",
    "not supported",
    "unsupported",
)


# ---------------------------------------------------------------------------
# Isolated provider transport (the ONLY place that talks to a provider)
# ---------------------------------------------------------------------------


def _get_client() -> AsyncOpenAI:
    """Create a provider client from configuration.

    Owned by this module so provider concerns stay isolated from the planner.
    """
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    return AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
    )


def _model_for_role(role: str) -> str:
    """Resolve the model for a role from configuration only."""
    try:
        return get_settings().resolve_model(role)
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )


def _with_schema_in_prompt(
    messages: list[dict[str, str]], schema: dict[str, Any]
) -> list[dict[str, str]]:
    """Return messages with the schema appended to the last user message.

    Used for the prompt-constrained fallback when native JSON mode is
    unavailable.
    """
    augmented = [dict(m) for m in messages]
    schema_text = (
        "\n\nYou MUST respond with a single valid JSON object and nothing else. "
        "Strictly follow this JSON schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    for i in range(len(augmented) - 1, -1, -1):
        if augmented[i].get("role") == "user":
            augmented[i] = {
                "role": "user",
                "content": (augmented[i].get("content") or "") + schema_text,
            }
            return augmented
    augmented.append({"role": "user", "content": schema_text})
    return augmented


def _validate_required(parsed: dict[str, Any], schema: dict[str, Any]) -> bool:
    """Return True if all schema ``required`` keys are present in ``parsed``."""
    required = schema.get("required") or []
    return all(k in parsed for k in required)


def _safe_json(raw: str) -> dict[str, Any] | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _parse_once(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, str]],
    *,
    use_native: bool,
    temperature: float,
) -> str:
    """Make one completion call and return the raw text content.

    Raises the original provider exception on transport failure so callers can
    detect format-unsupported conditions and retry.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,  # type: ignore[arg-type]
        "temperature": temperature,
    }
    if use_native:
        kwargs["response_format"] = {"type": "json_object"}
    response = await asyncio.wait_for(
        client.chat.completions.create(**kwargs),
        timeout=900,
    )
    return (response.choices[0].message.content or "").strip()


def _is_format_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _FORMAT_UNSUPPORTED_HINTS)


# ---------------------------------------------------------------------------
# Public abstraction
# ---------------------------------------------------------------------------


async def request_structured(
    *,
    role: str,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    cache_key: str | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Request structured output for ``role`` and return a parsed dict.

    This is the ONLY function planners/callers should use. It is provider- and
    model-agnostic from the caller's perspective.

    Behaviour:
      * Native JSON mode is attempted first.
      * On a provider rejection, a prompt-constrained call is made instead.
      * The parsed object is validated for the schema's required keys; if they
        are missing after the fallback, a best-effort dict is returned (with a
        warning) so tolerant callers using ``.get()`` keep working.
      * Transport/parse failures raise ``HTTPException`` (502/504) to preserve
        the prior API contract.
    """
    model = _model_for_role(role)
    client = _get_client()

    # Phase 1: native JSON mode.
    try:
        raw = await _parse_once(client, model, messages, use_native=True, temperature=temperature)
    except Exception as exc:  # noqa: BLE001 — distinguish format rejection
        if _is_format_unsupported(exc):
            logger.info(
                "[structured_output] native JSON mode unsupported for role=%s; "
                "using prompt-constrained fallback",
                role,
            )
            return await _constrained_fallback(client, model, messages, schema, temperature)
        if isinstance(exc, asyncio.TimeoutError):
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="LLM request timed out after 45s")
        logger.error("Structured output request failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"LLM request failed: {exc}")
    else:
        parsed = _safe_json(raw) if raw else None
        if parsed is not None and _validate_required(parsed, schema):
            return parsed
        # Native returned non-JSON or missing required keys — fall back.
        logger.warning(
            "[structured_output] native response invalid/missing keys for role=%s; "
            "using prompt-constrained fallback",
            role,
        )
        return await _constrained_fallback(client, model, messages, schema, temperature)


async def _constrained_fallback(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    temperature: float,
) -> dict[str, Any]:
    """Prompt-constrained fallback: no ``response_format``, schema in prompt."""
    constrained = _with_schema_in_prompt(messages, schema)
    try:
        raw = await _parse_once(client, model, constrained, use_native=False, temperature=temperature)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="LLM request timed out after 45s")
    except Exception as exc:
        logger.error("Structured output (constrained) request failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"LLM request failed: {exc}")

    if not raw:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Empty response from LLM")

    parsed = _safe_json(raw)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM returned invalid JSON: {raw[:200]}",
        )
    # Best-effort: if required keys are still missing, return what we have so
    # tolerant callers (using .get) keep working, but warn.
    if not _validate_required(parsed, schema):
        logger.warning("[structured_output] constrained fallback still missing required keys: %s", schema.get("required"))
    return parsed
