# OptimisticLockError NameError — ROOT-CAUSE + FIX REPORT

**Rules honored:** no Git; no refactor; no architecture change; no rename; no wrapper; no SQL/migration/
frontend/Planner/Decision-Engine/Routing change. Only the verified missing-import NameError was fixed.

---

## A. ROOT CAUSE

**A. Class exists but import is missing.**

`OptimisticLockError` is defined once, at `services/conversation_reliability.py:53`
(`class OptimisticLockError(Exception)`). It is used (raised and caught) at
`services/approval_engine.py:313` (raise) and `:317` (except), but `approval_engine.py` **never imports it**.
Therefore at runtime, when the executor's `on_step_start` hook invokes the approval-persist path that hits
`raise OptimisticLockError(...)` / `except OptimisticLockError:`, Python raises
`NameError: name 'OptimisticLockError' is not defined`.

This is NOT a rename, NOT a deletion, NOT a circular import, NOT dead code — the symbol is simply absent
from `approval_engine.py`'s namespace.

## B. EVIDENCE

- **AST scan (authoritative):** `OptimisticLockError` ClassDef exists only at
  `conversation_reliability.py:53`. Usages: `approval_engine.py:313,317` and
  `conversation_reliability.py:244,271` (local to the defining module). `approval_engine.py` has no
  `ImportFrom` bringing `OptimisticLockError` into scope → the two `ast.Name` loads at 313/317 are unresolved.
- **grep:** `approval_engine.py` contains `raise OptimisticLockError(` (313) and `except OptimisticLockError:`
  (317) but no import of it. `conversation_reliability.py` defines it (53) and uses it locally (244,271).
- **Circular-import check:** `conversation_reliability.py:279` does
  `from services.approval_engine import ApprovalEngine` **inside a function body** (lazy local import), NOT at
  module level. So adding a module-level `from services.conversation_reliability import OptimisticLockError`
  to `approval_engine.py` does NOT create a cycle. Verified safe.
- **Definition signature:** `OptimisticLockError(expected: int, actual: int)` (conversation_reliability.py:56)
  — matches the call site `raise OptimisticLockError(expected=..., actual=...)` at approval_engine.py:313-316.

## C. FILES MODIFIED

- `services/approval_engine.py` — added one import line after `from models.job import JobStatus`:
  ```python
  from services.conversation_reliability import OptimisticLockError
  ```
  No other change. Behavior unchanged.

## D. VALIDATION

- **AST:** both `approval_engine.py` usages (314, 318 post-edit line numbers) now report
  `imported_in_module=True`. The two `conversation_reliability.py` usages are in the defining module
  (no import needed). Zero unresolved references.
- **grep:** confirms the import now exists in `approval_engine.py` alongside the two usages.
- **py_compile:** `services/approval_engine.py` compiles (exit 0).
- **import smoke:** `import services.approval_engine` succeeds; `ae.OptimisticLockError.__name__` resolves to
  `'OptimisticLockError'` — proving the name is defined in the module and there is NO circular import.
- **Regression:** targeted suite `211 passed, 1 skipped, 0 failures` (identical to pre-fix baseline).

## E. REMAINING RUNTIME ISSUES

- None from this defect. The `on_step_start` hook NameError is resolved.
- Unrelated, out-of-scope issues previously identified (not addressed here, per task scope):
  - WebSocket send-after-close lifecycle in `routers/ws.py`.
  - Pre-existing test failures `test_endpoints.py` / `tests/test_google_oauth.py`.

## F. NOT VERIFIED

- Whether the LIVE process was rebuilt/redeployed with this import — NOT VERIFIED (no build/Git access);
  the static AST + import-smoke proof confirms the NameError is eliminated at the source level.
- Whether any other module references `OptimisticLockError` without importing it — AST confirms only
  `approval_engine.py` and its defining module use it; `approval_engine.py` is now fixed. No other references
  exist in the repository.

---

## SUCCESS CRITERIA CHECK

- [x] No Git used.
- [x] No refactor.
- [x] No architecture changes.
- [x] Only verified NameError fixed (missing import added).
- [x] Zero unresolved `OptimisticLockError` references (AST confirms both usages now import-resolved).

**Confidence: HIGH (production-grade).**
