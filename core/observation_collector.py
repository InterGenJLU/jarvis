"""Observation Collector — JARVIS self-evolution Phase 1.

Runs on a background schedule, queries event data for patterns,
and stores findings as scores in the event logger. Each pattern
detector is an independent function that asks one question of the
database and returns what it found.

Adding a new detector:
  1. Write a function: def detect_something(el, hours) -> list[Finding]
  2. Add it to DETECTORS list at the bottom of this file
  3. Done. Next collection cycle picks it up automatically.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)

_instance: Optional["ObservationCollector"] = None


def get_observation_collector(config=None):
    """Get or create the singleton ObservationCollector."""
    global _instance
    if _instance is None and config is not None:
        _instance = ObservationCollector(config)
    return _instance


# -----------------------------------------------------------------------
# Finding dataclass — the standard output of every detector
# -----------------------------------------------------------------------

@dataclass
class Finding:
    """A single pattern detected in operational data."""
    detector: str           # which detector found it
    severity: str           # critical, high, medium, low, info
    title: str              # short description
    detail: str             # full explanation with evidence
    evidence_ids: list = field(default_factory=list)  # observation/event IDs
    count: int = 0          # how many occurrences
    category: str = ""      # grouping: stt, tts, routing, latency, etc.
    suggested_action: str = ""  # what should be done about it

    SEVERITIES = ("critical", "high", "medium", "low", "info")


# -----------------------------------------------------------------------
# Pattern Detectors — each is an independent function
# -----------------------------------------------------------------------

def detect_stt_failures(el, hours: float = 24) -> list[Finding]:
    """Find repeated STT mistranscriptions and empty results."""
    findings = []
    events = el.query(event="stt_transcription", hours=hours, limit=500)
    if not events:
        return findings

    total = len(events)
    empty = [e for e in events if e.get("status") == "empty"]
    errors = [e for e in events if e.get("status") == "error"]

    # High empty rate
    if total >= 5 and len(empty) / total > 0.20:
        findings.append(Finding(
            detector="stt_failures",
            severity="medium",
            title=f"STT empty rate {len(empty)}/{total} ({100*len(empty)//total}%)",
            detail=f"{len(empty)} of {total} transcriptions returned empty in the last {hours}h. "
                   f"This may indicate mic issues, VAD sensitivity, or ambient noise.",
            evidence_ids=[e.get("id", "") for e in empty[:10]],
            count=len(empty),
            category="stt",
            suggested_action="Check mic levels, VAD threshold, and ambient noise conditions.",
        ))

    # Any errors
    if errors:
        findings.append(Finding(
            detector="stt_failures",
            severity="high",
            title=f"STT errors: {len(errors)} in last {hours}h",
            detail=f"{len(errors)} Whisper transcription errors.",
            evidence_ids=[e.get("id", "") for e in errors[:10]],
            count=len(errors),
            category="stt",
            suggested_action="Check Whisper model loading and GPU memory.",
        ))

    return findings


def detect_missing_ack(el, hours: float = 24) -> list[Finding]:
    """Find LLM fallback routes that had no contextual acknowledgment."""
    findings = []
    routes = el.query(event="route_completed", hours=hours, limit=500)
    if not routes:
        return findings

    # LLM fallback routes (not handled by skill/CAL-L0)
    fallbacks = []
    for r in routes:
        meta = r.get("metadata", {}) or {}
        if not meta.get("handled", False):
            fallbacks.append(r)

    if not fallbacks:
        return findings

    # Check if there's an ack event near each fallback
    # For now, count fallbacks as potential missing-ack events
    # (True ack detection requires correlating with TTS events)
    if len(fallbacks) >= 3:
        findings.append(Finding(
            detector="missing_ack",
            severity="medium",
            title=f"{len(fallbacks)} voice LLM-fallback routes in {hours}h",
            detail=f"{len(fallbacks)} voice queries fell to LLM without skill handling. "
                   f"These are likely missing contextual acknowledgments — the user hears "
                   f"silence while the LLM generates.",
            evidence_ids=[r.get("id", "") for r in fallbacks[:10]],
            count=len(fallbacks),
            category="routing",
            suggested_action="Ensure contextual ack (4B) fires on ALL LLM fallback voice paths.",
        ))

    return findings


def detect_tts_pronunciation(el, hours: float = 168) -> list[Finding]:
    """Surface TTS pronunciation issues from odd events."""
    findings = []
    odd_events = el.get_odd_events(resolved=False)
    tts_events = [e for e in odd_events
                  if e.get("category") == "tts" and not e.get("resolved")]

    if tts_events:
        findings.append(Finding(
            detector="tts_pronunciation",
            severity="low",
            title=f"{len(tts_events)} unresolved TTS pronunciation issues",
            detail="Reported TTS pronunciation problems: " +
                   "; ".join(e.get("description", "")[:80] for e in tts_events[:5]),
            evidence_ids=[e.get("id", "") for e in tts_events],
            count=len(tts_events),
            category="tts",
            suggested_action="Build a pronunciation dictionary for problem words/phrases.",
        ))

    return findings


def detect_watchdog_interventions(el, hours: float = 24) -> list[Finding]:
    """Track watchdog recovery frequency."""
    findings = []
    events = el.query(category="error_recovery", hours=hours, limit=200)
    if not events:
        return findings

    # Group by event type
    by_type = {}
    for e in events:
        evt = e.get("event", "unknown")
        by_type.setdefault(evt, []).append(e)

    for evt_type, evts in by_type.items():
        severity = "high" if len(evts) >= 5 else "medium" if len(evts) >= 2 else "low"
        findings.append(Finding(
            detector="watchdog_interventions",
            severity=severity,
            title=f"Watchdog: {evt_type} x{len(evts)} in {hours}h",
            detail=f"{len(evts)} {evt_type} recoveries in the last {hours} hours.",
            evidence_ids=[e.get("id", "") for e in evts[:10]],
            count=len(evts),
            category="error_recovery",
            suggested_action="Investigate root cause of repeated watchdog triggers.",
        ))

    return findings


def detect_tool_decision_failures(el, hours: float = 24) -> list[Finding]:
    """Find queries asking for current info that didn't trigger web search."""
    findings = []
    routes = el.query(event="route_completed", hours=hours, limit=500)
    if not routes:
        return findings

    CURRENT_INFO_SIGNALS = (
        "latest", "current", "update", "right now", "today",
        "this week", "this month", "recent", "just happened",
        "breaking", "new", "what's happening",
    )

    missed = []
    for r in routes:
        meta = r.get("metadata", {}) or {}
        command = (meta.get("command") or "").lower()
        intent = meta.get("intent", "")
        has_tools = meta.get("has_tools", False)

        # Query contains current-info signals but didn't use tools
        if any(signal in command for signal in CURRENT_INFO_SIGNALS):
            if intent not in ("tool_calling",) and not has_tools:
                missed.append(r)

    if missed:
        commands = [((r.get("metadata") or {}).get("command") or "")[:60]
                    for r in missed[:5]]
        findings.append(Finding(
            detector="tool_decision_failures",
            severity="high",
            title=f"{len(missed)} queries needed web search but didn't get it",
            detail=f"Queries containing current-info signals (latest, update, etc.) "
                   f"that were answered without web search: {commands}",
            evidence_ids=[r.get("id", "") for r in missed[:10]],
            count=len(missed),
            category="routing",
            suggested_action="Strengthen tool-use prompting for queries with temporal signals. "
                             "Consider a pre-LLM classifier that forces web_search for "
                             "'latest/current/update' patterns.",
        ))

    return findings


