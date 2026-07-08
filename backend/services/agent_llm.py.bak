"""LLM integration for the ThinkSync agent.

Public functions:
  generate_plan()        — two-phase plan generation (used by forge_v2)
  evaluate_step()        — per-step LLM evaluation (used by forge_v2)
  revise_plan()          — plan revision after partial execution
  run_tool_calling_loop() — ReAct-style tool-calling loop (primary agent loop)
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]
    _REDIS_AVAILABLE = True
except ImportError:
    aioredis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False

from fastapi import HTTPException, status
from openai import AsyncOpenAI

from core.config import get_settings
from core.value_coercion import value_to_str
from models.agent import (
    AgentDecision,
    AgentPlan,
    AgentStep,
    DecisionAction,
    StepResult,
    ToolCallingLoopResult,
    ToolName,
)
from services.guardrails import apply_text_patches, validate_patched_files
from agents.constitution import ConstitutionEngine, PlatformContextMissingError, TargetDriftError
logger = logging.getLogger(__name__)
constitution = ConstitutionEngine()
# ---------------------------------------------------------------------------
# Intent classification (chat/code/server)
# ---------------------------------------------------------------------------

_INTENT_VALUES: tuple[str, ...] = ("chat", "code", "server")
_TASK_MODE_VALUES: tuple[str, ...] = ("simple", "complex")

_TASK_MODE_SCHEMA = {
    "type": "object",
    "required": ["task_mode"],
    "additionalProperties": False,
    "properties": {
        "task_mode": {"type": "string", "enum": list(_TASK_MODE_VALUES)},
    },
}

_NON_SERVER_PLAN_SCHEMA = {
    "type": "object",
    "required": ["steps"],
    "additionalProperties": False,
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["step", "tool", "args", "reason"],
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "integer", "minimum": 1},
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

_FAILURE_ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["root_cause", "next_steps"],
    "additionalProperties": False,
    "properties": {
        "root_cause": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}


_FALLBACK_CODE_RE = re.compile(
    r"(?ix)\b("
    r"bot|telegram|telebot|aiogram|"
    r"script|function|class|module|library|sdk|api|endpoint|router|"
    r"python|py|js|javascript|typescript|node|deno|bun|"
    r"sql|postgres|mysql|sqlite|redis|"
    r"write\s+code|generate\s+code|build\s+an?\s+api|"
    r"yoz(?:ib)?\s+ber|kod\s+yoz|dastur|skript|"
    r"код|скрипт|функц|бот"
    r")\b"
)
_DEPLOYMENT_INTENT_RE = re.compile(
    r"(?ix)\b("
    r"deploy|server|app|run|website"
    r")\b"
)
_FALLBACK_SERVER_RE = re.compile(
    r"(?ix)\b("
    r"deploy|rollout|restart|reload|status|logs|"
    r"server|app|run|website|"
    r"nginx|apache2|httpd|systemctl|journalctl|service|"
    r"docker|docker-compose|kubectl|helm|pm2|supervisorctl|"
    r"ssh|port|firewall|iptables|ufw|"
    r"деплой|разверн|перезапуст|рестарт|логи|сервер|"
    r"qayta\s+ishga\s+tushir|restart\s+qil|server|log(?:lar)?"
    r")\b"
)


def fallback_intent(text: str) -> str:
    """Keyword-based fallback intent classifier (chat/code/server)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "chat"
    lowered = cleaned.lower()

    # Deployment-style requests must enter the execution pipeline.
    if _DEPLOYMENT_INTENT_RE.search(lowered):
        return "server"

    # Prefer "code" on non-deployment overlap to avoid accidental server actions.
    if _FALLBACK_CODE_RE.search(lowered):
        return "code"
    if _FALLBACK_SERVER_RE.search(lowered):
        return "server"
    return "chat"


def _normalize_intent(value: Any) -> str:
    intent = str(value or "").strip().lower()
    return intent if intent in _INTENT_VALUES else "chat"


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = 0.0
    if confidence != confidence:  # NaN
        confidence = 0.0
    return max(0.0, min(1.0, confidence))


