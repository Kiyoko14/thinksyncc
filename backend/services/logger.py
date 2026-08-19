from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
Layer = Literal["router", "template", "guardrails", "execution", "self_healing", "tools"]

_base_logger = logging.getLogger("thinksync.observability")


def new_trace_id() -> str:
    return str(uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_log(
    *,
    level: Level,
    layer: Layer,
    message: str,
    meta: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": now_iso(),
        "level": level,
        "layer": layer,
        "message": message,
        "meta": meta or {},
    }
    if trace_id:
        payload["meta"] = dict(payload["meta"])
        payload["meta"]["trace_id"] = trace_id
    return payload


def emit(
    *,
    level: Level,
    layer: Layer,
    message: str,
    meta: dict[str, Any] | None = None,
    trace_id: str | None = None,
    exc_info: bool = False,
) -> dict[str, Any]:
    payload = make_log(level=level, layer=layer, message=message, meta=meta, trace_id=trace_id)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    if level == "DEBUG":
        _base_logger.debug(text, exc_info=exc_info)
    elif level == "INFO":
        _base_logger.info(text, exc_info=exc_info)
    elif level == "WARNING":
        _base_logger.warning(text, exc_info=exc_info)
    else:
        _base_logger.error(text, exc_info=exc_info)
    return payload


@dataclass
class MetricsSnapshot:
    total_requests: int
    success_count: int
    error_count: int
    avg_execution_time: float

    def to_public(self) -> dict[str, Any]:
        requests = max(0, int(self.total_requests))
        success = max(0, int(self.success_count))
        errors = max(0, int(self.error_count))
        avg_time = float(self.avg_execution_time)
        rate = (success / requests) if requests else 0.0
        return {
            "requests": requests,
            "success_rate": rate,
            "avg_time": avg_time,
            "success": success,
            "errors": errors,
        }


class InMemoryMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_requests = 0
        self._success_count = 0
        self._error_count = 0
        self._total_execution_time = 0.0

    def record_request(self, *, success: bool, execution_time_seconds: float | None) -> None:
        with self._lock:
            self._total_requests += 1
            if success:
                self._success_count += 1
            else:
                self._error_count += 1
            if execution_time_seconds is not None:
                try:
                    self._total_execution_time += max(0.0, float(execution_time_seconds))
                except Exception:
                    pass

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            total = int(self._total_requests)
            success = int(self._success_count)
            errors = int(self._error_count)
            total_time = float(self._total_execution_time)
        avg_time = (total_time / total) if total else 0.0
        return MetricsSnapshot(
            total_requests=total,
            success_count=success,
            error_count=errors,
            avg_execution_time=avg_time,
        )


METRICS = InMemoryMetrics()


class Timer:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self._start)

