#!/usr/bin/env python3
"""
Standalone test for conversational memory Phase 1.

Tests MemoryManager fact store + pattern extraction + CRUD.
No JARVIS startup required — uses a temp SQLite DB.

Usage:
    python3 scripts/test_memory.py
"""

import json
import os
import sys
import tempfile
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MockConfig:
    """Minimal config mock that supports dot-notation get()."""

    def __init__(self, db_path):
        self._values = {
            "conversational_memory.db_path": db_path,
            "conversational_memory.faiss_index_path": "/tmp/test_faiss",
            "conversational_memory.batch_extraction_interval": 25,
            "conversational_memory.proactive_surfacing": True,
            "conversational_memory.proactive_confidence_threshold": 0.70,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


def make_message(content, role="user", user_id="user"):
    """Create a message dict matching JARVIS format."""
    return {
        "role": role,
        "content": content,
        "user_id": user_id,
        "timestamp": time.time(),
    }


def test_pattern_extraction(mm):
    """Test regex-based fact extraction from user messages."""
    print("\n" + "=" * 60)
    print("TEST: Pattern-based Fact Extraction")
    print("=" * 60)

    test_cases = [
        # (message, expected_category, expected_content_substring)
        ("I prefer dark roast coffee", "preference", "dark roast coffee"),
        ("I love hiking in the mountains", "preference", "hiking"),
        ("My favorite editor is vim", "preference", "editor: vim"),
        ("I don't like spicy food", "preference", "spicy food"),
        ("I hate waking up early", "preference", "waking up early"),
        ("Remember that my dentist appointment is on March 5th", "general", "dentist appointment"),
        ("My wife's name is Sarah", "relationship", "wife's: Sarah"),
        ("My brother is called David", "relationship", "brother: David"),
        ("I work for CyberDefense Corp", "work", "CyberDefense Corp"),
        ("My job is senior threat analyst", "work", "senior threat analyst"),
        ("I live in Nashville", "location", "Nashville"),
        ("I'm allergic to shellfish", "health", "shellfish"),
        ("I usually check email first thing in the morning", "habit", "check email"),
    ]

    passed = 0
    failed = 0

    for message_text, expected_cat, expected_substr in test_cases:
        msg = make_message(message_text)
        facts = mm.extract_facts_realtime(msg)

        if not facts:
            print(f"  FAIL: No facts extracted from: \"{message_text}\"")
            failed += 1
            continue

        fact = facts[0]
        cat_ok = fact["category"] == expected_cat
        content_ok = expected_substr.lower() in fact["content"].lower()

        if cat_ok and content_ok:
            print(f"  PASS: \"{message_text}\" -> [{fact['category']}] {fact['content']}")
            passed += 1
        else:
            print(f"  FAIL: \"{message_text}\"")
            print(f"        Expected category={expected_cat}, got={fact['category']}")
            print(f"        Expected content containing '{expected_substr}', got='{fact['content']}'")
            failed += 1

    # Test: assistant messages should be ignored
    msg = make_message("I prefer dark mode", role="assistant")
    facts = mm.extract_facts_realtime(msg)
    if not facts:
        print(f"  PASS: Assistant messages correctly ignored")
        passed += 1
    else:
        print(f"  FAIL: Assistant message should not produce facts")
        failed += 1

    # Test: very short messages should be ignored
    msg = make_message("yes")
    facts = mm.extract_facts_realtime(msg)
    if not facts:
        print(f"  PASS: Very short messages correctly ignored")
        passed += 1
    else:
        print(f"  FAIL: Short message should not produce facts")
        failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed out of {passed + failed}")
    return failed == 0


def test_crud(mm):
    """Test CRUD operations on the fact store."""
    print("\n" + "=" * 60)
    print("TEST: CRUD Operations")
    print("=" * 60)

    passed = 0
    failed = 0

    # -- Store --
    fact_id = mm.store_fact({
        "user_id": "user",
        "category": "preference",
        "subject": "test preference",
        "content": "I prefer tests that pass",
        "source": "explicit",
        "confidence": 0.95,
    })
    if fact_id:
        print(f"  PASS: store_fact returned id={fact_id[:8]}...")
        passed += 1
    else:
        print(f"  FAIL: store_fact returned None")
        failed += 1

    # -- Duplicate detection --
    dup_id = mm.store_fact({
        "user_id": "user",
        "category": "preference",
        "subject": "test preference",
        "content": "I prefer tests that pass",
        "source": "explicit",
        "confidence": 0.95,
    })
    if dup_id is None:
        print(f"  PASS: Exact duplicate correctly rejected")
        passed += 1
    else:
        print(f"  FAIL: Duplicate should return None, got {dup_id[:8]}...")
        failed += 1

    # -- Supersede --
    new_id = mm.store_fact({
        "user_id": "user",
        "category": "preference",
        "subject": "test preference",
        "content": "Actually I prefer tests that fail gracefully",
        "source": "explicit",
        "confidence": 0.90,
    })
    if new_id and new_id != fact_id:
        print(f"  PASS: Updated fact got new id={new_id[:8]}... (superseded old)")
        passed += 1
    else:
        print(f"  FAIL: Supersede should return new id")
        failed += 1

    # -- Get facts --
    facts = mm.get_facts("primary_user")
    # The superseded fact should not appear, only the new one
    active_test_facts = [f for f in facts if "test" in f["subject"]]
    if len(active_test_facts) == 1 and active_test_facts[0]["fact_id"] == new_id:
        print(f"  PASS: get_facts returns only active (non-superseded) facts")
        passed += 1
    else:
        print(f"  FAIL: Expected 1 active test fact, got {len(active_test_facts)}")
        failed += 1

    # -- Get facts by category --
    pref_facts = mm.get_facts("primary_user", category="preference")
    if all(f["category"] == "preference" for f in pref_facts):
        print(f"  PASS: get_facts(category='preference') returns {len(pref_facts)} preference facts")
        passed += 1
    else:
        print(f"  FAIL: Category filter not working")
        failed += 1

    # -- Text search --
    results = mm.search_facts_text("coffee", "primary_user")
    coffee_found = any("coffee" in f["content"].lower() for f in results)
    if coffee_found:
        print(f"  PASS: search_facts_text('coffee') found {len(results)} result(s)")
        passed += 1
    else:
        print(f"  FAIL: search_facts_text('coffee') found nothing")
        failed += 1

    # -- Fact count --
    counts = mm.get_fact_count("primary_user")
    total = sum(counts.values())
    if total > 0:
        print(f"  PASS: get_fact_count: {dict(counts)} (total={total})")
        passed += 1
    else:
        print(f"  FAIL: get_fact_count returned 0")
        failed += 1

    # -- Soft delete --
    deleted = mm.delete_fact(new_id, soft=True)
    if deleted:
        remaining = mm.get_facts("primary_user")
        deleted_still_visible = any(f["fact_id"] == new_id for f in remaining)
        if not deleted_still_visible:
            print(f"  PASS: Soft delete hides fact from get_facts")
            passed += 1
        else:
            print(f"  FAIL: Soft-deleted fact still visible")
            failed += 1
    else:
        print(f"  FAIL: delete_fact returned False")
        failed += 1

    # -- Update --
    # Store a fresh fact for update test
    upd_id = mm.store_fact({
        "user_id": "user",
        "category": "general",
        "subject": "update test",
        "content": "This fact will be updated",
        "source": "explicit",
        "confidence": 0.80,
    })
    updated = mm.update_fact(upd_id, confidence=0.99, category="preference")
    if updated:
        # Verify the update
        facts = mm.get_facts("primary_user", category="preference")
        upd_fact = next((f for f in facts if f["fact_id"] == upd_id), None)
        if upd_fact and upd_fact["confidence"] == 0.99:
            print(f"  PASS: update_fact correctly updated confidence and category")
            passed += 1
        else:
            print(f"  FAIL: update_fact did not persist changes")
            failed += 1
    else:
        print(f"  FAIL: update_fact returned False")
        failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed out of {passed + failed}")
    return failed == 0


def test_on_message(mm):
    """Test the on_message hook."""
    print("\n" + "=" * 60)
    print("TEST: on_message Hook")
    print("=" * 60)

    passed = 0
    failed = 0

    count_before = sum(mm.get_fact_count("primary_user").values())
    mm.on_message(make_message("I always drink green tea in the afternoon"))
    count_after = sum(mm.get_fact_count("primary_user").values())

    if count_after > count_before:
        print(f"  PASS: on_message extracted fact (count {count_before} -> {count_after})")
        passed += 1
    else:
        print(f"  FAIL: on_message did not extract fact")
        failed += 1

    # Batch counter should increment
    if mm._message_count_since_batch > 0:
        print(f"  PASS: Batch counter incremented to {mm._message_count_since_batch}")
        passed += 1
    else:
        print(f"  FAIL: Batch counter not incremented")
        failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed out of {passed + failed}")
    return failed == 0


def test_faiss_indexing(mm):
    """Test FAISS vector indexing and persistence."""
    print("\n" + "=" * 60)
    print("TEST: FAISS Indexing (Phase 2)")
    print("=" * 60)

    passed = 0
    failed = 0

    if mm.faiss_index is None:
        print("  SKIP: FAISS not available (no embedding model)")
        return True

    initial_count = mm.faiss_index.ntotal

    # Index some messages via on_message
    test_msgs = [
        make_message("I've been working on the Docker container setup all morning"),
        make_message("The GPU acceleration is finally working after the ROCm fix"),
        make_message("Let me check the weather forecast for Nashville this weekend"),
        make_message("Can you help me debug the network configuration?", role="assistant"),
    ]

    for msg in test_msgs:
        mm.on_message(msg)

    new_count = mm.faiss_index.ntotal
    expected_indexed = sum(1 for m in test_msgs if len(m["content"].strip()) >= 10)

    if new_count == initial_count + expected_indexed:
        print(f"  PASS: FAISS index grew from {initial_count} to {new_count} "
              f"({expected_indexed} messages indexed)")
        passed += 1
    else:
        print(f"  FAIL: Expected {initial_count + expected_indexed} vectors, got {new_count}")
        failed += 1

    # Metadata should match
    if len(mm.faiss_metadata) == new_count:
        print(f"  PASS: Metadata count matches index ({len(mm.faiss_metadata)})")
        passed += 1
    else:
        print(f"  FAIL: Metadata count {len(mm.faiss_metadata)} != index {new_count}")
        failed += 1

    # Short messages should be skipped
    mm.on_message(make_message("yes"))
    mm.on_message(make_message("ok"))
    if mm.faiss_index.ntotal == new_count:
        print(f"  PASS: Short messages correctly skipped")
        passed += 1
    else:
        print(f"  FAIL: Short messages should not be indexed")
        failed += 1

    # Save and verify persistence
    mm._save_faiss_index()
    index_file = mm.faiss_index_path / "default.index"
    meta_file = mm.faiss_index_path / "default_meta.jsonl"
    if index_file.exists() and meta_file.exists():
        print(f"  PASS: Index files persisted to disk")
        passed += 1
    else:
        print(f"  FAIL: Index files not found on disk")
        failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed out of {passed + failed}")
    return failed == 0


def test_performance(mm):
    """Verify extraction is fast (<5ms per message)."""
    print("\n" + "=" * 60)
    print("TEST: Performance")
    print("=" * 60)

    messages = [
        make_message("I prefer dark roast coffee"),
        make_message("What time is it?"),
        make_message("I love programming in Python"),
        make_message("Tell me about the weather"),
        make_message("My favorite color is blue"),
        make_message("Can you search for something?"),
        make_message("I work for a cybersecurity firm"),
        make_message("How's the network doing?"),
        make_message("I live in Tennessee"),
        make_message("Remember that the server IP is 192.168.1.100"),
    ]

    # Warm up
    mm.extract_facts_realtime(make_message("warmup message"))

    start = time.perf_counter()
    iterations = 100
    for _ in range(iterations):
        for msg in messages:
            mm.extract_facts_realtime(msg)
    elapsed = time.perf_counter() - start

    per_message_us = (elapsed / (iterations * len(messages))) * 1_000_000
    per_message_ms = per_message_us / 1000

    if per_message_ms < 5.0:
        print(f"  PASS: {per_message_us:.1f} µs per message ({per_message_ms:.3f} ms) — well under 5ms target")
    else:
        print(f"  FAIL: {per_message_ms:.2f} ms per message — exceeds 5ms target")

    print(f"         ({iterations * len(messages)} total extractions in {elapsed:.3f}s)")
    return per_message_ms < 5.0


def main():
    # Use a temp DB and temp FAISS dir so tests don't pollute production data
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db_path = f.name
    test_faiss_dir = tempfile.mkdtemp(prefix="test_faiss_")

    print(f"Using temp DB: {test_db_path}")
    print(f"Using temp FAISS dir: {test_faiss_dir}")

    # Try loading embedding model for FAISS tests
    embedding_model = None
    try:
        from sentence_transformers import SentenceTransformer
        print("Loading embedding model (all-MiniLM-L6-v2)...")
        embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
        print("Embedding model loaded")
    except ImportError:
        print("sentence-transformers not available — FAISS tests will be skipped")

    try:
        config = MockConfig(test_db_path)
        config._values["conversational_memory.faiss_index_path"] = test_faiss_dir

        # Reset singleton for test
        import core.memory_manager as mm_module
        mm_module._instance = None
        mm = mm_module.get_memory_manager(
            config=config, conversation=None,
            embedding_model=embedding_model,
        )

        results = []
        results.append(("Pattern Extraction", test_pattern_extraction(mm)))
        results.append(("CRUD Operations", test_crud(mm)))
        results.append(("on_message Hook", test_on_message(mm)))
        results.append(("FAISS Indexing", test_faiss_indexing(mm)))
        results.append(("Performance", test_performance(mm)))

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        all_passed = True
        for name, passed in results:
            status = "PASS" if passed else "FAIL"
            print(f"  {status}: {name}")
            if not passed:
                all_passed = False

        if all_passed:
            print("\n  All tests passed!")
        else:
            print("\n  Some tests failed.")
            sys.exit(1)

    finally:
        # Clean up temp DB and FAISS dir
        os.unlink(test_db_path)
        import shutil
        shutil.rmtree(test_faiss_dir, ignore_errors=True)
        # Reset singleton
        import core.memory_manager as mm_module
        mm_module._instance = None


if __name__ == "__main__":
    main()
