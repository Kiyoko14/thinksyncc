from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_REQUEST_MODE: ContextVar[str | None] = ContextVar("thinksync_request_mode", default=None)


def get_request_mode() -> str | None:
    return _REQUEST_MODE.get()


def set_request_mode(mode: Any) -> Token:
    cleaned = (str(mode) if mode is not None else "").strip().lower()
    return _REQUEST_MODE.set(cleaned or None)


def reset_request_mode(token: Token) -> None:
    _REQUEST_MODE.reset(token)

