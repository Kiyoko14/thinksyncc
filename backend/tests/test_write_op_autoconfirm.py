"""
Unit tests for write-op auto-confirm behaviour in the tool execution layer.

BUG #3 fix coverage:
  SAFE (auto-confirmed):
    1. echo 'text' > file           — shell redirect >
    2. echo 'text' >> file          — shell redirect >>
    3. mkdir -p path                — directory creation
    4. touch file                   — file creation
    5. tee file                     — tee write
    6. cp src dst                   — copy
    7. mv src dst                   — move

  MUST NOT auto-confirm (require explicit confirm=true):
    8.  rm file                     — single file removal
    9.  rm -rf /                    — blocked by _BLOCKED_PATTERNS
    10. shutdown now                — blocked by _BLOCKED_PATTERNS
    11. ls -la                      — read-only, not a write op
"""

from __future__ import annotations

import pytest

from services.tools import _is_write_op, _WRITE_OP_RE


# ---------------------------------------------------------------------------
# _is_write_op — safe write ops that SHOULD auto-confirm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "echo 'ThinkSync working' > index.html",
    "echo 'line' >> /var/log/app.log",
    "mkdir -p /srv/app/public",
    "mkdir /srv/app",
    "touch /srv/app/health",
    "tee /etc/app.conf",
    "echo data | tee output.txt",
    "cp dist/index.js /srv/app/index.js",
    "mv /tmp/build /srv/app/release",
])
def test_is_write_op_returns_true(command: str) -> None:
    assert _is_write_op(command), f"Expected write-op=True for: {command!r}"


# ---------------------------------------------------------------------------
# _is_write_op — rm must NEVER auto-confirm (BUG #3 regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "rm old_config.json",
    "rm -f stale.lock",
    "rm -rf /",
    "rm -rf /srv/app",
    "rm -rf ./dist",
    "rm /etc/important.conf",
])
def test_rm_never_auto_confirmed(command: str) -> None:
    assert not _is_write_op(command), (
        f"rm must NEVER be auto-confirmed — matched incorrectly: {command!r}"
    )


# ---------------------------------------------------------------------------
# _is_write_op — read-only commands must NOT auto-confirm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "ls -la",
    "cat /etc/hosts",
    "ps aux",
    "df -h",
    "echo hello",
    "pm2 list",
    "git status",
    "shutdown now",
    "reboot",
    "kill -9 1234",
])
def test_is_write_op_returns_false_for_readonly_or_blocked(command: str) -> None:
    assert not _is_write_op(command), f"Expected write-op=False for: {command!r}"


# ---------------------------------------------------------------------------
# Regex edge-case validation
# ---------------------------------------------------------------------------

def test_redirect_single_gt_matched() -> None:
    assert _WRITE_OP_RE.search("echo foo > bar.txt") is not None


def test_redirect_double_gt_matched() -> None:
    assert _WRITE_OP_RE.search("echo foo >> bar.txt") is not None


def test_mkdir_p_matched() -> None:
    assert _WRITE_OP_RE.search("mkdir -p /a/b/c") is not None


def test_touch_matched() -> None:
    assert _WRITE_OP_RE.search("touch .env") is not None


def test_rm_not_in_pattern() -> None:
    assert _WRITE_OP_RE.search("rm file.txt") is None, (
        "rm must not appear in _WRITE_OP_RE — it was re-added accidentally"
    )