def detect_latency_outliers(el, hours: float = 24) -> list[Finding]:
    """Find interactions with abnormally high latency."""
    findings = []
    events = el.query(event="route_completed", hours=hours, limit=500)
    if not events:
        return findings

    latencies = []
    for e in events:
        meta = e.get("metadata", {}) or {}
        lat = meta.get("latency_ms")
        if lat and isinstance(lat, (int, float)):
            latencies.append((lat, e))

    if len(latencies) < 5:
        return findings

    latencies.sort(key=lambda x: x[0])
    p50_idx = len(latencies) // 2
    p95_idx = int(len(latencies) * 0.95)
    p50 = latencies[p50_idx][0]
    p95 = latencies[p95_idx][0]

    # Flag if p95 is more than 3x p50
    if p95 > p50 * 3 and p95 > 5000:
        outliers = [(lat, e) for lat, e in latencies if lat > p95]
        findings.append(Finding(
            detector="latency_outliers",
            severity="medium",
            title=f"Latency p95={p95:.0f}ms (p50={p50:.0f}ms) — {len(outliers)} outliers",
            detail=f"P95 latency is {p95/p50:.1f}x the median. "
                   f"{len(outliers)} interactions exceeded {p95:.0f}ms.",
            evidence_ids=[e.get("id", "") for _, e in outliers[:10]],
            count=len(outliers),
            category="latency",
            suggested_action="Investigate slow interactions — check LLM response time, "
                             "tool execution latency, and TTS generation.",
        ))

    return findings


