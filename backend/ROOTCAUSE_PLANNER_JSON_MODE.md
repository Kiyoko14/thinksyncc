# STRICT ROOT-CAUSE INVESTIGATION — PLANNER LLM FAILURE (code 20024, "Json mode is not supported")

**Method:** read-only source inspection. No fix, no patch, no config/env change, no git, no test/db/frontend
touch. Repository files are the only source of truth.
**Verdict (code-proven):** **E + C + A combined** — a **legacy OpenAI assumption** (JSON mode unconditionally
forced) compounded by **missing provider/model capability detection** and a **planner model-resolution
configuration gap**. The provider abstraction correctly routes to SiliconFlow, but the planner hardcodes
`response_format={"type":"json_object"}` for *every* model, and the runtime model (`tencent/Hy3`, a
SiliconFlow model) does not support JSON mode on that provider → SiliconFlow returns `400 code 20024`.

---

## 1. COMPLETE EXECUTION TRACE (planner path)

```
run_agent_pipeline (agent_service.py:1342)
  ↓ (server intent)
run_server_execution(...)                         (agent_service.py:2073)
  ↓
_execute_with_lock(...)                           (executor.py:352)
  ↓
build_plan(...)                                   (agent_service.py:1770 → services/planner.py)
  ↓
agent_llm.generate_plan(objective, context, ...)  (agent_llm.py:1772)
  ↓
raw = await _chat_json(messages, _PLAN_SCHEMA,
                       model = settings.OPENAI_MODEL_PLANNER or settings.OPENAI_MODEL)  (agent_llm.py:1824)
  ↓
_chat_json(...)                                    (agent_llm.py:1703)
  ↓
client.chat.completions.create(
    model = "tencent/Hy3",                         (resolved; see §2)
    messages = ...,
    response_format = {"type": "json_object"},     (agent_llm.py:1726 — HARDCODED, unconditional)
    temperature = 0.0,
)                                                  (agent_llm.py:1723-1728)
  ↓
client = _get_openai_client()  → AsyncOpenAI(base_url = settings.OPENAI_BASE_URL)  (agent_llm.py:1719 / 1639-1649)
  ↓
HTTP POST https://api.siliconflow.com/v1/chat/completions
  ↓
400  {"code": 20024, "message": "Json mode is not supported for this model."}
```
The error is raised by SiliconFlow because the request carries `response_format={"type":"json_object"}`
while the selected model (`tencent/Hy3`) does not support JSON mode on SiliconFlow.

---

## 2. CONFIGURATION RESOLUTION TRACE (which model is used)

**Step A — planner model selection (`agent_llm.py:1824`):**
```python
raw = await _chat_json(messages, _PLAN_SCHEMA,
                       model=settings.OPENAI_MODEL_PLANNER or settings.OPENAI_MODEL)
```
**Step B — `OPENAI_MODEL_PLANNER` value:**
- `core/config.py:139` → `OPENAI_MODEL_PLANNER: str | None = None` (default `None`).
- `.env` key inventory (read directly): the ONLY model key present is `OPENAI_MODEL`. There is **no**
  `OPENAI_MODEL_PLANNER`, `OPENAI_MODEL_CODE`, `OPENAI_MODEL_REASONING`, or `OPENAI_MODEL_EXECUTOR` in
  `.env`. (Verified by enumerating every non-comment key in `.env`.)
- ⇒ `settings.OPENAI_MODEL_PLANNER` resolves to `None`.

**Step C — `OPENAI_MODEL` value:**
- `core/config.py:136` → `OPENAI_MODEL: str = "gpt-4o-mini"` (default).
- `.env` → `OPENAI_MODEL = tencent/Hy3`. (Verified: the line `OPENAI_MODEL = tencent/Hy3` exists in `.env`.)
- Settings precedence (pydantic-settings) loads `.env` over the default ⇒ `OPENAI_MODEL = "tencent/Hy3"`.

**Result of resolution chain:**
```
OPENAI_MODEL_PLANNER (None)
  ↓ (or)
OPENAI_MODEL = "tencent/Hy3"   ← ACTUAL RUNTIME MODEL
```
So `generate_plan` calls SiliconFlow with model `tencent/Hy3` and `response_format=json_object`.

---

## 3. PROVIDER ABSTRACTION TRACE

- `core/config.py:135` → `OPENAI_BASE_URL: str = "https://api.siliconflow.com/v1"` (default).
- `.env` key inventory: there is **no** `OPENAI_BASE_URL` key in `.env` ⇒ the default is used.
- `_get_openai_client()` (`agent_llm.py:1639-1649`) builds `AsyncOpenAI(api_key=..., base_url=settings.OPENAI_BASE_URL)`
  → `https://api.siliconflow.com/v1`.
