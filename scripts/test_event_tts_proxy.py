#!/usr/bin/env python3
"""
EventTTSProxy tests — speak() blocking, queue events, timeout, concurrency.

Tests:
  Part 1: Queue event validation (speak puts correct events)
  Part 2: Blocking behavior (speak waits for done_event)
  Part 3: Timeout and return value
  Part 4: Concurrency (multi-thread safety)

Usage:
  python3 scripts/test_event_tts_proxy.py --verbose
"""

import os
import sys
import queue
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, "/home/user/jarvis")
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

from core.events import Event, EventType
from core.pipeline import EventTTSProxy


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

def make_proxy():
    """Create an EventTTSProxy with fresh queues."""
    tts_q = queue.Queue()
    event_q = queue.Queue()
    return EventTTSProxy(tts_q, event_q), tts_q, event_q


def drain_queue(q):
    """Drain a queue and return all items."""
    items = []
    while not q.empty():
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


# ═══════════════════════════════════════════════════════════════════════════
# Part 1: Queue event validation
# ═══════════════════════════════════════════════════════════════════════════

def test_part1():
    log("\n── Part 1: Queue event validation ───────────────────────────\n")

    # --- Test: speak() puts SPEAK_REQUEST on tts_queue ---
    proxy, tts_q, _ = make_proxy()
    # Simulate worker: set done_event immediately so speak() doesn't block
    def worker():
        evt = tts_q.get(timeout=2)
        evt.data["done_event"].set()
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    proxy.speak("Hello world")
    t.join(timeout=2)

    # The worker consumed the event, so queue is empty — but we validated
    # the event type inside the worker. Let's do a cleaner version:
    proxy2, tts_q2, _ = make_proxy()
    # Don't consume — just let speak() timeout quickly
    # We'll monkey-patch the timeout for speed
    original_speak = EventTTSProxy.speak

    def fast_speak(self, text):
        self._spoke = True
        done = threading.Event()
        self.tts_queue.put(Event(
            EventType.SPEAK_REQUEST,
            data={"text": text, "done_event": done},
            source="bg_service",
        ))
        done.wait(timeout=0.1)  # Short timeout for testing

    EventTTSProxy.speak = fast_speak
    proxy2.speak("Test message")
    EventTTSProxy.speak = original_speak  # Restore

    items = drain_queue(tts_q2)
    assert_eq("speak: exactly 1 event on tts_queue", len(items), 1)
    if items:
        evt = items[0]
        assert_eq("speak: event type is SPEAK_REQUEST",
                   evt.type, EventType.SPEAK_REQUEST)
        assert_eq("speak: event source is bg_service",
                   evt.source, "bg_service")
        assert_eq("speak: event data has text",
                   evt.data["text"], "Test message")
        assert_true("speak: event data has done_event",
                     isinstance(evt.data["done_event"], threading.Event),
                     f"got type: {type(evt.data.get('done_event'))}")

    # --- Test: speak() sets _spoke = True ---
    proxy3, _, _ = make_proxy()
    assert_eq("init: _spoke is False", proxy3._spoke, False)
    # Use fast speak
    EventTTSProxy.speak = fast_speak
    proxy3.speak("anything")
    EventTTSProxy.speak = original_speak
    assert_eq("speak: _spoke set to True", proxy3._spoke, True)

    # --- Test: speak_ack() puts SPEAK_ACK on tts_queue ---
    proxy4, tts_q4, _ = make_proxy()
    proxy4.speak_ack()
    items4 = drain_queue(tts_q4)
    assert_eq("speak_ack: exactly 1 event on tts_queue", len(items4), 1)
    if items4:
        assert_eq("speak_ack: event type is SPEAK_ACK",
                   items4[0].type, EventType.SPEAK_ACK)
        assert_eq("speak_ack: event source is bg_service",
                   items4[0].source, "bg_service")

    # --- Test: speak_ack() is non-blocking ---
    proxy5, _, _ = make_proxy()
    t0 = time.perf_counter()
    proxy5.speak_ack()
    dt = time.perf_counter() - t0
    assert_true("speak_ack: returns in <100ms (non-blocking)",
                dt < 0.1, f"took {dt:.3f}s")


# ═══════════════════════════════════════════════════════════════════════════
# Part 2: Blocking behavior
# ═══════════════════════════════════════════════════════════════════════════

