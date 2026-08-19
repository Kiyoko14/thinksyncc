"""
Sprint 3C.E verification tests — Context Engineering.

These tests exercise the new components WITHOUT a live Supabase/Redis/SSH
backend, using pure-logic paths and a temp THINKSYNC.md file.

Run:  .venv/bin/python3 -m pytest backend/tests/test_sprint_3ce.py -q
"""

import asyncio
import tempfile
from datetime import datetime, timezone

import pytest

from services.project_brain import (
    Confidence,
    ContextDiffEngine,
    EngineeringMemoryLayer,
    KnowledgeItem,
    MemoryGarbageCollector,
    ProjectBrain,
)
from services.context_memory import (
    ConfidenceEngine,
    Freshness,
    SessionSnapshot,
    SessionSnapshotData,
    DecisionMemory,
    TaskMemory,
    TaskRecord,
    KnowledgeDependencyGraph,
)
from services.context_budget import (
    ContextBudgetManager,
    ContextCompressor,
    ContextPriority,
    estimate_tokens,
)
from services.repository_index import RepositoryIndex, _hash_row


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def brain(tmp_path):
    path = tmp_path / "THINKSYNC.md"
    path.write_text(
        "# THINKSYNC.md — Engineering Memory (AI-only)\n\n"
        "## Product & Mission\n\nThinkSync turns objectives into apps.\n\n"
        "## Key Design Decisions (per Decision Memory)\n\n"
        "- **Event Wait** — jobs suspend 30-60m.\n\n"
        "## Known Limitations / Technical Debt\n\n"
        "- old limitation [done]\n\n"
        "## Session Snapshot\n\n- **Goal:** init\n"
    )
    return ProjectBrain(config=type("C", (), {"path": str(path), "cache_ttl": 60})())


# --------------------------------------------------------------------------- #
# Context Diff Engine
# --------------------------------------------------------------------------- #

def test_diff_engine_finds_and_patches_section(brain):
    text = brain._read_file()
    # patch an existing section -> minimal change
    new_text, changed = ContextDiffEngine.patch_section(
        text, "Product & Mission", "ThinkSync turns objectives into running apps (updated)."
    )
    assert changed is True
    # the permanent section header is preserved
    assert "## Product & Mission" in new_text
    # other sections untouched
    assert "## Key Design Decisions" in new_text
    # idempotent: patching same body again yields no change
    _, changed2 = ContextDiffEngine.patch_section(
        new_text, "Product & Mission", "ThinkSync turns objectives into running apps (updated)."
    )
    assert changed2 is False


def test_diff_engine_appends_missing_section():
    text = "# A\n\n## X\n\nbody\n"
    new_text, changed = ContextDiffEngine.patch_section(text, "Y", "new body")
    assert changed is True
    assert "## Y" in new_text
    assert "new body" in new_text


def test_diff_items_minimal_change_set():
    prev = {
        "a": KnowledgeItem(key="a", value="1"),
        "b": KnowledgeItem(key="b", value="2"),
    }
    cur = {
        "a": KnowledgeItem(key="a", value="1"),
        "b": KnowledgeItem(key="b", value="CHANGED"),
        "c": KnowledgeItem(key="c", value="3"),
    }
    diff = ContextDiffEngine.diff_items(prev, cur)
    assert diff["unchanged"] == ["a"]
    assert diff["changed"] == ["b"]
    assert diff["added"] == ["c"]
    assert diff["removed"] == []


# --------------------------------------------------------------------------- #
# Memory Garbage Collector
# --------------------------------------------------------------------------- #

def test_gc_keeps_permanent_sections(brain):
    # Permanent sections must be preserved by the GC.
    assert MemoryGarbageCollector.is_permanent("Product & Mission") is True
    assert MemoryGarbageCollector.is_permanent("Technology Stack") is True
    # Temporary sections are NOT treated as permanent.
    assert MemoryGarbageCollector.is_permanent("Session Snapshot") is False
    assert MemoryGarbageCollector.is_permanent("Known Limitations / Technical Debt") is False


def test_gc_removes_temp_task_entries(brain):
    # The seeded limitation line has [done] -> should be GC'd
    result = brain._read_file()
    assert "[done]" in result
    stats = asyncio.run(brain.garbage_collect())
    assert stats["removed_temp_entries"] >= 1
    assert "[done]" not in brain._read_file()


def test_gc_never_removes_permanent(brain):
    # Append a fake permanent section with a [done] marker -> preserved
    brain._write_file(
        brain._read_file().rstrip()
        + "\n\n## Security Decisions\n\n- encryption at rest [done]\n"
    )
    before = brain._read_file()
    asyncio.run(brain.garbage_collect())
    after = brain._read_file()
    assert "encryption at rest" in after
    assert "## Security Decisions" in after


# --------------------------------------------------------------------------- #
# Confidence + Freshness
# --------------------------------------------------------------------------- #

