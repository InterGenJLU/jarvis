#!/usr/bin/env python3
"""
Self-Managing Memory tests — per-turn extraction + recall_memory tool + registry.

Tests:
  Part 1: Per-turn extraction (store side)
  Part 2: recall_memory tool (recall side)
  Part 3: Registry integration (ALWAYS_INCLUDED_TOOLS, TOOL_HANDLERS)

Usage:
  python3 scripts/test_manage_memory.py --verbose
"""

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, "/home/user/jarvis")

# Stub heavy imports before they're pulled in
sys.modules.setdefault("porcupine", MagicMock())
sys.modules.setdefault("pvporcupine", MagicMock())
sys.modules.setdefault("resemblyzer", MagicMock())
sys.modules.setdefault("resemblyzer.VoiceEncoder", MagicMock())

# ─── Test infrastructure ──────────────────────────────────────────────────

VERBOSE = "--verbose" in sys.argv


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


results: list[TestResult] = []


def log(msg):
    if VERBOSE:
        print(msg)


def assert_eq(name, actual, expected, context=""):
    ok = actual == expected
    detail = f"expected={expected!r}, got={actual!r}"
    if context:
        detail += f" [{context}]"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


def assert_true(name, condition, detail=""):
    ok = bool(condition)
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


def assert_in(name, needle, haystack, detail=""):
    ok = needle in haystack
    if not detail:
        detail = f"{needle!r} not found in {type(haystack).__name__}"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


def assert_not_in(name, needle, haystack, detail=""):
    ok = needle not in haystack
    if not detail:
        detail = f"{needle!r} unexpectedly found in {type(haystack).__name__}"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


# ─── Helpers ──────────────────────────────────────────────────────────────

def make_test_config(db_path: str, per_turn: bool = True) -> MagicMock:
    """Create a mock config with conversational_memory settings."""
    data = {
        "conversational_memory.enabled": True,
        "conversational_memory.db_path": db_path,
        "conversational_memory.faiss_index_path": db_path.replace(".db", "_faiss"),
        "conversational_memory.batch_extraction_interval": 25,
        "conversational_memory.proactive_surfacing": False,
        "conversational_memory.proactive_confidence_threshold": 0.45,
        "conversational_memory.per_turn_extraction": per_turn,
        "logging.level": "WARNING",
    }
    config = MagicMock()
    config.get = lambda key, default=None: data.get(key, default)
    return config


def make_memory_manager(db_path: str, per_turn: bool = True):
    """Create a MemoryManager with a temp DB for testing."""
    from core.memory_manager import MemoryManager
    config = make_test_config(db_path, per_turn=per_turn)
    conversation = MagicMock()
    conversation.session_history = []
    mm = MemoryManager(config, conversation, embedding_model=None)
    return mm


