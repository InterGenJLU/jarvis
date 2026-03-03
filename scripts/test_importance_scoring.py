#!/usr/bin/env python3
"""
Importance Scoring (CMA Selective Retention) tests.

Tests:
  1. Default importance score
  2. record_access increments with correct weights
  3. Weight table correctness (cumulative multi-type)
  4. Parent bubbling (50% to parent)
  5. Unknown access type fallback
  6. Promotion ranking by importance
  7. Cold-tier search ranking by importance
  8. Rehydrate boosts original cold artifact
  9. SQLite persistence of scores

Usage:
  python3 scripts/test_importance_scoring.py --verbose
"""

import os
import sys
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

from core.interaction_cache import InteractionCache, Artifact

ACCESS_WEIGHTS = InteractionCache.ACCESS_WEIGHTS

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


# ─── Helpers ──────────────────────────────────────────────────────────────

def make_cache():
    """Create a temp InteractionCache for testing."""
    tmpdir = tempfile.mkdtemp(prefix="jarvis_test_importance_")

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


def make_artifact(cache, window_id="test_window", turn=1, index=0,
                  art_type="search_result_set",
                  content="Test content that is long enough to pass the 50-char promotion filter in promote_window",
                  summary="Test", source="test", parent_id=None):
    """Create and store an artifact, return its ID."""
    art = Artifact(
        artifact_id=f"art_{window_id}_{turn}_{index}",
        turn_id=turn,
        item_index=index,
        artifact_type=art_type,
        content=content,
        summary=summary,
        source=source,
        parent_id=parent_id,
        window_id=window_id,
        created_at=__import__("time").time(),
    )
    cache.store(art)
    return art.artifact_id


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Default importance score
# ═══════════════════════════════════════════════════════════════════════════