def test_freshness_stale_detection():
    old = KnowledgeItem(
        key="k", value="v", layer=EngineeringMemoryLayer.TASK,
        updated="2020-01-01T00:00:00+00:00",
    )
    assert Freshness.is_stale(old) is True
    fresh = KnowledgeItem(key="k", value="v", layer=EngineeringMemoryLayer.TASK)
    assert Freshness.is_stale(fresh) is False


def test_confidence_engine_reload_on_low():
    low = KnowledgeItem(key="k", value="v", confidence=Confidence.LOW,
                        layer=EngineeringMemoryLayer.REPOSITORY)
    assert ConfidenceEngine.should_reload(low) is True
    high = KnowledgeItem(key="k", value="v", confidence=Confidence.HIGH)
    assert ConfidenceEngine.should_reload(high) is False


# --------------------------------------------------------------------------- #
# Session Snapshot
# --------------------------------------------------------------------------- #

def test_session_snapshot_roundtrip(brain):
    snap = SessionSnapshotData(
        goal="Implement 3C.E",
        completed=["audit", "brain"],
        progress="60%",
        blockers=[],
        pending_tasks=["wire loader"],
        open_questions=["auto-write policy?"],
        next_step="integrate",
    )
    ss = SessionSnapshot(brain)
    asyncio.run(ss.save(snapshot=snap))
    loaded = asyncio.run(ss.load())
    assert loaded is not None
    assert loaded.goal == "Implement 3C.E"
    assert "audit" in loaded.completed
    assert loaded.next_step == "integrate"


# --------------------------------------------------------------------------- #
# Decision / Task Memory
# --------------------------------------------------------------------------- #

def test_decision_memory_no_duplicate(brain):
    dm = DecisionMemory(brain)
    asyncio.run(dm.record(title="Redis chosen", rationale="fast cache"))
    asyncio.run(dm.record(title="Redis chosen", rationale="fast cache updated"))
    body = asyncio.run(brain.get_section("Key Design Decisions (per Decision Memory)"))
    assert body.count("Redis chosen") == 1  # updated, not duplicated
    assert "updated" in body


def test_task_memory_lifecycle():
    tm = TaskMemory(ProjectBrain())
    rec = TaskRecord(objective="fix login bug", files=["auth.py"], progress="starting")
    tm.begin(record=rec)
    assert tm.current() is not None
    tm.complete(objective="fix login bug")
    assert tm.current() is None


# --------------------------------------------------------------------------- #
# Knowledge Dependency Graph
# --------------------------------------------------------------------------- #

def test_dependency_graph_downstream():
    g = KnowledgeDependencyGraph()
    g.build_default()
    down = g.downstream("workspace")
    # server, deployment, approval, resume, implementation, context
    assert "server" in down
    assert "context" in down
    assert "workspace" not in down


# --------------------------------------------------------------------------- #
# Context Budget + Compression
# --------------------------------------------------------------------------- #

def test_budget_drops_low_priority_first():
    mgr = ContextBudgetManager(config=type("C", (), {"max_tokens": 50, "overflow_keep_top": 12})())
    blocks = [
        {"name": "conv", "priority": ContextPriority.CONVERSATION, "content": "x" * 400},
        {"name": "task", "priority": ContextPriority.CURRENT_TASK, "content": "y" * 20},
        {"name": "brain", "priority": ContextPriority.PROJECT_BRAIN, "content": "z" * 20},
    ]
    fit = mgr.fit(blocks)
    assert "task" in fit["included"]
    assert "brain" in fit["included"]
    # conversation (lowest priority) dropped when over budget
    assert "conv" in fit["dropped"]


def test_compressor_preserves_engineering_facts():
    c = ContextCompressor(keep_recent=2)
    turns = [
        {"content": "hi there"},
        {"content": "the architecture decision was to use Redis"},
        {"content": "ok thanks"},
        {"content": "lets deploy now"},
    ]
    out = c.compress_conversation(turns)
    texts = " ".join(t["content"] for t in out)
    # engineering fact preserved even though not in recent 2
    assert "architecture" in texts
    assert "Redis" in texts


def test_estimate_tokens_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 40) >= estimate_tokens("a" * 4)


# --------------------------------------------------------------------------- #
# Repository Index (pure logic; no SSH)
# --------------------------------------------------------------------------- #

def test_repo_index_hash_row_stable():
    r1 = {"path": "a.py", "size": 10, "last_modified": "2024-01-01"}
    r2 = {"path": "a.py", "size": 10, "last_modified": "2024-01-01"}
    assert _hash_row(r1) == _hash_row(r2)
    r3 = {"path": "a.py", "size": 11, "last_modified": "2024-01-01"}
    assert _hash_row(r1) != _hash_row(r3)


def test_repo_index_queries_empty():
    idx = RepositoryIndex()
    assert idx.entry_points() == []
    assert idx.services() == []
    assert idx.relevant_to(task="auth") == []
