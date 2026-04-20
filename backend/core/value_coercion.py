from __future__ import annotations

from typing import Any


def value_to_str(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        try:
            value = value.value
        except Exception:
            pass
    return value if isinstance(value, str) else str(value)

