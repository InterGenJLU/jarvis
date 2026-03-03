#!/usr/bin/env python3
"""
Tool Artifact Wiring tests — unit + integration.

Tests:
  Part 1: store_tool_artifact() unit tests (correct types, summaries, skip conditions)
  Part 2: _TYPE_REFERENCE_PATTERNS matching for new artifact types
  Part 3: Integration — mock tool result → artifact stored → reference resolves

Usage:
  python3 scripts/test_tool_artifacts.py --verbose
"""

import os
import re
import sys
import time
import tempfile
import shutil
from dataclasses import dataclass
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, "/home/user/jarvis")

# Stub heavy imports before they're pulled in
sys.modules.setdefault("porcupine", MagicMock())
sys.modules.setdefault("pvporcupine", MagicMock())
sys.modules.setdefault("resemblyzer", MagicMock())
sys.modules.setdefault("resemblyzer.VoiceEncoder", MagicMock())

# ─── Imports ───────────────────────────────────────────────────────────────

from core.interaction_cache import (
    InteractionCache, Artifact, store_tool_artifact,
    _TOOL_ARTIFACT_META, _SKIP_PREFIXES,
)
from core.conversation_state import ConversationState

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


def assert_none(name, value, detail=""):
    ok = value is None
    if not detail:
        detail = f"expected None, got {value!r}"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


def assert_not_none(name, value, detail=""):
    ok = value is not None
    if not detail:
        detail = "expected non-None, got None"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


# ─── Helpers ──────────────────────────────────────────────────────────────

def make_cache():
    """Create a temp InteractionCache for testing."""
    tmpdir = tempfile.mkdtemp(prefix="jarvis_test_cache_")

    def _config_get(key, default=None):
        if key == "system.storage_path":
            return tmpdir
        if key == "logging.level":
            return "WARNING"
        if key == "logging.file":
            return None
        return default

    config = MagicMock()
    config.get = _config_get
    cache = InteractionCache(config)
    return cache, tmpdir


def make_conv_state(turn=1, window_id="test_window_001"):
    """Create a ConversationState with preset fields."""
    cs = ConversationState()
    cs.turn_count = turn
    cs.window_id = window_id
    return cs


