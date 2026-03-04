#!/usr/bin/env python3
"""
Consolidation & Abstraction (CMA) tests.

Tests:
  --- Frequency detection ---
  1. 3 searches same topic, 2+ windows -> interest insight
  2. 2 searches (below threshold) -> no insight
  3. 3 searches same window only -> no insight (need 2+ windows)
  4. Topic extraction from provenance.query
  5. Topic extraction from summary
  6. Topic normalization (case-insensitive)
  --- Temporal detection ---
  7. Same action at ~7am for 5 days -> morning habit
  8. Scattered times (high variance) -> no habit
  9. Too few data points (< 5) -> no habit
  --- Interest clustering (link graph) ---
  10. 3+ linked artifacts with keyword overlap -> interest
  11. Linked artifacts with no keyword overlap -> no insight
  12. BFS depth-2 walk finds transitive clusters (A->B->C)
  13. Cluster dedup (same cluster not reported twice)
  --- Engagement detection ---
  14. access_count >= 3, grouped by topic -> interest
  15. Low access_count -> no insight
  --- Deduplication ---
  16. Same pattern detected twice -> updated, not duplicated
  17. Evidence count grows on re-detection
  18. Confidence increases with evidence
  --- Confidence & promotion ---
  19. Below threshold -> not promoted
  20. Above threshold + enough evidence -> promoted to facts
  21. Promoted flag prevents double-promotion
  --- consolidate() integration ---
  22. Full run with mixed artifacts -> correct insights
  23. Idempotent (running twice doesn't duplicate)
  24. No cold artifacts -> no insights
  25. Only analyzes cold tier (ignores hot/warm)
  --- get_consolidated_knowledge ---
  26. Filter by pattern_type
  27. Filter by min_confidence
  28. Returns all when no filters
  --- Persistence ---
  29. Knowledge survives cache restart
  30. Schema migration on existing DB
  --- Topic extraction ---
  31. Web search query extraction
  32. Weather summary extraction
  33. Short/empty content -> no topic
  34. Provenance with no query field -> falls back to summary

Usage:
  python3 scripts/test_consolidation.py --verbose
"""

import json
import math
import os
import sys
import time
import tempfile
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, "/home/user/jarvis")

# Stub heavy imports before they're pulled in
sys.modules.setdefault("porcupine", MagicMock())
sys.modules.setdefault("pvporcupine", MagicMock())
sys.modules.setdefault("resemblyzer", MagicMock())
sys.modules.setdefault("resemblyzer.VoiceEncoder", MagicMock())

# --- Imports ---------------------------------------------------------------

from core.interaction_cache import InteractionCache, Artifact

# --- Test infrastructure ---------------------------------------------------

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
        print(f"  [{status}] {name}" + (f" -- {detail}" if not ok else ""))
    return ok


def assert_true(name, condition, detail=""):
    ok = bool(condition)
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" -- {detail}" if not ok else ""))
    return ok


# --- Helpers ---------------------------------------------------------------

def make_cache(tmpdir=None):
    """Create a temp InteractionCache for testing."""
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp(prefix="jarvis_test_consolidation_")

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


def make_cold_artifact(cache, window_id, turn=1, index=0,
                       art_type="search_result_set",
                       content="Test content that is long enough to pass the 50-char promotion filter in promote_window",
                       summary="Test", source="test",
                       provenance=None, created_at=None):
    """Create an artifact, demote, and promote to cold tier. Returns artifact_id."""
    art = Artifact(
        artifact_id=f"art_{window_id}_{turn}_{index}",
        turn_id=turn,
        item_index=index,
        artifact_type=art_type,
        content=content,
        summary=summary,
        source=source,
        provenance=provenance or {},
        parent_id=None,
        window_id=window_id,
        created_at=created_at or time.time(),
    )
    cache.store(art)
    return art.artifact_id


def promote_window(cache, window_id):
    """Demote then promote a window to cold tier."""
    cache.demote_window(window_id)
    return cache.promote_window(window_id)


def make_mock_memory_manager():
    """Create a mock memory_manager with store_fact that tracks calls."""
    mm = MagicMock()
    mm.stored_facts = []

    def _store_fact(fact_dict):
        fid = f"fact_{len(mm.stored_facts)}"
        mm.stored_facts.append(fact_dict)
        return fid

    mm.store_fact = MagicMock(side_effect=_store_fact)
    return mm


# ===========================================================================
# TEST 1: 3 searches same topic, 2+ windows -> interest insight
# ===========================================================================

