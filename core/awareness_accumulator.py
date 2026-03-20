"""
Awareness Accumulator — CAL Component 1

Maintains a ranked priority queue of surfaceable items from all data sources.
Pure Python, no GPU, no LLM inference. Deterministic scoring.

Data sources (via thin adapters):
  - Calendar → events within configurable lookahead
  - Weather → active NWS alerts

Future adapters (Phase 4):
  - News → critical/high items not yet delivered
  - Reminders → pending acknowledgments, due/overdue
  - Memory → facts tagged for follow-up

Scoring formula:
  score = (urgency × 0.3) + (time_pressure × 0.3) + (novelty × 0.2) + (user_relevance × 0.2)
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

from core.logger import get_logger


# ---------------------------------------------------------------------------
# AwarenessItem — normalized schema for all surfaceable items
# ---------------------------------------------------------------------------

@dataclass
class AwarenessItem:
    """A single surfaceable item from any data source."""
    id: str                          # Unique identifier
    source: str                      # "calendar", "weather", "news", "reminder", "memory"
    priority_score: float = 0.0      # 0.0-1.0, computed by scoring formula
    user_scope: set = field(default_factory=lambda: {"primary_user"})
    payload: dict = field(default_factory=dict)    # Source-specific structured data
    summary: str = ""                # One-line human-readable summary
    created_at: float = 0.0          # time.time()
    expires_at: float = 0.0          # When this stops being relevant
    surfaced_at: float = 0.0         # 0 = never delivered
    dedupe_key: str = ""             # Prevents re-surfacing same item
    tags: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _time_pressure(minutes_until: float, horizon_minutes: float = 240) -> float:
    """Exponential decay: meeting in 30min → 0.95, meeting in 4h → 0.1."""
    if minutes_until <= 0:
        return 1.0  # Already happening or overdue
    if minutes_until >= horizon_minutes:
        return 0.05
    # Exponential decay: score = e^(-k * minutes)
    # Calibrated so 30min ≈ 0.95, 240min ≈ 0.1
    k = -math.log(0.1) / horizon_minutes
    return math.exp(-k * minutes_until)


def _compute_score(urgency: float, time_pressure: float,
                   novelty: float, user_relevance: float) -> float:
    """Deterministic scoring formula from CAL plan."""
    return (
        urgency * 0.3 +
        time_pressure * 0.3 +
        novelty * 0.2 +
        user_relevance * 0.2
    )


# ---------------------------------------------------------------------------
# Calendar adapter
# ---------------------------------------------------------------------------

def adapt_calendar(calendar_manager, user_id: str = "primary_user") -> list[AwarenessItem]:
    """Convert upcoming calendar events to AwarenessItems.

    Args:
        calendar_manager: GoogleCalendarSync instance
        user_id: Owner of these calendar events

    Returns:
        List of scored AwarenessItems
    """
    if not calendar_manager:
        return []

    # Morning gets a wider lookahead
    hour = datetime.now().hour
    lookahead = 4 if hour < 12 else 2

    try:
        events = calendar_manager.get_upcoming_context(hours=lookahead)
    except Exception:
        return []

    items = []
    now = time.time()

    for event in events:
        title = event.get("title", "")
        minutes_until = event.get("minutes_until", 0)
        start_time = event.get("start_time")
        attendees = event.get("attendees", [])

        if not title:
            continue

        # Build summary
        if minutes_until <= 5:
            time_label = "now"
        elif minutes_until < 60:
            time_label = f"in {minutes_until} minutes"
        else:
            hours = minutes_until // 60
            mins = minutes_until % 60
            time_label = f"in {hours}h{mins}m" if mins else f"in {hours} hour{'s' if hours > 1 else ''}"

        summary = f"{title} {time_label}"
        if attendees:
            summary += f" (with {', '.join(attendees[:3])})"

        # Dedupe key: same event on same day
        dedupe = f"cal:{title}:{start_time.strftime('%Y-%m-%d') if start_time else ''}"

        # Expiry: event start time (no point surfacing after it starts)
        expires = start_time.timestamp() if start_time else now + 3600

        # Score components
        tp = _time_pressure(minutes_until)
        urgency = 0.6  # Calendar events are moderately urgent by default
        if minutes_until <= 15:
            urgency = 0.9  # Imminent meetings are high urgency

        score = _compute_score(
            urgency=urgency,
            time_pressure=tp,
            novelty=1.0,  # Delivery log handles dedup
            user_relevance=1.0,  # User's own calendar
        )

        items.append(AwarenessItem(
            id=f"cal_{hashlib.md5(dedupe.encode()).hexdigest()[:12]}",
            source="calendar",
            priority_score=score,
            user_scope={user_id},
            payload=event,
            summary=summary,
            created_at=now,
            expires_at=expires,
            dedupe_key=dedupe,
            tags=["meeting", "calendar"],
        ))

    return items


# ---------------------------------------------------------------------------
# Weather adapter
# ---------------------------------------------------------------------------

# NWS severity → urgency mapping
_WEATHER_URGENCY = {
    "extreme": 1.0,
    "severe": 0.9,
    "moderate": 0.7,
    "minor": 0.4,
}


def adapt_weather(weather_db=None, user_id: str = "primary_user") -> list[AwarenessItem]:
    """Convert active weather alerts to AwarenessItems.

    Args:
        weather_db: WeatherDB instance (or None to fetch singleton)
        user_id: Owner scope

    Returns:
        List of scored AwarenessItems
    """
    if weather_db is None:
        try:
            from core.weather_db import get_weather_db
            weather_db = get_weather_db()
        except Exception:
            return []

    if not weather_db:
        return []

    try:
        alerts = weather_db.get_active_alerts()
    except Exception:
        return []

    items = []
    now = time.time()

    for alert in alerts:
        headline = alert.get("headline", "")
        severity = alert.get("severity", "minor").lower()
        event_type = alert.get("event", "")
        expires = alert.get("expires", "")
        description = alert.get("description", "")

        if not headline and not event_type:
            continue

        summary = headline or event_type

        # Parse expiry
        expires_ts = now + 86400  # Default: 24h
        if expires:
            try:
                expires_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                expires_ts = expires_dt.timestamp()
            except (ValueError, TypeError):
                pass

        # Time pressure: how soon does the alert expire?
        minutes_remaining = max(0, (expires_ts - now) / 60)

        # Dedupe key
        dedupe = f"wx:{event_type}:{severity}:{expires}"

        urgency = _WEATHER_URGENCY.get(severity, 0.4)
        score = _compute_score(
            urgency=urgency,
            time_pressure=_time_pressure(minutes_remaining, horizon_minutes=1440),
            novelty=1.0,  # Delivery log handles dedup
            user_relevance=0.7,  # Weather is relevant but not user-specific
        )

        items.append(AwarenessItem(
            id=f"wx_{hashlib.md5(dedupe.encode()).hexdigest()[:12]}",
            source="weather",
            priority_score=score,
            user_scope={user_id},
            payload=alert,
            summary=summary,
            created_at=now,
            expires_at=expires_ts,
            dedupe_key=dedupe,
            tags=["weather", "alert", severity],
        ))

    return items


# ---------------------------------------------------------------------------
# Delivery Log — SQLite dedup
# ---------------------------------------------------------------------------

class DeliveryLog:
    """Tracks which items have been surfaced to which users."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS briefing_log (
                        id INTEGER PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        item_hash TEXT NOT NULL,
                        source TEXT,
                        summary TEXT,
                        surfaced_at REAL NOT NULL,
                        session_id TEXT,
                        UNIQUE(user_id, item_hash)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_briefing_user
                    ON briefing_log(user_id, surfaced_at)
                """)
                conn.commit()
            finally:
                conn.close()

    def was_surfaced(self, user_id: str, dedupe_key: str, ttl_hours: float = 24) -> bool:
        """Check if an item was already surfaced within the TTL window."""
        item_hash = hashlib.md5(dedupe_key.encode()).hexdigest()
        cutoff = time.time() - (ttl_hours * 3600)
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT surfaced_at FROM briefing_log "
                    "WHERE user_id = ? AND item_hash = ? AND surfaced_at > ?",
                    (user_id, item_hash, cutoff),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    def mark_surfaced(self, user_id: str, item: AwarenessItem, session_id: str = ""):
        """Record that an item was surfaced to a user."""
        item_hash = hashlib.md5(item.dedupe_key.encode()).hexdigest()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO briefing_log "
                    "(user_id, item_hash, source, summary, surfaced_at, session_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, item_hash, item.source, item.summary, time.time(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    def cleanup(self, max_age_days: int = 30):
        """Remove old delivery log entries."""
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "DELETE FROM briefing_log WHERE surfaced_at < ?", (cutoff,)
                )
                conn.commit()
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Awareness Accumulator
# ---------------------------------------------------------------------------

class AwarenessAccumulator:
    """Maintains a ranked priority queue of surfaceable items.

    Pure Python, no GPU, no LLM. Adapters pull from existing data sources
    and normalize into AwarenessItems with deterministic scoring.
    """

    def __init__(self, config, calendar_manager=None, weather_db=None):
        self.logger = get_logger("awareness", config)
        self._config = config
        self._calendar_manager = calendar_manager
        self._weather_db = weather_db

        # Delivery log for dedup
        data_dir = config.get("system.storage_path", "/mnt/storage/jarvis")
        db_path = Path(data_dir) / "data" / "briefing_log.db"
        self._delivery_log = DeliveryLog(db_path)

        # Cached items (refreshed on demand)
        self._items: list[AwarenessItem] = []
        self._last_refresh: float = 0
        self._lock = threading.Lock()

        self.logger.info("Awareness Accumulator initialized")

    def refresh(self, user_id: str = "primary_user") -> int:
        """Pull from all adapters, score, rank, and cache.

        Returns number of items in the queue after refresh.
        """
        with self._lock:
            items = []

            # Calendar adapter
            try:
                cal_items = adapt_calendar(self._calendar_manager, user_id)
                items.extend(cal_items)
            except Exception as e:
                self.logger.warning("Calendar adapter failed: %s", e)

            # Weather adapter
            try:
                wx_items = adapt_weather(self._weather_db, user_id)
                items.extend(wx_items)
            except Exception as e:
                self.logger.warning("Weather adapter failed: %s", e)

            # Apply novelty scoring from delivery log
            for item in items:
                if self._delivery_log.was_surfaced(user_id, item.dedupe_key):
                    # Already surfaced — reduce novelty component
                    item.priority_score = _compute_score(
                        urgency=item.priority_score / 0.3 * 0.3,  # preserve urgency
                        time_pressure=item.priority_score,  # rough preservation
                        novelty=0.1,  # Already seen
                        user_relevance=1.0 if item.source == "calendar" else 0.7,
                    )

            # Filter expired items
            now = time.time()
            items = [i for i in items if i.expires_at > now]

            # Sort by priority (highest first)
            items.sort(key=lambda i: i.priority_score, reverse=True)

            self._items = items
            self._last_refresh = now

            self.logger.info(
                "Awareness refresh: %d items (cal=%d, wx=%d)",
                len(items),
                sum(1 for i in items if i.source == "calendar"),
                sum(1 for i in items if i.source == "weather"),
            )

            return len(items)

    def get_top(self, n: int = 3, threshold: float = 0.3,
                user_id: str = "primary_user") -> list[AwarenessItem]:
        """Return top N items above the score threshold for a user.

        If data is stale (>60s), triggers a refresh first.
        """
        # Auto-refresh if stale
        if time.time() - self._last_refresh > 60:
            self.refresh(user_id)

        with self._lock:
            results = []
            for item in self._items:
                if item.priority_score < threshold:
                    break  # Sorted descending, no more above threshold
                if user_id in item.user_scope:
                    results.append(item)
                if len(results) >= n:
                    break
            return results

    def mark_surfaced(self, items: list[AwarenessItem], user_id: str = "primary_user",
                      session_id: str = ""):
        """Record that items were delivered to a user."""
        for item in items:
            self._delivery_log.mark_surfaced(user_id, item, session_id)
            item.surfaced_at = time.time()

    @property
    def item_count(self) -> int:
        """Number of items currently in the queue."""
        return len(self._items)

    @property
    def last_refresh_age(self) -> float:
        """Seconds since last refresh."""
        return time.time() - self._last_refresh if self._last_refresh else float('inf')


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_instance: Optional[AwarenessAccumulator] = None


def get_awareness_accumulator(config=None, calendar_manager=None,
                               weather_db=None) -> Optional[AwarenessAccumulator]:
    """Get or create the singleton AwarenessAccumulator."""
    global _instance
    if _instance is None and config is not None:
        _instance = AwarenessAccumulator(
            config=config,
            calendar_manager=calendar_manager,
            weather_db=weather_db,
        )
    return _instance