- The client is a generic OpenAI-compatible client pointed at SiliconFlow. There is **no** provider branch,
  no model-routing, and no capability dispatch anywhere in `_get_openai_client` or `generate_plan`.
- Conclusion: the provider abstraction is **correct** (it does reach SiliconFlow). It is NOT the bug. The
  bug is that the request it sends (JSON mode) is rejected by SiliconFlow for this model. The abstraction
  does not know or care about JSON-mode support — it blindly forwards `response_format`.

---

## 4. MODEL SELECTION TRACE

- Planner uses `OPENAI_MODEL_PLANNER or OPENAI_MODEL` (agent_llm.py:1824). Because `OPENAI_MODEL_PLANNER`
  is unset, it selects the single global `OPENAI_MODEL = tencent/Hy3`.
- Note: the repo *does* define dedicated model slots (`OPENAI_MODEL_PLANNER`, `OPENAI_MODEL_EXECUTOR`,
  `OPENAI_MODEL_CLASSIFIER`, `OPENAI_MODEL_DEBUG`, `OPENAI_MODEL_SUMMARY` in config.py:138-142), but only
  `classifier`/`debug`/`summary` have non-`OPENAI_MODEL` fallback callers; the planner caller (1824) also
  supports a dedicated slot but **it is left unconfigured in `.env`**. So model selection "works" but
  collapses to the one SiliconFlow model for every role.

---

## 5. CAPABILITY ANALYSIS

**5.1 Is JSON mode forced? WHERE:**
- `_chat_json` (`agent_llm.py:1703-1761`) is the single JSON-mode helper. Line **1726**:
  ```python
  response_format = {"type": "json_object"},
  ```
  This is a **literal, unconditional** argument in every `_chat_json` call. Not gated by model, provider,
  or any flag.
- Two *other* call sites also hardcode it: `agent_llm.py:196` (intent classifier) and `:1144`
  (decision/structured). All three are unconditional.
- `implementation_intelligence.py:789` passes `json_mode=True` to a *different* (non-OpenAI-client) helper;
  it is not the planner path and is noted only for completeness. It does not constitute capability detection
  for the OpenAI/SiliconFlow client.

**5.2 Is JSON mode: always / conditional / provider-dependent / model-dependent / hardcoded?**
- **Hardcoded / always-enabled.** It is a constant literal in the request, independent of provider and model.
  There is no `if provider.supports_json(...)`, no model allow-list, no try/except fallback to non-JSON.

**5.3 Does SiliconFlow support JSON mode for `tencent/Hy3`?**
- **Repository evidence: NOT available.** The repository contains **no capability registry**, no
  provider capability map, and no model-capability table. (Repo-wide search: `supports_json` → none;
  `MODEL_REGISTRY`/`model_registry` → none; `provider` capability logic → only in vendored
  `site-packages` and unrelated Redis/pytest code; `capability` hits are `capability_service.py` which is
  about *server OS capabilities*, not LLM JSON-mode support.)
- The *only* repository evidence about the rejection is the **runtime error itself**: SiliconFlow returned
  `400 code 20024 "Json mode is not supported for this model."` That is direct evidence the current model
  does not support JSON mode on this provider. We cannot prove from the repo *which* SiliconFlow models do
  support it (no registry), but we can prove the *selected* model (`tencent/Hy3`) does not.
- **Statement: "Capability registry does NOT exist in the repository"** — explicitly confirmed.

**5.4 Does the planner ALWAYS assume JSON mode?**
- Yes. `generate_plan` (1772) unconditionally routes through `_chat_json` (1724→1726), which always sends
  `json_object`. There is no non-JSON planner path.

**5.5 Does provider abstraction contain capability detection?**
- No. `_get_openai_client` only builds the client; `generate_plan`/`_chat_json` only build the request. No
  capability detection exists anywhere in `agent_llm.py` or `core/config.py`.

**5.6 Does provider-specific behavior exist?**
- No provider-specific branch exists. The only "provider" signal is the static `OPENAI_BASE_URL` default
  (`https://api.siliconflow.com/v1`). There is no per-provider request shaping.

---

## 6. REPOSITORY EVIDENCE (verbatim)

