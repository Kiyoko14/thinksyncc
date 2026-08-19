"""Structured Clarification Form — model, mapping, validation, secrets.

PROVES:
  1. ClarificationForm.from_questions maps ClarificationQuestion -> form schema
     generically (no project-type / Telegram hard-coding).
  2. Client + authoritative backend validation agree (validate_submission).
  3. Secret fields are redacted and NEVER appear in chat history / redacted copy.
  4. One structured submission folds EVERY question into the spec (end-to-end).
  5. The free-text / regex fallback still works (backward compatibility).

No production logic is rewritten — only extended.
"""

import asyncio
from unittest import mock

import services.agent_service as A
from models.clarification_form import (
    ClarificationForm,
    ClarificationFormSubmission,
    ClarificationFormAnswer,
    ClarificationQuestionType,
    ClarificationValidation,
)


# ---------------------------------------------------------------------------
# 1. Adapter mapping (generic)
# ---------------------------------------------------------------------------

def _telegram_questions():
    """Mimic what the Adaptive Clarification Engine would produce for a bot."""
    from models.interaction import ClarificationQuestion, QuestionType, QuestionPriority

    return [
        ClarificationQuestion(
            question_type=QuestionType.REQUIREMENT,
            priority=QuestionPriority.CRITICAL,
            question="What is the Bot Token?",
            required_field="bot_token",
            blocking=True,
        ),
        ClarificationQuestion(
            question_type=QuestionType.REQUIREMENT,
            priority=QuestionPriority.HIGH,
            question="Webhook Mode?",
            required_field="webhook_mode",
            blocking=True,
            options=["Polling", "Webhook"],
        ),
        ClarificationQuestion(
            question_type=QuestionType.REQUIREMENT,
            priority=QuestionPriority.MEDIUM,
            question="Which Framework?",
            required_field="framework",
            blocking=False,
            options=["Aiogram", "python-telegram-bot"],
        ),
        ClarificationQuestion(
            question_type=QuestionType.REQUIREMENT,
            priority=QuestionPriority.CRITICAL,
            question="What port should it listen on?",
            required_field="port",
            blocking=True,
        ),
        ClarificationQuestion(
            question_type=QuestionType.REQUIREMENT,
            priority=QuestionPriority.LOW,
            question="Notification email?",
            required_field="email",
            blocking=False,
        ),
    ]


def test_from_questions_maps_telegram_bot():
    form = ClarificationForm.from_questions(_telegram_questions())
    assert form.title == "Clarification Required"
    assert len(form.questions) == 5

    by_field = {q.required_field: q for q in form.questions}
    # Token -> secret
    assert by_field["bot_token"].secret is True
    assert by_field["bot_token"].type is ClarificationQuestionType.SECRET
    # Webhook mode -> single_select with choices
    assert by_field["webhook_mode"].type is ClarificationQuestionType.SINGLE_SELECT
    assert {c.value for c in by_field["webhook_mode"].choices} == {"Polling", "Webhook"}
    # Framework -> single_select (non-blocking)
    assert by_field["framework"].required is False
    # Port -> port type with bounds
    assert by_field["port"].type is ClarificationQuestionType.PORT
    assert by_field["port"].validation.min == 1.0
    assert by_field["port"].validation.max == 65535.0
    # Email -> email type with regex
    assert by_field["email"].type is ClarificationQuestionType.EMAIL
    assert by_field["email"].validation.regex is not None


# ---------------------------------------------------------------------------
# 2. Authoritative validation
# ---------------------------------------------------------------------------

def test_validate_submission_required_missing():
    form = ClarificationForm.from_questions(_telegram_questions())
    sub = ClarificationFormSubmission(clarification_id=form.id, answers=[])
    errors = form.validate_submission(sub)
    # All required fields (token, webhook_mode, port) should yield errors.
    required_fields = {q.required_field for q in form.questions if q.required}
    reported = {e["required_field"] for e in errors}
    assert required_fields.issubset(reported)