# ═══════════════════════════════════════════════════════════════════════════
# Part 1: Per-turn extraction tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part1():
    log("\n── Part 1: Per-turn extraction ──────────────────────────────\n")

    tmpdir = tempfile.mkdtemp(prefix="jarvis_mem_test_")
    db_path = os.path.join(tmpdir, "test_memory.db")

    # --- Test: state variables initialized ---
    mm = make_memory_manager(db_path)
    assert_eq("init: _per_turn_in_progress is False", mm._per_turn_in_progress, False)
    assert_eq("init: _last_user_message is None", mm._last_user_message, None)

    # --- Test: on_message caches user message ---
    mm.on_message({"role": "user", "content": "My favorite color is blue"})
    assert_eq("on_message: caches user message", mm._last_user_message, "My favorite color is blue")

    # --- Test: short message skipped ---
    mm2 = make_memory_manager(os.path.join(tmpdir, "test2.db"))
    mm2.on_message({"role": "user", "content": "hi"})  # < 15 chars
    triggered = []
    mm2._trigger_per_turn_extraction = lambda u, a, uid: triggered.append((u, a))
    mm2.on_message({"role": "assistant", "content": "Hello!"})
    assert_eq("short user msg: extraction NOT triggered", len(triggered), 0)

    # --- Test: long enough message triggers extraction ---
    mm3 = make_memory_manager(os.path.join(tmpdir, "test3.db"))
    triggered3 = []
    original_trigger = mm3._trigger_per_turn_extraction
    mm3._trigger_per_turn_extraction = lambda u, a, uid: triggered3.append((u, a))
    mm3.on_message({"role": "user", "content": "My favorite programming language is Python"})
    mm3.on_message({"role": "assistant", "content": "I'll remember that, sir."})
    assert_eq("long user msg: extraction triggered", len(triggered3), 1)
    assert_true("triggered with correct user msg",
                triggered3[0][0] == "My favorite programming language is Python",
                f"got {triggered3[0][0]!r}")

    # --- Test: debounce prevents concurrent triggers ---
    mm4 = make_memory_manager(os.path.join(tmpdir, "test4.db"))
    mm4._per_turn_in_progress = True  # Simulate in-progress
    triggered4 = []
    mm4._trigger_per_turn_extraction = lambda u, a, uid: triggered4.append(True)
    mm4.on_message({"role": "user", "content": "I also love hiking in the mountains"})
    mm4.on_message({"role": "assistant", "content": "Wonderful hobby, sir."})
    assert_eq("debounce: extraction NOT triggered when in progress", len(triggered4), 0)

    # --- Test: per_turn_extraction=False disables feature ---
    mm5 = make_memory_manager(os.path.join(tmpdir, "test5.db"), per_turn=False)
    triggered5 = []
    mm5._trigger_per_turn_extraction = lambda u, a, uid: triggered5.append(True)
    mm5.on_message({"role": "user", "content": "I work at a cybersecurity company"})
    mm5.on_message({"role": "assistant", "content": "Noted, sir."})
    assert_eq("config disabled: extraction NOT triggered", len(triggered5), 0)

    # --- Test: _run_per_turn_extraction stores fact with correct source/confidence ---
    mm6 = make_memory_manager(os.path.join(tmpdir, "test6.db"))
    llm_response = '{"category": "preference", "subject": "color", "content": "User prefers blue"}'
    mock_llm = MagicMock()
    mock_llm.chat = MagicMock(return_value=llm_response)

    with patch("core.llm_router.get_llm_router", return_value=mock_llm):
        # Call directly (not in thread) for testing
        mm6._run_per_turn_extraction("My favorite color is blue", "Noted, sir.", "primary_user")

    facts = mm6.get_facts("primary_user")
    per_turn_facts = [f for f in facts if f["source"] == "per_turn"]
    assert_true("fact stored with source=per_turn", len(per_turn_facts) >= 1,
                f"got {len(per_turn_facts)} per_turn facts")
    if per_turn_facts:
        assert_eq("fact confidence=0.75", per_turn_facts[0]["confidence"], 0.75)
        assert_eq("fact category=preference", per_turn_facts[0]["category"], "preference")
        assert_true("fact content matches", "blue" in per_turn_facts[0]["content"].lower(),
                     f"got: {per_turn_facts[0]['content']!r}")

    # --- Test: _run_per_turn_extraction handles empty LLM response ---
    mm7 = make_memory_manager(os.path.join(tmpdir, "test7.db"))
    mock_llm2 = MagicMock()
    mock_llm2.chat = MagicMock(return_value="")  # No facts found

    with patch("core.llm_router.get_llm_router", return_value=mock_llm2):
        mm7._run_per_turn_extraction("How's the weather?", "Let me check.", "primary_user")

    facts7 = mm7.get_facts("primary_user")
    assert_eq("empty response: no facts stored", len(facts7), 0)

    # --- Test: _run_per_turn_extraction handles malformed JSON ---
    mm8 = make_memory_manager(os.path.join(tmpdir, "test8.db"))
    mock_llm3 = MagicMock()
    mock_llm3.chat = MagicMock(return_value="not json at all\n{broken")

    with patch("core.llm_router.get_llm_router", return_value=mock_llm3):
        mm8._run_per_turn_extraction("Some message here", "Some response.", "primary_user")

    facts8 = mm8.get_facts("primary_user")
    assert_eq("malformed JSON: no facts stored", len(facts8), 0)

    # --- Test: truncation at 500 chars ---
    mm9 = make_memory_manager(os.path.join(tmpdir, "test9.db"))
    long_msg = "x" * 1000
    mock_llm4 = MagicMock()
    mock_llm4.chat = MagicMock(return_value="")
    with patch("core.llm_router.get_llm_router", return_value=mock_llm4):
        mm9._run_per_turn_extraction(long_msg, long_msg, "primary_user")
    # Verify the LLM was called with truncated messages
    call_args = mock_llm4.chat.call_args
    prompt = call_args[1].get("user_message", call_args[0][0] if call_args[0] else "")
    assert_true("truncation: user msg truncated to 500",
                ("x" * 501) not in prompt,
                "message was NOT truncated")

    # --- Test: debounce flag reset in finally block ---
    mm10 = make_memory_manager(os.path.join(tmpdir, "test10.db"))
    mock_llm5 = MagicMock()
    mock_llm5.chat = MagicMock(side_effect=Exception("LLM error"))
    mm10._per_turn_in_progress = True
    with patch("core.llm_router.get_llm_router", return_value=mock_llm5):
        mm10._run_per_turn_extraction("Test msg here", "Response.", "primary_user")
    assert_eq("error recovery: _per_turn_in_progress reset", mm10._per_turn_in_progress, False)

    # --- Test: dedup — same fact not stored twice ---
    mm11 = make_memory_manager(os.path.join(tmpdir, "test11.db"))
    llm_resp = '{"category": "preference", "subject": "color", "content": "User prefers blue"}'
    mock_llm6 = MagicMock()
    mock_llm6.chat = MagicMock(return_value=llm_resp)

    with patch("core.llm_router.get_llm_router", return_value=mock_llm6):
        mm11._run_per_turn_extraction("Fav color is blue", "Noted.", "primary_user")
        mm11._run_per_turn_extraction("I like blue best", "Noted.", "primary_user")

    facts11 = mm11.get_facts("primary_user")
    active_color = [f for f in facts11 if f["subject"] == "color" and not f.get("superseded_by")]
    assert_eq("dedup: only 1 active fact for same subject", len(active_color), 1)

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Part 2: recall_memory tool tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part2():
    log("\n── Part 2: recall_memory tool ───────────────────────────────\n")

    tmpdir = tempfile.mkdtemp(prefix="jarvis_mem_test_")
    db_path = os.path.join(tmpdir, "test_recall.db")

    # Create a memory manager with some facts
    mm = make_memory_manager(db_path)
    mm.store_fact({
        "user_id": "user",
        "category": "preference",
        "subject": "color",
        "content": "User's favorite color is blue",
        "source": "explicit",
        "confidence": 0.90,
    })
    mm.store_fact({
        "user_id": "user",
        "category": "work",
        "subject": "job",
        "content": "User works in cybersecurity",
        "source": "per_turn",
        "confidence": 0.75,
    })
    mm.store_fact({
        "user_id": "user",
        "category": "habit",
        "subject": "coffee",
        "content": "User drinks dark roast coffee every morning",
        "source": "inferred",
        "confidence": 0.70,
    })

    # Import and wire the tool
    import core.tools.recall_memory as recall_mod
    recall_mod._memory_manager = mm
    recall_mod._current_user_fn = lambda: "primary_user"

    # --- Test: text search finds matching fact ---
    result = recall_mod.handler({"query": "color"})
    assert_true("text search: finds color fact", "blue" in result.lower(),
                f"got: {result!r}")

    # --- Test: returns formatted output ---
    assert_true("format: includes category tag", "[preference]" in result.lower(),
                f"got: {result!r}")
    assert_true("format: includes confidence", "90%" in result or "confidence" in result.lower(),
                f"got: {result!r}")

    # --- Test: search for work ---
    result2 = recall_mod.handler({"query": "job"})
    assert_true("text search: finds work fact", "cybersecurity" in result2.lower(),
                f"got: {result2!r}")

    # --- Test: no results ---
    result3 = recall_mod.handler({"query": "xyznonexistent"})
    assert_true("no results: returns appropriate message",
                "no memories" in result3.lower(),
                f"got: {result3!r}")

    # --- Test: empty query error ---
    result4 = recall_mod.handler({"query": ""})
    assert_true("empty query: returns error", "error" in result4.lower(),
                f"got: {result4!r}")

    # --- Test: missing query param ---
    result5 = recall_mod.handler({})
    assert_true("missing query: returns error", "error" in result5.lower(),
                f"got: {result5!r}")

    # --- Test: uninitialized memory manager ---
    recall_mod._memory_manager = None
    result6 = recall_mod.handler({"query": "anything"})
    assert_true("uninitialized: returns error", "not initialized" in result6.lower(),
                f"got: {result6!r}")
    recall_mod._memory_manager = mm  # Restore

    # --- Test: max 5 results ---
    for i in range(10):
        mm.store_fact({
            "user_id": "user",
            "category": "general",
            "subject": f"thing{i}",
            "content": f"User knows about thing number {i} which is interesting",
            "source": "explicit",
            "confidence": 0.85,
        })
    result7 = recall_mod.handler({"query": "thing"})
    lines = [l for l in result7.strip().split("\n") if l.strip()]
    assert_true("max results: at most 5 lines", len(lines) <= 5,
                f"got {len(lines)} lines")

    # --- Test: user_id from current_user_fn ---
    recall_mod._current_user_fn = lambda: "secondary_user"
    result8 = recall_mod.handler({"query": "color"})
    # Secondary user has no facts stored, so should return "no memories"
    assert_true("user isolation: erica has no facts",
                "no memories" in result8.lower(),
                f"got: {result8!r}")
    recall_mod._current_user_fn = lambda: "primary_user"  # Restore

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Part 3: Registry integration tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part3():
    log("\n── Part 3: Registry integration ─────────────────────────────\n")

    from core.tool_registry import (
        ALWAYS_INCLUDED_TOOLS, SKILL_TOOLS, ALL_TOOLS,
        TOOL_HANDLERS, RECALL_MEMORY_TOOL,
    )

    # --- Test: recall_memory in ALWAYS_INCLUDED_TOOLS ---
    assert_in("registry: recall_memory in ALWAYS_INCLUDED_TOOLS",
              "recall_memory", ALWAYS_INCLUDED_TOOLS)

    # --- Test: web_search still in ALWAYS_INCLUDED_TOOLS (regression) ---
    assert_in("registry: web_search in ALWAYS_INCLUDED_TOOLS",
              "web_search", ALWAYS_INCLUDED_TOOLS)

    # --- Test: recall_memory NOT in SKILL_TOOLS (it's not skill-gated) ---
    assert_not_in("registry: recall_memory NOT in SKILL_TOOLS",
                  "recall_memory", SKILL_TOOLS)

    # --- Test: recall_memory in ALL_TOOLS ---
    assert_in("registry: recall_memory in ALL_TOOLS",
              "recall_memory", ALL_TOOLS)

    # --- Test: recall_memory handler registered ---
    assert_in("registry: recall_memory in TOOL_HANDLERS",
              "recall_memory", TOOL_HANDLERS)

    # --- Test: RECALL_MEMORY_TOOL schema is valid ---
    assert_true("schema: RECALL_MEMORY_TOOL is non-empty dict",
                isinstance(RECALL_MEMORY_TOOL, dict) and len(RECALL_MEMORY_TOOL) > 0,
                f"got: {type(RECALL_MEMORY_TOOL)}")
    assert_eq("schema: function name", RECALL_MEMORY_TOOL["function"]["name"], "recall_memory")
    assert_in("schema: query param exists", "query",
              RECALL_MEMORY_TOOL["function"]["parameters"]["properties"])

    # --- Test: web_search handler is still None (regression) ---
    assert_true("regression: web_search handler is None",
                TOOL_HANDLERS.get("web_search") is None,
                f"got: {TOOL_HANDLERS.get('web_search')!r}")

    # --- Test: ALWAYS_INCLUDED_TOOLS count ---
    assert_eq("registry: exactly 4 always-included tools",
              len(ALWAYS_INCLUDED_TOOLS), 4)
    ai_names = set(ALWAYS_INCLUDED_TOOLS.keys()) if isinstance(ALWAYS_INCLUDED_TOOLS, dict) else {t["function"]["name"] for t in ALWAYS_INCLUDED_TOOLS}
    for expected in ("web_search", "recall_memory", "take_screenshot", "capture_webcam"):
        assert_true(f"registry: {expected} is always-included",
                    expected in ai_names,
                    f"{expected} not found in ALWAYS_INCLUDED_TOOLS")

    # --- Test: build_tool_prompt_rules includes recall_memory rule ---
    from core.tool_registry import build_tool_prompt_rules
    rules = build_tool_prompt_rules({"recall_memory", "web_search"})
    assert_true("prompt rules: includes recall_memory guidance",
                "recall_memory" in rules,
                f"recall_memory not found in rules output")

    # --- Test: tool_executor has set_memory_manager ---
    from core.tool_executor import set_memory_manager
    assert_true("tool_executor: set_memory_manager is callable",
                callable(set_memory_manager))


# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("Self-Managing Memory — Test Suite\n")

    test_part1()
    test_part2()
    test_part3()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print(f"\n{'═' * 60}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f" ({failed} failed)")
        for r in results:
            if not r.passed:
                print(f"  FAIL: {r.name} — {r.detail}")
    else:
        print(" ✓")
    print(f"{'═' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
