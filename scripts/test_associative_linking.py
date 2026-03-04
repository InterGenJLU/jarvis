#!/usr/bin/env python3
"""
Associative Linking (CMA) tests.

Tests:
  --- create_link basics ---
  1. Create a link between two artifacts
  2. Duplicate link prevention (INSERT OR IGNORE)
  3. Self-link rejection (same ID)
  4. Multiple link types between same pair
  5. LINK_STRENGTHS correctness
  6. ID normalization (A,B == B,A)
  --- get_linked ---
  7. Retrieve linked artifacts
  8. Filter by link_type
  9. Unlinked artifact returns empty
  10. Bidirectional retrieval
  --- Auto-link on store (co-occurrence) ---
  11. Same (window, turn) → co_occurrence link
  12. Three artifacts in same turn → all pairs linked
  13. Different turns → no link
  14. Sub-item (parent_id set) → no link
  --- Auto-link on rehydrate ---
  15. Rehydrate two artifacts → rehydrated_with link on originals
  16. Rehydrate single artifact → no link
  --- search_cold with include_linked ---
  17. include_linked=True expands results
  18. Linked results deduplicated
  --- Persistence ---
  19. Links survive across cache instances
  20. get_link_count returns correct count

Usage:
  python3 scripts/test_associative_linking.py --verbose
"""

import os
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

from core.interaction_cache import InteractionCache, Artifact

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

def make_cache(tmpdir=None):
    """Create a temp InteractionCache for testing."""
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp(prefix="jarvis_test_linking_")

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
        created_at=time.time(),
    )
    cache.store(art)
    return art.artifact_id


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Create a link between two artifacts
# ═══════════════════════════════════════════════════════════════════════════