def test_validate_submission_valid_choice_and_value():
    form = ClarificationForm.from_questions(_telegram_questions())
    token_q = next(q for q in form.questions if q.required_field == "bot_token")
    port_q = next(q for q in form.questions if q.required_field == "port")
    mode_q = next(q for q in form.questions if q.required_field == "webhook_mode")
    sub = ClarificationFormSubmission(
        clarification_id=form.id,
        answers=[
            ClarificationFormAnswer(question_id=token_q.id, required_field="bot_token", value="123:abc"),
            ClarificationFormAnswer(question_id=mode_q.id, required_field="webhook_mode", selected_choice="Webhook"),
            ClarificationFormAnswer(question_id=port_q.id, required_field="port", value="8443"),
        ],
    )
    assert form.validate_submission(sub) == []


def test_validate_submission_bad_port_and_bad_choice():
    form = ClarificationForm.from_questions(_telegram_questions())
    token_q = next(q for q in form.questions if q.required_field == "bot_token")
    port_q = next(q for q in form.questions if q.required_field == "port")
    mode_q = next(q for q in form.questions if q.required_field == "webhook_mode")
    sub = ClarificationFormSubmission(
        clarification_id=form.id,
        answers=[
            ClarificationFormAnswer(question_id=token_q.id, required_field="bot_token", value="123:abc"),
            ClarificationFormAnswer(question_id=mode_q.id, required_field="webhook_mode", selected_choice="NOPE"),
            ClarificationFormAnswer(question_id=port_q.id, required_field="port", value="999999"),
        ],
    )
    errors = form.validate_submission(sub)
    err_fields = {e["required_field"] for e in errors}
    assert "webhook_mode" in err_fields
    assert "port" in err_fields


# ---------------------------------------------------------------------------
# 3. Secrets never leak
# ---------------------------------------------------------------------------

def test_secret_redacted_in_submission():
    form = ClarificationForm.from_questions(_telegram_questions())
    token_q = next(q for q in form.questions if q.required_field == "bot_token")
    sub = ClarificationFormSubmission(
        clarification_id=form.id,
        answers=[
            ClarificationFormAnswer(question_id=token_q.id, required_field="bot_token", value="SUPER_SECRET_TOKEN"),
            ClarificationFormAnswer(question_id="x", required_field="framework", selected_choice="Aiogram"),
        ],
    )
    redacted = sub.redacted_with_form(form)
    by_field = {a.required_field: a for a in redacted.answers}
    assert by_field["bot_token"].value is None, "secret must be stripped"
    assert by_field["framework"].selected_choice == "Aiogram", "non-secret label preserved"


# ---------------------------------------------------------------------------
# 4. End-to-end: one structured submission folds EVERY question into spec
# ---------------------------------------------------------------------------

def _make_state_with_submission(submission_dict, answer_text, questions, missing_info, spec_extra=None):
    from models.approval import JobInteractionState, JobState

    class _Msg:
        def __init__(self, sender, message_type, content, structured=False):
            self.sender = sender
            self.message_type = message_type
            self.content = content
            self.structured = structured

    st = JobInteractionState(job_id="job_x", conversation_id="conv_x")
    st.transition_to(JobState.RESUMED)
    st.add_message(_Msg("user", "answer", answer_text or "Submitted N answers.", structured=bool(submission_dict)))
    st.clarification_submission = submission_dict

    spec = {"missing_info": list(missing_info), "needs_user_input": True, "readiness": "Blocked"}
    spec.update(spec_extra or {})
    row = {
        "specification": spec,
        "clarification_session": {"questions": questions},
    }
    return st, row


