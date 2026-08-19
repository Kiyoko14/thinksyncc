"""
Unit tests for BUG #2 — deployment URL verification before emitting success.

Covers _deployment_verified_from_steps:
  1. Step with HTTP 200 stdout → verified
  2. Step with curl exit_code=0 → verified
  3. Step with no HTTP evidence → not verified
  4. Empty steps → not verified
  5. Step with non-200 HTTP response → not verified
"""

from __future__ import annotations

import pytest

from services.agent_service import _deployment_verified_from_steps


def _make_step(stdout: str = "", exit_code: int = 1, command: str = "") -> dict:
    return {
        "stdout": stdout,
        "exit_code": exit_code,
        "args": {"command": command},
    }


# ---------------------------------------------------------------------------
# Positive — URL should be emitted
# ---------------------------------------------------------------------------

def test_http_200_ok_in_stdout_verifies() -> None:
    steps = [_make_step(stdout="HTTP/1.1 200 OK\nContent-Type: text/html")]
    assert _deployment_verified_from_steps(steps) is True


def test_http2_200_in_stdout_verifies() -> None:
    steps = [_make_step(stdout="HTTP/2 200\nserver: nginx")]
    assert _deployment_verified_from_steps(steps) is True


def test_curl_verbose_lt_http_verifies() -> None:
    steps = [_make_step(stdout="* Connected\n< HTTP/1.1 200 OK\n< server: nginx")]
    assert _deployment_verified_from_steps(steps) is True


def test_curl_exit0_with_200_verifies() -> None:
    steps = [_make_step(stdout="200 OK", exit_code=0, command="curl -I http://localhost:3000")]
    assert _deployment_verified_from_steps(steps) is True


def test_200_ok_case_insensitive() -> None:
    steps = [_make_step(stdout="200 ok")]
    assert _deployment_verified_from_steps(steps) is True


# ---------------------------------------------------------------------------
# Negative — URL must NOT be emitted
# ---------------------------------------------------------------------------

def test_empty_steps_not_verified() -> None:
    assert _deployment_verified_from_steps([]) is False


def test_no_http_evidence_not_verified() -> None:
    steps = [_make_step(stdout="Process started on port 3000")]
    assert _deployment_verified_from_steps(steps) is False


def test_non_200_http_not_verified() -> None:
    steps = [_make_step(stdout="HTTP/1.1 502 Bad Gateway")]
    assert _deployment_verified_from_steps(steps) is False


def test_curl_exit0_without_200_not_verified() -> None:
    # curl/wget exit_code alone is never sufficient — stdout must contain HTTP 200.
    # Even if curl exits 0, we require explicit "200 OK" evidence in stdout.
    steps = [_make_step(stdout="curl: (7) Failed to connect", exit_code=0, command="curl http://example.com")]
    assert _deployment_verified_from_steps(steps) is False


def test_curl_exit0_without_http_response_not_verified() -> None:
    # curl exit_code 0 with non-HTTP stdout (e.g. FTP or redirect) is not enough.
    steps = [_make_step(stdout="Redirecting...", exit_code=0, command="curl -L http://example.com")]
    assert _deployment_verified_from_steps(steps) is False


def test_successful_non_http_step_not_verified() -> None:
    steps = [
        _make_step(stdout="pm2 started successfully", exit_code=0, command="pm2 start app.js"),
        _make_step(stdout="Listening on port 3000", exit_code=0, command="node -e 'require(\"./app\")'"),
    ]
    assert _deployment_verified_from_steps(steps) is False


def test_all_steps_fail_not_verified() -> None:
    steps = [
        _make_step(stdout="", exit_code=1, command="curl http://localhost"),
        _make_step(stdout="error", exit_code=1),
    ]
    assert _deployment_verified_from_steps(steps) is False


def test_mixed_steps_verified_if_one_has_http_200() -> None:
    steps = [
        _make_step(stdout="pm2 started", exit_code=0),
        _make_step(stdout="HTTP/1.1 200 OK", exit_code=0),
    ]
    assert _deployment_verified_from_steps(steps) is True
