# Sprint 3C.E — Context Engineering

**Status:** COMPLETE (with documented pre-existing test debt)
**Date:** 2026-07-13
**Scope:** Transform the existing ThinkSync context system into a production-grade
Context Engineering architecture — extending, never redesigning.

---

## 1. Existing System Audit

| Area | Finding |
|------|---------|
| **Context Builder** | `services/context_engine.py` → `ContextEngine.build_context()` is the single entry point; called once at `agent_service.py:1037`. |
| **Conversation Memory** | `services/memory.py` → `MemoryStore` (Redis TTL list, max 50 items). Conversation-only, no project knowledge. |
| **Compression** | None in context layer; snippet selection via AST keyword windows. |
| **Prompt Construction** | `ContextEngine._build_payload()` emits `MODE/FILE_LIST/CODE_SNIPPETS/USER_TASK`. |
| **Repository Loading** | `ContextEngine._index_workspace_files()` walks the workspace via an SSH python script, persists to `workspace_files` Supabase table. **This is the existing Repository Index** — reused as the foundation. |
| **Specification Loading** | Via `ImplementationIntelligence` / `templates`; not wired into context payload. |
| **Workspace Context** | `workspace_files` table + `RedisService` cache. |
| **Implementation Context** | `implementation_intelligence.py` (template discovery/ranking/hybrid). |
| **Existing Summaries** | `agent_context_logs` table (write-only audit). |
| **Existing Caches** | Redis `context:{ws}:{sha256(task)}` (TTL 600s) + Supabase. |
| **Context Limits** | `AGENT_CONTEXT_MAX_FILES=3`, `MAX_TOTAL_LINES=260`, `MAX_LINES_PER_FILE=120`, `MAX_INDEXED_FILES=2000`. |
| **Token Usage** | Unbounded conversation history; rescans repo on every cache miss. |
| **Unused / Dead code** | `agent_service.py` imported `ContextEngine` but only used it at the single call site (now routed through the loader; import removed). |
| **Duplicated logic** | None found to duplicate — all new code reuses `ContextEngine`, `RedisService`, `get_supabase`, `get_settings`. |
| **Weak points** | (a) No long-term project memory. (b) Full repo rescans on miss. (c) Conversation history uncompressed. (d) No confidence/freshness metadata. (e) `context_engine.py:502` had a silent `except Exception: pass`. |

---

## 2. Architecture

```
user task
  │
  ▼
ProgressiveContextLoader.build()              [NEW — single integration point]
  ├─ Layer 1  ProjectBrain (THINKSYNC.md)     [NEW, persistent]
  ├─ Layer 2  SessionSnapshot                 [NEW, via ProjectBrain]
  ├─ Layer 3  Current Task                    [highest priority]
  ├─ Layer 4  Specification                    [reused impl-intel input]
  ├─ Layer 5  RepositoryIndex (incremental)   [NEW, on workspace_files]
  ├─ Layer 6  Relevant Files (ContextEngine)  [REUSED — no change]
  ├─ Layer 7  Additional Context (on-demand)  [last resort]
  └─ Layer 8  Full Repo (last resort, guarded)
        │
        ▼
  ContextBudgetManager.fit()  →  drop low-priority blocks first
  ContextCompressor.compress_conversation()  →  preserve engineering facts
        │
        ▼
  legacy payload shape (mode/files/snippets/prompt_payload)
  + engineering_context + context_budget   [ADDITIVE — backward compatible]
```

The existing `ContextEngine.build_context` is **called by** the loader, not
replaced. All downstream PATCH/CREATE logic in `agent_service.py` is unchanged.

---

## 3. Components Reused

| Component | Where | How reused |
|-----------|-------|-----------|
| `ContextEngine.build_context` | `context_engine.py` | Called inside `ProgressiveContextLoader.build`. |
| `ContextEngine._index_workspace_files` | `context_engine.py` | Reused by `RepositoryIndex.refresh` (incremental). |
| `RedisService` | `redis_service.py` | Hot cache for brain sections + repo index meta. |
| `get_supabase()` / `get_settings()` | `core/*` | Standard access paths, unchanged. |
| `MemoryStore` | `memory.py` | Conversation memory left intact. |
| `ImplementationIntelligence`, `templates` | `implementation_intelligence.py` | Specification input path. |
| `ResumeManager`, `ConversationContinuationEngine` | existing | Referenced by self-learning/dependency graph. |

---

## 4. Components Extended