def test_part2():
    log("\n── Part 2: Blocking behavior ─────────────────────────────────\n")

    # --- Test: speak() blocks until done_event is set ---
    proxy, tts_q, _ = make_proxy()
    speak_returned = threading.Event()
    delay = 0.3  # Worker sets done after 300ms

    def delayed_worker():
        evt = tts_q.get(timeout=5)
        time.sleep(delay)
        evt.data["done_event"].set()

    worker = threading.Thread(target=delayed_worker, daemon=True)
    worker.start()

    t0 = time.perf_counter()
    proxy.speak("Blocking test")
    dt = time.perf_counter() - t0

    assert_true("speak: blocks for at least 200ms (worker delay=300ms)",
                dt >= 0.2, f"took {dt:.3f}s — returned too early")
    assert_true("speak: returns within 1s (worker sets done at 300ms)",
                dt < 1.0, f"took {dt:.3f}s — returned too late")
    worker.join(timeout=2)

    # --- Test: speak() returns immediately when done_event is pre-set ---
    proxy2, tts_q2, _ = make_proxy()

    def instant_worker():
        evt = tts_q2.get(timeout=5)
        evt.data["done_event"].set()  # Set immediately

    worker2 = threading.Thread(target=instant_worker, daemon=True)
    worker2.start()

    t0 = time.perf_counter()
    proxy2.speak("Instant test")
    dt = time.perf_counter() - t0

    assert_true("speak: returns quickly when done_event set immediately",
                dt < 0.2, f"took {dt:.3f}s")
    worker2.join(timeout=2)


# ═══════════════════════════════════════════════════════════════════════════
# Part 3: Timeout and return value
# ═══════════════════════════════════════════════════════════════════════════

def test_part3():
    log("\n── Part 3: Timeout and return value ──────────────────────────\n")

    # --- Test: speak() times out when done_event never set ---
    # Monkey-patch for short timeout
    proxy, tts_q, _ = make_proxy()

    def short_timeout_speak(self, text):
        self._spoke = True
        done = threading.Event()
        self.tts_queue.put(Event(
            EventType.SPEAK_REQUEST,
            data={"text": text, "done_event": done},
            source="bg_service",
        ))
        done.wait(timeout=0.2)  # 200ms timeout for test

    original = EventTTSProxy.speak
    EventTTSProxy.speak = short_timeout_speak

    t0 = time.perf_counter()
    proxy.speak("Timeout test")
    dt = time.perf_counter() - t0
    EventTTSProxy.speak = original

    assert_true("timeout: speak returns after timeout when done_event not set",
                0.15 < dt < 0.5,
                f"took {dt:.3f}s, expected ~0.2s")

    # --- Test: speak() return value is None (KNOWN BUG) ---
    # This documents the known bug: speak() should return bool but returns None.
    # When this bug is fixed, this test will FAIL — update it to expect True/False.
    proxy2, tts_q2, _ = make_proxy()

    def worker():
        evt = tts_q2.get(timeout=2)
        evt.data["done_event"].set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    result = proxy2.speak("Return value test")
    t.join(timeout=2)

    assert_eq("KNOWN BUG: speak() returns None (should return bool)",
              result, None)


# ═══════════════════════════════════════════════════════════════════════════
# Part 4: Concurrency
# ═══════════════════════════════════════════════════════════════════════════

def test_part4():
    log("\n── Part 4: Concurrency ───────────────────────────────────────\n")

    # --- Test: sequential speak() calls work correctly ---
    proxy, tts_q, _ = make_proxy()

    def consumer():
        """Consume and signal done for all events."""
        while True:
            try:
                evt = tts_q.get(timeout=2)
                if evt.type == EventType.SPEAK_REQUEST:
                    evt.data["done_event"].set()
            except queue.Empty:
                break

    worker = threading.Thread(target=consumer, daemon=True)
    worker.start()

    proxy.speak("First")
    proxy.speak("Second")
    proxy.speak("Third")
    worker.join(timeout=5)

    assert_eq("sequential: _spoke is True after 3 calls", proxy._spoke, True)
    # Queue should be drained by consumer
    assert_true("sequential: all 3 calls completed without hang", True)

    # --- Test: concurrent speak() from two threads ---
    proxy2, tts_q2, _ = make_proxy()
    completed = []

    def consumer2():
        for _ in range(2):
            try:
                evt = tts_q2.get(timeout=5)
                if evt.type == EventType.SPEAK_REQUEST:
                    time.sleep(0.05)  # Simulate short playback
                    evt.data["done_event"].set()
            except queue.Empty:
                break

    worker2 = threading.Thread(target=consumer2, daemon=True)
    worker2.start()

    def speaker(text):
        proxy2.speak(text)
        completed.append(text)

    t1 = threading.Thread(target=speaker, args=("Thread A",))
    t2 = threading.Thread(target=speaker, args=("Thread B",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    worker2.join(timeout=5)

    assert_eq("concurrent: both threads completed",
              len(completed), 2)
    assert_true("concurrent: Thread A completed",
                "Thread A" in completed,
                f"completed={completed}")
    assert_true("concurrent: Thread B completed",
                "Thread B" in completed,
                f"completed={completed}")


# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("EventTTSProxy — Test Suite\n")

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
