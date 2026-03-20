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
        is_all_day = event.get("all_day", False)

        if not title:
            continue

        # Build summary
        if is_all_day:
            # Pre-naturalize all-day event summaries so the 4B has less to interpret
            title_lower = title.lower()
            if "birthday" in title_lower:
                summary = f"Today is {title}"
            elif "paycheck" in title_lower or "payday" in title_lower or "pay day" in title_lower:
                summary = "Paycheck day today"
            elif "holiday" in title_lower or "day off" in title_lower:
                summary = f"{title} today"
            elif "anniversary" in title_lower:
                summary = f"Today is {title}"
            else:
                summary = f"{title} today"
        elif minutes_until <= 5:
            time_label = "now"
            summary = f"{title} {time_label}"
        elif minutes_until < 60:
            time_label = f"in {minutes_until} minutes"
            summary = f"{title} {time_label}"
        else:
            hours = minutes_until // 60
            mins = minutes_until % 60
            time_label = f"in {hours}h{mins}m" if mins else f"in {hours} hour{'s' if hours > 1 else ''}"
            summary = f"{title} {time_label}"

        if attendees:
            summary += f" (with {', '.join(attendees[:3])})"

        # Dedupe key: same event on same day
        dedupe = f"cal:{title}:{start_time.strftime('%Y-%m-%d') if start_time else ''}"

        # Expiry: event start time for timed events, end of day for all-day
        if is_all_day:
            expires = start_time.replace(hour=23, minute=59).timestamp() if start_time else now + 86400
        else:
            expires = start_time.timestamp() if start_time else now + 3600

        # Score components
        if is_all_day:
            tp = 0.3  # All-day events have moderate time pressure (not imminent)
            urgency = 0.4  # Lower urgency than timed meetings
        else:
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
# Reminder adapter
# ---------------------------------------------------------------------------