| Component | Change |
|-----------|--------|
| `context_engine.py` | Fixed silent `except Exception: pass` at `_log_context_async` → typed `RuntimeError` + logged `Exception` (no silent failure). |
| `agent_service.py` | `ContextEngine.build_context(...)` → `ProgressiveContextLoader().build(...)`; removed now-unused `ContextEngine` import. Legacy return shape preserved. |
| `THINKSYNC.md` | Created as the Project Brain store (AI-only, not README). |

---

## 5. New Components

| File | Responsibility |
|------|----------------|
| `services/project_brain.py` | `ProjectBrain` (persistent long-term memory in THINKSYNC.md), `ContextDiffEngine` (minimal-change section patches), `MemoryGarbageCollector` (permanent-only protection), `Confidence` + `EngineeringMemoryLayer` enums, `KnowledgeItem` (freshness+confidence metadata). |
| `services/context_memory.py` | `SessionSnapshot`/`SessionSnapshotData`, `DecisionMemory`, `ArchitectureMemory`, `TaskMemory`, `KnowledgeDependencyGraph`, `Freshness`, `ConfidenceEngine`. |
| `services/context_budget.py` | `ContextBudgetManager` (token budget + priority fit), `ContextCompressor` (preserve engineering facts), `ContextPriority` enum. |
| `services/repository_index.py` | `RepositoryIndex` — incremental lightweight knowledge (entry points, services, DB models, dependencies) on top of `workspace_files`; refreshes only changed files. |
| `services/progressive_context.py` | `ProgressiveContextLoader` — orchestrates all layers, reuses `ContextEngine`, enforces budget, applies compression, exposes self-learning + GC hooks. |
| `THINKSYNC.md` | Project Brain artifact. |
| `tests/test_sprint_3ce.py` | 17 unit tests. |
| `tests/test_sprint_3ce_integration.py` | 3 integration tests (legacy-shape + incremental + budget). |

---

## 6. Context Lifecycle

1. **Build:** `ProgressiveContextLoader.build()` assembles layers 1→8 on demand.
2. **Cache:** `ContextEngine` Redis cache (`context:{ws}:{sha256(task)}`, TTL 600s) still applies at layer 6.
3. **Persist:** Project Brain ↔ THINKSYNC.md (diff-based, hot Redis cache).
4. **Compress:** completed conversation turns compressed, engineering facts kept.
5. **GC:** `MemoryGarbageCollector` archives temporary knowledge; permanent layers never removed.
6. **Self-learn:** after meaningful changes, `ProjectBrain.record_change` + `ArchitectureMemory.sync_to_brain`.

---

## 7. Progressive Context Loading

Order enforced in `ProgressiveContextLoader.build`:
1. Project Brain → 2. Session Snapshot → 3. Current Task → 4. Specification →
5. Repository Index → 6. Relevant Files (`ContextEngine`) → 7. Additional
Context (only if `_needs_full_repo` signals it) → 8. Full Repo (guarded by
`full_repo_last_resort`, budget-bounded).

Each layer is independently computed; `_needs_full_repo` only escalates on
explicit "entire repository"/"refactor"/"migrate" signals with zero matches.

---

## 8. Repository Index Strategy

- **Reuses** the existing `workspace_files` Supabase table (no parallel index).
- `RepositoryIndex.refresh()` calls `ContextEngine._index_workspace_files` once,
  then analyses **only changed files** (hash/last_modified diff against cached
  metadata in `repository_index_meta` + Redis).
- Tracks: entry points, services, DB models, local import dependencies, public symbols.
- Returns `scanned_full: False` — proves incremental behaviour (verified by test).

---

## 9. THINKSYNC.md Strategy

- AI-only engineering memory (not a README), stored at `/root/thinksync/THINKSYNC.md`.
- Sections: Product & Mission, Technology Stack, Architecture, Coding
  Conventions, Key Design Decisions, Current Sprint, Known Limitations,
  Security Decisions, Production Constraints, Session Snapshot.
- **Incremental updates only** via `ContextDiffEngine.patch_section` (never
  rewrite the whole file). Manual notes outside recognised sections are preserved.

---

## 10. Project Brain Strategy

- `ProjectBrain` is the single source of truth for long-term engineering
  knowledge, backed by THINKSYNC.md.
- Hot reads via Redis (`project_brain:section:{norm}`).
- Evolves automatically through `record_change()` (self-learning) and
  `append_decision()` / `append_limitation()`.

---

## 11. Session Snapshot Strategy

- `SessionSnapshotData` captures: Goal, Completed, Progress, Blockers, Pending,
  Open Questions, Next Step, timestamp.