async def classify_intent_with_confidence(text: str) -> dict[str, Any]:
    """
    LLM-based intent classifier returning {"intent": "...", "confidence": 0.0-1.0}.
    If the LLM is unavailable or returns invalid output, returns a mid-confidence result to trigger fallback logic.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {"intent": "chat", "confidence": 1.0}

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return {"intent": "chat", "confidence": 0.50}

    cache_key = _context_hash("intent_confidence", {"message": cleaned})
    cache_key = f"intent_confidence:{cache_key}"
    cached = await _cache_get(cache_key)
    if cached:
        try:
            obj = json.loads(cached)
            return {"intent": _normalize_intent(obj.get("intent")), "confidence": _normalize_confidence(obj.get("confidence"))}
        except Exception:
            pass

    client = _get_openai_client()
    model = settings.OPENAI_MODEL_CLASSIFIER or settings.OPENAI_MODEL

    payload = {"message": cleaned, "intents": list(_INTENT_VALUES)}
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": constitution.build_prompt("intent_classifier")},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=45,
        )
        raw = (response.choices[0].message.content or "").strip()
        obj = json.loads(raw) if raw else {}
        intent = _normalize_intent(obj.get("intent"))
        confidence = _normalize_confidence(obj.get("confidence"))
        await _cache_set(cache_key, json.dumps({"intent": intent, "confidence": confidence}))
        return {"intent": intent, "confidence": confidence}
    except asyncio.TimeoutError:
        logger.warning("[intent] LLM timed out after 45s; returning mid-confidence fallback")
        return {"intent": "chat", "confidence": 0.50}
    except Exception:
        # Parsing / provider failures: return mid-confidence to force fallback rules.
        return {"intent": "chat", "confidence": 0.50}


async def classify_intent(
    *,
    user_input: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    """
    Classify a user message into exactly one intent: chat, code, server.

    Safety posture: default away from "server" unless explicitly requested.
    """
    cleaned = (user_input or "").strip()
    if not cleaned:
        return "chat"

    llm_result = await classify_intent_with_confidence(cleaned)
    llm_intent = _normalize_intent(llm_result.get("intent"))
    confidence = _normalize_confidence(llm_result.get("confidence"))

    if confidence >= 0.80:
        final_intent = llm_intent
        decision = "accept_llm"
    else:
        final_intent = fallback_intent(cleaned)
        decision = "fallback_keywords"

    logger.info(
        "[intent] input=%r | llm_intent=%s | confidence=%.2f | final=%s | decision=%s",
        cleaned[:500],
        llm_intent,
        confidence,
        final_intent,
        decision,
    )
    return final_intent


async def detect_task_mode(
    *,
    intent: str,
    user_input: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    """
    Classify task complexity into simple vs complex.
    This never grants tool access; tool access is controlled by intent == 'server'.
    """
    cleaned = (user_input or "").strip()
    if not cleaned:
        return "simple"

    normalized_intent = (intent or "").strip().lower()
    if normalized_intent == "chat":
        # Chat is usually single-turn; let the LLM override only for long/problem statements.
        if len(cleaned) < 200 and "\n" not in cleaned:
            return "simple"

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return "complex" if len(cleaned) > 220 or "\n" in cleaned or " and " in cleaned.lower() else "simple"

    context_tail = (conversation_history or [])[-8:]
    cache_key = _context_hash("task_mode", {"intent": normalized_intent, "message": cleaned, "history": context_tail})
    cache_key = f"task_mode:{cache_key}"

    try:
        payload = {
            "intent": normalized_intent,
            "message": cleaned,
            "recent_context": context_tail,
            "task_modes": list(_TASK_MODE_VALUES),
        }
        model = settings.OPENAI_MODEL_CLASSIFIER or settings.OPENAI_MODEL
        result = await _chat_json(
            messages=[
                {"role": "system", "content": constitution.build_prompt("task_mode_classifier")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            schema=_TASK_MODE_SCHEMA,
            cache_key=cache_key,
            model=model,
        )
        mode = str(result.get("task_mode", "")).strip().lower()
        if mode in _TASK_MODE_VALUES:
            return mode
    except Exception:
        pass

    return "complex" if len(cleaned) > 220 or "\n" in cleaned or " and " in cleaned.lower() else "simple"


async def generate_non_server_plan(
    *,
    intent: str,
    objective: str,
    max_steps: int,
    conversation_history: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Generate a small structured plan for chat/code intents (no tools)."""
    cleaned = (objective or "").strip()
    if not cleaned:
        return []

    bounded_steps = max(1, min(int(max_steps or 5), 8))
    normalized_intent = (intent or "").strip().lower()
    context_tail = (conversation_history or [])[-8:]

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        # Minimal deterministic fallback plan.
        tool = "llm_generate_code" if normalized_intent == "code" else "llm_chat"
        return [{"step": 1, "tool": tool, "args": {}, "reason": "Produce the requested response safely without any server actions."}]

    cache_key = _context_hash(
        "non_server_plan",
        {"intent": normalized_intent, "objective": cleaned, "history": context_tail, "max_steps": bounded_steps},
    )
    cache_key = f"non_server_plan:{cache_key}"

    payload = {
        "intent": normalized_intent,
        "objective": cleaned,
        "max_steps": bounded_steps,
        "recent_context": context_tail,
    }
    result = await _chat_json(
        messages=[
            {"role": "system", "content": constitution.build_prompt("non_server_planner")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        schema=_NON_SERVER_PLAN_SCHEMA,
        cache_key=cache_key,
        model=get_settings().OPENAI_MODEL_PLANNER or get_settings().OPENAI_MODEL,
    )
    steps = result.get("steps") or []
    if not isinstance(steps, list):
        return []

    # Normalize and bound.
    normalized: list[dict[str, Any]] = []
    for item in steps[:bounded_steps]:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        tool = item.get("tool")
        args = item.get("args")
        reason = item.get("reason")
        if not isinstance(step, int) or step < 1:
            continue
        if not isinstance(tool, str) or not tool.strip():
            continue
        if not isinstance(args, dict):
            args = {}
        if not isinstance(reason, str) or not reason.strip():
            continue
        normalized.append({"step": step, "tool": tool.strip(), "args": args, "reason": reason.strip()})

    # Ensure sequential steps.
    normalized.sort(key=lambda x: int(x["step"]))
    for idx, item in enumerate(normalized, start=1):
        item["step"] = idx
    return normalized[:bounded_steps]


async def analyze_failure(
    *,
    step: AgentStep,
    result: StepResult,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Debug agent: analyze a failed step to aid self-healing."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return {"root_cause": "LLM unavailable", "next_steps": [], "notes": ""}

    payload = {
        "objective": context.get("objective", ""),
        "allow_write": bool(context.get("allow_write", False)),
        "step": step.model_dump(mode="json"),
        "result": {
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:2000],
            "exit_code": result.exit_code,
            "success": result.success,
        },
        "server_metadata": context.get("server_metadata", {}),
        "memory": context.get("memory", [])[-10:],
    }
    model = settings.OPENAI_MODEL_DEBUG or settings.OPENAI_MODEL
    try:
        return await _chat_json(
            messages=[
                {"role": "system", "content": constitution.build_prompt("debug")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            schema=_FAILURE_ANALYSIS_SCHEMA,
            model=model,
        )
    except Exception:
        return {"root_cause": "Failure analysis unavailable", "next_steps": [], "notes": ""}


async def generate_chat_response(
    *,
    user_input: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    """LLM-only chat response (no tools)."""
    settings = get_settings()
    client = _get_openai_client()

    cleaned = (user_input or "").strip()
    if not cleaned:
        return ""

    messages: list[dict[str, str]] = [{"role": "system", "content": constitution.build_prompt("chat")}]
    for msg in (conversation_history or [])[-12:]:
        role = msg.get("role")
        content = msg.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": cleaned})

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=messages,  # type: ignore[arg-type]
                temperature=0.3,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[chat] LLM timed out after 45s; returning empty response")
        return ""
    return (response.choices[0].message.content or "").strip()


async def generate_code_response(
    prompt: str,
    conversation_history: list[dict[str, str]] | None = None,
    context_bundle: dict[str, Any] | None = None,
) -> str:
    """LLM-only code generation response (no tools)."""
    settings = get_settings()
    client = _get_openai_client()

    cleaned = (prompt or "").strip()
    if not cleaned:
        return ""

    forced = force_template_if_needed(cleaned)
    if forced is not None:
        logger.info("[codegen] forced template applied")
        return forced

    system_prompt = constitution.build_prompt("code")
    if context_bundle and isinstance(context_bundle.get("prompt_payload"), dict):
        system_prompt += (
            "\nWORKSPACE CONTEXT:\n"
            "Use only the real workspace files and snippets below. "
            "Do not invent filenames. If MODE is PATCH, align the code to these files.\n"
            f"{json.dumps(context_bundle['prompt_payload'], ensure_ascii=False)}\n"
        )
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for msg in (conversation_history or [])[-12:]:
        role = msg.get("role")
        content = msg.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": f"Task:\n{cleaned}\n"})

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=messages,  # type: ignore[arg-type]
                temperature=0.2,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[codegen] LLM timed out after 45s; returning empty")
        return ""
    content = (response.choices[0].message.content or "").strip()
    if not content:
        return ""

    # If the model returns a single fenced code block, strip fences to keep the output clean.
    fenced = re.match(r"(?s)^```[a-zA-Z0-9_+-]*\\n(.*)\\n```\\s*$", content)
    if fenced:
        content = str(fenced.group(1)).strip("\n")

    task_kind = _detect_code_task_kind(cleaned, content)
    valid, reasons = validate_code_output(content, task_kind=task_kind)
    logger.info(
        "[codegen] attempt=1 kind=%s valid=%s reasons=%s",
        task_kind,
        valid,
        ",".join(reasons) if reasons else "-",
    )
    if valid:
        return content

    corrected = await regenerate_on_error(
        prompt=cleaned,
        conversation_history=conversation_history,
        invalid_code=content,
        reasons=reasons,
        task_kind=task_kind,
    )
    valid2, reasons2 = validate_code_output(corrected, task_kind=task_kind)
    logger.info(
        "[codegen] attempt=2 kind=%s valid=%s reasons=%s",
        task_kind,
        valid2,
        ",".join(reasons2) if reasons2 else "-",
    )
    if valid2:
        return corrected

    fallback = force_template_if_needed(cleaned)
    if fallback is not None:
        logger.warning("[codegen] fallback template applied after invalid output")
        return fallback

    logger.warning("[codegen] returning best-effort code despite invalid output")
    return corrected




def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_existing_files(existing_files: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in existing_files or []:
        if not isinstance(item, dict):
            continue
        path = _coerce_str(item.get("path")).strip()
        if not path:
            continue
        normalized.append({"path": path, "content": _coerce_str(item.get("content"))})
    return normalized


def _validate_patch_response_shape(obj: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    patches = obj.get("patches")
    validation = obj.get("validation")
    report = obj.get("report")

    if not isinstance(patches, list):
        errors.append("missing_or_invalid:patches")
        patches = []
    if not isinstance(validation, dict):
        errors.append("missing_or_invalid:validation")
    if not isinstance(report, dict):
        errors.append("missing_or_invalid:report")

    for idx, p in enumerate(patches, start=1):
        if not isinstance(p, dict):
            errors.append(f"patch[{idx}]:invalid_object")
            continue
        file = p.get("file")
        operation = p.get("operation")
        target = p.get("target")
        replacement = p.get("replacement")
        if not isinstance(file, str) or not file.strip():
            errors.append(f"patch[{idx}]:missing_file")
        if operation not in {"replace", "insert", "delete"}:
            errors.append(f"patch[{idx}]:invalid_operation")
        if not isinstance(target, str) or not target:
            errors.append(f"patch[{idx}]:empty_target")
        if not isinstance(replacement, str):
            errors.append(f"patch[{idx}]:invalid_replacement")

    if isinstance(validation, dict):
        checks = validation.get("checks")
        if not isinstance(checks, list) or not all(isinstance(x, str) and x.strip() for x in checks):
            errors.append("validation:invalid_checks")

    if isinstance(report, dict):
        status = report.get("status")
        if status not in {"success", "failed"}:
            errors.append("report:invalid_status")
        files_modified = report.get("files_modified")
        if not isinstance(files_modified, list) or not all(isinstance(x, str) and x.strip() for x in files_modified):
            errors.append("report:invalid_files_modified")
        summary = report.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            errors.append("report:invalid_summary")

    return errors


def _clone_context_bundle(context_bundle: dict[str, Any] | None) -> dict[str, Any]:
    if not context_bundle:
        return {}
    try:
        cloned = json.loads(json.dumps(context_bundle))
        return cloned if isinstance(cloned, dict) else {}
    except Exception:
        return dict(context_bundle)


def _expand_code_snippets(
    *,
    context_bundle: dict[str, Any] | None,
    existing_files: list[dict[str, Any]],
    paths: set[str],
    extra_lines: int,
) -> dict[str, Any] | None:
    cloned = _clone_context_bundle(context_bundle)
    if not cloned:
        return context_bundle

    snippets = cloned.get("snippets")
    prompt_payload = cloned.get("prompt_payload")
    if not isinstance(snippets, list) or not isinstance(prompt_payload, dict):
        return cloned

    existing_by_path = {str(item.get("path") or "").strip(): _coerce_str(item.get("content")) for item in existing_files if isinstance(item, dict)}
    code_snippets = prompt_payload.get("CODE_SNIPPETS")
    if not isinstance(code_snippets, dict):
        code_snippets = {}
        prompt_payload["CODE_SNIPPETS"] = code_snippets

    for item in snippets:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path or (paths and path not in paths):
            continue
        content = _coerce_str(item.get("content")) or existing_by_path.get(path, "")
        if not content:
            continue
        lines = content.splitlines()
        if not lines:
            continue
        ranges = item.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            start = 1
            end = min(len(lines), max(1, min(len(lines), extra_lines * 2)))
        else:
            start = min(max(1, int(r.get("start") or 1)) for r in ranges if isinstance(r, dict))
            end = max(min(len(lines), int(r.get("end") or len(lines))) for r in ranges if isinstance(r, dict))
            start = max(1, start - extra_lines)
            end = min(len(lines), end + extra_lines)
        snippet = "\n".join(lines[start - 1:end])
        item["snippet"] = snippet
        item["ranges"] = [{"start": start, "end": end}]
        code_snippets[path] = snippet
    return cloned


def _augment_retry_constraints(
    *,
    constraints: dict[str, Any] | None,
    instruction: str,
) -> dict[str, Any]:
    cloned = dict(constraints or {})
    existing = _coerce_str(cloned.get("retry_instruction")).strip()
    cloned["retry_instruction"] = f"{existing}\n{instruction}".strip() if existing else instruction
    return cloned


def _classify_patch_failure(error_details: list[dict[str, Any]]) -> tuple[str, set[str], int]:
    max_match_count = 0
    paths: set[str] = set()
    for detail in error_details:
        if not isinstance(detail, dict):
            continue
        path = _coerce_str(detail.get("path")).strip()
        if path:
            paths.add(path)
        try:
            max_match_count = max(max_match_count, int(detail.get("match_count") or 0))
        except Exception:
            pass

    if any(detail.get("type") == "target_mismatch" and int(detail.get("match_count") or 0) == 0 for detail in error_details if isinstance(detail, dict)):
        return ("expand_zero_match", paths, max_match_count)
    if any(detail.get("type") == "target_mismatch" and int(detail.get("match_count") or 0) > 1 for detail in error_details if isinstance(detail, dict)):
        return ("expand_multi_match", paths, max_match_count)
    if any(detail.get("type") in {"no_change", "change_verification_failed"} for detail in error_details if isinstance(detail, dict)):
        return ("strict_exact_function", paths, max_match_count)
    return ("retry_same_context", paths, max_match_count)


def _failure_code_from_error_details(error_details: list[dict[str, Any]]) -> str:
    for detail in error_details:
        if not isinstance(detail, dict):
            continue
        if detail.get("type") == "target_mismatch" and int(detail.get("match_count") or 0) == 0:
            return "ZERO_MATCH"
        if detail.get("type") == "target_mismatch" and int(detail.get("match_count") or 0) > 1:
            return "MULTI_MATCH"
        if detail.get("type") in {"no_change", "change_verification_failed"}:
            return "NO_CHANGE"
    return "APPLY_ERROR"


def _patch_signature(patches: list[dict[str, Any]]) -> list[str]:
    signature: list[str] = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        file_path = _coerce_str(patch.get("file")).strip()
        target = _coerce_str(patch.get("target"))
        if not file_path:
            continue
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]
        signature.append(f"{file_path}:{digest}")
    return sorted(signature)


def _has_inconsistent_targets(previous: list[str] | None, current: list[str]) -> bool:
    if not previous or not current:
        return False
    return previous != current


def _build_diff_summary(
    *,
    original_files: list[dict[str, Any]],
    updated_files: list[dict[str, Any]],
    applied_files: list[str],
) -> str:
    original_by_path = {
        _coerce_str(item.get("path")).strip(): _coerce_str(item.get("content"))
        for item in original_files
        if isinstance(item, dict) and _coerce_str(item.get("path")).strip()
    }
    updated_by_path = {
        _coerce_str(item.get("path")).strip(): _coerce_str(item.get("content"))
        for item in updated_files
        if isinstance(item, dict) and _coerce_str(item.get("path")).strip()
    }
    chunks: list[str] = []
    for path in applied_files:
        before = original_by_path.get(path, "")
        after = updated_by_path.get(path, "")
        if before == after:
            continue
        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=path,
                tofile=path,
                lineterm="",
                n=3,
            )
        )
        if diff_lines:
            chunks.append("\n".join(diff_lines))
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def _infer_learning_scope(existing_files: list[dict[str, Any]], context_bundle: dict[str, Any] | None) -> tuple[str, str]:
    extension_counts: dict[str, int] = {}
    for item in existing_files:
        if not isinstance(item, dict):
            continue
        path = _coerce_str(item.get("path")).strip().lower()
        if not path:
            continue
        ext = path.rsplit(".", 1)[-1] if "." in path else ""
        if ext:
            extension_counts[ext] = extension_counts.get(ext, 0) + 1

    dominant_ext = max(extension_counts.items(), key=lambda item: item[1])[0] if extension_counts else ""
    language_map = {
        "py": "python",
        "ts": "typescript",
        "tsx": "typescript",
        "js": "javascript",
        "jsx": "javascript",
        "sql": "sql",
        "go": "go",
        "rs": "rust",
    }
    language = language_map.get(dominant_ext, "unknown")

    all_paths = " ".join(
        _coerce_str(item.get("path")).strip().lower()
        for item in existing_files
        if isinstance(item, dict)
    )
    prompt_payload = (context_bundle or {}).get("prompt_payload") if isinstance(context_bundle, dict) else {}
    file_list = prompt_payload.get("FILE_LIST") if isinstance(prompt_payload, dict) else []
    if isinstance(file_list, list):
        all_paths = f"{all_paths} {' '.join(_coerce_str(path).lower() for path in file_list)}".strip()

    if any(token in all_paths for token in ("package.json", "next.config", "tsconfig", "app/", "components/")):
        project_type = "webapp"
    elif any(token in all_paths for token in ("requirements.txt", "pyproject.toml", "fastapi", "flask", "routers/")):
        project_type = "backend"
    elif any(token in all_paths for token in ("dockerfile", "docker-compose", "infra/", "k8s", "helm")):
        project_type = "infra"
    else:
        project_type = "generic"

    return (language, project_type)


def _strategy_key(failure_type: str, language: str, project_type: str) -> str:
    return f"agent:strategy:{failure_type}:{language}:{project_type}"


async def _load_strategy_stat(failure_type: str, language: str, project_type: str) -> dict[str, Any]:
    redis = await _get_redis_client()
    if redis is None:
        return {"count": 0, "last_used": None}
    try:
        raw = await redis.get(_strategy_key(failure_type, language, project_type))
        if not raw:
            return {"count": 0, "last_used": None}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {"count": 0, "last_used": None}
        return {
            "count": int(payload.get("count") or 0),
            "last_used": payload.get("last_used"),
        }
    except Exception:
        return {"count": 0, "last_used": None}
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _store_strategy_learning(failure_type: str, language: str, project_type: str) -> None:
    if failure_type not in {"ZERO_MATCH", "MULTI_MATCH", "NO_CHANGE", "APPLY_ERROR"}:
        return
    redis = await _get_redis_client()
    if redis is None:
        return
    try:
        key = _strategy_key(failure_type, language, project_type)
        raw = await redis.get(key)
        payload: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                payload = {}
        payload["count"] = int(payload.get("count") or 0) + 1
        payload["last_used"] = datetime.now(timezone.utc).isoformat()
        await redis.setex(key, 60 * 60 * 24, json.dumps(payload, ensure_ascii=False))
    except Exception:
        return
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _decrease_strategy_learning(failure_type: str, language: str, project_type: str) -> None:
    if failure_type not in {"ZERO_MATCH", "MULTI_MATCH", "NO_CHANGE", "APPLY_ERROR"}:
        return
    redis = await _get_redis_client()
    if redis is None:
        return
    try:
        key = _strategy_key(failure_type, language, project_type)
        raw = await redis.get(key)
        if not raw:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
        next_count = max(0, int(payload.get("count") or 0) - 1)
        payload["count"] = next_count
        payload["last_used"] = datetime.now(timezone.utc).isoformat()
        await redis.setex(key, 60 * 60 * 24, json.dumps(payload, ensure_ascii=False))
    except Exception:
        return
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _load_strategy_stats(language: str, project_type: str) -> dict[str, dict[str, Any]]:
    failure_types = ("ZERO_MATCH", "MULTI_MATCH", "NO_CHANGE", "APPLY_ERROR")
    stats: dict[str, dict[str, Any]] = {}
    for failure_type in failure_types:
        stats[failure_type] = await _load_strategy_stat(failure_type, language, project_type)
    return stats


def _apply_learned_adaptation(
    *,
    constraints: dict[str, Any],
    context_bundle: dict[str, Any],
    existing_files: list[dict[str, Any]],
    strategy_stats: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    adapted_constraints = dict(constraints)
    adapted_context = _clone_context_bundle(context_bundle)
    max_context_window = 25
    max_instruction_strength = 3
    adaptation_summary: dict[str, int] = {}

    zero_match_boost = min(max_instruction_strength, int((strategy_stats.get("ZERO_MATCH") or {}).get("count") or 0))
    if zero_match_boost > 0:
        context_window = min(max_context_window, 10 + (5 * zero_match_boost))
        adapted_context = _expand_code_snippets(
            context_bundle=adapted_context,
            existing_files=existing_files,
            paths=set(),
            extra_lines=context_window,
        ) or adapted_context
        adapted_constraints = _augment_retry_constraints(
            constraints=adapted_constraints,
            instruction="Prefer a larger exact target block from the provided code snippets.",
        )
        adaptation_summary["ZERO_MATCH"] = context_window

    multi_match_boost = min(max_instruction_strength, int((strategy_stats.get("MULTI_MATCH") or {}).get("count") or 0))
    if multi_match_boost > 0:
        adapted_constraints = _augment_retry_constraints(
            constraints=adapted_constraints,
            instruction="Increase target specificity by including more surrounding unique lines.",
        )
        adaptation_summary["MULTI_MATCH"] = multi_match_boost

    no_change_boost = min(max_instruction_strength, int((strategy_stats.get("NO_CHANGE") or {}).get("count") or 0))
    if no_change_boost > 0:
        adapted_constraints = _augment_retry_constraints(
            constraints=adapted_constraints,
            instruction="Make a concrete modification to the exact function. Do not return a no-op patch.",
        )
        adaptation_summary["NO_CHANGE"] = no_change_boost

    apply_error_boost = min(max_instruction_strength, int((strategy_stats.get("APPLY_ERROR") or {}).get("count") or 0))
    if apply_error_boost > 0:
        adapted_constraints = _augment_retry_constraints(
            constraints=adapted_constraints,
            instruction="Use a simplified minimal patch with one precise change per target.",
        )
        adaptation_summary["APPLY_ERROR"] = apply_error_boost

    if adaptation_summary:
        adapted_constraints["self_improvement"] = adaptation_summary
        adapted_constraints["max_context_window"] = max_context_window
        adapted_constraints["max_instruction_strength"] = max_instruction_strength

    return adapted_constraints, adapted_context


def _patch_task_hash(task: str) -> str:
    return hashlib.sha256((task or "").strip().encode("utf-8")).hexdigest()


def _success_cache_key(task_hash: str) -> str:
    return f"agent:success:{task_hash}"


def _processing_key(task_hash: str) -> str:
    return f"agent:processing:{task_hash}"


async def _get_successful_patch_cache(task_hash: str) -> dict[str, Any] | None:
    redis = await _get_redis_client()
    if redis is None:
        return None
    try:
        raw = await redis.get(_success_cache_key(task_hash))
        if not raw:
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _set_successful_patch_cache(task_hash: str, payload: dict[str, Any]) -> None:
    redis = await _get_redis_client()
    if redis is None:
        return
    try:
        await redis.setex(
            _success_cache_key(task_hash),
            get_settings().REDIS_PATCH_SUCCESS_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        return
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _acquire_processing_lock(task_hash: str) -> bool:
    redis = await _get_redis_client()
    if redis is None:
        return True
    try:
        result = await redis.set(
            _processing_key(task_hash),
            "1",
            ex=get_settings().REDIS_PATCH_PROCESSING_TTL_SECONDS,
            nx=True,
        )
        return bool(result)
    except Exception:
        return True
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _release_processing_lock(task_hash: str) -> None:
    redis = await _get_redis_client()
    if redis is None:
        return
    try:
        await redis.delete(_processing_key(task_hash))
    except Exception:
        return
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _update_patch_metrics(*, llm_requests: int, retries: int, cache_hit: bool) -> dict[str, float]:
    redis = await _get_redis_client()
    if redis is None:
        tasks = 1.0
        return {
            "avg_requests_per_task": float(llm_requests),
            "retry_rate": float(retries) / tasks,
            "cache_hit_rate": 1.0 if cache_hit else 0.0,
        }
    try:
        total_tasks = await redis.incr("agent:metrics:tasks_total")
        if llm_requests:
            await redis.incrby("agent:metrics:llm_requests_total", llm_requests)
        if retries:
            await redis.incrby("agent:metrics:retries_total", retries)
        if cache_hit:
            await redis.incr("agent:metrics:cache_hits_total")
        pipe = redis.pipeline()
        pipe.get("agent:metrics:llm_requests_total")
        pipe.get("agent:metrics:retries_total")
        pipe.get("agent:metrics:cache_hits_total")
        totals = await pipe.execute()
        llm_total = float(totals[0] or 0)
        retry_total = float(totals[1] or 0)
        cache_total = float(totals[2] or 0)
        task_total = float(total_tasks or 1)
        return {
            "avg_requests_per_task": llm_total / task_total if task_total else 0.0,
            "retry_rate": retry_total / task_total if task_total else 0.0,
            "cache_hit_rate": cache_total / task_total if task_total else 0.0,
        }
    except Exception:
        tasks = 1.0
        return {
            "avg_requests_per_task": float(llm_requests),
            "retry_rate": float(retries) / tasks,
            "cache_hit_rate": 1.0 if cache_hit else 0.0,
        }
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def generate_patch_response(
    *,
    task: str,
    existing_files: list[dict[str, Any]],
    constraints: dict[str, Any] | None = None,
    failure_history: list[dict[str, Any]] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    client = _get_openai_client()

    cleaned_task = (task or "").strip()
    prompt_payload = (context_bundle or {}).get("prompt_payload") or {}
    payload = {
        "FILE_LIST": prompt_payload.get("FILE_LIST") or [item.get("path") for item in _normalize_existing_files(existing_files)],
        "CODE_SNIPPETS": prompt_payload.get("CODE_SNIPPETS") or {
            item["path"]: item["content"] for item in _normalize_existing_files(existing_files)
        },
        "USER_TASK": prompt_payload.get("USER_TASK") or cleaned_task,
        "constraints": constraints or {},
        "failure_history": failure_history or [],
        "recent_context": (conversation_history or [])[-8:],
    }

    _patch_timeout_failure: dict[str, Any] = {"patches": [], "validation": {"checks": []}, "report": {"summary": "llm timeout", "files_modified": [], "status": "failed"}}
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": constitution.build_prompt("patch")},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[patch] LLM timed out after 45s")
        return _patch_timeout_failure
    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return {"patches": [], "validation": {"checks": []}, "report": {"summary": "empty llm response", "files_modified": [], "status": "failed"}}
    try:
        obj = json.loads(raw)
    except Exception:
        return {"patches": [], "validation": {"checks": []}, "report": {"summary": "invalid json from llm", "files_modified": [], "status": "failed"}}
    return obj if isinstance(obj, dict) else {"patches": [], "validation": {"checks": []}, "report": {"summary": "invalid llm shape", "files_modified": [], "status": "failed"}}


async def run_safe_patch_edit(
    *,
    existing_files: list[dict[str, Any]],
    task: str,
    constraints: dict[str, Any] | None = None,
    max_attempts: int = 3,
    conversation_history: list[dict[str, str]] | None = None,
    context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    End-to-end patch editing loop:
      1) ask LLM for minimal patches
      2) apply patches in-memory
      3) validate patched files
      4) retry with failure context (up to max_attempts)
    """
    bounded_attempts = max(1, min(int(max_attempts or 2), 2))
    failure_history: list[dict[str, Any]] = []
    normalized_existing = _normalize_existing_files(existing_files)
    task_hash = _patch_task_hash(task)
    llm_requests = 0
    retries_used = 0

    if not normalized_existing:
        metrics = await _update_patch_metrics(llm_requests=0, retries=0, cache_hit=False)
        return {
            "status": "failed",
            "final_reason": "ZERO_MATCH: no matching files",
            "attempts": [],
            "applied_files": [],
            "diff_summary": "",
            "last_error_details": [{"type": "no_matching_files", "path": "", "match_count": 0}],
            "patches": [],
            "validation": {"checks": ["syntax_valid", "required_feature_present", "no_unintended_changes"]},
            "report": {"summary": "ZERO_MATCH: no matching files", "files_modified": [], "status": "failed"},
            "updated_files": normalized_existing,
            "failure_history": failure_history,
            "metrics": metrics,
        }

    selected_files = (context_bundle or {}).get("selected_files") if isinstance(context_bundle, dict) else None
    prompt_payload = (context_bundle or {}).get("prompt_payload") if isinstance(context_bundle, dict) else None
    if not isinstance(context_bundle, dict) or not isinstance(prompt_payload, dict) or not isinstance(selected_files, list):
        metrics = await _update_patch_metrics(llm_requests=0, retries=0, cache_hit=False)
        return {
            "status": "failed",
            "final_reason": "APPLY_ERROR: invalid context",
            "attempts": [],
            "applied_files": [],
            "diff_summary": "",
            "last_error_details": [{"type": "invalid_context", "path": "", "match_count": 0}],
            "patches": [],
            "validation": {"checks": ["syntax_valid", "required_feature_present", "no_unintended_changes"]},
            "report": {"summary": "APPLY_ERROR: invalid context", "files_modified": [], "status": "failed"},
            "updated_files": normalized_existing,
            "failure_history": failure_history,
            "metrics": metrics,
        }

    cached_success = await _get_successful_patch_cache(task_hash)
    if cached_success is not None:
        metrics = await _update_patch_metrics(llm_requests=0, retries=0, cache_hit=True)
        cached_success["metrics"] = metrics
        cached_success.setdefault("attempts", []).insert(0, {"attempt": 0, "strategy": "cache", "result": "cache_hit"})
        return cached_success

    lock_acquired = await _acquire_processing_lock(task_hash)
    if not lock_acquired:
        metrics = await _update_patch_metrics(llm_requests=0, retries=0, cache_hit=False)
        return {
            "status": "processing",
            "final_reason": "processing",
            "attempts": [{"attempt": 0, "strategy": "dedupe", "result": "processing"}],
            "applied_files": [],
            "diff_summary": "",
            "patches": [],
            "validation": {"checks": []},
            "report": {"summary": "processing", "files_modified": [], "status": "failed"},
            "updated_files": normalized_existing,
            "failure_history": failure_history,
            "metrics": metrics,
        }

    try:
        language, project_type = _infer_learning_scope(normalized_existing, context_bundle)
        strategy_stats = await _load_strategy_stats(language, project_type)
        current_constraints, current_context_bundle = _apply_learned_adaptation(
            constraints=dict(constraints or {}),
            context_bundle=_clone_context_bundle(context_bundle),
            existing_files=normalized_existing,
            strategy_stats=strategy_stats,
        )
        if isinstance(current_constraints.get("self_improvement"), dict):
            zero_match_window = int(current_constraints["self_improvement"].get("ZERO_MATCH") or 0)
            if zero_match_window > 25:
                current_constraints["self_improvement"]["ZERO_MATCH"] = 25
        attempts_log: list[dict[str, Any]] = []
        last_error_details: list[dict[str, Any]] = []
        previous_patch_signature: list[str] | None = None

        for attempt in range(1, bounded_attempts + 1):
            llm_requests += 1
            if attempt > 1:
                retries_used += 1
            strategy = "initial" if attempt == 1 else _coerce_str(current_constraints.get("retry_strategy")).strip() or "retry"
            resp = await generate_patch_response(
                task=task,
                existing_files=normalized_existing,
                constraints=current_constraints,
                failure_history=failure_history,
                conversation_history=conversation_history,
                context_bundle=current_context_bundle,
            )
            if not isinstance(resp, dict):
                result_text = "APPLY_ERROR"
                attempts_log.append({"attempt": attempt, "strategy": strategy, "result": result_text})
                failure_history.append({"attempt": attempt, "error": result_text, "strategy": strategy})
                continue

            shape_errors = _validate_patch_response_shape(resp)
            if shape_errors:
                result_text = "APPLY_ERROR"
                attempts_log.append({"attempt": attempt, "strategy": strategy, "result": result_text})
                failure_history.append({"attempt": attempt, "error": result_text, "details": shape_errors, "strategy": strategy})
                continue

            patches = resp.get("patches") or []
            current_patch_signature = _patch_signature(patches if isinstance(patches, list) else [])
            if attempt > 1:
                # Constitution enforcement: abort immediately if patch targets drifted.
                try:
                    constitution.check_patch_drift(previous_patch_signature, current_patch_signature)
                except TargetDriftError:
                    final_reason = "TARGET_DRIFT: patch target changed across retries"
                    await _store_strategy_learning("APPLY_ERROR", language, project_type)
                    metrics = await _update_patch_metrics(llm_requests=llm_requests, retries=retries_used, cache_hit=False)
                    attempts_log.append({"attempt": attempt, "strategy": strategy, "result": final_reason})
                    return {
                        "status": "failed",
                        "final_reason": final_reason,
                        "attempts": attempts_log,
                        "applied_files": [],
                        "diff_summary": "",
                        "last_error_details": last_error_details,
                        "patches": [],
                        "validation": {"checks": ["syntax_valid", "required_feature_present", "no_unintended_changes"]},
                        "report": {"summary": final_reason, "files_modified": [], "status": "failed"},
                        "updated_files": normalized_existing,
                        "failure_history": failure_history,
                        "metrics": metrics,
                    }
            previous_patch_signature = current_patch_signature
            apply_res = apply_text_patches(existing_files=normalized_existing, patches=patches)
            if not bool(apply_res.get("ok")):
                error_details = [detail for detail in (apply_res.get("error_details") or []) if isinstance(detail, dict)]
                last_error_details = error_details
                next_strategy, paths, match_count = _classify_patch_failure(error_details)
                failure_code = _failure_code_from_error_details(error_details)
                logger.info(
                    "[patch-retry] attempt=%s strategy=%s next_strategy=%s match_count=%s paths=%s",
                    attempt,
                    strategy,
                    next_strategy,
                    match_count,
                    sorted(paths),
                )
                attempts_log.append(
                    {
                        "attempt": attempt,
                        "strategy": strategy,
                        "result": f"{failure_code}(match_count={match_count}, next={next_strategy})",
                    }
                )
                failure_history.append(
                    {
                        "attempt": attempt,
                        "error": failure_code,
                        "details": apply_res.get("errors") or [],
                        "error_details": error_details,
                        "strategy": strategy,
                        "match_count": match_count,
                    }
                )
                if attempt < bounded_attempts:
                    if next_strategy in {"expand_zero_match", "expand_multi_match"}:
                        current_context_bundle = _expand_code_snippets(
                            context_bundle=current_context_bundle,
                            existing_files=normalized_existing,
                            paths=paths,
                            extra_lines=min(20, 25),
                        )
                        instruction = (
                            "The previous patch target was not unique enough. Use more surrounding lines and make the target exact."
                            if next_strategy == "expand_multi_match"
                            else "The previous patch target was not found. Use a larger exact block from the provided code."
                        )
                        current_constraints = _augment_retry_constraints(constraints=current_constraints, instruction=instruction)
                    elif next_strategy == "strict_exact_function":
                        current_constraints = _augment_retry_constraints(
                            constraints=current_constraints,
                            instruction="Modify this exact function. Return a patch that changes the code, not a no-op.",
                        )
                    current_constraints["retry_strategy"] = next_strategy
                continue

            updated_files = apply_res.get("updated_files") or []
            checks = (resp.get("validation") or {}).get("checks") if isinstance(resp.get("validation"), dict) else None
            v_res = validate_patched_files(
                original_files=normalized_existing,
                updated_files=updated_files,
                patches=patches,
                checks=checks if isinstance(checks, list) else None,
            )
            if not bool(v_res.get("ok")):
                result_text = "APPLY_ERROR"
                attempts_log.append({"attempt": attempt, "strategy": strategy, "result": result_text})
                failure_history.append({"attempt": attempt, "error": result_text, "details": v_res.get("errors") or [], "strategy": strategy})
                continue

            report = resp.get("report") if isinstance(resp.get("report"), dict) else {}
            applied_files = apply_res.get("changed_files") or []
            diff_summary = _build_diff_summary(
                original_files=normalized_existing,
                updated_files=updated_files,
                applied_files=applied_files,
            )
            if not applied_files or not diff_summary.strip():
                final_reason = "NO_CHANGE: patch reported success without a verifiable diff"
                attempts_log.append({"attempt": attempt, "strategy": strategy, "result": final_reason})
                last_error_details = [{"type": "no_change", "path": "", "match_count": 1}]
                continue
            report_out = {
                "summary": _coerce_str(report.get("summary")).strip() or "patches applied successfully",
                "files_modified": applied_files,
                "status": "success",
            }
            attempts_log.append({"attempt": attempt, "strategy": strategy, "result": "success"})
            for failure_type in ("ZERO_MATCH", "MULTI_MATCH", "NO_CHANGE", "APPLY_ERROR"):
                if int((strategy_stats.get(failure_type) or {}).get("count") or 0) > 0:
                    await _decrease_strategy_learning(failure_type, language, project_type)
            result = {
                "status": "success",
                "final_reason": "success",
                "attempts": attempts_log,
                "applied_files": applied_files,
                "diff_summary": diff_summary,
                "patches": patches,
                "validation": {"checks": (checks or [])},
                "report": report_out,
                "updated_files": updated_files,
                "metrics": await _update_patch_metrics(llm_requests=llm_requests, retries=retries_used, cache_hit=False),
            }
            await _set_successful_patch_cache(task_hash, result)
            return result

        final_reason = "APPLY_ERROR: patch edit failed after retries"
        if last_error_details:
            learned_failure = _failure_code_from_error_details(last_error_details)
            final_reason = f"{learned_failure}: patch edit failed after retries"
            await _store_strategy_learning(learned_failure, language, project_type)
        else:
            await _store_strategy_learning("APPLY_ERROR", language, project_type)
        return {
            "status": "failed",
            "final_reason": final_reason,
            "attempts": attempts_log,
            "applied_files": [],
            "diff_summary": "",
            "last_error_details": last_error_details,
            "patches": [],
            "validation": {"checks": ["syntax_valid", "required_feature_present", "no_unintended_changes"]},
            "report": {"summary": final_reason, "files_modified": [], "status": "failed"},
            "updated_files": normalized_existing,
            "failure_history": failure_history,
            "metrics": await _update_patch_metrics(llm_requests=llm_requests, retries=retries_used, cache_hit=False),
        }
    finally:
        await _release_processing_lock(task_hash)


_TELEGRAM_TEMPLATE = """\
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

TOKEN = "{TOKEN}"

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Men botman")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.TEXT, handler))

app.run_polling()
"""


def _detect_code_task_kind(prompt: str, code: str | None = None) -> str | None:
    text = f"{prompt}\n\n{code or ''}".lower()
    if re.search(r"\btelegram\b", text) and re.search(r"\bbot\b", text):
        return "telegram_bot"
    if re.search(r"\bpython-telegram-bot\b", text):
        return "telegram_bot"
    if re.search(r"\bfrom\s+telegram\b|\btelegram\.ext\b|\bApplicationBuilder\b", code or ""):
        return "telegram_bot"
    return None


def force_template_if_needed(prompt: str) -> str | None:
    kind = _detect_code_task_kind(prompt)
    if kind != "telegram_bot":
        return None

    token = "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"
    m = re.search(r"(\d{5,}:[A-Za-z0-9_-]{20,})", prompt)
    if m:
        token = m.group(1)

    return _TELEGRAM_TEMPLATE.replace("{TOKEN}", token)


def validate_code_output(code: str, *, task_kind: str | None = None) -> tuple[bool, list[str]]:
    text = code or ""
    if not text.strip():
        return False, ["empty_output"]

    kind = task_kind or _detect_code_task_kind("", text)
    if kind != "telegram_bot":
        return True, []

    forbidden_checks: list[tuple[str, str]] = [
        ("asyncio.run", r"\basyncio\.run\s*\("),
        ("async def main", r"\basync\s+def\s+main\s*\("),
        ("await run_polling", r"\bawait\s+.*\brun_polling\s*\("),
        ("Updater", r"\bUpdater\s*\("),
        ("Filters import", r"\bfrom\s+telegram\.ext\s+import\s+Filters\b"),
        ("Filters usage", r"\bFilters\b|Filters\."),
        ("CallbackContext", r"\bCallbackContext\b"),
    ]

    reasons: list[str] = []
    for label, pattern in forbidden_checks:
        if re.search(pattern, text):
            reasons.append(label)

    required_checks: list[tuple[str, str]] = [
        ("missing ApplicationBuilder", r"\bApplicationBuilder\b"),
        ("missing run_polling", r"\brun_polling\s*\("),
        ("missing v20 filters", r"\bfilters\b"),
    ]
    for label, pattern in required_checks:
        if not re.search(pattern, text):
            reasons.append(label)

    return (len(reasons) == 0), reasons


async def regenerate_on_error(
    *,
    prompt: str,
    conversation_history: list[dict[str, str]] | None,
    invalid_code: str,
    reasons: list[str],
    task_kind: str | None,
) -> str:
    settings = get_settings()
    client = _get_openai_client()

    kind_hint = f"task_kind={task_kind}" if task_kind else "task_kind=unknown"
    reason_text = ", ".join(reasons) if reasons else "unknown"
    user_payload = (
        f"Task:\n{prompt.strip()}\n\n"
        f"Context:\n{kind_hint}\n"
        f"Validation failures: {reason_text}\n\n"
        "Invalid code:\n"
        f"{invalid_code.strip()}\n"
    )

    rewrite_messages: list[dict[str, str]] = [{"role": "system", "content": constitution.build_prompt("code_regenerate")}]
    for msg in (conversation_history or [])[-6:]:
        role = msg.get("role")
        content = msg.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str) and content.strip():
            rewrite_messages.append({"role": role, "content": content})
    rewrite_messages.append({"role": "user", "content": user_payload})

    logger.info("[codegen] regeneration attempt=2")
    try:
        rewrite = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=rewrite_messages,  # type: ignore[arg-type]
                temperature=0.1,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[codegen] regeneration LLM timed out after 45s; returning original")
        return invalid_code
    rewritten = (rewrite.choices[0].message.content or "").strip()
    if not rewritten:
        return invalid_code

    fenced = re.match(r"(?s)^```[a-zA-Z0-9_+-]*\\n(.*)\\n```\\s*$", rewritten)
    if fenced:
        return str(fenced.group(1)).strip("\n")
    return rewritten


def build_simple_plan(*, objective: str) -> list[AgentStep]:
    """Public wrapper for the deterministic single-step server plan."""
    return _build_simple_plan(objective)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PLAN_SCHEMA = {
    "type": "object",
    "required": ["objective", "steps", "context_summary"],
    "additionalProperties": False,
    "properties": {
        "objective": {"type": "string"},
        "context_summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["step", "tool", "args", "reason", "risk_level"],
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "integer"},
                    "tool": {
                        "type": "string",
                        "enum": [t.value for t in ToolName],
                    },
                    "args": {"type": "object"},
                    "reason": {"type": "string"},
                    "risk_level": {"type": "string", "enum": ["safe", "moderate", "dangerous"]},
                },
            },
        },
    },
}

_DECISION_SCHEMA = {
    "type": "object",
    "required": ["action", "reason", "summary_so_far"],
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [a.value for a in DecisionAction],
        },
        "reason": {"type": "string"},
        "summary_so_far": {"type": "string"},
        "modified_step": {
            "anyOf": [
                {
                    "type": "object",
                    "required": ["step", "tool", "args", "reason", "risk_level"],
                    "additionalProperties": False,
                    "properties": {
                        "step": {"type": "integer"},
                        "tool": {"type": "string", "enum": [t.value for t in ToolName]},
                        "args": {"type": "object"},
                        "reason": {"type": "string"},
                        "risk_level": {"type": "string", "enum": ["safe", "moderate", "dangerous"]},
                    },
                },
                {"type": "null"},
            ]
        },
    },
}


def _get_openai_client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


def _context_hash(objective: str, context: dict[str, Any]) -> str:
    raw = json.dumps({"objective": objective, "context": context}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_redis_client() -> "aioredis.Redis | None":  # type: ignore[name-defined]
    """Return an async Redis client if REDIS_URL is configured, else None."""
    if not _REDIS_AVAILABLE or aioredis is None:
        logger.warning("redis package not installed; LLM caching disabled")
        return None
    settings = get_settings()
    if not settings.REDIS_URL:
        return None
    try:
        return aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("Failed to connect to Redis; LLM caching disabled: %s", exc)
        return None


async def _cache_get(key: str) -> str | None:
    redis = await _get_redis_client()
    if redis is None:
        return None
    try:
        return await redis.get(key)
    except Exception as exc:
        logger.warning("Redis GET failed: %s", exc)
        return None
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _cache_set(key: str, value: str, ttl_seconds: int = 300) -> None:
    redis = await _get_redis_client()
    if redis is None:
        return
    try:
        await redis.setex(key, ttl_seconds, value)
    except Exception as exc:
        logger.warning("Redis SET failed: %s", exc)
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _chat_json(
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    cache_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Call OpenAI chat completions with JSON mode; optionally cache result."""
    if cache_key:
        cached = await _cache_get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass

    settings = get_settings()
    client = _get_openai_client()

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model or settings.OPENAI_MODEL,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="LLM request timed out after 45s",
        )
    except Exception as exc:
        logger.error("OpenAI call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM request failed: {exc}",
        )

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Empty response from LLM",
        )

    try:
        result: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM returned invalid JSON: {exc}",
        )

    if cache_key:
        await _cache_set(cache_key, raw)

    return result


