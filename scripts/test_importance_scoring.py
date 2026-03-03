#!/usr/bin/env python3
"""
Importance Scoring + Retrieval-Driven Mutation (CMA) tests.

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
  10. ACCESS_WEIGHTS completeness
  --- Retrieval-driven mutation ---
  11. record_access updates last_accessed_at + access_count
  12. effective_score decay (older → lower score)
  13. effective_score frequency boost (more accesses → higher)
  14. effective_score combined (recent+frequent beats old+important)
  15. search_cold ranks by effective_score
  16. Parent bubbling updates access fields
  17. SQLite persistence of new fields

Usage:
  python3 scripts/test_importance_scoring.py --verbose
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
# TEST 11: record_access updates last_accessed_at + access_count
# ═══════════════════════════════════════════════════════════════════════════

def test_last_accessed_tracking():
    log("\n--- 11. record_access updates last_accessed_at + access_count ---")
    cache, tmpdir = make_cache()
    try:
        aid = make_artifact(cache)

        # Before any access
        art = cache.get_by_id(aid)
        assert_eq("Initial access_count = 0", art.access_count, 0)
        assert_eq("Initial last_accessed_at = 0.0", art.last_accessed_at, 0.0)

        # First access
        before = time.time()
        cache.record_access(aid, "ordinal_reference")
        after = time.time()

        art = cache.get_by_id(aid)
        assert_eq("access_count after 1 access = 1", art.access_count, 1)
        assert_true("last_accessed_at is set after access",
                     before <= art.last_accessed_at <= after,
                     detail=f"{before} <= {art.last_accessed_at} <= {after}")

        # Second access
        cache.record_access(aid, "nav_advance")
        art = cache.get_by_id(aid)
        assert_eq("access_count after 2 accesses = 2", art.access_count, 2)
        assert_true("last_accessed_at updated on second access",
                     art.last_accessed_at >= before)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 12: effective_score decay
# ═══════════════════════════════════════════════════════════════════════════

def test_effective_score_decay():
    log("\n--- 12. effective_score decay (older → lower) ---")

    now = time.time()

    # Artifact accessed just now: no decay
    fresh = Artifact(
        artifact_id="decay_fresh", turn_id=1, item_index=0,
        artifact_type="test", content="x", summary="x", source="test",
        window_id="w", created_at=now - 100, importance_score=10.0,
        last_accessed_at=now, access_count=1,
    )
    score_fresh = InteractionCache.effective_score(fresh, now)

    # Artifact accessed 7 days ago: half-life decay
    week_old = Artifact(
        artifact_id="decay_week", turn_id=1, item_index=0,
        artifact_type="test", content="x", summary="x", source="test",
        window_id="w", created_at=now - 100, importance_score=10.0,
        last_accessed_at=now - 7 * 86400, access_count=1,
    )
    score_week = InteractionCache.effective_score(week_old, now)

    # Artifact accessed 14 days ago: two half-lives
    old = Artifact(
        artifact_id="decay_old", turn_id=1, item_index=0,
        artifact_type="test", content="x", summary="x", source="test",
        window_id="w", created_at=now - 100, importance_score=10.0,
        last_accessed_at=now - 14 * 86400, access_count=1,
    )
    score_old = InteractionCache.effective_score(old, now)

    assert_true("Fresh > week-old", score_fresh > score_week,
                detail=f"{score_fresh:.2f} > {score_week:.2f}")
    assert_true("Week-old > 2-week-old", score_week > score_old,
                detail=f"{score_week:.2f} > {score_old:.2f}")

    # At half-life, score should be roughly half (modulo frequency boost)
    ratio = score_week / score_fresh
    assert_true("7-day decay ratio ≈ 0.50",
                0.45 <= ratio <= 0.55,
                detail=f"ratio={ratio:.3f}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 13: effective_score frequency boost
# ═══════════════════════════════════════════════════════════════════════════

def test_effective_score_frequency():
    log("\n--- 13. effective_score frequency boost ---")

    now = time.time()

    def make_freq_art(count):
        return Artifact(
            artifact_id=f"freq_{count}", turn_id=1, item_index=0,
            artifact_type="test", content="x", summary="x", source="test",
            window_id="w", created_at=now - 100, importance_score=10.0,
            last_accessed_at=now, access_count=count,
        )

    s0 = InteractionCache.effective_score(make_freq_art(0), now)
    s1 = InteractionCache.effective_score(make_freq_art(1), now)
    s3 = InteractionCache.effective_score(make_freq_art(3), now)
    s7 = InteractionCache.effective_score(make_freq_art(7), now)
    s15 = InteractionCache.effective_score(make_freq_art(15), now)

    assert_true("0 accesses < 1 access", s0 < s1,
                detail=f"{s0:.2f} < {s1:.2f}")
    assert_true("1 access < 3 accesses", s1 < s3,
                detail=f"{s1:.2f} < {s3:.2f}")
    assert_true("3 accesses < 7 accesses", s3 < s7,
                detail=f"{s3:.2f} < {s7:.2f}")
    assert_true("7 accesses < 15 accesses", s7 < s15,
                detail=f"{s7:.2f} < {s15:.2f}")

    # Boost is gentle — 15 accesses shouldn't more than double the score
    ratio_15 = s15 / s0
    assert_true("15-access boost < 2x", ratio_15 < 2.0,
                detail=f"ratio={ratio_15:.3f}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 14: effective_score combined — recency+frequency beats raw importance
# ═══════════════════════════════════════════════════════════════════════════

def test_effective_score_combined():
    log("\n--- 14. effective_score combined ranking ---")

    now = time.time()

    # Artifact A: moderate importance, recently accessed, frequently recalled
    art_a = Artifact(
        artifact_id="combo_a", turn_id=1, item_index=0,
        artifact_type="test", content="x", summary="x", source="test",
        window_id="w", created_at=now - 30 * 86400,
        importance_score=10.0, last_accessed_at=now - 3 * 86400,
        access_count=5,
    )

    # Artifact B: higher importance, stale (14 days), single access
    art_b = Artifact(
        artifact_id="combo_b", turn_id=1, item_index=0,
        artifact_type="test", content="x", summary="x", source="test",
        window_id="w", created_at=now - 30 * 86400,
        importance_score=12.0, last_accessed_at=now - 14 * 86400,
        access_count=1,
    )

    sa = InteractionCache.effective_score(art_a, now)
    sb = InteractionCache.effective_score(art_b, now)

    assert_true("Recent+frequent (10.0) beats stale+important (12.0)",
                sa > sb,
                detail=f"A={sa:.2f}, B={sb:.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 15: search_cold ranks by effective_score
# ═══════════════════════════════════════════════════════════════════════════

def test_cold_search_effective_ranking():
    log("\n--- 15. search_cold ranks by effective_score ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3 as _sqlite3
        wid = "effective_cold_window"
        now = time.time()

        # Artifact A: lower importance but recently accessed
        a1 = make_artifact(cache, window_id=wid, turn=1, index=0,
                           content="Recently accessed artifact with enough content for promotion filter pass one",
                           summary="Recent")
        # Artifact B: higher importance but stale
        a2 = make_artifact(cache, window_id=wid, turn=2, index=0,
                           content="Stale high importance artifact with different content for deduplication filter check",
                           summary="Stale")

        # Boost B's importance higher than A
        cache.record_access(a2, "rehydrate")          # +5.0 → 6.0
        cache.record_access(a2, "ordinal_reference")  # +3.0 → 9.0
        cache.record_access(a1, "type_reference")     # +2.0 → 3.0

        # Demote → promote to cold
        cache.demote_window(wid)
        cache.promote_window(wid)

        # Now backdate B's last_accessed_at to 14 days ago in SQLite
        db_path = os.path.join(tmpdir, "data", "interaction_cache.db")
        conn = _sqlite3.connect(db_path)
        conn.execute(
            "UPDATE artifacts SET last_accessed_at = ? WHERE artifact_id = ?",
            (now - 14 * 86400, a2),
        )
        # And set A's last_accessed_at to recently (1 day ago)
        conn.execute(
            "UPDATE artifacts SET last_accessed_at = ? WHERE artifact_id = ?",
            (now - 1 * 86400, a1),
        )
        conn.commit()
        conn.close()

        # search_cold should now rank A (recent, lower importance) above B (stale, higher importance)
        cold = cache.search_cold(user_id="user")
        assert_true("At least 2 cold results", len(cold) >= 2,
                     detail=f"got {len(cold)}")

        if len(cold) >= 2:
            # A should be first because effective_score favors recency
            assert_eq("Recent artifact ranked first",
                       cold[0].artifact_id, a1)
            assert_eq("Stale artifact ranked second",
                       cold[1].artifact_id, a2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 16: Parent bubbling updates access fields
# ═══════════════════════════════════════════════════════════════════════════

def test_parent_bubbling_access_fields():
    log("\n--- 16. Parent bubbling updates access fields ---")
    cache, tmpdir = make_cache()
    try:
        wid = "bubble_access_window"
        parent_id = make_artifact(cache, window_id=wid, turn=1, index=0,
                                  summary="Bubble parent")
        child_id = make_artifact(cache, window_id=wid, turn=1, index=1,
                                 summary="Bubble child", parent_id=parent_id)

        before = time.time()
        cache.record_access(child_id, "nav_advance")
        after = time.time()

        parent = cache.get_by_id(parent_id)
        assert_eq("Parent access_count = 1 after child access",
                   parent.access_count, 1)
        assert_true("Parent last_accessed_at set after child access",
                     before <= parent.last_accessed_at <= after,
                     detail=f"{before} <= {parent.last_accessed_at} <= {after}")

        # Second child access
        cache.record_access(child_id, "nav_advance")
        parent = cache.get_by_id(parent_id)
        assert_eq("Parent access_count = 2 after 2 child accesses",
                   parent.access_count, 2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 17: SQLite persistence of new fields
# ═══════════════════════════════════════════════════════════════════════════

def test_sqlite_persistence_new_fields():
    log("\n--- 17. SQLite persistence of new fields ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3 as _sqlite3
        aid = make_artifact(cache)

        before = time.time()
        cache.record_access(aid, "ordinal_reference")
        cache.record_access(aid, "nav_advance")
        after = time.time()

        # Read directly from SQLite
        db_path = os.path.join(tmpdir, "data", "interaction_cache.db")
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT last_accessed_at, access_count FROM artifacts "
            "WHERE artifact_id = ?", (aid,)
        ).fetchone()
        conn.close()

        assert_eq("SQLite access_count = 2", row["access_count"], 2)
        assert_true("SQLite last_accessed_at in range",
                     before <= row["last_accessed_at"] <= after,
                     detail=f"{before} <= {row['last_accessed_at']} <= {after}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 18: effective_score uses created_at when never accessed
# ═══════════════════════════════════════════════════════════════════════════

def test_effective_score_fallback_to_created_at():
    log("\n--- 18. effective_score uses created_at when never accessed ---")

    now = time.time()

    # Never accessed (last_accessed_at = 0.0) → should use created_at
    art = Artifact(
        artifact_id="fallback_test", turn_id=1, item_index=0,
        artifact_type="test", content="x", summary="x", source="test",
        window_id="w", created_at=now - 3 * 86400,
        importance_score=5.0, last_accessed_at=0.0, access_count=0,
    )

    score = InteractionCache.effective_score(art, now)
    # With 3-day-old created_at and 7-day half-life:
    # decay = 0.5^(3/7) ≈ 0.74, freq_boost = 1.0, effective ≈ 3.7
    assert_true("Score decays from created_at when never accessed",
                3.0 <= score <= 4.5,
                detail=f"score={score:.3f}")


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("Importance Scoring + Retrieval-Driven Mutation — Test Suite\n")

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
    # Retrieval-driven mutation tests
    test_last_accessed_tracking()
    test_effective_score_decay()
    test_effective_score_frequency()
    test_effective_score_combined()
    test_cold_search_effective_ranking()
    test_parent_bubbling_access_fields()
    test_sqlite_persistence_new_fields()
    test_effective_score_fallback_to_created_at()

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