def test_frequency_basic():
    log("\n--- 1. 3 searches same topic, 2+ windows -> interest ---")
    cache, tmpdir = make_cache()
    try:
        # Create 3 artifacts about "italian recipes" across 2 windows
        for i, wid in enumerate(["win_a", "win_a", "win_b"]):
            make_cold_artifact(
                cache, window_id=wid, turn=i + 1, index=0,
                summary="Italian recipes",
                provenance={"query": "Italian recipes"},
            )
            promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_frequency_patterns("primary_user", conn)
        conn.close()

        assert_true("At least 1 frequency insight", len(insights) >= 1,
                     detail=f"got {len(insights)}")
        if insights:
            assert_eq("Pattern type is interest",
                       insights[0]["pattern_type"], "interest")
            assert_true("Evidence count >= 3",
                         insights[0]["evidence_count"] >= 3)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 2: 2 searches (below threshold) -> no insight
# ===========================================================================

def test_frequency_below_threshold():
    log("\n--- 2. 2 searches (below threshold) -> no insight ---")
    cache, tmpdir = make_cache()
    try:
        for i, wid in enumerate(["win_a", "win_b"]):
            make_cold_artifact(
                cache, window_id=wid, turn=i + 1, index=0,
                summary="rare topic xyz",
                provenance={"query": "rare topic xyz"},
            )
            promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_frequency_patterns("primary_user", conn)
        conn.close()

        # Filter for this specific topic
        matching = [i for i in insights
                    if "rare" in i.get("content", "").lower()
                    or "xyz" in i.get("topic_key", "").lower()]
        assert_eq("No insight for 2 searches", len(matching), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 3: 3 searches same window only -> no insight
# ===========================================================================

def test_frequency_single_window():
    log("\n--- 3. 3 searches same window -> no insight (need 2+ windows) ---")
    cache, tmpdir = make_cache()
    try:
        wid = "single_win"
        for i in range(3):
            make_cold_artifact(
                cache, window_id=wid, turn=i + 1, index=0,
                summary="python decorators",
                provenance={"query": "python decorators"},
            )
        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_frequency_patterns("primary_user", conn)
        conn.close()

        matching = [i for i in insights
                    if "decorator" in i.get("topic_key", "").lower()]
        assert_eq("No insight for single-window topic", len(matching), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 4: Topic extraction from provenance.query
# ===========================================================================

def test_topic_from_provenance():
    log("\n--- 4. Topic extraction from provenance.query ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3

        # Simulate a row dict
        row = {
            "provenance": json.dumps({"query": "Docker container networking"}),
            "summary": "Some summary",
            "content": "Full content here",
        }

        topic = cache._extract_topic(row)
        assert_true("Topic is non-empty", len(topic) > 0,
                     detail=f"topic={topic!r}")
        assert_true("Contains 'docker'", "docker" in topic,
                     detail=f"topic={topic!r}")
        assert_true("Contains 'container'", "container" in topic,
                     detail=f"topic={topic!r}")
        assert_true("Contains 'network'", "network" in topic,
                     detail=f"topic={topic!r}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 5: Topic extraction from summary
# ===========================================================================

def test_topic_from_summary():
    log("\n--- 5. Topic extraction from summary ---")
    cache, tmpdir = make_cache()
    try:
        row = {
            "provenance": "{}",
            "summary": "Weather forecast for Nashville",
            "content": "Full content here",
        }

        topic = cache._extract_topic(row)
        assert_true("Topic is non-empty", len(topic) > 0,
                     detail=f"topic={topic!r}")
        assert_true("Contains 'nashville'", "nashville" in topic,
                     detail=f"topic={topic!r}")
        assert_true("Contains 'forecast'", "forecast" in topic,
                     detail=f"topic={topic!r}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 6: Topic normalization (case-insensitive)
# ===========================================================================

def test_topic_normalization():
    log("\n--- 6. Topic normalization (case-insensitive) ---")
    cache, tmpdir = make_cache()
    try:
        row1 = {
            "provenance": json.dumps({"query": "Italian Recipes"}),
            "summary": "", "content": "",
        }
        row2 = {
            "provenance": json.dumps({"query": "italian recipes"}),
            "summary": "", "content": "",
        }

        t1 = cache._extract_topic(row1)
        t2 = cache._extract_topic(row2)
        assert_eq("Normalized topics are equal", t1, t2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 7: Same action at ~7am for 5 days -> morning habit
# ===========================================================================

def test_temporal_morning_habit():
    log("\n--- 7. Same action at ~7am for 5 days -> morning habit ---")
    cache, tmpdir = make_cache()
    try:
        base_time = datetime(2026, 3, 1, 7, 0, 0)
        for day in range(5):
            dt = base_time + timedelta(days=day, minutes=day * 10)  # 7:00-7:40
            ts = dt.timestamp()
            wid = f"morning_win_{day}"
            make_cold_artifact(
                cache, window_id=wid, turn=1, index=0,
                art_type="weather_report", source="get_weather",
                summary="Weather forecast for Nashville",
                created_at=ts,
            )
            promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_temporal_patterns("primary_user", conn)
        conn.close()

        assert_true("At least 1 temporal insight", len(insights) >= 1,
                     detail=f"got {len(insights)}")
        if insights:
            assert_eq("Pattern type is habit",
                       insights[0]["pattern_type"], "habit")
            assert_true("Content mentions 'morning'",
                         "morning" in insights[0]["content"],
                         detail=f"content={insights[0]['content']!r}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 8: Scattered times (high variance) -> no habit
# ===========================================================================

def test_temporal_scattered():
    log("\n--- 8. Scattered times -> no habit ---")
    cache, tmpdir = make_cache()
    try:
        # Create artifacts at wildly different times
        hours = [2, 8, 14, 20, 5]  # spread across 24h
        base_date = datetime(2026, 3, 1)
        for i, hour in enumerate(hours):
            dt = base_date + timedelta(days=i, hours=hour)
            ts = dt.timestamp()
            wid = f"scatter_win_{i}"
            make_cold_artifact(
                cache, window_id=wid, turn=1, index=0,
                art_type="news_summary", source="get_news",
                summary="News headlines",
                created_at=ts,
            )
            promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_temporal_patterns("primary_user", conn)
        conn.close()

        news_habits = [i for i in insights
                       if "news" in i.get("content", "").lower()]
        assert_eq("No habit for scattered times", len(news_habits), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 9: Too few data points (< 5) -> no habit
# ===========================================================================

def test_temporal_too_few():
    log("\n--- 9. Too few data points -> no habit ---")
    cache, tmpdir = make_cache()
    try:
        base_time = datetime(2026, 3, 1, 9, 0, 0)
        for day in range(3):  # only 3 days
            dt = base_time + timedelta(days=day)
            ts = dt.timestamp()
            wid = f"few_win_{day}"
            make_cold_artifact(
                cache, window_id=wid, turn=1, index=0,
                art_type="system_info", source="get_system_info",
                summary="System info: general",
                created_at=ts,
            )
            promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_temporal_patterns("primary_user", conn)
        conn.close()

        sysinfo = [i for i in insights
                    if "system" in i.get("content", "").lower()]
        assert_eq("No habit for < 5 data points", len(sysinfo), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 10: 3+ linked artifacts with keyword overlap -> interest
# ===========================================================================

def test_cluster_keyword_overlap():
    log("\n--- 10. 3+ linked artifacts with keyword overlap -> interest ---")
    cache, tmpdir = make_cache()
    try:
        wid = "cluster_win"
        a1 = make_cold_artifact(
            cache, window_id=wid, turn=1, index=0,
            content="Docker container orchestration setup guide with detailed steps for production deployment",
            summary="Docker containers",
            provenance={"query": "docker containers"},
        )
        a2 = make_cold_artifact(
            cache, window_id=wid, turn=2, index=0,
            content="Kubernetes container management and pod orchestration for microservices architecture",
            summary="Kubernetes containers",
            provenance={"query": "kubernetes containers"},
        )
        a3 = make_cold_artifact(
            cache, window_id=wid, turn=3, index=0,
            content="Container networking fundamentals and Docker bridge network configuration explained",
            summary="Container networking",
            provenance={"query": "container networking"},
        )

        # Link them
        cache.create_link(a1, a2, "co_occurrence")
        cache.create_link(a2, a3, "co_occurrence")

        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_interest_clusters("primary_user", conn)
        conn.close()

        assert_true("At least 1 cluster insight", len(insights) >= 1,
                     detail=f"got {len(insights)}: {[i['content'] for i in insights]}")
        if insights:
            assert_eq("Pattern type is interest",
                       insights[0]["pattern_type"], "interest")
            assert_true("Evidence count >= 3",
                         insights[0]["evidence_count"] >= 3)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 11: Linked artifacts with no keyword overlap -> no insight
# ===========================================================================

def test_cluster_no_overlap():
    log("\n--- 11. Linked artifacts with no keyword overlap -> no insight ---")
    cache, tmpdir = make_cache()
    try:
        wid = "no_overlap_win"
        a1 = make_cold_artifact(
            cache, window_id=wid, turn=1, index=0,
            content="Quantum physics and wave function collapse experiments in laboratory settings explained",
            summary="Quantum physics",
            provenance={"query": "quantum physics"},
        )
        a2 = make_cold_artifact(
            cache, window_id=wid, turn=2, index=0,
            content="Italian pasta recipes from traditional Roman cuisine heritage and cooking traditions from Italy",
            summary="Italian pasta recipes",
            provenance={"query": "italian pasta"},
        )
        a3 = make_cold_artifact(
            cache, window_id=wid, turn=3, index=0,
            content="Basketball championship highlights from the finals game between rival metropolitan teams",
            summary="Basketball highlights",
            provenance={"query": "basketball highlights"},
        )

        cache.create_link(a1, a2, "co_occurrence")
        cache.create_link(a2, a3, "co_occurrence")

        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_interest_clusters("primary_user", conn)
        conn.close()

        assert_eq("No cluster insight for unrelated topics", len(insights), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 12: BFS depth-2 finds transitive clusters (A->B->C)
# ===========================================================================

def test_cluster_transitive():
    log("\n--- 12. BFS depth-2 finds transitive clusters ---")
    cache, tmpdir = make_cache()
    try:
        wid = "transitive_win"
        a1 = make_cold_artifact(
            cache, window_id=wid, turn=1, index=0,
            content="Python machine learning model training with scikit-learn library algorithms and datasets",
            summary="Python ML training",
            provenance={"query": "python machine learning"},
        )
        a2 = make_cold_artifact(
            cache, window_id=wid, turn=2, index=0,
            content="Machine learning data preprocessing and feature engineering for python model pipelines",
            summary="ML data preprocessing",
            provenance={"query": "machine learning preprocessing"},
        )
        a3 = make_cold_artifact(
            cache, window_id=wid, turn=3, index=0,
            content="Python neural network training optimization with learning rate scheduling and machine learning",
            summary="Python neural networks",
            provenance={"query": "python neural network learning"},
        )

        # A->B and B->C (A and C not directly linked)
        cache.create_link(a1, a2, "co_occurrence")
        cache.create_link(a2, a3, "co_occurrence")

        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_interest_clusters("primary_user", conn)
        conn.close()

        assert_true("Cluster found via transitive links", len(insights) >= 1,
                     detail=f"got {len(insights)}")
        if insights:
            assert_true("Cluster includes all 3",
                         insights[0]["evidence_count"] >= 3,
                         detail=f"evidence_count={insights[0]['evidence_count']}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 13: Cluster dedup (same cluster not reported twice)
# ===========================================================================

def test_cluster_dedup():
    log("\n--- 13. Cluster dedup ---")
    cache, tmpdir = make_cache()
    try:
        wid = "dedup_cluster_win"
        ids = []
        for i in range(4):
            aid = make_cold_artifact(
                cache, window_id=wid, turn=i + 1, index=0,
                content=f"Security threat hunting SIEM analysis number {i} with advanced detection and security monitoring",
                summary=f"Security SIEM analysis {i}",
                provenance={"query": f"security SIEM threat hunting {i}"},
            )
            ids.append(aid)

        # Fully connect them (A-B, B-C, C-D, A-C)
        cache.create_link(ids[0], ids[1], "co_occurrence")
        cache.create_link(ids[1], ids[2], "co_occurrence")
        cache.create_link(ids[2], ids[3], "co_occurrence")
        cache.create_link(ids[0], ids[2], "co_occurrence")

        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_interest_clusters("primary_user", conn)
        conn.close()

        # Should be exactly 1 cluster, not 4 (one per start node)
        assert_eq("Exactly 1 cluster (not duplicated)", len(insights), 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 14: access_count >= 3, grouped by topic -> interest
# ===========================================================================

def test_engagement_high_access():
    log("\n--- 14. High engagement -> interest ---")
    cache, tmpdir = make_cache()
    try:
        wid = "engage_win"
        a1 = make_cold_artifact(
            cache, window_id=wid, turn=1, index=0,
            summary="Docker setup guide",
            provenance={"query": "docker setup"},
        )

        # Boost access count
        for _ in range(4):
            cache.record_access(a1, "readback_recall")

        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_engagement_patterns("primary_user", conn)
        conn.close()

        assert_true("At least 1 engagement insight", len(insights) >= 1,
                     detail=f"got {len(insights)}")
        if insights:
            assert_eq("Pattern type is interest",
                       insights[0]["pattern_type"], "interest")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 15: Low access_count -> no insight
# ===========================================================================

def test_engagement_low_access():
    log("\n--- 15. Low access_count -> no insight ---")
    cache, tmpdir = make_cache()
    try:
        wid = "low_engage_win"
        make_cold_artifact(
            cache, window_id=wid, turn=1, index=0,
            summary="Random topic",
            provenance={"query": "random topic"},
        )
        promote_window(cache, wid)

        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        insights = cache._detect_engagement_patterns("primary_user", conn)
        conn.close()

        assert_eq("No insight for low access", len(insights), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 16: Same pattern detected twice -> updated, not duplicated
# ===========================================================================

def test_upsert_no_duplicate():
    log("\n--- 16. Upsert dedup ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3

        insight = {
            "pattern_type": "interest",
            "content": "frequently searches for docker",
            "evidence_count": 3,
            "evidence_ids": ["a1", "a2", "a3"],
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "distinct_windows": 2,
            "topic_key": "docker",
            "user_id": "user",
        }

        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        kid1 = cache._upsert_knowledge(conn, insight)
        conn.commit()

        # Insert same insight again
        kid2 = cache._upsert_knowledge(conn, insight)
        conn.commit()

        assert_eq("Same knowledge_id returned", kid1, kid2)

        count = conn.execute(
            "SELECT COUNT(*) FROM consolidated_knowledge"
        ).fetchone()[0]
        conn.close()

        assert_eq("Only 1 row in table", count, 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 17: Evidence count grows on re-detection
# ===========================================================================

def test_evidence_grows():
    log("\n--- 17. Evidence count grows ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3

        insight1 = {
            "pattern_type": "interest",
            "content": "frequently searches for kubernetes",
            "evidence_count": 3,
            "evidence_ids": ["a1", "a2", "a3"],
            "first_seen": time.time() - 7200,
            "last_seen": time.time() - 3600,
            "distinct_windows": 2,
            "topic_key": "kubernetes",
            "user_id": "user",
        }

        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        cache._upsert_knowledge(conn, insight1)
        conn.commit()

        # Now detect with more evidence
        insight2 = dict(insight1)
        insight2["evidence_count"] = 5
        insight2["evidence_ids"] = ["a1", "a2", "a3", "a4", "a5"]
        insight2["last_seen"] = time.time()
        insight2["distinct_windows"] = 3

        cache._upsert_knowledge(conn, insight2)
        conn.commit()

        row = conn.execute(
            "SELECT evidence_count, evidence_ids FROM consolidated_knowledge"
        ).fetchone()
        conn.close()

        assert_eq("Evidence count updated to 5", row["evidence_count"], 5)
        ids = json.loads(row["evidence_ids"])
        assert_eq("All 5 evidence IDs stored", len(ids), 5)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 18: Confidence increases with evidence
# ===========================================================================

def test_confidence_grows():
    log("\n--- 18. Confidence increases with evidence ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3

        insight = {
            "pattern_type": "interest",
            "content": "frequently searches for python",
            "evidence_count": 3,
            "evidence_ids": ["a1", "a2", "a3"],
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "distinct_windows": 2,
            "topic_key": "python_lang",
            "user_id": "user",
        }

        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        cache._upsert_knowledge(conn, insight)
        conn.commit()

        row1 = conn.execute(
            "SELECT confidence FROM consolidated_knowledge"
        ).fetchone()
        conf1 = row1["confidence"]

        # More evidence
        insight2 = dict(insight)
        insight2["evidence_count"] = 6
        insight2["evidence_ids"] = [f"a{i}" for i in range(6)]
        insight2["distinct_windows"] = 4

        cache._upsert_knowledge(conn, insight2)
        conn.commit()

        row2 = conn.execute(
            "SELECT confidence FROM consolidated_knowledge"
        ).fetchone()
        conf2 = row2["confidence"]
        conn.close()

        assert_true("Confidence increased", conf2 > conf1,
                     detail=f"before={conf1}, after={conf2}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 19: Below threshold -> not promoted
# ===========================================================================

def test_no_promote_low_confidence():
    log("\n--- 19. Below threshold -> not promoted ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        mm = make_mock_memory_manager()

        insight = {
            "pattern_type": "interest",
            "content": "frequently searches for golang",
            "evidence_count": 3,
            "evidence_ids": ["a1", "a2", "a3"],
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "distinct_windows": 2,
            "topic_key": "golang_low",
            "user_id": "user",
        }

        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        cache._upsert_knowledge(conn, insight)
        conn.commit()

        cache._promote_mature_insights(conn, "primary_user", mm)
        conn.commit()
        conn.close()

        assert_eq("store_fact not called", mm.store_fact.call_count, 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 20: Above threshold -> promoted to facts
# ===========================================================================

def test_promote_high_confidence():
    log("\n--- 20. Above threshold -> promoted to facts ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        mm = make_mock_memory_manager()

        insight = {
            "pattern_type": "interest",
            "content": "frequently searches for rust programming",
            "evidence_count": 6,
            "evidence_ids": [f"a{i}" for i in range(6)],
            "first_seen": time.time() - 86400,
            "last_seen": time.time(),
            "distinct_windows": 4,
            "topic_key": "rust_promo",
            "user_id": "user",
        }

        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        cache._upsert_knowledge(conn, insight)
        conn.commit()

        cache._promote_mature_insights(conn, "primary_user", mm)
        conn.commit()

        # Verify promoted flag
        row = conn.execute(
            "SELECT promoted FROM consolidated_knowledge"
        ).fetchone()
        conn.close()

        assert_eq("store_fact called once", mm.store_fact.call_count, 1)
        assert_eq("Promoted flag set to 1", row["promoted"], 1)
        assert_eq("Fact source is 'consolidated'",
                   mm.stored_facts[0]["source"], "consolidated")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 21: Promoted flag prevents double-promotion
# ===========================================================================

def test_no_double_promote():
    log("\n--- 21. Promoted flag prevents double-promotion ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        mm = make_mock_memory_manager()

        insight = {
            "pattern_type": "interest",
            "content": "frequently searches for typescript",
            "evidence_count": 7,
            "evidence_ids": [f"a{i}" for i in range(7)],
            "first_seen": time.time() - 86400,
            "last_seen": time.time(),
            "distinct_windows": 4,
            "topic_key": "typescript_dbl",
            "user_id": "user",
        }

        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row
        cache._upsert_knowledge(conn, insight)
        conn.commit()

        # Promote once
        cache._promote_mature_insights(conn, "primary_user", mm)
        conn.commit()
        assert_eq("First promote: 1 call", mm.store_fact.call_count, 1)

        # Promote again
        cache._promote_mature_insights(conn, "primary_user", mm)
        conn.commit()
        conn.close()

        assert_eq("Second promote: still 1 call", mm.store_fact.call_count, 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 22: Full consolidate() run with mixed artifacts
# ===========================================================================

def test_consolidate_full():
    log("\n--- 22. Full consolidate() run ---")
    cache, tmpdir = make_cache()
    try:
        # Create enough frequency data (3 searches, 2 windows)
        for i, wid in enumerate(["full_a", "full_a", "full_b"]):
            make_cold_artifact(
                cache, window_id=wid, turn=i + 1, index=0,
                summary="cybersecurity threat analysis",
                provenance={"query": "cybersecurity threat analysis"},
            )
            promote_window(cache, wid)

        cache.consolidate(user_id="user")

        knowledge = cache.get_consolidated_knowledge()
        assert_true("At least 1 consolidated insight", len(knowledge) >= 1,
                     detail=f"got {len(knowledge)}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 23: Idempotent (running twice doesn't duplicate)
# ===========================================================================

def test_consolidate_idempotent():
    log("\n--- 23. Idempotent ---")
    cache, tmpdir = make_cache()
    try:
        for i, wid in enumerate(["idem_a", "idem_a", "idem_b"]):
            make_cold_artifact(
                cache, window_id=wid, turn=i + 1, index=0,
                summary="machine learning models",
                provenance={"query": "machine learning models"},
            )
            promote_window(cache, wid)

        cache.consolidate(user_id="user")
        count1 = len(cache.get_consolidated_knowledge())

        cache.consolidate(user_id="user")
        count2 = len(cache.get_consolidated_knowledge())

        assert_eq("Same count after second consolidation", count1, count2)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 24: No cold artifacts -> no insights
# ===========================================================================

def test_consolidate_empty():
    log("\n--- 24. No cold artifacts -> no insights ---")
    cache, tmpdir = make_cache()
    try:
        cache.consolidate(user_id="user")
        knowledge = cache.get_consolidated_knowledge()
        assert_eq("No insights from empty cache", len(knowledge), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 25: Only analyzes cold tier (ignores hot/warm)
# ===========================================================================

def test_consolidate_cold_only():
    log("\n--- 25. Only analyzes cold tier ---")
    cache, tmpdir = make_cache()
    try:
        # Create hot artifacts (not demoted)
        for i in range(4):
            wid = f"hot_win_{i}"
            art = Artifact(
                artifact_id=f"hot_art_{i}",
                turn_id=1,
                item_index=0,
                artifact_type="search_result_set",
                content="Hot tier content that should not be analyzed by consolidation at all for this test",
                summary="hot topic repeated",
                source="test",
                provenance={"query": "hot topic repeated"},
                window_id=wid,
                created_at=time.time(),
            )
            cache.store(art)

        cache.consolidate(user_id="user")
        knowledge = cache.get_consolidated_knowledge()
        assert_eq("No insights from hot-only artifacts", len(knowledge), 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 26: Filter by pattern_type
# ===========================================================================

def test_filter_pattern_type():
    log("\n--- 26. Filter by pattern_type ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row

        # Insert two different types
        for ptype, topic in [("interest", "docker_filter"),
                             ("habit", "weather_filter")]:
            cache._upsert_knowledge(conn, {
                "pattern_type": ptype,
                "content": f"test {ptype}",
                "evidence_count": 3,
                "evidence_ids": ["a1", "a2", "a3"],
                "first_seen": time.time() - 3600,
                "last_seen": time.time(),
                "distinct_windows": 2,
                "topic_key": topic,
                "user_id": "user",
            })
        conn.commit()
        conn.close()

        interests = cache.get_consolidated_knowledge(pattern_type="interest")
        habits = cache.get_consolidated_knowledge(pattern_type="habit")

        assert_true("At least 1 interest", len(interests) >= 1)
        assert_true("At least 1 habit", len(habits) >= 1)
        assert_true("Interest filter excludes habits",
                     all(k["pattern_type"] == "interest" for k in interests))
        assert_true("Habit filter excludes interests",
                     all(k["pattern_type"] == "habit" for k in habits))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 27: Filter by min_confidence
# ===========================================================================

def test_filter_min_confidence():
    log("\n--- 27. Filter by min_confidence ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row

        # Low confidence (3 evidence, 2 windows = 0.7)
        cache._upsert_knowledge(conn, {
            "pattern_type": "interest",
            "content": "low confidence topic",
            "evidence_count": 3,
            "evidence_ids": ["a1", "a2", "a3"],
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "distinct_windows": 2,
            "topic_key": "low_conf",
            "user_id": "user",
        })
        # High confidence (6 evidence, 4 windows = 0.95 capped)
        cache._upsert_knowledge(conn, {
            "pattern_type": "interest",
            "content": "high confidence topic",
            "evidence_count": 6,
            "evidence_ids": [f"a{i}" for i in range(6)],
            "first_seen": time.time() - 86400,
            "last_seen": time.time(),
            "distinct_windows": 4,
            "topic_key": "high_conf",
            "user_id": "user",
        })
        conn.commit()
        conn.close()

        all_k = cache.get_consolidated_knowledge(min_confidence=0.0)
        high_k = cache.get_consolidated_knowledge(min_confidence=0.8)

        assert_eq("All filter returns 2", len(all_k), 2)
        assert_eq("High filter returns 1", len(high_k), 1)
        assert_true("High filter returns the right one",
                     "high confidence" in high_k[0]["content"])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 28: Returns all when no filters
# ===========================================================================

def test_no_filters():
    log("\n--- 28. Returns all when no filters ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        conn.row_factory = sqlite3.Row

        for i in range(3):
            cache._upsert_knowledge(conn, {
                "pattern_type": "interest",
                "content": f"topic number {i}",
                "evidence_count": 3,
                "evidence_ids": [f"a{i}"],
                "first_seen": time.time() - 3600,
                "last_seen": time.time(),
                "distinct_windows": 2,
                "topic_key": f"unfiltered_{i}",
                "user_id": "user",
            })
        conn.commit()
        conn.close()

        all_k = cache.get_consolidated_knowledge()
        assert_eq("Returns all 3", len(all_k), 3)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 29: Knowledge survives cache restart
# ===========================================================================

def test_persistence():
    log("\n--- 29. Knowledge survives cache restart ---")
    tmpdir = tempfile.mkdtemp(prefix="jarvis_test_consol_persist_")
    try:
        import sqlite3

        cache1, _ = make_cache(tmpdir)
        conn = sqlite3.connect(str(cache1.db_path))
        conn.row_factory = sqlite3.Row
        cache1._upsert_knowledge(conn, {
            "pattern_type": "interest",
            "content": "persistent insight",
            "evidence_count": 4,
            "evidence_ids": ["a1", "a2", "a3", "a4"],
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "distinct_windows": 3,
            "topic_key": "persist_test",
            "user_id": "user",
        })
        conn.commit()
        conn.close()

        # New cache instance
        cache2, _ = make_cache(tmpdir)
        knowledge = cache2.get_consolidated_knowledge()
        assert_eq("1 insight after restart", len(knowledge), 1)
        assert_true("Content preserved",
                     "persistent" in knowledge[0]["content"])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 30: Schema migration on existing DB
# ===========================================================================

def test_schema_migration():
    log("\n--- 30. Schema migration on existing DB ---")
    cache, tmpdir = make_cache()
    try:
        import sqlite3
        conn = sqlite3.connect(str(cache.db_path))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()

        assert_true("consolidated_knowledge table exists",
                     "consolidated_knowledge" in tables,
                     detail=f"tables={tables}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 31: Web search query extraction
# ===========================================================================

def test_topic_web_search():
    log("\n--- 31. Web search query extraction ---")
    cache, tmpdir = make_cache()
    try:
        row = {
            "provenance": json.dumps({"query": "Web search: best python frameworks 2026"}),
            "summary": "Web search results",
            "content": "",
        }
        topic = cache._extract_topic(row)
        assert_true("Topic extracted", len(topic) > 0, detail=f"topic={topic!r}")
        assert_true("Contains 'python'", "python" in topic)
        assert_true("Contains 'framework'", "framework" in topic)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 32: Weather summary extraction
# ===========================================================================

def test_topic_weather():
    log("\n--- 32. Weather summary extraction ---")
    cache, tmpdir = make_cache()
    try:
        row = {
            "provenance": "{}",
            "summary": "Weather forecast for Nashville Tennessee",
            "content": "",
        }
        topic = cache._extract_topic(row)
        assert_true("Topic extracted", len(topic) > 0, detail=f"topic={topic!r}")
        assert_true("Contains 'nashville'", "nashville" in topic)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 33: Short/empty content -> no topic
# ===========================================================================

def test_topic_empty():
    log("\n--- 33. Short/empty content -> no topic ---")
    cache, tmpdir = make_cache()
    try:
        row = {
            "provenance": "{}",
            "summary": "",
            "content": "ab",
        }
        topic = cache._extract_topic(row)
        assert_eq("Empty topic for short content", topic, "")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# TEST 34: Provenance with no query -> falls back to summary
# ===========================================================================

def test_topic_fallback():
    log("\n--- 34. Provenance no query -> falls back to summary ---")
    cache, tmpdir = make_cache()
    try:
        row = {
            "provenance": json.dumps({"tool_args": {"category": "general"}}),
            "summary": "System info: CPU temperature monitoring",
            "content": "",
        }
        topic = cache._extract_topic(row)
        assert_true("Topic from summary fallback", len(topic) > 0,
                     detail=f"topic={topic!r}")
        assert_true("Contains 'cpu' or 'temperature'",
                     "cpu" in topic or "temperature" in topic,
                     detail=f"topic={topic!r}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# Runner
# ===========================================================================

def main():
    log("Consolidation & Abstraction (CMA) -- Test Suite\n")

    # Frequency detection
    test_frequency_basic()
    test_frequency_below_threshold()
    test_frequency_single_window()
    test_topic_from_provenance()
    test_topic_from_summary()
    test_topic_normalization()
    # Temporal detection
    test_temporal_morning_habit()
    test_temporal_scattered()
    test_temporal_too_few()
    # Interest clustering
    test_cluster_keyword_overlap()
    test_cluster_no_overlap()
    test_cluster_transitive()
    test_cluster_dedup()
    # Engagement detection
    test_engagement_high_access()
    test_engagement_low_access()
    # Deduplication
    test_upsert_no_duplicate()
    test_evidence_grows()
    test_confidence_grows()
    # Confidence & promotion
    test_no_promote_low_confidence()
    test_promote_high_confidence()
    test_no_double_promote()
    # consolidate() integration
    test_consolidate_full()
    test_consolidate_idempotent()
    test_consolidate_empty()
    test_consolidate_cold_only()
    # get_consolidated_knowledge
    test_filter_pattern_type()
    test_filter_min_confidence()
    test_no_filters()
    # Persistence
    test_persistence()
    test_schema_migration()
    # Topic extraction
    test_topic_web_search()
    test_topic_weather()
    test_topic_empty()
    test_topic_fallback()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f" ({failed} failed)")
        for r in results:
            if not r.passed:
                print(f"  FAIL: {r.name} -- {r.detail}")
    else:
        print(" \u2713")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