# ---------------------------------------------------------------------------
# Public async functions
# ---------------------------------------------------------------------------





async def generate_plan(
    objective: str,
    context: dict[str, Any],
    max_steps: int = 8,
) -> AgentPlan:
    """Generate a multi-step execution plan using the LLM."""
    cache_key = f"forge_v2:plan:{_context_hash(objective, {**context, 'max_steps': max_steps})}"

    user_content = json.dumps(
        {
            "objective": objective,
            "max_steps": max_steps,
            "server_metadata": context.get("server_metadata", {}),
            "failure_history": context.get("failure_history", []),
            "allow_write": context.get("allow_write", False),
            "task_mode": context.get("task_mode", "complex"),
            "memory": context.get("memory", []),
            "capabilities": context.get("capabilities", {}),
            "workspace_path": context.get("workspace_path", ""),
            "template": context.get("template", {"matched": False}),
            # BUG #1 fix: pass authoritative platform state so the LLM always
            # uses the correct allocated port, subdomain, and protocol.
            "workspace_platform": context.get("workspace_platform", {}),
        },
        indent=2,
    )

    # Constitution enforcement: only require workspace_platform for deployment intents.
    classified_intent = (context.get("intent") or "").strip().lower()
    needs_platform = False
    if classified_intent == "server":
        needs_platform = True
    elif classified_intent not in ("chat", "code"):
        # Intent missing or uncertain — use objective as fallback only.
        needs_platform = bool(_DEPLOYMENT_INTENT_RE.search(objective or ""))
    if needs_platform:
        try:
            constitution.check_platform_context(context.get("workspace_platform"))
        except PlatformContextMissingError as exc:
            logger.warning("[generate_plan] platform context missing for deployment intent — aborting plan: %s", exc)
            return AgentPlan(
                objective=objective,
                steps=[],
                context_summary=f"PLATFORM_CONTEXT_MISSING: {exc}",
            )

    messages = [
        {"role": "system", "content": constitution.build_prompt("planner")},
        {"role": "user", "content": user_content},
    ]

    settings = get_settings()
    raw = await _chat_json(messages, _PLAN_SCHEMA, cache_key=cache_key, model=settings.OPENAI_MODEL_PLANNER or settings.OPENAI_MODEL)

    steps_raw = raw.get("steps", [])
    steps = []
    for s in steps_raw[:max_steps]:
        try:
            steps.append(AgentStep(**s))
        except Exception as exc:
            logger.warning("Skipping malformed plan step %s: %s", s, exc)

    # BUG #1 guard — always use the caller's objective, never the LLM's echoed
    # version. The LLM may paraphrase or substitute it; the planner must stay
    # bound to the exact current user request.
    return AgentPlan(
        objective=objective,
        steps=steps,
        context_summary=raw.get("context_summary", ""),
    )


