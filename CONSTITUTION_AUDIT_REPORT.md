# ThinkSync Constitution Audit Report

**Date:** 2026-06-01
**Status:** PROPOSAL ONLY — No implementation changes made

---

## 1. Analysis Methodology

- Read `backend/agents/constitution.py` (181 lines)
- Read `backend/tests/test_constitution.py` (56 lines, 9 tests, 5 currently failing)
- Traced usage across `executor.py`, `agent_service.py`, `agent_llm.py`
- Compared against modern agent best practices (2025-2026)

---

## Section A: Current Constitution Strengths

### A.1 Existing Rule Categories (8 categories, 10 exception types)

| Category | Rule | Implementation | Enforcement |
|----------|------|---------------|-------------|
| **Identity** | 6 Cardinal Rules | `get_core_identity()` | Prompt injection |
| **Objective Drift** | Never drift from objective | `check_objective()` | ⚠️ Weak (only checks non-empty) |
| **Command Safety** | Dangerous command patterns | `check_dangerous_commands()` | 8 regex patterns |
| **Tool Discipline** | Only whitelisted tools | `check_tool_discipline()` | String compare |
| **Success Contract** | Deployment must verify | `check_success_contract()` | Dict key check |
| **Patch Targeting** | Patch target must be in context | `check_patch_target()` | List membership |
| **Runtime State** | Prevent redundant operations | `check_runtime_state()` | ❌ No-op placeholder |
| **Workspace Safety** | Workspace lock concurrency | `WorkspaceBusyError` | Redis lock (external) |

### A.2 Prompt Architecture (8 modes)
- chat, code, patch, planner, debug, execution, evaluation, revision
- Each mode inherits the 6 Cardinal Rules
- Well-structured separation of concerns

### A.3 What Works Well
1. **Exception taxonomy is clear** — 10 distinct exception types with semantic names
2. **Dangerous command patterns** — `rm -rf`, `dd`, `userdel`, `iptables`, `reboot` are covered
3. **Prompt engineering** — Cardinal Rules 1-6 are strong identity anchors
4. **Mode separation** — 8 execution modes prevent role confusion
5. **Patch discipline** — Rule 4: "Never overwrite full files unless explicitly requested"
6. **Failure transparency** — Rule 6: "Fail explicitly instead of guessing"

---

## Section B: Current Constitution Weaknesses

### B.1 Currently Failing Tests (5/9 = 55% failure rate)

| Test | Reason | Constitution Gap |
|------|--------|-----------------|
| `test_fake_localhost_url` | `check_runtime_state()` is a no-op | No URL validation |
| `test_kill_without_confirmation` | Parameter name mismatch (`confirmed` vs `confirmation`) | `kill` not in dangerous patterns |
| `test_rm_without_confirmation` | Parameter name mismatch (`confirmed` vs `confirmation`) | Test expects `confirmation` param |
| `test_objective_mismatch` | `check_objective()` only checks non-empty | No drift detection |
| `test_stale_patch_target` | Method renamed `check_patch_discipline` → `check_patch_target` | Signature mismatch |

### B.2 Missing Rule Categories

| Missing Category | Risk | Current Gap |
|-----------------|------|-------------|
| **Evidence-based success** | Fake success claims | `check_success_contract` only checks `{"success": True}` — LLM can set this arbitrarily |
| **Validation before reporting** | Unverified completion | No validation gate between execution and success |
| **Patch vs rewrite preference** | Excessive file rewrites | Cardinal Rule 4 exists but no enforcement mechanism |
| **Uncertainty reporting** | Overconfident failures | No rule requiring "I don't know" reporting |
| **Workspace isolation** | Cross-job contamination | `WorkspaceBusyError` is external (Redis), not constitutional |
| **Idempotency** | Double execution | No rule that steps must be idempotent |
| **Evidence preservation** | Lost debugging context | No rule requiring log/artifact preservation |
| **Tool output validation** | Blind execution | No rule that tool output must be parsed before proceeding |
| **Hardcoded assumptions** | Brittle configs | No rule against hardcoded ports/URLs |
| **Self-confirmation bias** | LLM hallucinates success | No rule against the LLM evaluating its own output |

### B.3 Weakly Defined Rules