# ═══════════════════════════════════════════════════════════════════════════
# PART 1: store_tool_artifact() unit tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part1():
    log("\n═══ Part 1: store_tool_artifact() unit tests ═══")

    cache, tmpdir = make_cache()
    try:
        # --- 1.1: Each tool creates the correct artifact type ---
        log("\n── 1.1: Correct artifact types ──")

        test_cases = [
            (
                "get_weather",
                {"query_type": "current"},
                "Current conditions: 72°F, partly cloudy, wind 5mph NW. Humidity 45%.",
                "weather_report",
                "Weather current",
            ),
            (
                "get_weather",
                {"query_type": "forecast", "location": "London"},
                "3-day forecast for London:\nMon: 55°F/45°F, rain\nTue: 58°F/48°F, cloudy\nWed: 60°F/50°F, sunny",
                "weather_report",
                "Weather forecast for London",
            ),
            (
                "get_system_info",
                {"category": "memory"},
                "Memory: 32.0 GB total, 18.5 GB used (57.8%), 13.5 GB available",
                "system_info",
                "System info: memory",
            ),
            (
                "find_files",
                {"action": "search", "pattern": "*.pdf"},
                "Found 12 files matching '*.pdf':\n/home/user/docs/report.pdf\n/home/user/docs/invoice.pdf\n...",
                "file_search",
                "File search: *.pdf",
            ),
            (
                "find_files",
                {"action": "count_files", "directory": "documents"},
                "Directory /home/user/documents contains 247 files across 18 subdirectories.",
                "file_search",
                "File count_files",
            ),
            (
                "developer_tools",
                {"action": "git_status"},
                "Repository: ~/jarvis (master)\n  Modified: 3 files\n  Untracked: 1 file\n  Staged: 0 files",
                "dev_tool_output",
                "Developer: git_status",
            ),
            (
                "developer_tools",
                {"action": "codebase_search", "pattern": "def handle_intent"},
                "Found 5 matches for 'def handle_intent':\n  core/skill_manager.py:142\n  skills/system/weather/skill.py:38\n...",
                "dev_tool_output",
                "Developer: codebase_search",
            ),
            (
                "manage_reminders",
                {"action": "list"},
                "Upcoming reminders (3):\n1. Call dentist — Tomorrow at 9:00 AM\n2. Take out trash — Tonight at 7:00 PM\n3. Meeting — Friday at 2:00 PM",
                "reminder_result",
                "Reminders: list",
            ),
            (
                "manage_reminders",
                {"action": "add", "title": "Pick up groceries", "time_text": "5 PM today"},
                "Reminder set: 'Pick up groceries' for today at 5:00 PM.",
                "reminder_result",
                "Reminders: add",
            ),
            (
                "get_news",
                {"action": "read"},
                "Top headlines (5 unread):\n1. [CYBER] Major vulnerability discovered in popular library\n2. [TECH] New chip architecture announced\n3. [GENERAL] Weather pattern shifts expected\n4. [POLITICS] Senate votes on new bill\n5. [TECH] Open source project reaches milestone",
                "news_headlines",
                "News: read",
            ),
            (
                "get_news",
                {"action": "count"},
                "Unread headlines by category:\n  tech: 12\n  cyber: 5\n  politics: 3\n  general: 8\n  local: 2\nTotal: 30 unread",
                "news_headlines",
                "News: count",
            ),
            (
                "get_news",
                {"action": "read", "category": "cyber"},
                "Cyber headlines (5 unread):\n1. Critical zero-day in widely-used library\n2. Ransomware group targets healthcare\n3. New phishing campaign uses AI-generated content",
                "news_headlines",
                "News: read (cyber)",
            ),
        ]

        for tool_name, tool_args, tool_result, expected_type, expected_summary in test_cases:
            cs = make_conv_state(turn=1)
            art_id = store_tool_artifact(
                tool_name, tool_args, tool_result, cache, cs,
            )
            assert_not_none(
                f"store_{tool_name}({tool_args})",
                art_id,
                detail=f"store_tool_artifact returned None for {tool_name}",
            )
            if art_id:
                art = cache.get_by_id(art_id)
                assert_eq(
                    f"type_{tool_name}({tool_args.get('action', tool_args.get('query_type', ''))})",
                    art.artifact_type, expected_type,
                )
                assert_eq(
                    f"summary_{tool_name}({tool_args.get('action', tool_args.get('query_type', ''))})",
                    art.summary, expected_summary,
                )
                assert_eq(
                    f"source_{tool_name}",
                    art.source, tool_name,
                )
                assert_eq(
                    f"provenance_{tool_name}",
                    art.provenance, {"tool_args": tool_args},
                )

        # --- 1.2: Skip conditions ---
        log("\n── 1.2: Skip conditions ──")

        cs = make_conv_state(turn=2)

        # Error prefix
        art_id = store_tool_artifact(
            "get_weather", {"query_type": "current"},
            "Error: API key not set", cache, cs,
        )
        assert_none("skip_error_prefix", art_id)

        # BLOCKED prefix
        art_id = store_tool_artifact(
            "developer_tools", {"action": "run_command", "command": "rm -rf /"},
            "BLOCKED: This command is classified as destructive.", cache, cs,
        )
        assert_none("skip_blocked_prefix", art_id)

        # CONFIRMATION REQUIRED prefix
        art_id = store_tool_artifact(
            "developer_tools", {"action": "run_command", "command": "apt update"},
            "CONFIRMATION REQUIRED: Run 'apt update'?", cache, cs,
        )
        assert_none("skip_confirmation_prefix", art_id)

        # Too short result
        art_id = store_tool_artifact(
            "get_system_info", {"category": "hostname"},
            "hostname: jarvis-pc", cache, cs,
        )
        assert_none("skip_short_result", art_id,
                     detail=f"expected None for 19-char result, got {art_id}")

        # Empty result
        art_id = store_tool_artifact(
            "get_weather", {"query_type": "current"},
            "", cache, cs,
        )
        assert_none("skip_empty_result", art_id)

        # No cache
        art_id = store_tool_artifact(
            "get_weather", {"query_type": "current"},
            "Some weather data that is long enough to store",
            None, cs,
        )
        assert_none("skip_no_cache", art_id)

        # No conv_state
        art_id = store_tool_artifact(
            "get_weather", {"query_type": "current"},
            "Some weather data that is long enough to store",
            cache, None,
        )
        assert_none("skip_no_conv_state", art_id)

        # Unknown tool
        art_id = store_tool_artifact(
            "unknown_tool", {"query": "test"},
            "Some result data that is definitely long enough",
            cache, cs,
        )
        assert_none("skip_unknown_tool", art_id)

        # --- 1.3: Metadata correctness ---
        log("\n── 1.3: Metadata & provenance ──")

        cs = make_conv_state(turn=5, window_id="meta_test_window")

        art_id = store_tool_artifact(
            "get_weather",
            {"query_type": "forecast", "location": "Paris"},
            "3-day forecast for Paris: Mon 60F, Tue 65F, Wed 58F with rain expected.",
            cache, cs, user_id="testuser",
        )
        art = cache.get_by_id(art_id)
        assert_eq("metadata_turn_id", art.turn_id, 5)
        assert_eq("metadata_window_id", art.window_id, "meta_test_window")
        assert_eq("metadata_user_id", art.user_id, "testuser")
        assert_eq("metadata_tier", art.tier, "hot")
        assert_eq("metadata_tool_name", art.metadata.get("tool_name"), "get_weather")
        assert_true("metadata_created_at", art.created_at > 0)
        assert_eq("metadata_parent_id", art.parent_id, None)
        assert_eq("metadata_item_index", art.item_index, 0)

        # --- 1.4: Window ID generation ---
        log("\n── 1.4: Window ID auto-generation ──")

        cs_no_wid = make_conv_state(turn=1, window_id="")
        art_id = store_tool_artifact(
            "get_system_info", {"category": "cpu"},
            "CPU: AMD Ryzen 9 5900X (12 cores / 24 threads, 3.7 GHz base, 4.8 GHz boost)",
            cache, cs_no_wid,
        )
        assert_not_none("window_id_generated", art_id)
        assert_true("window_id_nonempty", len(cs_no_wid.window_id) > 0,
                     detail=f"window_id should be generated, got '{cs_no_wid.window_id}'")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# PART 2: _TYPE_REFERENCE_PATTERNS matching
