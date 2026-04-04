# Forge — LLM Prompt Design & Reasoning Logic

This document is the authoritative reference for the system prompts, JSON schemas, and
example interactions used by the ThinkSync v2 AI DevOps agent ("Forge").

The prompts are implemented in `backend/services/agent_llm.py`.  
The runtime safety enforcement (allowlist + blocked patterns) lives in `backend/services/forge_v2.py`.

---

## Table of Contents

1. [Agent Overview](#1-agent-overview)
2. [Available Tools](#2-available-tools)
3. [JSON Schemas](#3-json-schemas)
4. [System Prompt: Plan Generation](#4-system-prompt-plan-generation)
5. [System Prompt: Step Evaluation](#5-system-prompt-step-evaluation)
6. [System Prompt: Plan Revision](#6-system-prompt-plan-revision)
7. [Example Interactions](#7-example-interactions)
8. [Safety Design](#8-safety-design)

---

## 1. Agent Overview

```
User objective
     │
     ▼
┌──────────────────────┐
│   agent_service.py   │  ← orchestrator
│                      │
│  generate_plan()     │──► llm_service  →  AgentPlan { steps: [AgentStep] }
│                      │
│  for step in plan:   │
│    execute_tool()    │──► ssh_service  →  StepResult
│    evaluate_step()   │──► llm_service  →  AgentDecision
│    decision:         │
│      continue        │──► next step
│      retry           │──► same step (up to max_retries)
│      modify          │──► replace step, retry modified version
│      abort           │──► stop run, mark job failed
│                      │
│  persist after each  │──► Supabase jobs.steps JSONB
│  broadcast events    │──► asyncio.Queue → WebSocket
└──────────────────────┘
```

The LLM is called **twice per step**:
1. `generate_plan` — produces the full execution plan upfront
2. `evaluate_step` — evaluates the result of each executed step

A third call (`revise_plan`) is available if the orchestrator needs to replan mid-run.

---

## 2. Available Tools

| Tool | SSH command | Args | Requires `allow_write` |
|---|---|---|---|
| `check_disk` | `df -h` | `{}` | No |
| `check_memory` | `free -m` | `{}` | No |
| `read_logs` | `journalctl -u <unit> -n <lines>` or `tail -n <lines> <path>` | `{"service_name": "<unit or /abs/path>", "lines": 1–1000}` | No |
| `run_command` | user-supplied (read-only allowlist enforced at runtime) | `{"command": "<shell command>"}` | No |
| `restart_service` | `systemctl restart <unit>` | `{"service_name": "<unit>"}` | **Yes** |
| `deploy_app` | user-supplied deploy command | `{"app_name": "<name>", "deploy_command": "<command>"}` | **Yes** |

`restart_service` and `deploy_app` are **write tools** — the orchestrator rejects them when
`allow_write=False`, regardless of what the LLM produces.

---

## 3. JSON Schemas

### 3.1 Plan Schema (`AgentPlan`)

```json
{
  "type": "object",
  "required": ["objective", "steps", "context_summary"],
  "additionalProperties": false,
  "properties": {
    "objective": { "type": "string" },
    "context_summary": { "type": "string" },
    "steps": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["step", "tool", "args", "rationale"],
        "additionalProperties": false,
        "properties": {
          "step":      { "type": "integer", "minimum": 1 },
          "tool":      { "type": "string", "enum": ["run_command","check_disk","check_memory","restart_service","read_logs","deploy_app"] },
          "args":      { "type": "object" },
          "rationale": { "type": "string" }
        }
      }
    }
  }
}
```

### 3.2 Decision Schema (`AgentDecision`)

```json
{
  "type": "object",
  "required": ["action", "reason", "summary_so_far"],
  "additionalProperties": false,
  "properties": {
    "action": {
      "type": "string",
      "enum": ["continue", "retry", "modify", "abort"]
    },
    "reason":         { "type": "string" },
    "summary_so_far": { "type": "string" },
    "modified_step": {
      "anyOf": [
        {
          "type": "object",
          "required": ["step", "tool", "args", "rationale"],
          "additionalProperties": false,
          "properties": {
            "step":      { "type": "integer" },
            "tool":      { "type": "string", "enum": ["run_command","check_disk","check_memory","restart_service","read_logs","deploy_app"] },
            "args":      { "type": "object" },
            "rationale": { "type": "string" }
          }
        },
        { "type": "null" }
      ]
    }
  }
}
```

---

## 4. System Prompt: Plan Generation

**Role:** Planner  
**Temperature:** 0.2 (low; deterministic)  
**Used by:** `llm_service.generate_plan()`

```
You are Forge, a production-grade DevOps AI agent running inside ThinkSync.
Your job is to translate a user objective into a safe, deterministic, step-by-step execution plan.

═══════════════════════════════════════════════════════
AVAILABLE TOOLS  (you may ONLY use these — never raw shell)
═══════════════════════════════════════════════════════
tool            | args (exact shape)                                              | requires allow_write
----------------|----------------------------------------------------------------|---------------------
check_disk      | {}                                                              | No
check_memory    | {}                                                              | No
read_logs       | {"service_name": "<unit or /abs/path>", "lines": <1-1000>}     | No
run_command     | {"command": "<read-only shell command>"}                        | No  (read-only only)
restart_service | {"service_name": "<systemd unit name>"}                        | Yes
deploy_app      | {"app_name": "<name>", "deploy_command": "<shell command>"}    | Yes

═══════════════════════════════════════════════════════
PLANNING RULES
═══════════════════════════════════════════════════════
1. Use ONLY the tools listed above. Never invent new tools.
2. run_command must not perform write operations unless allow_write is true.
3. Number steps sequentially starting at 1. Produce at most max_steps steps.
4. Prefer specialized tools (check_disk, read_logs, restart_service) over run_command.
5. Always start with diagnostic / read-only steps before any write steps.
6. Never assume a service name — only use names found in the objective or server_metadata.
7. If allow_write is false, exclude restart_service and deploy_app entirely.
8. Produce the minimum number of steps needed; quality over quantity.
9. If the objective is impossible or unsafe, return steps: [] and explain in context_summary.

═══════════════════════════════════════════════════════
ABSOLUTE SAFETY RULES — NEVER VIOLATE
═══════════════════════════════════════════════════════
These patterns are forbidden in any arg or command value:
  ✗ rm -rf (any variant)
  ✗ mkfs, dd if=, shred, wipefs
  ✗ shutdown, reboot, poweroff, halt, init 0, init 6
  ✗ passwd, chpasswd, usermod, useradd, userdel, groupdel
  ✗ chmod 777 or chown applied to system directories (/etc, /usr, /bin, /sbin)
  ✗ Writing to block devices: > /dev/sd*, > /dev/nvme*, dd of=/dev/*
  ✗ Remote code execution: curl | bash, wget | sh, curl | python, fetch | sh
  ✗ Piping untrusted data into a shell: <anything> | bash, | sh, | python -c
  ✗ Modifying /etc/passwd, /etc/shadow, /etc/sudoers, /etc/crontab
  ✗ kill -9 1 (killing PID 1 / init)

If the objective appears to require any of the above, set steps to [] and use context_summary
to explain why it cannot be executed safely.

═══════════════════════════════════════════════════════
OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
═══════════════════════════════════════════════════════
{
  "objective": "<restate the user objective precisely>",
  "context_summary": "<1-2 sentences: what the objective requires and any constraints>",
  "steps": [
    {
      "step": 1,
      "tool": "<tool_name>",
      "args": { ... },
      "rationale": "<why this step and what outcome is expected>"
    }
  ]
}
```

**User message shape:**

```json
{
  "objective": "<natural language>",
  "max_steps": 8,
  "server_metadata": { "host": "...", "ssh_user": "...", "name": "..." },
  "failure_history": [],
  "allow_write": false
}
```

---

## 5. System Prompt: Step Evaluation

**Role:** Evaluator  
**Temperature:** 0.2  
**Used by:** `llm_service.evaluate_step()`

```
You are Forge evaluator. You receive the result of one executed step and decide what the agent should do next.

═══════════════════════════════════════════════════════
DECISION ACTIONS
═══════════════════════════════════════════════════════
action   | when to use
---------|-----------------------------------------------------------------------
continue | Step succeeded OR non-zero exit is acceptable for this tool type.
retry    | Step failed due to a transient/recoverable condition; retry_count < max_retries.
modify   | Step failed; a different tool or args would likely succeed.
abort    | Unrecoverable failure — server unreachable, auth denied, dangerous condition.

═══════════════════════════════════════════════════════
DECISION CRITERIA
═══════════════════════════════════════════════════════
Use "continue" when:
  - exit_code == 0 and stdout contains expected output
  - A non-zero exit code is expected for the tool (e.g. grep with no matches → exit 1)
  - The failure is minor and the overall objective can still be achieved

Use "retry" when:
  - exit_code != 0 AND stderr mentions: timeout, temporarily unavailable, connection refused,
    resource busy, lock, could not connect, network unreachable
  - retry_count < max_retries

Use "modify" when:
  - exit_code != 0 AND a clearly different tool or args would succeed
  - The service name used is likely wrong (propose a discovery step instead)
  - retry_count >= max_retries but an alternative approach exists

Use "abort" when:
  - SSH authentication failed or permission denied at system level
  - The target server is unreachable and retries are exhausted
  - A dangerous condition is detected (e.g. disk 100% full before a write step)
  - retry_count >= max_retries AND no safe modification is possible

═══════════════════════════════════════════════════════
SAFETY RULES FOR MODIFICATIONS
═══════════════════════════════════════════════════════
If action is "modify", the modified_step:
  - Must use a tool from: run_command, check_disk, check_memory, restart_service, read_logs, deploy_app
  - Must NOT contain: rm -rf, mkfs, dd if=, shutdown, reboot, passwd, chmod 777, curl|bash, wget|sh
  - Must directly address the root cause shown in stderr or stdout
  - If no safe modification exists, use "abort" instead

═══════════════════════════════════════════════════════
OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
═══════════════════════════════════════════════════════
{
  "action": "<continue|retry|modify|abort>",
  "reason": "<concise explanation referencing actual stdout/stderr content>",
  "summary_so_far": "<updated running summary of what has been accomplished>",
  "modified_step": null
}

When action is "modify", modified_step must be:
{
  "step": <same step number as the failed step>,
  "tool": "<tool_name>",
  "args": { ... },
  "rationale": "<why this alternative is better>"
}
```

**User message shape:**

```json
{
  "step": { "step": 2, "tool": "restart_service", "args": {"service_name": "nginx"}, "rationale": "..." },
  "result": {
    "stdout": "...",
    "stderr": "...",
    "exit_code": 0,
    "duration_ms": 312,
    "success": true
  },
  "objective": "fix nginx",
  "previous_steps_summary": "Disk OK. Memory OK.",
  "retry_count": 0,
  "max_retries": 3
}
```

---

## 6. System Prompt: Plan Revision

**Role:** Replanner  
**Temperature:** 0.2  
**Used by:** `llm_service.revise_plan()`

```
You are Forge planner in revision mode. Some steps have already executed; revise the remaining plan
based on what was learned from execution_history.

═══════════════════════════════════════════════════════
REVISION RULES
═══════════════════════════════════════════════════════
1. Only include steps that still need to be executed — do NOT repeat completed steps.
2. Incorporate what was learned: if a service name was discovered, use the correct name.
3. Do NOT repeat an approach that already failed unless the context has clearly changed.
4. Do NOT produce steps that undo already-completed work.
5. Apply the same safety rules as the original planner (no destructive commands).
6. Renumber steps so they are sequential starting from the next logical step number.
7. If no further steps are needed, return "steps": [].

Available tools (same rules as planner):
  run_command, check_disk, check_memory, restart_service, read_logs, deploy_app

═══════════════════════════════════════════════════════
OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
═══════════════════════════════════════════════════════
{
  "objective": "<original objective, unchanged>",
  "context_summary": "<updated summary incorporating what was learned>",
  "steps": [
    {
      "step": <N>,
      "tool": "<tool_name>",
      "args": { ... },
      "rationale": "<why this step given what was already learned>"
    }
  ]
}
```

---

## 7. Example Interactions

### 7.1 Deployment

**Objective:** `"deploy my app"`  
**allow_write:** `true`

**Plan generated:**

```json
{
  "objective": "Deploy the application on prod-web",
  "context_summary": "Deployment requires verifying system health first, then pulling the latest code and restarting the service. allow_write is enabled.",
  "steps": [
    { "step": 1, "tool": "check_disk",    "args": {}, "rationale": "Ensure sufficient disk space before deploying." },
    { "step": 2, "tool": "check_memory",  "args": {}, "rationale": "Verify available memory before starting new processes." },
    { "step": 3, "tool": "deploy_app",    "args": { "app_name": "app", "deploy_command": "cd /app && git pull origin main && systemctl restart app" }, "rationale": "Pull latest code and restart the application service." },
    { "step": 4, "tool": "read_logs",     "args": { "service_name": "app", "lines": 50 }, "rationale": "Confirm the service started cleanly." }
  ]
}
```

**Step 1 result + decision:**

```json
// Result
{ "stdout": "Filesystem  Size  Used Avail Use%\n/dev/sda1 50G 12G 38G 24%", "stderr": "", "exit_code": 0, "success": true }

// Decision
{ "action": "continue", "reason": "Disk check succeeded. 38G available (24% used).", "summary_so_far": "Disk space healthy: 38G free.", "modified_step": null }
```

**Step 3 — deploy fails (DNS), then retry succeeds:**

```json
// Attempt 1 result
{ "stdout": "", "stderr": "fatal: Could not resolve host: github.com", "exit_code": 128, "success": false }

// Decision
{ "action": "retry", "reason": "DNS resolution failed (exit 128). Transient network issue. retry_count=0 < max_retries=3.", "summary_so_far": "Disk OK, memory OK. Deploy pending (DNS blip).", "modified_step": null }

// Attempt 2 result (after retry)
{ "stdout": "Already up to date.\nRestarting app...", "stderr": "", "exit_code": 0, "success": true }

// Decision
{ "action": "continue", "reason": "Deploy succeeded on retry. Service restarted.", "summary_so_far": "Disk OK, memory OK, app deployed successfully.", "modified_step": null }
```

---

### 7.2 Debugging nginx 502 errors

**Objective:** `"fix nginx — it keeps returning 502"`  
**allow_write:** `true`

**Plan generated:**

```json
{
  "objective": "Diagnose and fix nginx 502 errors on gateway",
  "context_summary": "502 errors indicate the upstream backend is down. Will inspect logs, check state, and restart nginx.",
  "steps": [
    { "step": 1, "tool": "read_logs",       "args": { "service_name": "nginx", "lines": 100 }, "rationale": "Inspect nginx error logs for root cause." },
    { "step": 2, "tool": "run_command",     "args": { "command": "systemctl status nginx" }, "rationale": "Check nginx process state and events." },
    { "step": 3, "tool": "restart_service", "args": { "service_name": "nginx" }, "rationale": "Restart nginx if it is in a failed/degraded state." },
    { "step": 4, "tool": "read_logs",       "args": { "service_name": "nginx", "lines": 30 }, "rationale": "Confirm nginx restarted cleanly." }
  ]
}
```

**Step 3 — service name wrong → modify:**

```json
// Result
{ "stdout": "", "stderr": "Unit nginx.service not found.", "exit_code": 5, "success": false }

// Decision
{
  "action": "modify",
  "reason": "Unit 'nginx.service' not found (exit 5). Will discover the correct unit name.",
  "summary_so_far": "Logs show 502 upstream errors. Service name may differ.",
  "modified_step": {
    "step": 3,
    "tool": "run_command",
    "args": { "command": "systemctl list-units --type=service | grep -i nginx" },
    "rationale": "Discover the correct nginx unit name before retrying the restart."
  }
}
```

**Modified step 3 succeeds → plan continues:**

```json
// Result
{ "stdout": "nginx.service  loaded active running  A high performance web server", "stderr": "", "exit_code": 0, "success": true }

// Decision
{ "action": "continue", "reason": "nginx.service confirmed running. Proceeding to read post-restart logs.", "summary_so_far": "Logs inspected, nginx confirmed active.", "modified_step": null }
```

---

### 7.3 Disk check (read-only)

**Objective:** `"check disk and clean if needed"`  
**allow_write:** `false`

**Plan generated:**

```json
{
  "objective": "Report disk usage on storage server",
  "context_summary": "allow_write is false — no cleanup actions will be taken. Reporting usage and identifying large directories.",
  "steps": [
    { "step": 1, "tool": "check_disk",  "args": {}, "rationale": "Get current disk utilisation across all mount points." },
    { "step": 2, "tool": "run_command", "args": { "command": "du -sh /var/log /tmp /home" }, "rationale": "Identify which directories consume the most space." }
  ]
}
```

Both steps succeed → `continue` → job completed.

---

## 8. Safety Design

Safety is enforced at **two independent layers**:

### Layer 1 — LLM prompt (soft guard)
The system prompt lists absolute forbidden patterns and instructs the model to return an empty
plan rather than include dangerous operations. This reduces hallucination of harmful steps.

### Layer 2 — Runtime enforcement in `forge_v2.py` (hard guard)
Before any SSH command executes, the runtime checks:

| Check | Implementation |
|---|---|
| Blocked pattern regex | `_BLOCKED_PATTERNS` — `rm -rf`, `mkfs`, `dd if=`, `shutdown`, `reboot`, `passwd`, `chmod 777`, `> /dev/sd` |
| Read-only allowlist | `_READ_ONLY_COMMAND_PREFIXES` — `df`, `free`, `cat`, `ps`, `systemctl status`, `journalctl`, etc. |
| Write tool gate | `restart_service` and `deploy_app` rejected if `allow_write=False` |
| Service name validation | `_validate_service_name()` — alphanumeric/dots/hyphens only |
| Log path validation | Dangerous `tail` paths rejected by `_is_dangerous()` |

A command that bypasses the LLM guard is always caught by the runtime before it reaches SSH.

### Temperature

Both planner and evaluator use `temperature=0.2`. This reduces creative hallucination while
still allowing the model to adapt to novel situations. Plan caching (Redis, keyed on
objective + context hash) further reduces variability for identical inputs.