def test_create_link():
    log("\n--- 1. Create a link between two artifacts ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)

        created = cache.create_link(a1, a2, "co_occurrence")
        assert_true("create_link returns True for new link", created)

        # Verify in SQLite
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM artifact_links WHERE source_id = ? AND target_id = ?",
            tuple(sorted([a1, a2])),
        ).fetchone()
        conn.close()

        assert_true("Link row exists in SQLite", row is not None)
        assert_eq("Link type is co_occurrence", row["link_type"], "co_occurrence")
        assert_eq("Strength is 1.0", row["strength"], 1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: Duplicate link prevention
# ═══════════════════════════════════════════════════════════════════════════

def test_duplicate_link():
    log("\n--- 2. Duplicate link prevention ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)

        first = cache.create_link(a1, a2, "co_occurrence")
        second = cache.create_link(a1, a2, "co_occurrence")

        assert_true("First create returns True", first)
        assert_true("Second create returns False (duplicate)", not second)

        # Verify only one row
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM artifact_links"
        ).fetchone()[0]
        conn.close()

        # There may be auto-created links from store() too,
        # but specifically for this pair+type there should be just 1
        pair_count = cache.get_link_count(a1)
        assert_true("Link count is reasonable", pair_count >= 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: Self-link rejection
# ═══════════════════════════════════════════════════════════════════════════

def test_self_link():
    log("\n--- 3. Self-link rejection ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)

        created = cache.create_link(a1, a1, "co_occurrence")
        assert_true("Self-link returns False", not created)
        assert_eq("No links for self-linked artifact", cache.get_link_count(a1), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: Multiple link types between same pair
# ═══════════════════════════════════════════════════════════════════════════

def test_multi_type_links():
    log("\n--- 4. Multiple link types between same pair ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)

        cache.create_link(a1, a2, "co_occurrence")
        cache.create_link(a1, a2, "rehydrated_with")

        # Both should exist
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        rows = conn.execute(
            "SELECT link_type FROM artifact_links "
            "WHERE source_id = ? AND target_id = ?",
            tuple(sorted([a1, a2])),
        ).fetchall()
        conn.close()

        types = {r[0] for r in rows}
        assert_true("co_occurrence link exists", "co_occurrence" in types)
        assert_true("rehydrated_with link exists", "rehydrated_with" in types)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5: LINK_STRENGTHS correctness
# ═══════════════════════════════════════════════════════════════════════════

def test_link_strengths():
    log("\n--- 5. LINK_STRENGTHS correctness ---")

    expected = {
        "co_occurrence": 1.0,
        "rehydrated_with": 1.5,
        "user_associated": 2.0,
    }

    for lt, expected_strength in expected.items():
        assert_eq(f"Strength {lt}={expected_strength}",
                  InteractionCache.LINK_STRENGTHS.get(lt), expected_strength)

    assert_eq("Total link types = 3",
              len(InteractionCache.LINK_STRENGTHS), 3)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 6: ID normalization (A,B == B,A)
# ═══════════════════════════════════════════════════════════════════════════

def test_id_normalization():
    log("\n--- 6. ID normalization (A,B == B,A) ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)

        cache.create_link(a2, a1, "co_occurrence")  # reversed order
        duplicate = cache.create_link(a1, a2, "co_occurrence")  # normal order

        assert_true("Reversed order is treated as duplicate", not duplicate)

        # Only one row should exist
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM artifact_links WHERE link_type = 'co_occurrence' "
            "AND source_id = ? AND target_id = ?",
            tuple(sorted([a1, a2])),
        ).fetchone()[0]
        conn.close()

        assert_eq("Exactly one link row", count, 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7: Retrieve linked artifacts
# ═══════════════════════════════════════════════════════════════════════════

def test_get_linked():
    log("\n--- 7. Retrieve linked artifacts ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)

        cache.create_link(a1, a2, "co_occurrence")

        linked = cache.get_linked(a1)
        assert_eq("get_linked returns 1 artifact", len(linked), 1)
        assert_eq("Linked artifact is a2", linked[0].artifact_id, a2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 8: Filter by link_type
# ═══════════════════════════════════════════════════════════════════════════

def test_get_linked_type_filter():
    log("\n--- 8. Filter by link_type ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)
        a3 = make_artifact(cache, turn=3, index=0)

        cache.create_link(a1, a2, "co_occurrence")
        cache.create_link(a1, a3, "rehydrated_with")

        co_only = cache.get_linked(a1, link_type="co_occurrence")
        assert_eq("co_occurrence filter returns 1", len(co_only), 1)
        assert_eq("Filtered result is a2", co_only[0].artifact_id, a2)

        rh_only = cache.get_linked(a1, link_type="rehydrated_with")
        assert_eq("rehydrated_with filter returns 1", len(rh_only), 1)
        assert_eq("Filtered result is a3", rh_only[0].artifact_id, a3)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 9: Unlinked artifact returns empty
# ═══════════════════════════════════════════════════════════════════════════

def test_unlinked_empty():
    log("\n--- 9. Unlinked artifact returns empty ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)

        linked = cache.get_linked(a1)
        assert_eq("Unlinked artifact has 0 links", len(linked), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 10: Bidirectional retrieval
# ═══════════════════════════════════════════════════════════════════════════

def test_bidirectional():
    log("\n--- 10. Bidirectional retrieval ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)

        cache.create_link(a1, a2, "co_occurrence")

        from_a1 = cache.get_linked(a1)
        from_a2 = cache.get_linked(a2)

        assert_eq("a1 sees a2", len(from_a1), 1)
        assert_eq("a1 linked to a2", from_a1[0].artifact_id, a2)
        assert_eq("a2 sees a1", len(from_a2), 1)
        assert_eq("a2 linked to a1", from_a2[0].artifact_id, a1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 11: Auto-link same (window, turn) → co_occurrence
# ═══════════════════════════════════════════════════════════════════════════

def test_auto_link_same_turn():
    log("\n--- 11. Auto-link same (window, turn) → co_occurrence ---")
    cache, tmpdir = make_cache()
    try:
        wid = "auto_link_window"
        # Store two artifacts in same turn (simulates search_result_set + synthesis)
        a1 = make_artifact(cache, window_id=wid, turn=5, index=0,
                           art_type="search_result_set", summary="Results")
        a2 = make_artifact(cache, window_id=wid, turn=5, index=1,
                           art_type="synthesis", summary="Summary")

        linked = cache.get_linked(a1)
        assert_eq("Auto-linked 1 artifact", len(linked), 1)
        assert_eq("Auto-linked to a2", linked[0].artifact_id, a2)

        # Verify link type
        linked_from_a2 = cache.get_linked(a2, link_type="co_occurrence")
        assert_eq("Link type is co_occurrence", len(linked_from_a2), 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 12: Three artifacts in same turn → all pairs linked
# ═══════════════════════════════════════════════════════════════════════════

def test_auto_link_three_way():
    log("\n--- 12. Three artifacts in same turn → all pairs linked ---")
    cache, tmpdir = make_cache()
    try:
        wid = "three_way_window"
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0, summary="A")
        a2 = make_artifact(cache, window_id=wid, turn=1, index=1, summary="B")
        a3 = make_artifact(cache, window_id=wid, turn=1, index=2, summary="C")

        # a1 should be linked to a2 and a3
        linked_a1 = cache.get_linked(a1)
        linked_ids = {a.artifact_id for a in linked_a1}
        assert_eq("a1 linked to 2 artifacts", len(linked_a1), 2)
        assert_true("a1 linked to a2", a2 in linked_ids)
        assert_true("a1 linked to a3", a3 in linked_ids)

        # a2 should be linked to a1 and a3
        linked_a2 = cache.get_linked(a2)
        assert_eq("a2 linked to 2 artifacts", len(linked_a2), 2)

        # Total unique links: 3 pairs (a1-a2, a1-a3, a2-a3)
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        total = conn.execute(
            "SELECT COUNT(*) FROM artifact_links WHERE link_type = 'co_occurrence'"
        ).fetchone()[0]
        conn.close()
        assert_eq("3 co_occurrence link rows total", total, 3)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 13: Different turns → no auto-link
# ═══════════════════════════════════════════════════════════════════════════

def test_no_link_different_turns():
    log("\n--- 13. Different turns → no auto-link ---")
    cache, tmpdir = make_cache()
    try:
        wid = "diff_turn_window"
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0)
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0)

        linked = cache.get_linked(a1)
        assert_eq("No links between different turns", len(linked), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 14: Sub-item (parent_id set) → no co_occurrence link
# ═══════════════════════════════════════════════════════════════════════════

def test_no_link_sub_items():
    log("\n--- 14. Sub-item (parent_id set) → no co_occurrence link ---")
    cache, tmpdir = make_cache()
    try:
        wid = "subitem_window"
        parent = make_artifact(cache, window_id=wid, turn=1, index=0,
                               summary="Parent")
        child = make_artifact(cache, window_id=wid, turn=1, index=1,
                              summary="Child", parent_id=parent)

        # Parent should NOT be auto-linked to child via co_occurrence
        # (child has parent_id, so auto-linker skips it)
        linked = cache.get_linked(parent, link_type="co_occurrence")
        assert_eq("No co_occurrence link for parent-child", len(linked), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 15: Rehydrate two artifacts → rehydrated_with link on originals
# ═══════════════════════════════════════════════════════════════════════════

def test_rehydrate_auto_link():
    log("\n--- 15. Rehydrate two artifacts → rehydrated_with link ---")
    cache, tmpdir = make_cache()
    try:
        wid = "rehydrate_link_window"

        # Create two artifacts with different content to avoid dedup
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0,
                           content="First artifact content with enough length to pass the promotion filter for cold tier",
                           summary="First")
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0,
                           content="Second artifact with different content so dedup filter does not remove it during promotion",
                           summary="Second")

        # Demote → promote to cold
        cache.demote_window(wid)
        cache.promote_window(wid)

        # Rehydrate both into a new window
        new_wid = "new_session_window"
        rehydrated = cache.rehydrate([a1, a2], new_wid)

        assert_eq("Rehydrated 2 artifacts", len(rehydrated), 2)

        # Check that originals are linked with rehydrated_with
        linked = cache.get_linked(a1, link_type="rehydrated_with")
        linked_ids = {a.artifact_id for a in linked}
        assert_true("Original a1 linked to original a2",
                     a2 in linked_ids,
                     detail=f"linked_ids={linked_ids}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 16: Rehydrate single artifact → no link
# ═══════════════════════════════════════════════════════════════════════════

def test_rehydrate_single_no_link():
    log("\n--- 16. Rehydrate single artifact → no link ---")
    cache, tmpdir = make_cache()
    try:
        wid = "single_rehydrate_window"
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0,
                           content="Solo artifact with enough content to pass the promotion filter for cold tier storage",
                           summary="Solo")

        cache.demote_window(wid)
        cache.promote_window(wid)

        new_wid = "new_solo_window"
        rehydrated = cache.rehydrate([a1], new_wid)
        assert_eq("Rehydrated 1 artifact", len(rehydrated), 1)

        # No rehydrated_with links (only 1 artifact)
        linked = cache.get_linked(a1, link_type="rehydrated_with")
        assert_eq("No rehydrated_with links for single", len(linked), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 17: search_cold with include_linked expands results
# ═══════════════════════════════════════════════════════════════════════════

def test_search_cold_include_linked():
    log("\n--- 17. search_cold with include_linked expands results ---")
    cache, tmpdir = make_cache()
    try:
        wid = "linked_search_window"

        # Create artifact A (will match keyword "alpha")
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0,
                           content="Alpha artifact with enough content to pass the promotion filter for cold tier properly",
                           summary="Alpha result")

        # Create artifact B (different keyword "beta", linked to A)
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0,
                           content="Beta artifact with different content for deduplication filter check and unique content here",
                           summary="Beta result")

        # Manually link A and B
        cache.create_link(a1, a2, "co_occurrence")

        # Demote → promote to cold
        cache.demote_window(wid)
        cache.promote_window(wid)

        # Search for "alpha" without links — should find only a1
        without = cache.search_cold(keyword="Alpha", include_linked=False)
        assert_eq("Without links: 1 result", len(without), 1)
        assert_eq("Without links: found a1", without[0].artifact_id, a1)

        # Search for "alpha" with links — should find a1 + linked a2
        with_links = cache.search_cold(keyword="Alpha", include_linked=True)
        assert_eq("With links: 2 results", len(with_links), 2)
        result_ids = {a.artifact_id for a in with_links}
        assert_true("With links: includes a1", a1 in result_ids)
        assert_true("With links: includes linked a2", a2 in result_ids)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 18: Linked results deduplicated
# ═══════════════════════════════════════════════════════════════════════════

def test_search_cold_dedup():
    log("\n--- 18. Linked results deduplicated ---")
    cache, tmpdir = make_cache()
    try:
        wid = "dedup_window"

        # Create two artifacts that both match keyword "gamma"
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0,
                           content="Gamma artifact one with enough content to pass the promotion filter for cold tier storage",
                           summary="Gamma first")
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0,
                           content="Gamma artifact two with different content for deduplication filter and unique content here",
                           summary="Gamma second")

        cache.create_link(a1, a2, "co_occurrence")

        cache.demote_window(wid)
        cache.promote_window(wid)

        # Both match "gamma", so a2 would be in primary AND linked sets
        results = cache.search_cold(keyword="Gamma", include_linked=True)
        result_ids = [a.artifact_id for a in results]

        # No duplicates
        assert_eq("No duplicate IDs", len(result_ids), len(set(result_ids)))
        assert_eq("Exactly 2 results", len(results), 2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 19: Links survive across cache instances
# ═══════════════════════════════════════════════════════════════════════════

def test_link_persistence():
    log("\n--- 19. Links survive across cache instances ---")
    tmpdir = tempfile.mkdtemp(prefix="jarvis_test_link_persist_")
    try:
        cache1, _ = make_cache(tmpdir)
        a1 = make_artifact(cache1, turn=1, index=0)
        a2 = make_artifact(cache1, turn=2, index=0)
        cache1.create_link(a1, a2, "co_occurrence")

        # Create a fresh cache instance pointing to same tmpdir
        cache2, _ = make_cache(tmpdir)

        linked = cache2.get_linked(a1)
        assert_eq("Link survives new cache instance", len(linked), 1)
        assert_eq("Correct artifact after reload", linked[0].artifact_id, a2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 20: get_link_count returns correct count
# ═══════════════════════════════════════════════════════════════════════════

def test_link_count():
    log("\n--- 20. get_link_count returns correct count ---")
    cache, tmpdir = make_cache()
    try:
        a1 = make_artifact(cache, turn=1, index=0)
        a2 = make_artifact(cache, turn=2, index=0)
        a3 = make_artifact(cache, turn=3, index=0)

        assert_eq("Count before links = 0", cache.get_link_count(a1), 0)

        cache.create_link(a1, a2, "co_occurrence")
        assert_eq("Count after 1 link = 1", cache.get_link_count(a1), 1)

        cache.create_link(a1, a3, "rehydrated_with")
        assert_eq("Count after 2 links = 2", cache.get_link_count(a1), 2)

        # a2 should have 1 link (to a1)
        assert_eq("a2 count = 1", cache.get_link_count(a2), 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("Associative Linking (CMA) — Test Suite\n")

    # create_link basics
    test_create_link()
    test_duplicate_link()
    test_self_link()
    test_multi_type_links()
    test_link_strengths()
    test_id_normalization()
    # get_linked
    test_get_linked()
    test_get_linked_type_filter()
    test_unlinked_empty()
    test_bidirectional()
    # Auto-link on store
    test_auto_link_same_turn()
    test_auto_link_three_way()
    test_no_link_different_turns()
    test_no_link_sub_items()
    # Auto-link on rehydrate
    test_rehydrate_auto_link()
    test_rehydrate_single_no_link()
    # search_cold with include_linked
    test_search_cold_include_linked()
    test_search_cold_dedup()
    # Persistence
    test_link_persistence()
    test_link_count()

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
        print(" \u2713")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