# ═══════════════════════════════════════════════════════════════════════════

def test_part2():
    log("\n═══ Part 2: _TYPE_REFERENCE_PATTERNS matching ═══")

    from core.conversation_router import ConversationRouter

    patterns = ConversationRouter._TYPE_REFERENCE_PATTERNS

    # Test cases: (phrase, expected_artifact_type, expected_keyword)
    test_cases = [
        # Existing patterns
        ("those results", "search_result_set", None),
        ("the search results", "search_result_set", None),
        ("that recipe", None, "recipe"),
        ("the recipe", None, "recipe"),
        ("that weather", "weather_report", None),
        ("the forecast", "weather_report", None),
        ("that article", None, "article"),
        # New tool artifact patterns
        ("those files", "file_search", None),
        ("the files", "file_search", None),
        ("that system info", "system_info", None),
        ("the hardware info", "system_info", None),
        ("the git status", "dev_tool_output", None),
        ("that diff", "dev_tool_output", None),
        ("that log", "dev_tool_output", None),
        ("my reminders", "reminder_result", None),
        ("the reminders", "reminder_result", None),
        ("those reminders", "reminder_result", None),
        ("those headlines", "news_headlines", None),
        ("the news", "news_headlines", None),
        ("the headlines", "news_headlines", None),
    ]

    for phrase, expected_type, expected_keyword in test_cases:
        matched = False
        for pattern, art_type, kw_filter in patterns:
            if pattern.search(phrase):
                assert_eq(
                    f"type_pattern '{phrase}'",
                    art_type, expected_type,
                    context=f"keyword={kw_filter}",
                )
                assert_eq(
                    f"keyword_pattern '{phrase}'",
                    kw_filter, expected_keyword,
                )
                matched = True
                break
        if not matched:
            assert_true(
                f"pattern_match '{phrase}'",
                False,
                detail=f"no pattern matched for '{phrase}'",
            )

    # Negative cases — should NOT match any type pattern
    # Note: "what's the weather like" and "tell me the news" DO match because
    # they contain "the weather" / "the news". This is acceptable — the router's
    # P3.5 handler only acts if a cached artifact exists, and returns the cached
    # result (which is often the desired behavior for follow-ups).
    negatives = [
        "find my files",            # command, not reference
        "set a reminder",           # command
        "hello there",
        "how's the CPU doing",      # question but no pattern match
    ]

    for phrase in negatives:
        matched_any = False
        for pattern, art_type, kw_filter in patterns:
            if pattern.search(phrase):
                matched_any = True
                break
        assert_true(
            f"no_match '{phrase}'",
            not matched_any,
            detail=f"should not match any type pattern but matched {art_type}",
        )