| Rule | Current Definition | Weakness |
|------|-------------------|----------|
| `check_objective()` | "if not original or not current: raise" | No semantic drift detection. "Deploy app" vs "Launch app" passes. |
| `check_runtime_state()` | `pass` | Completely unimplemented. No-op since creation. |
| `check_success_contract()` | `if not results.get("success")` | LLM generates the `results` dict. Circular trust. |
| `check_dangerous_commands()` | 8 regex patterns | `kill -9` not covered. `sudo` not covered. `wget | sh` not covered. `curl | bash` not covered. |
| Cardinal Rule 2 | "Never drift from objective" | No enforcement mechanism. |
| Cardinal Rule 5 | "Prefer deterministic structured outputs" | No validation of output structure. |

### B.4 Rules That May Cause Fake Success Behavior

1. **`check_success_contract` is circular**
   - The LLM generates both the execution result AND the verification dict
   - `verification_results.get("success")` is set by the same agent being verified
   - **Risk: HIGH** — Agent can declare its own success

2. **No independent validation requirement**
   - `evaluation` mode says "Judge based on validation results" but validation is internal
   - No external probe (HTTP request, port check, file existence) is constitutionally required
   - **Risk: HIGH** — Agent reports success without external evidence

3. **No evidence preservation rule**
   - Agent can claim success and discard logs that contradict it
   - **Risk: MEDIUM** — Audit trail is incomplete

### B.5 Rules That May Cause Excessive File Rewrites

1. **Cardinal Rule 4 is prompt-only**
   - "Never overwrite full files unless explicitly requested" — in prompt text
   - No code enforcement: `executor.py` does not check rewrite vs patch
   - `guardrails.py` has `validate_patched_files` but it's not called constitutionally
   - **Risk: MEDIUM** — Agent can and does rewrite full files

2. **No patch size limit**
   - `patch` mode says "minimal, focused patches" but no max_lines or max_context enforcement
   - **Risk: LOW** — Could generate large patches

### B.6 Rules That May Cause Workspace Safety Issues

1. **`WorkspaceBusyError` is external, not constitutional**
   - Lock is managed by Redis, not by the ConstitutionEngine
   - If Redis fails, lock is bypassed
   - **Risk: MEDIUM** — Two jobs could run on same workspace

2. **`check_dangerous_commands` is regex-only**
   - `sudo rm -rf` — `sudo` prefix bypasses the `rm -rf` pattern
   - `bash -c "rm -rf /"` — quoted command bypasses
   - `find / -name "*.log" -delete` — no pattern covers `-delete`
   - `docker system prune -f` — not covered
   - **Risk: HIGH** — Many destructive commands pass

3. **No workspace path validation**
   - `check_patch_target` checks context file list but not path traversal
   - `../../../etc/passwd` could be in context
   - **Risk: LOW** — Context engine limits this

4. **No resource limit rules**
   - No rule against `fork bombs`, `infinite loops`, `disk filling`
   - **Risk: MEDIUM** — Agent could exhaust workspace resources

### B.7 Redundant Rules

| Rule | Redundancy | Notes |
|------|-----------|-------|
| `check_success_contract` | Redundant with `evaluation` mode | Both judge success; neither has external validation |
| `StaleWorkspaceContextError` | Not used in execution path | Defined but never raised in `executor.py` |
| `StepRetryExhaustedError` | Handled by executor loop | Exception defined but retry logic is in executor, not constitution |

### B.8 Conflicting Rules

| Conflict | Rule A | Rule B | Resolution |
|----------|--------|--------|------------|
| **Patch vs Code** | Cardinal Rule 4: "Never overwrite full files" | `code` mode: "Output clean, production-grade code" | Code mode implies full files; patch mode implies partial. No arbitrator. |
| **Fail vs Continue** | Cardinal Rule 6: "Fail explicitly" | `evaluation` mode: "decisions: continue, retry, or abort" | Retry is encouraged; but constitution says "fail explicitly". When to retry vs abort? |
| **Determinism vs LLM** | Cardinal Rule 5: "Prefer deterministic outputs" | All modes use LLM for reasoning | LLM is inherently non-deterministic. No rule for how to handle LLM hallucination. |

---

## Section C: Recommended New Rules

### C.1 Evidence-Based Success Rule (HIGH PRIORITY)

```
RULE: "Never claim success without independent verification."

ENFORCEMENT:
  - check_success_contract() must require at least ONE non-LLM signal:
    - HTTP health check (for web apps)
    - Process status check (for services)
    - File existence check (for artifacts)
    - Command exit code (for scripts)
  - verification_results must contain:
    - "verification_method": "http_probe" | "process_check" | "file_check" | "exit_code"
    - "verification_evidence": raw probe output (not LLM-generated)
  - LLM-generated "success": true alone is insufficient

RATIONALE: Prevents fake success. Addresses the circular trust problem.
```

### C.2 Validation Before Completion Rule (HIGH PRIORITY)