def test_default_score():
    log("\n--- 1. Default importance score ---")
    cache, tmpdir = make_cache()
    try:
        aid = make_artifact(cache)
        art = cache.get_by_id(aid)
        assert_eq("New artifact starts at 1.0", art.importance_score, 1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: record_access increments score
# ═══════════════════════════════════════════════════════════════════════════

def test_record_access():
    log("\n--- 2. record_access increments score ---")
    cache, tmpdir = make_cache()
    try:
        aid = make_artifact(cache)

        cache.record_access(aid, "ordinal_reference")  # +3.0
        art = cache.get_by_id(aid)
        assert_eq("ordinal_reference adds 3.0", art.importance_score, 4.0)

        cache.record_access(aid, "recency_reference")  # +1.5
        art = cache.get_by_id(aid)
        assert_eq("recency_reference adds 1.5 (cumulative 5.5)",
                   art.importance_score, 5.5)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: Weight table correctness
# ═══════════════════════════════════════════════════════════════════════════

def test_weight_table():
    log("\n--- 3. Weight table correctness ---")
    cache, tmpdir = make_cache()
    try:
        aid = make_artifact(cache)

        # Apply multiple access types: 3.0 + 2.0 + 1.5 + 1.5 = 8.0 + base 1.0 = 9.0
        cache.record_access(aid, "ordinal_reference")   # +3.0
        cache.record_access(aid, "type_reference")       # +2.0
        cache.record_access(aid, "recency_reference")    # +1.5
        cache.record_access(aid, "readback_repeat")      # +1.5

        art = cache.get_by_id(aid)
        assert_eq("Cumulative multi-type scoring = 9.0",
                   art.importance_score, 9.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: Parent bubbling (50% to parent)
# ═══════════════════════════════════════════════════════════════════════════

def test_parent_bubbling():
    log("\n--- 4. Parent bubbling (50% to parent) ---")
    cache, tmpdir = make_cache()
    try:
        parent_id = make_artifact(cache, turn=1, index=0, summary="Parent")
        child_id = make_artifact(cache, turn=1, index=1, summary="Child",
                                 parent_id=parent_id)

        # Access child with ordinal_reference (+3.0 to child, +1.5 to parent)
        cache.record_access(child_id, "ordinal_reference")

        child = cache.get_by_id(child_id)
        parent = cache.get_by_id(parent_id)
        assert_eq("Child gets full weight (4.0)", child.importance_score, 4.0)
        assert_eq("Parent gets 50% bubble (2.5)", parent.importance_score, 2.5)

        # Second access on child: nav_advance (+1.0 child, +0.5 parent)
        cache.record_access(child_id, "nav_advance")

        child = cache.get_by_id(child_id)
        parent = cache.get_by_id(parent_id)
        assert_eq("Child cumulative (5.0)", child.importance_score, 5.0)
        assert_eq("Parent cumulative (3.0)", parent.importance_score, 3.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5: Unknown access type fallback
# ═══════════════════════════════════════════════════════════════════════════

def test_unknown_access_type():
    log("\n--- 5. Unknown access type fallback ---")
    cache, tmpdir = make_cache()
    try:
        aid = make_artifact(cache)
        cache.record_access(aid, "totally_unknown_type")
        art = cache.get_by_id(aid)
        assert_eq("Unknown type defaults to +1.0", art.importance_score, 2.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 6: Promotion ranking by importance
# ═══════════════════════════════════════════════════════════════════════════

def test_promotion_ranking():
    log("\n--- 6. Promotion ranking by importance ---")
    cache, tmpdir = make_cache()
    try:
        wid = "promote_test_window"

        # Create 3 artifacts with different importance
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0, summary="Low",
                           content="Low importance artifact with enough content to pass the promotion filter easily")
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0, summary="Medium",
                           content="Medium importance artifact with different content so it passes dedup filter check")
        a3 = make_artifact(cache, window_id=wid, turn=3, index=0, summary="High",
                           content="High importance artifact with unique content that will rank first after scoring")

        # Boost: a3 gets highest, a2 medium, a1 stays at default
        cache.record_access(a3, "rehydrate")             # +5.0 → 6.0
        cache.record_access(a3, "generic_followup")       # +2.5 → 8.5
        cache.record_access(a2, "type_reference")         # +2.0 → 3.0

        # Demote then promote
        cache.demote_window(wid)
        promoted = cache.promote_window(wid)

        assert_eq("3 artifacts promoted", len(promoted), 3)
        assert_eq("Highest importance first (8.5)",
                   promoted[0].importance_score, 8.5)
        assert_eq("Medium importance second (3.0)",
                   promoted[1].importance_score, 3.0)
        assert_eq("Lowest importance last (1.0)",
                   promoted[2].importance_score, 1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7: Cold-tier search ranking
# ═══════════════════════════════════════════════════════════════════════════

def test_cold_search_ranking():
    log("\n--- 7. Cold-tier search ranking ---")
    cache, tmpdir = make_cache()
    try:
        wid = "cold_search_window"

        a1 = make_artifact(cache, window_id=wid, turn=1, index=0,
                           content="Alpha test content with enough length to pass the promotion filter for cold tier search",
                           summary="Alpha")
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0,
                           content="Alpha test high importance unique content that differs from the first artifact for dedup",
                           summary="Alpha high")

        cache.record_access(a2, "ordinal_reference")  # +3.0 → 4.0

        # Demote → promote (to cold)
        cache.demote_window(wid)
        cache.promote_window(wid)

        cold = cache.search_cold(keyword="Alpha")
        assert_true("Cold results ordered by importance DESC",
                     len(cold) >= 2 and cold[0].importance_score >= cold[1].importance_score,
                     detail=f"got scores: {[c.importance_score for c in cold]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 8: Rehydrate boosts original cold artifact
# ═══════════════════════════════════════════════════════════════════════════

def test_rehydrate_boost():
    log("\n--- 8. Rehydrate boosts original cold artifact ---")
    cache, tmpdir = make_cache()
    try:
        wid = "rehydrate_test_window"

        aid = make_artifact(cache, window_id=wid, turn=1, index=0,
                            content="Rehydration test content with enough characters to pass the fifty char promotion filter",
                            summary="Rehydrate me")
        cache.record_access(aid, "rehydrate")             # +5.0 → 6.0
        cache.record_access(aid, "generic_followup")       # +2.5 → 8.5

        # Demote → promote to cold
        cache.demote_window(wid)
        cache.promote_window(wid)

        # Rehydrate into new window
        new_wid = "new_window_001"
        rehydrated = cache.rehydrate([aid], new_wid)

        assert_eq("Rehydration returned 1 artifact", len(rehydrated), 1)

        # Original should be boosted by another 5.0
        orig = cache.get_by_id(aid)
        assert_eq("Original boosted by 5.0 (8.5 -> 13.5)",
                   orig.importance_score, 13.5)

        # Rehydrated clone is hot tier
        assert_eq("Rehydrated clone is hot tier", rehydrated[0].tier, "hot")
        assert_true("Rehydrated clone has provenance link",
                     rehydrated[0].provenance.get("rehydrated_from") == aid)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 9: SQLite persistence check
# ═══════════════════════════════════════════════════════════════════════════

def test_sqlite_persistence():
    log("\n--- 9. SQLite persistence check ---")
    cache, tmpdir = make_cache()
    try:
        wid = "persist_window"
        parent_id = make_artifact(cache, window_id=wid, turn=1, index=0,
                                  summary="Persist parent")
        child_id = make_artifact(cache, window_id=wid, turn=1, index=1,
                                 summary="Persist child", parent_id=parent_id)

        # Build up scores
        cache.record_access(child_id, "ordinal_reference")  # child +3.0, parent +1.5
        cache.record_access(child_id, "recency_reference")  # child +1.5, parent +0.75

        # Read directly from SQLite to verify persistence
        import sqlite3
        db_path = os.path.join(tmpdir, "data", "interaction_cache.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            "SELECT importance_score FROM artifacts WHERE artifact_id = ?",
            (child_id,)
        ).fetchone()
        assert_eq("SQLite has correct score for child (5.5)",
                   row["importance_score"], 5.5)

        row = conn.execute(
            "SELECT importance_score FROM artifacts WHERE artifact_id = ?",
            (parent_id,)
        ).fetchone()
        assert_eq("SQLite has correct bubbled parent score (3.25)",
                   row["importance_score"], 3.25)

        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 10: ACCESS_WEIGHTS completeness
# ═══════════════════════════════════════════════════════════════════════════

def test_access_weights_completeness():
    log("\n--- 10. ACCESS_WEIGHTS completeness ---")

    expected_types = {
        "rehydrate": 5.0,
        "ordinal_reference": 3.0,
        "nav_jump": 3.0,
        "nav_section_drill": 3.0,
        "generic_followup": 2.5,
        "type_reference": 2.0,
        "readback_recall": 2.0,
        "readback_section": 2.0,
        "recency_reference": 1.5,
        "readback_repeat": 1.5,
        "nav_advance": 1.0,
        "nav_retreat": 1.0,
        "readback_continue": 1.0,
        "nav_reset": 0.5,
        "nav_drill_out": 0.5,
    }

    for access_type, expected_weight in expected_types.items():
        assert_eq(f"Weight {access_type}={expected_weight}",
                   ACCESS_WEIGHTS.get(access_type), expected_weight)

    assert_eq("Total access types = 15", len(ACCESS_WEIGHTS), 15)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("Importance Scoring (CMA Selective Retention) — Test Suite\n")

    test_default_score()
    test_record_access()
    test_weight_table()
    test_parent_bubbling()
    test_unknown_access_type()
    test_promotion_ranking()
    test_cold_search_ranking()
    test_rehydrate_boost()
    test_sqlite_persistence()
    test_access_weights_completeness()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f" ({failed} failed)")
        for r in results:
            if not r.passed:
                print(f"  FAIL: {r.name} — {r.detail}")
    else:
        print(" ✓")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