# ═══════════════════════════════════════════════════════════════════════════
# PART 3: Integration — store + retrieve by type
# ═══════════════════════════════════════════════════════════════════════════

def test_part3():
    log("\n═══ Part 3: Integration — store + retrieve by type ═══")

    cache, tmpdir = make_cache()
    try:
        wid = "integration_test_001"
        cs = make_conv_state(turn=1, window_id=wid)

        # Store artifacts from multiple tools in sequence
        tools_data = [
            ("get_weather", {"query_type": "current"},
             "Current: 72°F, sunny, humidity 40%, wind 8mph NW. Feels like 70°F."),
            ("get_system_info", {"category": "disk"},
             "Disk usage:\n  /: 128GB / 512GB (25%)\n  /home: 256GB / 1TB (25%)\n  /mnt/storage: 200GB / 465GB (43%)"),
            ("find_files", {"action": "search", "pattern": "*.log"},
             "Found 8 files matching '*.log':\n  /var/log/syslog\n  /var/log/auth.log\n  /home/user/jarvis.log\n  ..."),
            ("developer_tools", {"action": "git_status"},
             "Repository: ~/jarvis (master)\n  Modified: core/pipeline.py, core/interaction_cache.py\n  Untracked: scripts/test_tool_artifacts.py"),
            ("manage_reminders", {"action": "list"},
             "Upcoming reminders (2):\n  1. Team meeting — Today at 3:00 PM\n  2. Dentist appointment — Tomorrow at 10:00 AM"),
            ("get_news", {"action": "read", "category": "cyber"},
             "Cyber headlines (3 unread):\n  1. [P1] Critical zero-day in OpenSSL\n  2. [P2] APT group targets energy sector\n  3. [P3] New browser exploit chain discovered"),
        ]

        stored_ids = {}
        for tool_name, args, result in tools_data:
            cs.turn_count += 1
            art_id = store_tool_artifact(tool_name, args, result, cache, cs)
            assert_not_none(f"integration_store_{tool_name}", art_id)
            stored_ids[tool_name] = art_id

        # Retrieve by type
        type_checks = [
            ("weather_report", "get_weather"),
            ("system_info", "get_system_info"),
            ("file_search", "find_files"),
            ("dev_tool_output", "developer_tools"),
            ("reminder_result", "manage_reminders"),
            ("news_headlines", "get_news"),
        ]

        for art_type, expected_source in type_checks:
            art = cache.get_latest(wid, artifact_type=art_type)
            assert_not_none(
                f"retrieve_{art_type}",
                art,
                detail=f"get_latest(type={art_type}) returned None",
            )
            if art:
                assert_eq(f"source_{art_type}", art.source, expected_source)

        # Retrieve all hot artifacts
        all_hot = cache.get_hot_artifacts(wid)
        assert_eq("hot_artifact_count", len(all_hot), 6)

        # Keyword search
        weather_art = cache.find_by_keyword(wid, "sunny")
        assert_not_none("keyword_sunny", weather_art)
        if weather_art:
            assert_eq("keyword_sunny_type", weather_art.artifact_type, "weather_report")

        cyber_art = cache.find_by_keyword(wid, "OpenSSL")
        assert_not_none("keyword_openssl", cyber_art)
        if cyber_art:
            assert_eq("keyword_openssl_type", cyber_art.artifact_type, "news_headlines")

        # Demote + promote lifecycle
        cache.demote_window(wid)
        hot_after = cache.get_hot_artifacts(wid)
        assert_eq("hot_after_demote", len(hot_after), 0)

        promoted = cache.promote_window(wid)
        assert_eq("promoted_count", len(promoted), 6)

        # Cross-session search
        cold = cache.search_cold(keyword="OpenSSL")
        assert_true("cold_search_openssl", len(cold) > 0,
                     detail="search_cold should find OpenSSL artifact")

        # Multi-tool same turn — both stored
        cs2 = make_conv_state(turn=10, window_id="multi_tool_window")
        id1 = store_tool_artifact(
            "get_weather", {"query_type": "current"},
            "Current temperature: 65°F, partly cloudy conditions with light winds.",
            cache, cs2,
        )
        id2 = store_tool_artifact(
            "get_system_info", {"category": "cpu"},
            "CPU: AMD Ryzen 9 5900X, 12 cores, 24 threads, base 3.7GHz, boost 4.8GHz",
            cache, cs2,
        )
        assert_not_none("multi_tool_weather", id1)
        assert_not_none("multi_tool_sysinfo", id2)
        assert_true("multi_tool_different_ids", id1 != id2)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# PART 4: _TOOL_ARTIFACT_META completeness