def detect_odd_event_clusters(el, hours: float = 168) -> list[Finding]:
    """Surface when odd events accumulate in a category."""
    findings = []
    patterns = el.get_odd_event_patterns(hours=hours)
    if not patterns or not patterns.get("by_category"):
        return findings

    for category, count in patterns["by_category"].items():
        if count >= 3:
            severity = "high" if count >= 5 else "medium"
            findings.append(Finding(
                detector="odd_event_clusters",
                severity=severity,
                title=f"Odd event cluster: {category} x{count}",
                detail=f"{count} unresolved odd events in category '{category}' "
                       f"over the last {hours/24:.0f} days. This suggests a recurring "
                       f"pattern that needs systematic attention.",
                count=count,
                category=category,
                suggested_action=f"Review odd events in category '{category}' and identify root cause.",
            ))

    return findings


def detect_conversation_imbalance(el, hours: float = 24) -> list[Finding]:
    """Check if conversation opens/closes are balanced."""
    findings = []
    opens = el.count(event="conversation_opened", hours=hours)
    closes = el.count(event="conversation_closed", hours=hours)

    if opens > 0 and closes == 0:
        findings.append(Finding(
            detector="conversation_imbalance",
            severity="medium",
            title=f"{opens} conversation opens, 0 closes in {hours}h",
            detail="Conversations are being opened but never closed. "
                   "This suggests the close event isn't firing on timeout.",
            count=opens,
            category="lifecycle",
            suggested_action="Verify conversation_closed emits on the timeout path.",
        ))
    elif opens > 0 and abs(opens - closes) > max(2, opens * 0.3):
        findings.append(Finding(
            detector="conversation_imbalance",
            severity="low",
            title=f"Conversation open/close imbalance: {opens} opens, {closes} closes",
            detail=f"Open/close counts don't match in the last {hours}h.",
            count=abs(opens - closes),
            category="lifecycle",
            suggested_action="Check for edge cases where conversation windows close "
                             "without emitting the close event.",
        ))

    return findings


# -----------------------------------------------------------------------
# The detector registry — add new detectors here
# -----------------------------------------------------------------------

DETECTORS = [
    detect_stt_failures,
    detect_missing_ack,
    detect_tts_pronunciation,
    detect_watchdog_interventions,
    detect_tool_decision_failures,
    detect_latency_outliers,
    detect_odd_event_clusters,
    detect_conversation_imbalance,
]


# -----------------------------------------------------------------------
# ObservationCollector — the scheduler that runs detectors
# -----------------------------------------------------------------------

