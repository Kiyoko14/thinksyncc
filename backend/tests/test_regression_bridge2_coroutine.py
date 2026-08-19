"""
Regression guard — Bridge 2 coroutine serialization fix.

ROOT CAUSE (verified by read-only audit):
    services/agent_service.py passed
        implementation_report=_build_implementation_report(...)
    (an async def) directly into build_plan() WITHOUT awaiting it.
    The coroutine then propagated into context["implementation_report"]
    -> agent_llm._context_hash -> json.dumps(coroutine)
    -> TypeError: Object of type coroutine is not JSON serializable
    plus RuntimeWarning: coroutine '_build_implementation_report' was never awaited.

FIX: the single caller now AWAITS the coroutine:
        implementation_report=await _build_implementation_report(...)

This test guards the regression WITHOUT executing the whole pipeline. It
asserts the two invariant properties that, if broken, reproduce the bug:

  1. The ONLY runtime caller (inside run_agent_pipeline) AWAITS the coroutine.
  2. When awaited, _build_implementation_report returns dict (or None) that is
     JSON-serializable -- never a coroutine.
"""

import ast
import asyncio
import json
import inspect

import services.agent_service as A
import services.implementation_intelligence as II


class _Report:
    def to_dict(self):
        return {"strategy": "exact", "compatibility_score": 0.9, "template_name": "tpl"}


class _ImplEngine:
    @staticmethod
    async def decide_strategy(*_a, **_k):
        return _Report()


def test_caller_awaits_coroutine():
    src = inspect.getsource(A.run_agent_pipeline)
    # The exact production call site must await the coroutine.
    assert "implementation_report=await _build_implementation_report(" in src, (
        "Caller of _build_implementation_report must AWAIT it; "
        "an un-awaited call reproduces the coroutine JSON-serialization crash."
    )
    # And there must be NO un-awaited form remaining in the caller.
    assert "implementation_report=_build_implementation_report(" not in src, (
        "Un-awaited form still present in run_agent_pipeline."
    )


def test_awaited_report_is_serializable_dict():
    II.ImplementationIntelligence = _ImplEngine
    assert inspect.iscoroutinefunction(A._build_implementation_report)

    async def _go():
        return await A._build_implementation_report(
            objective="deploy a node app", spec=None, server_id="srv", user_id="u1"
        )

    # -W error promotes any "coroutine never awaited" RuntimeWarning into a hard failure
    report = asyncio.run(_go())

    assert report is None or isinstance(report, dict), f"expected dict|None, got {type(report)}"
    # This is the exact sink that crashed before the fix:
    assert json.dumps(report) is not None