async def evaluate_step(
    step: AgentStep,
    result: StepResult,
    context: dict[str, Any],
) -> AgentDecision:
    """Ask the LLM to evaluate a completed step result and decide next action."""
    user_content = json.dumps(
        {
            "step": step.model_dump(),
            "result": {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "success": result.success,
            },
            "objective": context.get("objective", ""),
            "previous_steps_summary": context.get("previous_steps_summary", ""),
            "retry_count": context.get("retry_count", 0),
            "max_retries": context.get("max_retries", 3),
        },
        indent=2,
    )

    messages = [
        {"role": "system", "content": constitution.build_prompt("evaluation")},
        {"role": "user", "content": user_content},
    ]

    settings = get_settings()
    raw = await _chat_json(messages, _DECISION_SCHEMA, model=settings.OPENAI_MODEL_DEBUG or settings.OPENAI_MODEL)

    modified_step: AgentStep | None = None
    ms_raw = raw.get("modified_step")
    if ms_raw and isinstance(ms_raw, dict):
        try:
            modified_step = AgentStep(**ms_raw)
        except Exception as exc:
            logger.warning("LLM returned invalid modified_step: %s — %s", ms_raw, exc)

    action_raw = raw.get("action", "abort")
    try:
        action = DecisionAction(action_raw)
    except ValueError:
        logger.warning("Unknown decision action '%s'; defaulting to abort", action_raw)
        action = DecisionAction.ABORT

    return AgentDecision(
        action=action,
        reason=raw.get("reason", ""),
        modified_step=modified_step,
        summary_so_far=raw.get("summary_so_far", ""),
    )