def test_structured_submission_folds_all_questions():
    questions = [
        {"required_field": "bot_token", "question": "Bot Token?", "blocking": True},
        {"required_field": "webhook_mode", "question": "Webhook Mode?", "blocking": True},
        {"required_field": "framework", "question": "Framework?", "blocking": False},
    ]
    submission = {
        "clarification_id": "f1",
        "answers": [
            {"question_id": "q1", "required_field": "bot_token", "value": "123:TOKEN"},
            {"question_id": "q2", "required_field": "webhook_mode", "selected_choice": "Webhook"},
            {"question_id": "q3", "required_field": "framework", "selected_choice": "Aiogram"},
        ],
    }
    st, row = _make_state_with_submission(
        submission, "Submitted 3 answers.", questions, ["bot_token", "webhook_mode", "framework"]
    )

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

    orig_get_state = A.InteractiveWaitEngine.get_state
    A.InteractiveWaitEngine.get_state = staticmethod(fake_get_state)
    orig_get_sb = A.get_supabase_async

    def fake_load_spec():
        async def _a():
            return _Chain(row)
        return _a()
    A.get_supabase_async = fake_load_spec
    orig_db = A._db_update
    A._db_update = fake_db_update
    try:
        asyncio.run(A._apply_clarification_answer_to_spec("job_x", conversation_id="conv_x"))
    finally:
        A.InteractiveWaitEngine.get_state = orig_get_state
        A.get_supabase_async = orig_get_sb
        A._db_update = orig_db

    spec = row["specification"]
    assert spec["bot_token"] == "123:TOKEN"
    assert spec["webhook_mode"] == "Webhook"
    assert spec["framework"] == "Aiogram"
    assert spec["missing_info"] == []
    assert spec["needs_user_input"] is False
    assert spec["readiness"] == "Ready"


def test_structured_submission_omits_unanswered():
    questions = [
        {"required_field": "bot_token", "question": "Bot Token?", "blocking": True},
        {"required_field": "framework", "question": "Framework?", "blocking": False},
    ]
    submission = {
        "clarification_id": "f1",
        "answers": [
            {"question_id": "q1", "required_field": "bot_token", "value": "123:TOKEN"},
        ],
    }
    st, row = _make_state_with_submission(
        submission, "Submitted 1 answer.", questions, ["bot_token", "framework"]
    )

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

    orig_get_state = A.InteractiveWaitEngine.get_state
    A.InteractiveWaitEngine.get_state = staticmethod(fake_get_state)
    orig_get_sb = A.get_supabase_async

    def fake_load_spec():
        async def _a():
            return _Chain(row)
        return _a()
    A.get_supabase_async = fake_load_spec
    orig_db = A._db_update
    A._db_update = fake_db_update
    try:
        asyncio.run(A._apply_clarification_answer_to_spec("job_x", conversation_id="conv_x"))
    finally:
        A.InteractiveWaitEngine.get_state = orig_get_state
        A.get_supabase_async = orig_get_sb
        A._db_update = orig_db

    spec = row["specification"]
    assert spec["bot_token"] == "123:TOKEN"
    assert "framework" in spec["missing_info"]  # still unanswered
    assert spec["needs_user_input"] is True


# ---------------------------------------------------------------------------
# 5. Backward compatibility: free-text regex fallback still works
# ---------------------------------------------------------------------------

def test_legacy_freetext_fallback_still_works():
    questions = [
        {"required_field": "port", "question": "port?"},
        {"required_field": "db", "question": "db?"},
    ]
    st, row = _make_state_with_submission(
        None, "port=8080, db=postgres", questions, ["port", "db"]
    )

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

    orig_get_state = A.InteractiveWaitEngine.get_state
    A.InteractiveWaitEngine.get_state = staticmethod(fake_get_state)
    orig_get_sb = A.get_supabase_async

    def fake_load_spec():
        async def _a():
            return _Chain(row)
        return _a()
    A.get_supabase_async = fake_load_spec
    orig_db = A._db_update
    A._db_update = fake_db_update
    try:
        asyncio.run(A._apply_clarification_answer_to_spec("job_x", conversation_id="conv_x"))
    finally:
        A.InteractiveWaitEngine.get_state = orig_get_state
        A.get_supabase_async = orig_get_sb
        A._db_update = orig_db

    spec = row["specification"]
    assert spec["port"] == "8080"
    assert spec["db"] == "postgres"
    assert spec["needs_user_input"] is False
