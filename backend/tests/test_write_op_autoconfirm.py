"""
Unit tests for write-op auto-confirm behaviour in the tool execution layer.

Covers:
  1. echo 'text' > file           — shell redirect >
  2. echo 'text' >> file          — shell redirect >>
  3. mkdir -p path                — directory creation
  4. touch file                   — file creation
  5. tee file                     — tee write
  6. cp src dst                   — copy
  7. mv src dst                   — move
  8. rm file                      — single file removal (NOT rm -rf)

Negative (must NOT auto-confirm / must remain blocked):
  9.  rm -rf /                    — stays blocked by _BLOCKED_PATTERNS
  10. shutdown now                — stays blocked
  11. ls -la                      — not a write op, confirm stays False
"""

from __future__ import annotations

import pytest

from services.tools import _is_write_op, _WRITE_OP_RE


# ---------------------------------------------------------------------------
# _is_write_op — positive cases
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
    "rm old_config.json",
    "rm -f stale.lock",
])
def test_is_write_op_returns_true(command: str) -> None:
    assert _is_write_op(command), f"Expected write-op=True for: {command!r}"


# ---------------------------------------------------------------------------
# _is_write_op — negative cases (truly dangerous or non-write)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "ls -la",
    "cat /etc/hosts",
    "ps aux",
    "df -h",
    "echo hello",
    "pm2 list",
    "git status",
])
def test_is_write_op_returns_false_for_readonly(command: str) -> None:
    assert not _is_write_op(command), f"Expected write-op=False for: {command!r}"


# ---------------------------------------------------------------------------
# Confirm that rm -rf does NOT match _WRITE_OP_RE
# (it is caught earlier by _BLOCKED_PATTERNS, not auto-confirmed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf /srv/app",
    "rm -rf ./dist",
])
def test_rm_rf_not_matched_as_write_op(command: str) -> None:
    assert not _is_write_op(command), (
        f"rm -rf must NOT be auto-confirmed; matched incorrectly: {command!r}"
    )


# ---------------------------------------------------------------------------
# Validate the regex directly for edge cases
# ---------------------------------------------------------------------------

def test_redirect_single_gt_matched() -> None:
    assert _WRITE_OP_RE.search("echo foo > bar.txt") is not None


def test_redirect_double_gt_matched() -> None:
    assert _WRITE_OP_RE.search("echo foo >> bar.txt") is not None


def test_mkdir_p_matched() -> None:
    assert _WRITE_OP_RE.search("mkdir -p /a/b/c") is not None


def test_touch_matched() -> None:
    assert _WRITE_OP_RE.search("touch .env") is not None