async def revise_plan(
    plan: AgentPlan,
    history: list[StepResult],
    completed_step_indices: list[int],
) -> AgentPlan:
    """Ask the LLM to revise remaining steps given execution history."""
    history_payload = [
        {
            "step": r.step,
            "tool": value_to_str(getattr(r, "tool", None)),
            "exit_code": r.exit_code,
            "success": r.success,
            "stdout_snippet": r.stdout[:500],
            "stderr_snippet": r.stderr[:200],
        }
        for r in history
    ]

    remaining_steps = [s for s in plan.steps if s.step not in completed_step_indices]

    user_content = json.dumps(
        {
            "objective": plan.objective,
            "original_plan": [s.model_dump() for s in plan.steps],
            "remaining_steps": [s.model_dump() for s in remaining_steps],
            "execution_history": history_payload,
        },
        indent=2,
    )

    messages = [
        {"role": "system", "content": constitution.build_prompt("revision")},
        {"role": "user", "content": user_content},
    ]

    settings = get_settings()
    raw = await _chat_json(messages, _PLAN_SCHEMA, model=settings.OPENAI_MODEL_DEBUG or settings.OPENAI_MODEL)

    steps_raw = raw.get("steps", [])
    steps = []
    for s in steps_raw:
        try:
            steps.append(AgentStep(**s))
        except Exception as exc:
            logger.warning("Skipping malformed revised step %s: %s", s, exc)

    return AgentPlan(
        objective=raw.get("objective", plan.objective),
        steps=steps,
        context_summary=raw.get("context_summary", plan.context_summary),
    )