def adapt_reminders(reminder_manager, user_id: str = "primary_user") -> list[AwarenessItem]:
    """Convert pending/upcoming reminders to AwarenessItems.

    Surfaces two types:
      - Fired but unacknowledged reminders (needs attention)
      - Upcoming reminders due within 2 hours

    Args:
        reminder_manager: ReminderManager instance
        user_id: Owner scope

    Returns:
        List of scored AwarenessItems
    """
    if not reminder_manager:
        return []

    items = []
    now = time.time()

    # Pending acks — fired but not acknowledged
    try:
        pending = reminder_manager.get_pending_acks(created_by=user_id)
    except Exception:
        pending = []

    for rem in pending:
        title = rem.get("title", "")
        if not title:
            continue

        fired_at = rem.get("last_fired_at", "")
        summary = f"Reminder: {title} (awaiting acknowledgment)"

        dedupe = f"rem:ack:{rem.get('id', '')}"
        expires = now + 86400  # 24h — staleness guard handles cancellation

        score = _compute_score(
            urgency=0.7,
            time_pressure=0.6,
            novelty=1.0,
            user_relevance=1.0,
        )

        items.append(AwarenessItem(
            id=f"rem_{rem.get('id', 0)}",
            source="reminder",
            priority_score=score,
            user_scope={user_id},
            payload=rem,
            summary=summary,
            created_at=now,
            expires_at=expires,
            dedupe_key=dedupe,
            tags=["reminder", "pending_ack"],
        ))

    # Upcoming reminders — due within 2 hours
    try:
        upcoming = reminder_manager.list_reminders(status="pending", limit=10,
                                                    created_by=user_id)
    except Exception:
        upcoming = []

    for rem in upcoming:
        title = rem.get("title", "")
        reminder_time_str = rem.get("reminder_time", "")
        if not title or not reminder_time_str:
            continue

        try:
            reminder_dt = datetime.strptime(reminder_time_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue

        minutes_until = max(0, (reminder_dt - datetime.now()).total_seconds() / 60)

        # Only surface reminders within 2h lookahead
        if minutes_until > 120:
            continue

        # Build natural time reference
        if minutes_until <= 5:
            time_label = "now"
        elif minutes_until < 60:
            time_label = f"in {int(minutes_until)} minutes"
        else:
            hours = int(minutes_until) // 60
            mins = int(minutes_until) % 60
            time_label = f"in {hours}h{mins}m" if mins else f"in {hours} hour{'s' if hours > 1 else ''}"

        summary = f"Reminder: {title} {time_label}"
        dedupe = f"rem:upcoming:{rem.get('id', '')}"
        expires = reminder_dt.timestamp()

        urgency = 0.8 if minutes_until <= 30 else 0.5
        tp = _time_pressure(minutes_until)

        score = _compute_score(
            urgency=urgency,
            time_pressure=tp,
            novelty=1.0,
            user_relevance=1.0,
        )

        items.append(AwarenessItem(
            id=f"rem_{rem.get('id', 0)}",
            source="reminder",
            priority_score=score,
            user_scope={user_id},
            payload=rem,
            summary=summary,
            created_at=now,
            expires_at=expires,
            dedupe_key=dedupe,
            tags=["reminder", "upcoming"],
        ))

    return items


# ---------------------------------------------------------------------------
# News adapter
# ---------------------------------------------------------------------------

# News priority → urgency mapping (priority 1=critical, 2=high, 3=medium, 4=low)
_NEWS_URGENCY = {1: 0.85, 2: 0.5}


def adapt_news(news_manager, user_id: str = "primary_user") -> list[AwarenessItem]:
    """Convert critical/high priority unread news to AwarenessItems.

    Only surfaces priority 1 (critical) and 2 (high) headlines.
    Lower priority news is not time-sensitive enough for briefings.

    Args:
        news_manager: NewsManager instance
        user_id: Owner scope

    Returns:
        List of scored AwarenessItems
    """
    if not news_manager:
        return []

    try:
        headlines = news_manager.get_unread_by_category(
            max_priority=2, limit=5, user_id=user_id,
        )
    except Exception:
        return []

    items = []
    now = time.time()

    for headline in headlines:
        title = headline.get("headline", "")
        priority = headline.get("priority", 4)
        category = headline.get("category", "")
        source = headline.get("source", "")

        if not title:
            continue

        summary = title
        dedupe = f"news:{headline.get('id', '')}:{title[:50]}"
        expires = now + 86400  # 24h — news is ephemeral

        urgency = _NEWS_URGENCY.get(priority, 0.3)
        score = _compute_score(
            urgency=urgency,
            time_pressure=0.3,  # News isn't time-pressured like meetings
            novelty=1.0,
            user_relevance=0.7,  # Subscribed content, not user-specific
        )

        items.append(AwarenessItem(
            id=f"news_{headline.get('id', 0)}",
            source="news",
            priority_score=score,
            user_scope={user_id},
            payload=headline,
            summary=summary,
            created_at=now,
            expires_at=expires,
            dedupe_key=dedupe,
            tags=["news", category],
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
# Briefing Composer (CAL Component 3) — LLM-powered, on demand
# ---------------------------------------------------------------------------

# CAUTION: Qwen treats prompt examples as potential output content.
# Any specific word in an example (names, objects, actions) WILL leak
# into generation. Use generic placeholders unless the specific value
# IS the expected output. See feedback_qwen_prompt_leakage.md.

# Single-item prompt — tight, focused, no room for interpretation
_BRIEFING_PROMPT_SINGLE = """\
You are JARVIS delivering a brief spoken update to {honorific}.
The user's name is {user_name}. ONLY replace "{user_name}'s" with "your". \
Other people's names must remain unchanged.

Item: {items_block}

Rules:
- State this ONE item in a single natural sentence. Maximum 12 words.
- Do NOT add any other information, context, or commentary.
- Do NOT invent times, schedules, or actions.

Examples:
- "Your open meeting starts in two hours, {honorific}."
- "Malikai's birthday is today, {honorific}."
- "Freeze warning until nine tomorrow, {honorific}."
"""

# Multi-item prompt — gives the 4B room to weave and connect
_BRIEFING_PROMPT_MULTI = """\
You are JARVIS delivering a brief spoken update to {honorific}.
The user's name is {user_name}. ONLY replace "{user_name}'s" with "your". \
Other people's names must remain unchanged.
Context: {moment_type} at {time_of_day}.
Do NOT greet the user (they've already been greeted).

Items to weave together naturally (prioritized):
{items_block}

Rules:
- ONLY mention items listed above. Do NOT invent additional meetings, events, or details.
- Do NOT claim you have taken any actions (scheduled, arranged, prepared, set up). You are REPORTING, not acting.
- If an item says "today" without a specific time, do NOT invent a time. Say "today" only.
- Maximum 35 words
- Speak as a concise, warm butler
- Lead with the most time-sensitive item
- Group related items naturally
- Connect related items naturally when possible

Examples:
- "Your open meeting in two hours, and today marks Malikai's birthday, {honorific}."
- "Freeze warning until nine, and your team standup starts in thirty minutes, {honorific}."
"""

# Template fallback when LLM is unavailable
_BRIEFING_TEMPLATE = "You have {count} item{plural} to be aware of — {first_summary}, {honorific}."


def compose_briefing(items: list[AwarenessItem], llm_router,
                     honorific: str = "sir",
                     user_name: str = "",
                     moment_type: str = "greeting") -> str:
    """Compose a natural spoken briefing from ranked awareness items.

    Uses the 4B model for synthesis. Falls back to a simple template
    if the LLM is unavailable.

    Args:
        items: Ranked AwarenessItems (1-3, already filtered by budget)
        llm_router: LLMRouter instance for generate()
        honorific: User's honorific ("sir", "ma'am")
        user_name: User's display name (for "your" substitution)
        moment_type: "greeting", "return", "user_asked"

    Returns:
        Natural language briefing text ready for TTS
    """
    if not items:
        return ""

    # Determine time of day
    hour = datetime.now().hour
    if hour < 12:
        time_of_day = "morning"
    elif hour < 17:
        time_of_day = "afternoon"
    else:
        time_of_day = "evening"

    # Build structured items block
    lines = []
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. [{item.source}] {item.summary}")
    items_block = "\n".join(lines)

    # Select prompt: single-item (tight) vs multi-item (weaving)
    if len(items) == 1:
        prompt = _BRIEFING_PROMPT_SINGLE.format(
            honorific=honorific,
            user_name=user_name,
            items_block=items[0].summary,
        )
    else:
        prompt = _BRIEFING_PROMPT_MULTI.format(
            honorific=honorific,
            user_name=user_name,
            moment_type=moment_type,
            time_of_day=time_of_day,
            items_block=items_block,
        )

    # Try 4B synthesis — tight token budget for single items (no room to hallucinate)
    _max_tokens = 16 if len(items) == 1 else 60

    if llm_router:
        try:
            result = llm_router.generate(
                prompt,
                use_small=True,
                max_tokens=_max_tokens,
                temperature=0.6,
            )
            if result and result.strip():
                return result.strip()
        except Exception:
            pass

    # Fallback: simple template
    plural = "s" if len(items) > 1 else ""
    return _BRIEFING_TEMPLATE.format(
        count=len(items),
        plural=plural,
        first_summary=items[0].summary,
        honorific=honorific,
    )


# ---------------------------------------------------------------------------
# Awareness Accumulator
# ---------------------------------------------------------------------------

class AwarenessAccumulator:
    """Maintains a ranked priority queue of surfaceable items.

    Pure Python, no GPU, no LLM. Adapters pull from existing data sources
    and normalize into AwarenessItems with deterministic scoring.
    """

    def __init__(self, config, calendar_manager=None, weather_db=None,
                 reminder_manager=None, news_manager=None):
        self.logger = get_logger("awareness", config)
        self._config = config
        self._calendar_manager = calendar_manager
        self._weather_db = weather_db
        self._reminder_manager = reminder_manager
        self._news_manager = news_manager

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

            # Reminder adapter
            try:
                rem_items = adapt_reminders(self._reminder_manager, user_id)
                items.extend(rem_items)
            except Exception as e:
                self.logger.warning("Reminder adapter failed: %s", e)

            # News adapter
            try:
                news_items = adapt_news(self._news_manager, user_id)
                items.extend(news_items)
            except Exception as e:
                self.logger.warning("News adapter failed: %s", e)

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
                "Awareness refresh: %d items (cal=%d, wx=%d, rem=%d, news=%d)",
                len(items),
                sum(1 for i in items if i.source == "calendar"),
                sum(1 for i in items if i.source == "weather"),
                sum(1 for i in items if i.source == "reminder"),
                sum(1 for i in items if i.source == "news"),
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

    def get_critical(self, user_id: str = "primary_user") -> list[AwarenessItem]:
        """Return critical/safety items (score >= 0.85) for ambient awareness.

        Uses the same 60-second staleness check as get_top() — does NOT
        force a refresh every call. The presence detector polls every 10s;
        refreshing on every poll hammers data sources unnecessarily.
        """
        return self.get_top(n=1, threshold=0.85, user_id=user_id)

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
                               weather_db=None, reminder_manager=None,
                               news_manager=None) -> Optional[AwarenessAccumulator]:
    """Get or create the singleton AwarenessAccumulator."""
    global _instance
    if _instance is None and config is not None:
        _instance = AwarenessAccumulator(
            config=config,
            calendar_manager=calendar_manager,
            weather_db=weather_db,
            reminder_manager=reminder_manager,
            news_manager=news_manager,
        )
    return _instance