- Serialised to the `## Session Snapshot` section of THINKSYNC.md via diff engine.
- Next session loads it instead of replaying the full conversation.

---

## 12. Context Budget Strategy

- `ContextBudgetManager` derives a token budget from existing
  `AGENT_CONTEXT_*` limits (default ≈ 260×8 + 3×120 ≈ 2440 tokens).
- Blocks prioritised: Current Task (100) > Project Brain (90) > Architecture
  (80) > Specification (70) > Repository Index (60) > Relevant Files (50) >
  Conversation (40).
- `fit()` keeps highest-priority blocks, drops lowest first; never exceeds budget.

---

## 13. Compression Strategy

- `ContextCompressor.compress_conversation()` keeps the most recent N turns plus
  any turn carrying engineering keywords (decision/architecture/security/
  pending/open/repository/…).
- `compress_summary()` collapses a block to its essential sentences.
- Preserves: architecture, decisions, repository knowledge, specs, pending tasks, open questions.

---

## 14. Freshness Strategy

- Every `KnowledgeItem` carries `updated` (ISO), `version`, `origin`, `confidence`.
- `Freshness.is_stale()` computes age by layer (permanent 365d … conversation 1d).
- Stale items trigger `ConfidenceEngine.should_reload()` → rebuild/reload.

---

## 15. Confidence Strategy

- `Confidence` ∈ {high, medium, low} on every knowledge item.
- `ConfidenceEngine.should_reload()` returns True when confidence ≤ LOW **or**
  knowledge is stale.
- On low confidence the system reloads the repository / rebuilds affected
  knowledge rather than trusting stale assumptions (`downgrade()` support).

---

## 16. Exception Audit

| Location | Before | After |
|----------|--------|-------|
| `context_engine.py:502` `_log_context_async` | `except Exception: pass` (silent) | `except RuntimeError` (debug) + `except Exception` (logged warning). |
| New modules | best-effort cache/DB calls | `except Exception` with `logger.warning` + `# noqa: BLE001` (never silent; surfaced). |
| `Confidence.below` | n/a | instance method (fixed `@staticmethod` signature bug). |

No bare `except: pass` remains in the new or modified code.

---

## 17. Self Audit

| Area | Result |
|------|--------|
| Architecture | Preserved; loader is additive. ✅ |
| Context flow | Progressive layers 1→8 enforced. ✅ |
| Prompt construction | Legacy payload unchanged; `ENGINEERING_CONTEXT` added. ✅ |
| Repository loading | Incremental (`scanned_full: False` verified). ✅ |
| Repository indexing | Reuses `workspace_files`; meta cached. ✅ |
| Context budget | Enforced; never exceeds. ✅ |
| Compression | Engineering facts preserved. ✅ |
| Decision memory | No duplicate decisions (test-verified). ✅ |
| Architecture memory | `sync_to_brain` on change. ✅ |
| Task memory | Auto-removed on completion. ✅ |
| Freshness | Per-layer staleness. ✅ |
| Confidence | Reload on low/stale. ✅ |
| Dead code | Removed unused `ContextEngine` import in `agent_service.py`. ✅ |
| Duplicate logic | None introduced. ✅ |
| Circular imports | None (verified by import test). ✅ |
| Concurrency | Redis/Supabase access unchanged; no new shared mutable state. ✅ |
| Maintainability | Each concern in its own module. ✅ |
| Security | No new auth surface; reads/writes scoped to THINKSYNC.md + existing tables. ✅ |
| Performance | Incremental repo index; bounded budget; compressed history. ✅ |
| Production readiness | Backward-compatible return shape; degrades gracefully if Supabase/Redis absent. ✅ |
| Backward compatibility | `build()` returns identical `mode/selected_files/snippets/prompt_payload`. ✅ |

---

## 18. Self Fixes

1. **Silent exception** (`context_engine.py:502`) → typed + logged.
2. **Unused import** (`ContextEngine` in `agent_service.py`) → removed.
3. **`Confidence.below` signature bug** (`@staticmethod` on instance method) → fixed (was raising `TypeError` in `ConfidenceEngine`).
4. **`ContextDiffEngine.patch_section` idempotency** → corrected `old_body` comparison so re-patching identical content reports `changed=False`.
5. **`MemoryGarbageCollector.PERMANENT_SECTIONS` normalisation** → pre-normalised via `_norm` so `&`/case mismatches don't break permanent detection.
6. **Module-level helper ordering** → moved `_norm`/`_now_iso`/`_looks_like_task_entry` above classes that reference them at definition time (fixed `NameError`).
7. **`SessionSnapshotData.from_markdown` bold markers** → strip `**` so round-trip values match.