# ---------------------------------------------------------------------------
# ReAct tool-calling loop  (PRIMARY AGENT LOOP)
# ---------------------------------------------------------------------------


_SIMPLE_TASK_KEYWORDS = (
    "check",
    "status",
    "disk",
    "memory",
    "ram",
    "swap",
    "cpu",
    "uptime",
    "logs",
    "log",
    "restart",
    "run bot",
)

_COMPLEX_TASK_KEYWORDS = (
    "deploy",
    "release",
    "rollback",
    "migrate",
    "install",
    "provision",
    "configure",
    "pipeline",
    "orchestrate",
    "multi-step",
)


def _truncate_for_log(value: str | None, limit: int = 4000) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated>"


def _build_fallback_diagnostic_command(objective: str) -> str:
    lowered = objective.lower()

    if any(token in lowered for token in ("disk", "storage", "filesystem", "mount")):
        return "df -h"
    if any(token in lowered for token in ("memory", "ram", "swap")):
        return "free -m"
    if any(token in lowered for token in ("cpu", "load", "process", "processes")):
        return "top -bn1 | head -n 20"
    if any(token in lowered for token in ("docker", "container", "containers")):
        return "docker ps -a"
    if any(token in lowered for token in ("network", "port", "socket", "listen")):
        return "ss -tulpn"
    if any(token in lowered for token in ("log", "logs", "journal")):
        return "journalctl -n 100 --no-pager"
    if any(token in lowered for token in ("service", "status", "health", "uptime")):
        return "hostname && uptime && df -h && free -m"
    return "hostname && uptime && df -h && free -m"