```
RULE: "Validate before reporting. Always validate before reporting completion."

ENFORCEMENT:
  - Before final status=COMPLETED, executor must:
    1. Run a validation step (external probe)
    2. Store validation output in job_steps
    3. Only then mark completed
  - If validation fails, status must be FAILED, not COMPLETED

RATIONALE: Prevents "deploy succeeded but app is down" scenarios.
```

### C.3 Patch Over Rewrite Rule (MEDIUM PRIORITY)

```
RULE: "Prefer patch edits over full rewrites. If a file exists, patch it."

ENFORCEMENT:
  - Before writing a file, check if it exists
  - If it exists and user objective is "modify", use patch not write
  - Full rewrite only if:
    - file does not exist, OR
    - user explicitly says "rewrite", OR
    - patch application failed 3 times
  - Track rewrite_count per job; warn if > 3

RATIONALE: Reduces file rewrite volume. Prevents accidental overwrites.
```

### C.4 Uncertainty Reporting Rule (MEDIUM PRIORITY)

```
RULE: "Report uncertainty explicitly. 'I don't know' is better than a guess."

ENFORCEMENT:
  - If confidence < 0.7, output must include:
    - "confidence": "low" | "medium" | "high"
    - "uncertainty_reason": "missing logs" | "ambiguous error" | "unknown state"
    - "suggested_action": "retry" | "diagnose" | "ask_user"
  - Never fabricate file contents, logs, or error messages

RATIONALE: Prevents hallucination. Builds user trust.
```

### C.5 Idempotency Rule (MEDIUM PRIORITY)

```
RULE: "Make steps idempotent. Re-running a step should be safe."

ENFORCEMENT:
  - Before executing a command, check if its effect is already present
  - "npm install" → check if node_modules exists
  - "git clone" → check if repo exists
  - "mkdir" → use "mkdir -p"
  - Track "already_done" in step result

RATIONALE: Enables safe retry. Prevents duplicate work.
```

### C.6 Tool Output Validation Rule (MEDIUM PRIORITY)

```
RULE: "Parse tool output before proceeding. Never assume a command succeeded."

ENFORCEMENT:
  - After execute_tool(), must:
    1. Check exit_code
    2. Parse stderr for error patterns
    3. Verify stdout contains expected content (if applicable)
    4. Only proceed if all checks pass
  - If exit_code != 0, stop and evaluate

RATIONALE: Prevents cascading failures. Catches silent failures.
```

### C.7 Dangerous Command Expansion Rule (MEDIUM PRIORITY)

```
RULE: "Expand dangerous command coverage."

ENFORCEMENT:
  Add to DANGEROUS_COMMAND_PATTERNS:
    - sudo (when combined with destructive commands)
    - kill -9
    - curl | sh, wget | bash (pipe-to-shell)
    - find ... -delete
    - docker system prune
    - mkfs.*
    - > /dev/sd*

  Add check: "Does this command pipe to a shell interpreter?"

RATIONALE: Closes safety gaps. Many destructive patterns bypass current regex.
```

### C.8 Workspace Isolation Rule (LOW PRIORITY)

```
RULE: "One workspace, one job at a time."

ENFORCEMENT:
  - Move workspace lock from Redis-only to ConstitutionEngine
  - check_workspace_lock() as a constitutional rule
  - If lock fails, raise WorkspaceBusyError (already defined)

RATIONALE: Makes isolation a constitutional guarantee, not an infrastructure detail.
```

---

## Section D: Rules That Should NOT Be Added

| Rule | Reason |
|------|--------|
| "Always use specific tool X" | Too rigid. Tool selection should be context-dependent. |
| "Never use LLM for reasoning" | LLM is the core engine. Cannot eliminate it. |
| "Always ask user before any action" | Would make the agent non-autonomous. Breaks product value. |
| "Never retry failed steps" | Retries are necessary for transient failures. |
| "Always deploy to production" | User may want staging. Too prescriptive. |
| "Maximum 3 steps per plan" | Too arbitrary. Some tasks need 8 steps. |
| "Never use sudo" | Sometimes necessary. Covered by dangerous command check. |
| "Always verify via HTTPS" | Not all apps use HTTPS. localhost testing is valid. |

---

## Section E: Final Proposed Constitution Additions

### E.1 Add to `get_core_identity()` (Cardinal Rules)

```
7. Never claim success without independent verification
8. Prefer patch edits over full rewrites
9. Validate before reporting completion
10. Report uncertainty explicitly — 'I don't know' is better than a guess
```

### E.2 Add to `check_runtime_state()` (Implement the placeholder)