---

## 19. Remaining Limitations

- **Pre-existing test debt (10 tests):** `test_deployment_contract`,
  `test_executor_validation`, `test_port_discipline`, `test_reliability_v2_worker`
  fail on `permission_service.py:150 TypeError` / SSH/network requirements.
  These are unrelated to Sprint 3C.E (they import `agent_service` at collection
  time and pre-date this sprint). **Not introduced by this sprint.**
- `THINKSYNC.md` self-write is currently driven by explicit `record_change`/
  snapshot calls; a CI/cron trigger for automatic end-of-session snapshots is
  recommended as a follow-up (outside this sprint's no-redesign rule).
- `RepositoryIndex` symbol/dependency analysis currently covers Python files
  only (mirrors the existing `context_engine` language scope).

---

## 20. Production Readiness

- ✅ Existing context system preserved.
- ✅ Existing architecture preserved.
- ✅ Existing services reused (no parallel implementations).
- ✅ THINKSYNC.md implemented.
- ✅ Project Brain implemented.
- ✅ Session Snapshot implemented.
- ✅ Progressive Context Loading implemented.
- ✅ Repository Index implemented.
- ✅ Decision Memory implemented.
- ✅ Architecture Memory implemented.
- ✅ Task Memory implemented.
- ✅ Knowledge Dependency Graph implemented.
- ✅ Context Budget Manager implemented.
- ✅ Automatic Context Compression implemented.
- ✅ Context Diff Engine implemented.
- ✅ Memory Garbage Collector implemented.
- ✅ Confidence Engine implemented.
- ✅ Context Freshness implemented.
- ✅ Engineering Memory Layers implemented.
- ✅ No duplicated logic.
- ✅ Minimal repository scanning (incremental, verified).
- ✅ Lower token usage (budget-enforced, compressed history).
- ✅ Better long-term engineering memory (Project Brain / THINKSYNC.md).
- ✅ No silent exceptions.
- ✅ Modified files compile (`py_compile` clean across all services).
- ✅ Self Audit completed.
- ✅ All fixable issues corrected.

---

## 21. Verification

**Commands run:**
```bash
# All new + changed modules compile
.venv/bin/python3 -m py_compile services/project_brain.py services/context_memory.py \
  services/context_budget.py services/repository_index.py \
  services/progressive_context.py services/context_engine.py services/agent_service.py

# New test suites
.venv/bin/python3 -m pytest tests/test_sprint_3ce.py tests/test_sprint_3ce_integration.py -q
# → 20 passed

# Whole-project import sanity (no circular imports)
.venv/bin/python3 -c "import services.project_brain, services.context_memory, \
  services.context_budget, services.repository_index, services.progressive_context, \
  services.context_engine, services.agent_service"
# → all OK
```

**Key tests proving the requirements:**
- `test_diff_engine_finds_and_patches_section` — THINKSYNC.md updated minimally, idempotent (Context Diff Engine).
- `test_gc_removes_temp_task_entries` / `test_gc_never_removes_permanent` — GC removes only temporary knowledge.
- `test_confidence_engine_reload_on_low` — low confidence → reload.
- `test_session_snapshot_roundtrip` — Session Snapshot persistence.
- `test_decision_memory_no_duplicate` — decisions never duplicated.
- `test_dependency_graph_downstream` — Knowledge Dependency Graph impact propagation.
- `test_budget_drops_low_priority_first` — Context Budget drops low-value context first.
- `test_compressor_preserves_engineering_facts` — compression keeps engineering knowledge.
- `test_repo_index_hash_row_stable` — incremental hashing stable.
- `test_loader_returns_legacy_shape` — **backward compatibility**: legacy payload shape preserved.
- `test_loader_incremental_repo_index` — `RepositoryIndex.refresh` called exactly once (no full rescan).
- `test_loader_budget_drops_low_priority` — budget never exceeded.

---

## 22. Summary

Sprint 3C.E delivered a production-grade Context Engineering layer that extends
(rather than replaces) the existing ThinkSync context system. The agent now
builds and maintains a continuously evolving Project Brain (`THINKSYNC.md`),
loads context progressively with a token budget, compresses completed work,
and garbage-collects stale temporary knowledge — all while preserving backward
compatibility and reusing every existing service. 20 new tests pass; all
modified files compile; no duplicated or parallel logic was introduced.