# ═══════════════════════════════════════════════════════════════════════════

def test_part4():
    log("\n═══ Part 4: Meta mapping completeness ═══")

    expected_tools = {
        "get_weather", "get_system_info", "find_files",
        "developer_tools", "manage_reminders", "get_news",
    }

    # All expected tools have mappings
    for tool in expected_tools:
        assert_true(
            f"meta_has_{tool}",
            tool in _TOOL_ARTIFACT_META,
            detail=f"{tool} missing from _TOOL_ARTIFACT_META",
        )

    # Each mapping has (str, callable)
    for tool_name, (art_type, summary_fn) in _TOOL_ARTIFACT_META.items():
        assert_true(
            f"meta_type_str_{tool_name}",
            isinstance(art_type, str) and len(art_type) > 0,
            detail=f"artifact_type should be non-empty string, got {art_type!r}",
        )
        assert_true(
            f"meta_summary_callable_{tool_name}",
            callable(summary_fn),
            detail=f"summary_fn should be callable for {tool_name}",
        )

    # Summary functions don't crash with empty args
    for tool_name, (art_type, summary_fn) in _TOOL_ARTIFACT_META.items():
        try:
            result = summary_fn({}, "")
            assert_true(
                f"meta_summary_empty_{tool_name}",
                isinstance(result, str),
                detail=f"summary_fn({{}}) should return str, got {type(result)}",
            )
        except Exception as e:
            assert_true(
                f"meta_summary_empty_{tool_name}",
                False,
                detail=f"summary_fn crashed with empty args: {e}",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("Tool Artifact Wiring — Test Suite\n")

    test_part1()
    test_part2()
    test_part3()
    test_part4()

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
