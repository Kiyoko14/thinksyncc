"""
Regression tests — multi-question clarification answer folding (Bridge 1e).

PROVES: _apply_clarification_answer_to_spec folds EVERY question in
clarification_session.questions[] into ProjectSpecification (not only
questions[0]), removes only answered fields from missing_info, and
recalculates needs_user_input.

These tests stub the DB/state reads so the folding logic is exercised
deterministically without a live database. No production logic changed.
"""

import asyncio
from unittest import mock

import services.agent_service as A


def _make_state(answer_text, questions, missing_info, spec_extra=None):
    """Build a fake InteractionState + jobs row for the folding function."""
    from models.approval import JobInteractionState, JobState

    class _Msg:
        def __init__(self, sender, message_type, content):
            self.sender = sender
            self.message_type = message_type
            self.content = content

    st = JobInteractionState(job_id="job_x", conversation_id="conv_x")
    st.transition_to(JobState.RESUMED)
    st.add_message(_Msg("user", "answer", answer_text))

    spec = {"missing_info": list(missing_info), "needs_user_input": True, "readiness": "Blocked"}
    spec.update(spec_extra or {})
    row = {
        "specification": spec,
        "clarification_session": {"questions": questions},
    }
    return st, row


def _run_fold(answer_text, questions, missing_info, spec_extra=None, fields_in_answer=None):
    st, row = _make_state(answer_text, questions, missing_info, spec_extra)

    # Stub the two external reads used inside _apply_clarification_answer_to_spec.
    captured = {}
    async def fake_db_update(jid, patch):
        captured["patch"] = patch

    class _Chain:
        def __init__(self, row):
            self._row = row
        def table(self, name):
            return self
        def select(self, *a, **k):
            return self
        def eq(self, *a, **k):
            return self
        def limit(self, *a, **k):
            return self
        async def execute(self):
            class _R:
                data = [self._row]
            return _R()

    async def fake_get_state(cid, jid):
        return st

    def fake_load_spec():
        async def _a():
            return _Chain(row)
        return _a()

    orig_get_state = A.InteractiveWaitEngine.get_state
    A.InteractiveWaitEngine.get_state = staticmethod(fake_get_state)
    orig_get_sb = A.get_supabase_async
    A.get_supabase_async = fake_load_spec
    orig_db = A._db_update
    A._db_update = fake_db_update
    try:
        asyncio.run(A._apply_clarification_answer_to_spec("job_x", conversation_id="conv_x"))
    finally:
        A.InteractiveWaitEngine.get_state = orig_get_state
        A.get_supabase_async = orig_get_sb
        A._db_update = orig_db

    return row["specification"], captured.get("patch", {})


# ---------------------------------------------------------------------------
# 1 question
# ---------------------------------------------------------------------------
def test_one_question_folds():
    spec, patch = _run_fold(
        "8080",
        [{"required_field": "port", "question": "port?"}],
        ["port"],
    )
    assert spec["port"] == "8080", spec
    assert "port" not in spec["missing_info"], spec
    assert spec["needs_user_input"] is False, spec
    assert spec["readiness"] == "Ready", spec


# ---------------------------------------------------------------------------
# 2 questions (combined answer)
# ---------------------------------------------------------------------------
def test_two_questions_fold():
    spec, patch = _run_fold(
        "port=8080, db=postgres",
        [
            {"required_field": "port", "question": "port?"},
            {"required_field": "db", "question": "db?"},
        ],
        ["port", "db"],
    )
    assert spec["port"] == "8080", spec
    assert spec["db"] == "postgres", spec
    assert "port" not in spec["missing_info"], spec
    assert "db" not in spec["missing_info"], spec
    assert spec["needs_user_input"] is False, spec


# ---------------------------------------------------------------------------
# 5 questions
# ---------------------------------------------------------------------------
def test_five_questions_fold():
    fields = ["port", "db", "framework", "host", "user"]
    qs = [{"required_field": f, "question": f + "?"} for f in fields]
    answer = "port=8080, db=postgres, framework=flask, host=1.2.3.4, user=admin"
    spec, patch = _run_fold(answer, qs, fields)
    for f, v in [("port", "8080"), ("db", "postgres"), ("framework", "flask"),
                 ("host", "1.2.3.4"), ("user", "admin")]:
        assert spec[f] == v, spec
    assert spec["missing_info"] == [], spec["missing_info"]
    assert spec["needs_user_input"] is False, spec


# ---------------------------------------------------------------------------
# Unanswered questions remain pending
# ---------------------------------------------------------------------------
def test_unanswered_remains_in_missing_info():
    # Only answer 1 of 2 questions.
    spec, patch = _run_fold(
        "port=8080",
        [
            {"required_field": "port", "question": "port?"},
            {"required_field": "db", "question": "db?"},
        ],
        ["port", "db"],
    )
    assert spec["port"] == "8080", spec
    # db was NOT answered -> still in missing_info and needs_user_input stays True
    assert "db" in spec["missing_info"], spec
    assert spec["needs_user_input"] is True, spec
    assert "port" not in spec["missing_info"], spec


# ---------------------------------------------------------------------------
# Every answered field removed from missing_info; spec contains all answers
# ---------------------------------------------------------------------------
def test_answered_removed_unanswered_kept():
    spec, patch = _run_fold(
        "port=8080, db=postgres",
        [
            {"required_field": "port", "question": "port?"},
            {"required_field": "db", "question": "db?"},
            {"required_field": "host", "question": "host?"},
        ],
        ["port", "db", "host"],
    )
    assert spec["port"] == "8080"
    assert spec["db"] == "postgres"
    assert "host" in spec["missing_info"]  # unanswered
    assert "port" not in spec["missing_info"]
    assert "db" not in spec["missing_info"]
    assert spec["needs_user_input"] is True  # host still missing