| Claim | Evidence |
|---|---|
| Planner uses `OPENAI_MODEL_PLANNER or OPENAI_MODEL` | `agent_llm.py:1824` |
| `OPENAI_MODEL_PLANNER` default is `None` | `core/config.py:139` |
| `.env` has NO `OPENAI_MODEL_PLANNER` | `.env` key inventory (only `OPENAI_MODEL` present) |
| `.env` `OPENAI_MODEL = tencent/Hy3` | `.env` line `OPENAI_MODEL = tencent/Hy3` |
| `OPENAI_MODEL` default `gpt-4o-mini` | `core/config.py:136` |
| Provider base URL = SiliconFlow | `core/config.py:135` `OPENAI_BASE_URL = "https://api.siliconflow.com/v1"`; `.env` has no `OPENAI_BASE_URL` |
| `_chat_json` forces JSON mode | `agent_llm.py:1726` `response_format={"type":"json_object"}` |
| JSON mode unconditional (×3) | `agent_llm.py:196`, `:1144`, `:1726` |
| No capability registry | repo search `supports_json`/`MODEL_REGISTRY` → none; `capability_service.py` = server OS caps, not LLM |
| No provider-specific branch | `_get_openai_client` (1639-1649) only sets base_url |
| Runtime error | `400 code 20024 "Json mode is not supported for this model."` (provided in brief; consistent with `:1726`) |

---

## 7. VERIFIED ROOT CAUSE

**Primary (E) Legacy OpenAI assumption.** The planner assumes every model supports OpenAI-style
`response_format={"type":"json_object"}` (a feature that works on OpenAI `gpt-4o*` models, the original
default). The code hardcodes this literal (`agent_llm.py:1726`) and never negotiates or degrades.

**Contributing (C) Capability detection missing.** There is no provider/model capability registry or
runtime probe. The repo cannot express "this model/provider supports JSON mode" or fall back to a
non-JSON parse. So the unsafe assumption is unrecoverable at runtime.

**Contributing (A) Planner configuration bug.** The dedicated `OPENAI_MODEL_PLANNER` slot exists
(`config.py:139`) precisely to let the planner use a JSON-capable model, but it is left `None`/unset in
`.env`, so the planner inherits the global SiliconFlow model (`tencent/Hy3`) that does not support JSON
mode. Even with capability detection, the planner would still need a JSON-capable model selected.

**Not the cause:** (B) provider abstraction bug — the abstraction correctly reaches SiliconFlow; (D) model
*selection* logic is correctly implemented (it just collapses to one model because the slot is unset).

**Net:** This is **F — a combination**: E (hardcoded JSON assumption) + C (no capability detection) + A
(the planner-specific model slot is unconfigured, so it rides the non-JSON-capable global model). All three
must be addressed for a durable fix, but the *proximate* trigger is the hardcoded `response_format` at
`agent_llm.py:1726` combined with `tencent/Hy3` on SiliconFlow.

---

## 8. MINIMAL ARCHITECTURAL CORRECTION STRATEGY (no code/patch — direction only)

1. **Capability detection (fix C):** introduce a provider/model capability layer (registry or runtime
   probe) that answers "does `<provider>/<model>` support `response_format=json_object`?" before any
   `_chat_json` call. This is the missing piece the investigation proves is absent.
2. **Conditional JSON mode (fix E):** `_chat_json` should set `response_format` only when the resolved
   model is JSON-capable per (1); otherwise send no `response_format` and rely on the existing
   `json.loads(raw)` parse (already present at `agent_llm.py:1751`) — i.e. degrade gracefully instead of
   hard-failing. The other two hardcoded sites (`:196`, `:1144`) need the same treatment.
3. **Planner model configuration (fix A):** either set `OPENAI_MODEL_PLANNER` in `.env` to a SiliconFlow
   model that *does* support JSON mode (once capability is known), or route the planner through the
   capability-aware path so it automatically picks/avoids JSON mode. The existing slot (`config.py:139`) is
   the intended lever — it is currently unused.
4. **No provider-branching required (B is not the issue):** the generic OpenAI-compatible client already
   works against SiliconFlow; only the request shape (JSON mode) must become capability-aware.

**Do NOT:** change `.env`, config defaults, or any file — this is an investigation only. The above is the
recommended correction *strategy* for a follow-up fix sprint.

---

## SUMMARY

The planner fails because `agent_llm.generate_plan` → `_chat_json` unconditionally sends
`response_format={"type":"json_object"}` (`agent_llm.py:1726`) to a SiliconFlow model (`tencent/Hy3`,
resolved via `OPENAI_MODEL_PLANNER or OPENAI_MODEL` at `:1824`, where `OPENAI_MODEL_PLANNER` is unset in
`.env` and `OPENAI_MODEL = tencent/Hy3` per `config.py:135/136` + `.env`). SiliconFlow rejects it with
`400 code 20024`. Root cause = **legacy OpenAI JSON-mode assumption (E) + missing capability detection (C)
+ unconfigured planner model slot (A)**. The provider abstraction is correct; there is **no capability
registry in the repository** (explicitly confirmed). Recommended correction: add capability detection,
make JSON mode conditional with graceful non-JSON fallback, and configure `OPENAI_MODEL_PLANNER` (or
auto-route) to a JSON-capable model.