def _classify_task_mode(objective: str) -> str:
    lowered = objective.lower()
    if any(token in lowered for token in _COMPLEX_TASK_KEYWORDS):
        return "complex"
    if any(token in lowered for token in _SIMPLE_TASK_KEYWORDS):
        return "simple"
    return "complex"


def _build_fallback_tool(objective: str) -> tuple[ToolName, dict[str, Any], str]:
    lowered = objective.lower()
    if any(token in lowered for token in ("disk", "storage", "filesystem", "mount")):
        return ToolName.CHECK_DISK, {}, "Disk request maps directly to check_disk."
    if any(token in lowered for token in ("memory", "ram", "swap")):
        return ToolName.CHECK_MEMORY, {}, "Memory request maps directly to check_memory."
    if any(token in lowered for token in ("log", "logs", "journal")):
        return ToolName.RUN_COMMAND, {"command": "journalctl -n 100 --no-pager"}, "Use a safe log diagnostic."
    return (
        ToolName.RUN_COMMAND,
        {"command": _build_fallback_diagnostic_command(objective)},
        "Use a safe diagnostic fallback.",
    )


def _build_simple_plan(objective: str) -> list[AgentStep]:
    tool, args, reason = _build_fallback_tool(objective)
    return [AgentStep(step=1, tool=tool, args=args, reason=reason, risk_level="safe")]


def _build_complex_execution_messages(
    *,
    objective: str,
    task_mode: str,
    allow_write: bool,
    server: dict[str, Any],
    conversation_history: list[dict[str, str]] | None,
    plan: list[AgentStep],
    memory: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    server_info = {
        "name": server.get("name"),
        "host": server.get("host"),
        "ssh_user": server.get("ssh_user"),
    }
    messages: list[dict[str, Any]] = [{"role": "system", "content": constitution.build_prompt("execution")}]
    for history_message in conversation_history or []:
        role = history_message.get("role", "").strip()
        content = history_message.get("content", "").strip()
        if role in {"user", "assistant", "system"} and content:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "objective": objective,
                    "task_mode": task_mode,
                    "allow_write": allow_write,
                    "server": server_info,
                    "plan": [step.model_dump(mode="json") for step in plan],
                    "memory": (memory or [])[-10:],
                }
            ),
        }
    )
    return messages


async def _request_executor_tool_call(
    *,
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    current_step: AgentStep,
    tool_defs: list[dict[str, Any]],
) -> dict[str, Any]:
    executor_messages = list(messages)
    executor_messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": "Execute exactly one tool call for the current step.",
                    "step": current_step.model_dump(mode="json"),
                }
            ),
        }
    )
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=executor_messages,  # type: ignore[arg-type]
                tools=tool_defs,  # type: ignore[arg-type]
                tool_choice="required",
                temperature=0.0,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Executor LLM timed out after 45s",
        )
    return response.choices[0].message.model_dump(exclude_unset=True)


async def _request_final_summary(
    *,
    client: AsyncOpenAI,
    model: str,
    objective: str,
    results: list[StepResult],
) -> str:
    summary_payload = {
        "objective": objective,
        "results": [
            {
                "step": result.step,
                "tool": value_to_str(getattr(result, "tool", None)),
                "success": result.success,
                "exit_code": result.exit_code,
                "stdout": result.stdout[:1000],
                "stderr": result.stderr[:500],
            }
            for result in results
        ],
    }
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": constitution.build_prompt("execution")},
                    {"role": "user", "content": json.dumps(summary_payload)},
                    {
                        "role": "user",
                        "content": "Summarize only what the executed tools proved. Be concise and do not invent missing facts.",
                    },
                ],  # type: ignore[arg-type]
                temperature=0.0,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[summary] LLM timed out after 45s; returning empty")
        return ""
    return (response.choices[0].message.content or "").strip()


