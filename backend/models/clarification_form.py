"""Structured Clarification Form — generic, renderer-agnostic schema.

This module introduces the generic *structured clarification* contract that
replaces the free-text clarification conversation.  It is intentionally
**generic**: it contains NO Telegram-specific logic, NO project-type
hard-coding, and NO frontend/rendering concerns.  The backend produces one
``ClarificationForm`` (a list of ``ClarificationFormQuestion``); the frontend
is a pure renderer that collects answers and submits a single
``ClarificationFormSubmission``.

Design constraints (from the sprint brief)
-----------------------------------------
• The form is generic and extensible (``ClarificationQuestionType`` enum).
• Every question may carry ``choices`` (predefined actions) — the frontend
  decides how to render them; the backend only returns the schema.
• ``secret`` fields MUST never be echoed, logged, or stored in plaintext in
  chat history.  ``ClarificationFormSubmission.redacted()`` returns a copy with
  secret values stripped to metadata only.
• Validation schema is returned by the backend; the frontend performs client
  validation and the backend performs *authoritative* validation via
  ``ClarificationForm.validate_submission``.
• Mapping from the existing ``ClarificationQuestion`` (produced by the
  Adaptive Clarification Engine) to the form schema is a pure adapter
  (``ClarificationForm.from_questions``) — it does NOT rewrite the engine.

Everything here is additive; the legacy free-text clarification path is
preserved (see ``services.agent_service._apply_clarification_answer_to_spec``
which prefers a structured submission and falls back to regex parsing).
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supported input types (generic, extensible)
# ---------------------------------------------------------------------------


class ClarificationQuestionType(str, Enum):
    """Generic input types the renderer may present.

    The frontend maps each to an appropriate control.  Adding a new type is a
    matter of extending this enum + the renderer — no backend logic change.
    """

    TEXT = "text"
    TEXTAREA = "textarea"
    PASSWORD = "password"
    SECRET = "secret"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    PATH = "path"
    DIRECTORY = "directory"
    URL = "url"
    DOMAIN = "domain"
    PORT = "port"
    EMAIL = "email"
    SSH_KEY = "ssh_key"
    API_KEY = "api_key"
    ENVIRONMENT = "environment"


# ---------------------------------------------------------------------------
# Per-question building blocks
# ---------------------------------------------------------------------------


class ClarificationChoice(BaseModel):
    """A predefined action / option for a question.

    The backend only returns the schema.  The frontend decides whether to
    render ``choices`` as radio buttons, a dropdown, quick-action chips, etc.
    """

    id: str = ""  # stable id (defaults to value when empty)
    label: str
    value: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = self.value


class ClarificationValidation(BaseModel):
    """Declarative validation rules returned to the frontend.

    The frontend uses these for client-side validation; the backend re-runs
    them authoritatively (``ClarificationForm.validate_submission``).
    """

    required: bool = False
    regex: str | None = None  # raw pattern string (generic, no project logic)
    pattern_description: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None  # numeric lower bound
    max: float | None = None  # numeric upper bound
    allow_multi: bool = False  # multi_select membership


class ClarificationFormQuestion(BaseModel):
    """A single, fully-specified question for the dynamic form."""

    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    required_field: str = ""  # which spec field this clarifies (backend key)
    title: str = ""  # human-facing question text
    description: str = ""  # help / context
    placeholder: str = ""  # input placeholder
    example: str = ""  # example value
    required: bool = False  # blocks continuation when True
    secret: bool = False  # must never be echoed / logged / stored in plaintext
    type: ClarificationQuestionType = ClarificationQuestionType.TEXT
    default: Any = None
    choices: list[ClarificationChoice] = Field(default_factory=list)
    validation: ClarificationValidation = Field(default_factory=ClarificationValidation)
    depends_on: str | None = None  # question id this depends on
    visible_if: str | None = None  # conditional-visibility expression (renderer hint)
    metadata: dict[str, Any] = Field(default_factory=dict)  # non-secret hints


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


class ClarificationForm(BaseModel):
    """A complete structured clarification form.

    Produced by the backend.  The frontend renders ``title``, ``description``,
    each ``question`` (input, choices, validation, placeholder, examples) and
    submits a single ``ClarificationFormSubmission`` back.
    """

    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    title: str = "Clarification Required"
    description: str = "Please answer the questions below to continue."
    questions: list[ClarificationFormQuestion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Adapter: ClarificationQuestion -> ClarificationForm (composition, not rewrite)
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    def from_questions(
        cls,
        questions: list[Any],
        *,
        title: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ClarificationForm":
        """Build a generic form from a list of ``ClarificationQuestion``.

        Pure adapter — reuses the engine's output without altering it.  Type /
        secret derivation is keyword-based and generic (no project hard-coding).
        """
        form_questions: list[ClarificationFormQuestion] = []
        for q in questions:
            # Accept either a ClarificationQuestion or a plain dict.
            if isinstance(q, dict):
                d = q
            else:
                d = q.model_dump(mode="json") if hasattr(q, "model_dump") else {}

            qid = str(d.get("question_id") or "")
            if not qid:
                qid = __import__("uuid").uuid4().hex[:12]

            text = (d.get("question") or "").strip()
            reason = (d.get("reason") or "").strip()
            required_field = (d.get("required_field") or "").strip()
            blocking = bool(d.get("blocking", False))
            default = d.get("default_value", None)
            raw_options = d.get("options") or []
            validation_rule = (d.get("validation_rule") or "").strip()

            secret = _derive_secret(text, required_field)
            qtype = _derive_type(text, required_field, raw_options, secret)
            choices = [
                ClarificationChoice(label=str(o), value=str(o)) for o in raw_options
            ]

            validation = ClarificationValidation(required=blocking)
            if validation_rule:
                validation.regex = validation_rule
                validation.pattern_description = validation_rule
            # Generic type-specific bounds (no project knowledge).
            if qtype in (ClarificationQuestionType.NUMBER, ClarificationQuestionType.PORT):
                if qtype is ClarificationQuestionType.PORT:
                    validation.min = 1.0
                    validation.max = 65535.0
                # Integer-only for port.
                if qtype is ClarificationQuestionType.PORT:
                    validation.regex = r"^([1-9]\d{0,4})$"
                    validation.pattern_description = "Port number 1-65535"
                elif qtype is ClarificationQuestionType.NUMBER:
                    validation.regex = r"^-?\d+(\.\d+)?$"
                    validation.pattern_description = "Numeric value"
            elif qtype is ClarificationQuestionType.EMAIL:
                validation.regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
                validation.pattern_description = "Valid email address"
            elif qtype is ClarificationQuestionType.URL:
                validation.regex = r"^https?://\S+$"
                validation.pattern_description = "Valid http(s) URL"
            elif qtype is ClarificationQuestionType.DOMAIN:
                validation.regex = r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$"
                validation.pattern_description = "Valid domain name"

            form_q = ClarificationFormQuestion(
                id=qid,
                required_field=required_field,
                title=text or (required_field or "Clarification"),
                description=reason,
                placeholder="",
                example="",
                required=blocking,
                secret=secret,
                type=qtype,
                default=default,
                choices=choices,
                validation=validation,
                metadata={
                    "question_type": d.get("question_type"),
                    "priority": d.get("priority"),
                },
            )
            form_questions.append(form_q)

        return cls(
            title=title or "Clarification Required",
            description=description or "Please answer the questions below to continue.",
            questions=form_questions,
            metadata=metadata or {},
        )

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Authoritative validation (backend-side)
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    def validate_submission(self, submission: "ClarificationFormSubmission") -> list[dict[str, str]]:
        """Authoritatively validate a submission against this form's schema.

        Returns a list of errors ``{"question_id", "required_field", "error"}``.
        An empty list means the submission is valid.  Secret values are NEVER
        included in error messages.
        """
        by_id = {q.id: q for q in self.questions}
        by_field = {q.required_field: q for q in self.questions if q.required_field}

        errors: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        for ans in submission.answers:
            q = by_id.get(ans.question_id) or by_field.get(ans.required_field)
            if q is None:
                errors.append({
                    "question_id": ans.question_id,
                    "required_field": ans.required_field,
                    "error": "Unknown question",
                })
                continue
            seen_ids.add(q.id)
            self._validate_answer(q, ans, errors)

        # Required questions that were not answered at all.
        for q in self.questions:
            if q.required and q.id not in seen_ids:
                errors.append({
                    "question_id": q.id,
                    "required_field": q.required_field,
                    "error": "Required question not answered",
                })
        return errors

    @staticmethod
    def _validate_answer(
        q: ClarificationFormQuestion,
        ans: "ClarificationFormAnswer",
        errors: list[dict[str, str]],
    ) -> None:
        value = ans.value
        selected = ans.selected_choice
        has_value = value is not None and str(value).strip() != ""
        has_choice = bool(selected)

        if q.required and not has_value and not has_choice:
            errors.append({
                "question_id": q.id,
                "required_field": q.required_field,
                "error": "This question is required",
            })
            return

        # Choice membership (generic — no project logic).
        if q.choices and (has_value or has_choice):
            allowed = {c.value for c in q.choices}
            provided = selected if has_choice else str(value)
            if provided not in allowed:
                errors.append({
                    "question_id": q.id,
                    "required_field": q.required_field,
                    "error": "Value is not one of the allowed choices",
                })

        if not has_value:
            return  # a choice-only answer is accepted

        sval = str(value)
        # Type + regex validation (generic).
        v = q.validation
        if v.regex:
            try:
                if not re.search(v.regex, sval):
                    errors.append({
                        "question_id": q.id,
                        "required_field": q.required_field,
                        "error": v.pattern_description or "Value does not match the required format",
                    })
            except re.error:
                logger.warning("[clarification-form] invalid regex in validation schema: %r", v.regex)
        if v.min_length is not None and len(sval) < v.min_length:
            errors.append({
                "question_id": q.id,
                "required_field": q.required_field,
                "error": f"Minimum length is {v.min_length}",
            })
        if v.max_length is not None and len(sval) > v.max_length:
            errors.append({
                "question_id": q.id,
                "required_field": q.required_field,
                "error": f"Maximum length is {v.max_length}",
            })
        if q.type in (ClarificationQuestionType.NUMBER, ClarificationQuestionType.PORT):
            try:
                num = float(sval)
            except ValueError:
                errors.append({
                    "question_id": q.id,
                    "required_field": q.required_field,
                    "error": "Must be a number",
                })
                return
            if v.min is not None and num < v.min:
                errors.append({
                    "question_id": q.id,
                    "required_field": q.required_field,
                    "error": f"Minimum value is {v.min}",
                })
            if v.max is not None and num > v.max:
                errors.append({
                    "question_id": q.id,
                    "required_field": q.required_field,
                    "error": f"Maximum value is {v.max}",
                })
        if q.type is ClarificationQuestionType.BOOLEAN and sval.lower() not in {
            "true", "false", "0", "1", "yes", "no",
        }:
            errors.append({
                "question_id": q.id,
                "required_field": q.required_field,
                "error": "Must be a boolean",
            })


# ---------------------------------------------------------------------------
# Submission (frontend -> backend)
# ---------------------------------------------------------------------------


class ClarificationFormAnswer(BaseModel):
    """A single answered question in a submission."""

    question_id: str = ""
    required_field: str = ""
    value: Any = None
    selected_choice: str | None = None  # when the user picked a predefined choice


class ClarificationFormSubmission(BaseModel):
    """The single structured payload the frontend submits (one submit)."""

    clarification_id: str = ""  # echoes ClarificationForm.id
    answers: list[ClarificationFormAnswer] = Field(default_factory=list)

    def redacted(self) -> "ClarificationFormSubmission":
        """Return a copy with secret values stripped to metadata only.

        Secret answer values are replaced with ``None``; the ``selected_choice``
        (a non-secret label such as "I will set it in ENV") is preserved so the
        pipeline still knows the user's intent.  Use this copy for any logging
        or for storage in user-visible chat history.
        """
        safe_answers: list[ClarificationFormAnswer] = []
        for ans in self.answers:
            # The renderer labels secret answers; we cannot know secret-ness
            # without the form schema, so callers should redact with the form.
            safe_answers.append(ClarificationFormAnswer(
                question_id=ans.question_id,
                required_field=ans.required_field,
                value=None,
                selected_choice=ans.selected_choice,
            ))
        return ClarificationFormSubmission(
            clarification_id=self.clarification_id,
            answers=safe_answers,
        )

    def redacted_with_form(self, form: ClarificationForm) -> "ClarificationFormSubmission":
        """Redact secret answers using the form schema for accurate secret flags."""
        secret_ids = {q.id for q in form.questions if q.secret}
        secret_fields = {q.required_field for q in form.questions if q.secret}
        safe: list[ClarificationFormAnswer] = []
        for ans in self.answers:
            is_secret = ans.question_id in secret_ids or ans.required_field in secret_fields
            safe.append(ClarificationFormAnswer(
                question_id=ans.question_id,
                required_field=ans.required_field,
                value=None if is_secret else ans.value,
                selected_choice=ans.selected_choice,
            ))
        return ClarificationFormSubmission(clarification_id=self.clarification_id, answers=safe)


# ---------------------------------------------------------------------------
# Generic derivation helpers (NO project-type / Telegram hard-coding)
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|private[_-]?key|credential|"
    r"ssh[_-]?key|access[_-]?key|client[_-]?secret|webhook[_-]?secret)",
    re.IGNORECASE,
)

_TYPE_HINTS = [
    (ClarificationQuestionType.PORT, re.compile(r"(^|[^a-z])(port|tcp[_-]?port|udp[_-]?port)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.EMAIL, re.compile(r"(^|[^a-z])(email|e[_-]?mail|mail)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.URL, re.compile(r"(^|[^a-z])(url|endpoint|webhook|callback[_-]?url)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.DOMAIN, re.compile(r"(^|[^a-z])(domain|hostname|host[_-]?name)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.PATH, re.compile(r"(^|[^a-z])(path|file[_-]?path|config[_-]?path)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.DIRECTORY, re.compile(r"(^|[^a-z])(directory|dir|folder|work[_-]?dir)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.ENVIRONMENT, re.compile(r"(^|[^a-z])(env|environment|env[_-]?var)([^a-z]|$)", re.IGNORECASE)),
    (ClarificationQuestionType.SSH_KEY, re.compile(r"ssh[_-]?key", re.IGNORECASE)),
    (ClarificationQuestionType.API_KEY, re.compile(r"api[_-]?key|access[_-]?key|client[_-]?secret", re.IGNORECASE)),
]


def _derive_secret(text: str, required_field: str) -> bool:
    """Generic secret detection via keyword heuristics (no project logic)."""
    return bool(_SECRET_RE.search(f"{required_field} {text}"))


def _derive_type(
    text: str,
    required_field: str,
    options: list[Any],
    secret: bool,
) -> ClarificationQuestionType:
    """Generic type derivation (no project hard-coding)."""
    if secret:
        hay = f"{required_field} {text}".lower()
        if "ssh" in hay:
            return ClarificationQuestionType.SSH_KEY
        if "api" in hay or "key" in hay:
            return ClarificationQuestionType.API_KEY
        return ClarificationQuestionType.SECRET

    if options:
        return ClarificationQuestionType.SINGLE_SELECT

    blob = f"{required_field} {text}"
    for qtype, pattern in _TYPE_HINTS:
        if pattern.search(blob):
            return qtype

    if re.search(r"\b(yes|no|enable|disable|use|deploy|should|whether)\b", blob, re.IGNORECASE):
        return ClarificationQuestionType.BOOLEAN

    return ClarificationQuestionType.TEXT