```python
def check_runtime_state(self, command: str) -> None:
    """Check for runtime state violations."""
    # Hardcoded assumptions
    if "localhost" in command and "curl" in command:
        raise RuntimeStateViolationError("Localhost URLs are not externally verifiable.")
    # Pipe-to-shell
    if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)", command):
        raise ConfirmationRequiredError("Pipe-to-shell commands require confirmation.")
    # Sudo with destructive
    if re.search(r"\bsudo\b.*\b(rm|dd|mkfs|halt|reboot)\b", command):
        raise ConfirmationRequiredError("Sudo with destructive commands requires confirmation.")
```

### E.3 Add to `check_success_contract()` (Evidence requirement)

```python
def check_success_contract(self, verification_results: dict) -> None:
    if not verification_results.get("success"):
        raise DeploymentNotVerifiedError("Deployment failed verification contract.")
    # NEW: Require independent verification
    method = verification_results.get("verification_method")
    if not method or method == "llm_assessment":
        raise DeploymentNotVerifiedError(
            "Success claims require non-LLM verification (http_probe, process_check, file_check, or exit_code)."
        )
    evidence = verification_results.get("verification_evidence")
    if not evidence:
        raise DeploymentNotVerifiedError("Success claim requires verification evidence.")
```

### E.4 Add to `check_dangerous_commands()` (Expanded patterns)

```python
# Add to DANGEROUS_COMMAND_PATTERNS:
    re.compile(r"\bkill\s+-9\b"),
    re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)"),
    re.compile(r"\bfind\b.*\b-delete\b"),
    re.compile(r"\bdocker\s+system\s+prune\b"),
    re.compile(r"\bmkfs\.\b"),
    re.compile(r"\b>\s*/dev/sd[a-z]\b"),
    re.compile(r"\bsudo\b.*\b(rm|dd|mkfs|halt|reboot)\b"),
```

### E.5 Add new method: `check_idempotency()`

```python
def check_idempotency(self, command: str) -> dict:
    """Check if a command is idempotent. Suggest idempotent alternative if not."""
    non_idempotent = [
        (re.compile(r"\b(git clone|npm install|pip install)\b"), "check if already present first"),
        (re.compile(r"\b(mkdir)\b(?!\s+-p)"), "use mkdir -p"),
    ]
    for pattern, suggestion in non_idempotent:
        if pattern.search(command):
            return {"idempotent": False, "suggestion": suggestion}
    return {"idempotent": True}
```

### E.6 Add new method: `check_rewrite_vs_patch()`

```python
def check_rewrite_vs_patch(self, file_exists: bool, user_objective: str, operation: str) -> None:
    """Enforce patch preference over full rewrite."""
    if file_exists and operation == "write" and "rewrite" not in user_objective.lower():
        raise ConfirmationRequiredError(
            f"File exists. Use patch instead of full write. "
            f"If you must overwrite, include 'rewrite' in the objective."
        )
```

### E.7 Fix test-compatibility issues

| Fix | Current | Should Be |
|-----|---------|------------|
| Parameter name | `confirmed` | `confirmation` (to match test) |
| Method name | `check_patch_target` | `check_patch_discipline` (or update test) |
| `check_objective` | Non-empty check | Add semantic drift detection |

---

## F. Test Recommendations

The current test suite has 5 failures out of 9 tests. Recommended fixes:

1. **Rename parameter** `confirmed` → `confirmation` in `check_dangerous_commands`
2. **Add method alias** `check_patch_discipline` = `check_patch_target` (or update test)
3. **Implement `check_runtime_state`** with localhost detection
4. **Add `kill` to dangerous patterns** (test expects it)
5. **Strengthen `check_objective`** with semantic comparison (or adjust test expectation)

---

## G. Summary Matrix

| Principle | Current | Proposed | Priority |
|-----------|---------|----------|----------|
| Never claim success without evidence | ❌ Weak | ✅ Strong | HIGH |
| Prefer patch over rewrite | ✅ Prompt only | ✅ Enforced | MEDIUM |
| Validate before completion | ❌ None | ✅ Required | HIGH |
| Preserve workspace isolation | ⚠️ External | ✅ Constitutional | LOW |
| Report uncertainty | ❌ None | ✅ Required | MEDIUM |
| Explain failures | ✅ Cardinal Rule 6 | ✅ Stays | — |
| Tool output validation | ❌ None | ✅ Required | MEDIUM |
| Dangerous command coverage | ⚠️ 8 patterns | ✅ 15+ patterns | HIGH |

---

**End of Audit Report**