async def summarize_tool_results(*, objective: str, results: list[StepResult]) -> str:
    """Public helper: summarize only what executed tools proved."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return ""
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return await _request_final_summary(
        client=client,
        model=settings.OPENAI_MODEL_SUMMARY or settings.OPENAI_MODEL,
        objective=objective,
        results=results,
    )


async def run_tool_calling_loop(
    *,
    objective: str,
    intent: str,
    task_mode: str,
    server: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    max_steps: int,
    step_timeout: int,
    conversation_history: list[dict[str, str]] | None = None,
    memory: list[dict[str, Any]] | None = None,
    on_step_start: Callable[[int, str, dict[str, Any]], Awaitable[None]] | None = None,
    on_step_result: Callable[[StepResult], Awaitable[None]] | None = None,
    on_log_chunk: Callable[[int, str, str, str], Awaitable[None]] | None = None,
    on_plan: Callable[[list[AgentStep], str], Awaitable[None]] | None = None,
    on_decision: Callable[[AgentDecision], Awaitable[None]] | None = None,
) -> ToolCallingLoopResult:
    """Run the deterministic planner → executor → evaluator loop."""
    normalized_intent = (intent or "").strip().lower()
    if normalized_intent != "server":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INTENT_NOT_SERVER", "intent": normalized_intent},
        )

    # BUG #1 guard — objective must be the user's current request, never empty.
    _objective = (objective or "").strip()
    if not _objective:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_OBJECTIVE", "message": "Objective cannot be empty."},
        )

    from services.tools import execute_tool, get_tool_definitions

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured on the server.",
        )

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    normalized_task_mode = (task_mode or "").strip().lower()
    if normalized_task_mode not in _TASK_MODE_VALUES:
        normalized_task_mode = _classify_task_mode(objective)
    effective_max_steps = min(max_steps, 2) if normalized_task_mode == "simple" else max_steps
    tool_defs = get_tool_definitions(allow_write and normalized_task_mode == "complex")
    coordinator_context = {
        "server_metadata": {
            "host": server.get("host"),
            "ssh_user": server.get("ssh_user"),
            "name": server.get("name"),
        },
        "memory": (memory or [])[-10:],
        "failure_history": [],
        "allow_write": allow_write and normalized_task_mode == "complex",
        "objective": objective,
        "task_mode": normalized_task_mode,
    }

    if normalized_task_mode == "simple":
        plan = _build_simple_plan(_objective)
    else:
        generated_plan = await generate_plan(
            objective=_objective,
            context=coordinator_context,
            max_steps=effective_max_steps,
        )
        # BUG #1 guard — if LLM returned a different objective, override it and
        # stop execution rather than silently proceeding with the wrong plan.
        returned_obj = (generated_plan.objective or "").strip()
        if returned_obj.lower() != _objective.lower():
            logger.error(
                "[planner] objective_mismatch | expected=%r | got=%r — overriding with user input",
                _objective,
                returned_obj,
            )
            generated_plan = AgentPlan(
                objective=_objective,
                steps=generated_plan.steps,
                context_summary=generated_plan.context_summary,
            )
        plan = generated_plan.steps[:effective_max_steps] or _build_simple_plan(_objective)

    if on_plan:
        try:
            await on_plan(plan, normalized_task_mode)
        except Exception:
            pass

    messages = _build_complex_execution_messages(
        objective=objective,
        task_mode=normalized_task_mode,
        allow_write=allow_write,
        server=server,
        conversation_history=conversation_history,
        plan=plan,
        memory=memory,
    )
    results: list[StepResult] = []
    decisions: list[AgentDecision] = []

    async def _execute_single_tool(
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        step_number: int,
    ) -> StepResult:
        if on_step_start:
            try:
                await on_step_start(step_number, tool_name, tool_args)
            except Exception:
                pass

        result = await execute_tool(
            tool_name=tool_name,
            args=tool_args,
            intent=normalized_intent,
            server=server,
            workspace_path=workspace_path,
            allow_write=allow_write,
            timeout=step_timeout,
            step_number=step_number,
            on_output_chunk=on_log_chunk,
        )
        results.append(result)

        if on_step_result:
            try:
                await on_step_result(result)
            except Exception:
                pass

        return result

    step_pointer = 0
    while step_pointer < min(len(plan), effective_max_steps):
        current_step = plan[step_pointer]
        attempts = 0
        while True:
            if normalized_task_mode == "simple":
                tool_name = value_to_str(getattr(current_step, "tool", None))
                tool_args = current_step.args
                tool_call_id = f"simple-step-{current_step.step}"
            else:
                try:
                    raw_message = await _request_executor_tool_call(
                        client=client,
                        model=settings.OPENAI_MODEL,
                        messages=messages,
                        current_step=current_step,
                        tool_defs=tool_defs,
                    )
                except Exception as exc:
                    logger.error("OpenAI executor request failed: %s", exc)
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"LLM request failed: {exc}",
                    )
                messages.append(raw_message)
                tool_calls = raw_message.get("tool_calls") or []
                if not tool_calls:
                    tool_name = value_to_str(getattr(current_step, "tool", None))
                    tool_args = current_step.args
                    tool_call_id = f"forced-step-{current_step.step}"
                else:
                    first_call = tool_calls[0]
                    tool_name = first_call.get("function", {}).get("name") or value_to_str(getattr(current_step, "tool", None))
                    try:
                        tool_args = json.loads(first_call.get("function", {}).get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tool_args = current_step.args
                    current_tool = value_to_str(getattr(current_step, "tool", None))
                    if tool_name != current_tool:
                        tool_name = current_tool
                        tool_args = current_step.args
                    tool_call_id = first_call.get("id") or f"tool-step-{current_step.step}"

            result = await _execute_single_tool(
                tool_name=tool_name,
                tool_args=tool_args,
                step_number=current_step.step,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(
                        {
                            "stdout": result.stdout[:4000],
                            "stderr": result.stderr[:2000],
                            "exit_code": result.exit_code,
                            "success": result.success,
                            "duration_ms": result.duration_ms,
                        }
                    ),
                }
            )

            decision = AgentDecision(
                action=DecisionAction.CONTINUE if result.success else DecisionAction.ABORT,
                reason="Simple mode executes one minimal command." if normalized_task_mode == "simple" else "",
                summary_so_far="",
            )
            if normalized_task_mode == "complex":
                decision = await evaluate_step(
                    current_step,
                    result,
                    {
                        **coordinator_context,
                        "retry_count": attempts,
                        "max_retries": settings.AGENT_MAX_RETRIES,
                        "previous_steps_summary": " ".join(
                            existing.summary_so_far for existing in decisions if existing.summary_so_far
                        ),
                    },
                )
            decisions.append(decision)
            if on_decision:
                try:
                    await on_decision(decision)
                except Exception:
                    pass

            if decision.action == DecisionAction.CONTINUE:
                break
            if (
                decision.action == DecisionAction.RETRY
                and attempts < settings.AGENT_MAX_RETRIES
            ):
                attempts += 1
                continue
            if decision.action == DecisionAction.MODIFY and decision.modified_step is not None:
                current_step = decision.modified_step
                plan[step_pointer] = current_step
                attempts += 1
                continue
            step_pointer = len(plan)
            break

        step_pointer += 1

    if not results:
        fallback_tool, fallback_args, fallback_reason = _build_fallback_tool(objective)
        forced_step = AgentStep(step=1, tool=fallback_tool, args=fallback_args, reason=fallback_reason, risk_level="safe")
        plan = [forced_step]
        if on_plan:
            try:
                await on_plan(plan, normalized_task_mode)
            except Exception:
                pass
        forced_result = await _execute_single_tool(
            tool_name=value_to_str(getattr(forced_step, "tool", None)),
            tool_args=forced_step.args,
            step_number=forced_step.step,
        )
        decisions.append(
            AgentDecision(
                action=DecisionAction.CONTINUE if forced_result.success else DecisionAction.ABORT,
                reason="Forced fallback execution to satisfy the required tool-use contract.",
                summary_so_far="",
            )
        )

    overall_success = bool(results) and all(result.success for result in results)
    try:
        final_summary = await _request_final_summary(
            client=client,
            model=settings.OPENAI_MODEL,
            objective=objective,
            results=results,
        )
    except Exception as exc:
        logger.warning("Failed to get final summary from LLM: %s", exc)
        final_summary = ""

    if not final_summary:
        ok = sum(1 for result in results if result.success)
        final_summary = f"Executed {len(results)} tool step(s); {ok} succeeded."

    return ToolCallingLoopResult(
        task_mode=normalized_task_mode,
        plan=plan,
        steps=results,
        decisions=decisions,
        summary=final_summary,
        success=overall_success,
        steps_taken=len(results),
    )