class ObservationCollector:
    """Background service that periodically analyzes operational data."""

    def __init__(self, config, interval_hours: float = 6):
        self.config = config
        self.interval = interval_hours * 3600
        self.lookback_hours = interval_hours * 2  # overlap to catch patterns
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_run: float = 0
        self._findings_history: list[dict] = []
        logger.info(
            "ObservationCollector initialized: interval=%sh, lookback=%sh, detectors=%d",
            interval_hours, self.lookback_hours, len(DETECTORS),
        )

    def start(self):
        """Start the background collection thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="observation-collector",
        )
        self._thread.start()
        logger.info("ObservationCollector started")

    def stop(self):
        """Stop the background thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("ObservationCollector stopped")

    def _run_loop(self):
        """Main loop — run collection, sleep, repeat."""
        # Initial delay: let the system warm up before first analysis
        self._stop_event.wait(timeout=300)  # 5 min warmup
        while not self._stop_event.is_set():
            try:
                findings = self.collect()
                if findings:
                    logger.info(
                        "ObservationCollector: %d findings from %d detectors",
                        len(findings), len(DETECTORS),
                    )
                    self._store_findings(findings)
                else:
                    logger.info("ObservationCollector: no findings this cycle")
            except Exception as e:
                logger.error("ObservationCollector cycle failed: %s", e)
            self._last_run = time.time()
            self._stop_event.wait(timeout=self.interval)

    def collect(self, hours: float = None) -> list[Finding]:
        """Run all detectors and return findings.

        Can be called manually for on-demand analysis.
        """
        from core.event_logger import get_event_logger
        el = get_event_logger()
        if not el:
            logger.warning("ObservationCollector: no event logger available")
            return []

        lookback = hours or self.lookback_hours
        findings = []
        for detector in DETECTORS:
            try:
                t0 = time.time()
                results = detector(el, lookback)
                dt = (time.time() - t0) * 1000
                if results:
                    findings.extend(results)
                    logger.debug(
                        "Detector %s: %d findings in %.0fms",
                        detector.__name__, len(results), dt,
                    )
            except Exception as e:
                logger.error("Detector %s failed: %s", detector.__name__, e)

        # Sort by severity
        severity_order = {s: i for i, s in enumerate(Finding.SEVERITIES)}
        findings.sort(key=lambda f: severity_order.get(f.severity, 99))

        return findings

    def _store_findings(self, findings: list[Finding]):
        """Store findings as scores in the event logger."""
        from core.event_logger import get_event_logger
        el = get_event_logger()
        if not el:
            return

        for f in findings:
            try:
                # Use a synthetic observation ID based on detector + timestamp
                obs_id = f"finding_{f.detector}_{int(time.time())}"
                el.add_score(
                    observation_id=obs_id,
                    name=f"finding:{f.detector}",
                    value=f.count,
                    label=f.severity,
                    data_type="numeric",
                    source="automated",
                    comment=f"{f.title}\n\n{f.detail}\n\nSuggested: {f.suggested_action}",
                )
            except Exception as e:
                logger.error("Failed to store finding from %s: %s", f.detector, e)

        self._findings_history.append({
            "timestamp": time.time(),
            "count": len(findings),
            "findings": [
                {
                    "detector": f.detector,
                    "severity": f.severity,
                    "title": f.title,
                    "count": f.count,
                    "category": f.category,
                }
                for f in findings
            ],
        })
        # Keep last 50 cycles in memory
        if len(self._findings_history) > 50:
            self._findings_history = self._findings_history[-50:]

    def get_latest_findings(self) -> dict:
        """Return the most recent collection results."""
        if not self._findings_history:
            return {"timestamp": None, "count": 0, "findings": []}
        return self._findings_history[-1]

    def get_history(self, limit: int = 10) -> list[dict]:
        """Return recent collection history."""
        return self._findings_history[-limit:]

    def run_now(self) -> list[Finding]:
        """Trigger an immediate collection cycle (for testing/on-demand)."""
        logger.info("ObservationCollector: manual collection triggered")
        findings = self.collect()
        if findings:
            self._store_findings(findings)
        self._last_run = time.time()
        return findings
