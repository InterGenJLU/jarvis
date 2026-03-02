"""
Reminder Manager

Core engine for the JARVIS reminder system. Handles database storage,
background polling, firing reminders with priority-based tones,
acknowledgment tracking, nag behavior, missed reminder recovery,
and daily rundowns.

Uses a singleton pattern so the ReminderSkill can access the same instance.
"""

import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Callable

from core.logger import get_logger
from core.honorific import get_honorific


# Singleton instance
_instance: Optional["ReminderManager"] = None


def get_reminder_manager(config=None, tts=None, conversation=None) -> Optional["ReminderManager"]:
    """Get or create the singleton ReminderManager.

    Call with all args on first invocation (from jarvis_continuous.py).
    Call with no args from skills to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = ReminderManager(config, tts, conversation)
    return _instance


class ReminderManager:
    """Core reminder engine with background polling and priority-based notifications."""

    def __init__(self, config, tts, conversation):
        self.config = config
        self.tts = tts
        self.conversation = conversation
        self.logger = get_logger(__name__, config)

        # Database
        db_path = config.get("reminders.db_path",
                             "/mnt/storage/jarvis/data/reminders.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.Lock()
        self._init_db()

        # Polling config
        self.poll_interval = config.get("reminders.poll_interval_seconds", 30)
        self.missed_lookback_hours = config.get("reminders.missed_lookback_hours", 24)
        self.startup_delay = config.get("reminders.startup_announce_delay_seconds", 5)
        self.default_snooze = config.get("reminders.default_snooze_minutes", 15)

        # Nag config
        self.nag_critical_min = config.get("reminders.nag.critical_minutes", 5)
        self.nag_high_min = config.get("reminders.nag.high_minutes", 15)
        self.nag_backoff_count = config.get("reminders.nag.backoff_after_count", 3)
        self.nag_backoff_min = config.get("reminders.nag.backoff_minutes", 30)

        # Daily rundown config
        self.rundown_enabled = config.get("reminders.daily_rundown.enabled", True)
        rundown_time = config.get("reminders.daily_rundown.time", "08:15")
        parts = rundown_time.split(":")
        self.rundown_hour = int(parts[0])
        self.rundown_minute = int(parts[1])
        self._last_rundown_date = None
        self._offer_timeout = config.get("reminders.daily_rundown.offer_timeout_seconds", 30)
        self._retry_delay = config.get("reminders.daily_rundown.retry_delay_minutes", 5)

        # Weekly rundown config
        self.weekly_rundown_enabled = config.get("reminders.weekly_rundown.enabled", True)
        self._weekly_day = config.get("reminders.weekly_rundown.day", "monday").lower()
        self._weekly_day_num = {"monday": 0, "tuesday": 1, "wednesday": 2,
                                "thursday": 3, "friday": 4, "saturday": 5,
                                "sunday": 6}.get(self._weekly_day, 0)

        # Rundown state machine
        self._rundown_state = None  # None | "offered" | "re-asked" | "deferred"
        self._rundown_cycle = 0     # 1 or 2 (which retry cycle)
        self._rundown_offered_at = None  # datetime of last offer/re-ask/defer
        self._rundown_pending_mention = False  # mention on next wake word
        self._rundown_is_weekly = False  # True when weekly rundown pending
        self._last_weekly_rundown_date = None

        # Audio assets
        self._assets_dir = Path(__file__).parent.parent / "assets"
        self._audio_device = config.get("audio.output_device", "plughw:0,0")

        # Background thread state
        self._running = False
        self._poll_thread = None

        # Ack tracking
        self._last_announced_id = None
        self._ack_window_callback: Optional[Callable] = None
        self._open_window_callback: Optional[Callable] = None  # (duration) -> open conversation window
        self._pause_listener_callback: Optional[Callable] = None
        self._resume_listener_callback: Optional[Callable] = None

        # Google Calendar integration (set via set_calendar_manager)
        self._calendar_manager = None

        self.logger.info("ReminderManager initialized")

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create the reminders table if it doesn't exist, and run migrations."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS reminders (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        title           TEXT NOT NULL,
                        description     TEXT DEFAULT '',
                        reminder_time   TEXT NOT NULL,
                        reminder_type   TEXT NOT NULL DEFAULT 'one_time',
                        recurrence_rule TEXT DEFAULT NULL,
                        priority        INTEGER NOT NULL DEFAULT 3,
                        status          TEXT NOT NULL DEFAULT 'pending',
                        requires_ack    INTEGER NOT NULL DEFAULT 0,
                        ack_at          TEXT DEFAULT NULL,
                        fire_count      INTEGER NOT NULL DEFAULT 0,
                        last_fired_at   TEXT DEFAULT NULL,
                        snooze_until    TEXT DEFAULT NULL,
                        google_event_id TEXT DEFAULT NULL,
                        event_time      TEXT DEFAULT NULL,
                        created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                        updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                    )
                """)
                conn.commit()

                # Migrations: add columns if table already existed without them
                columns = [row[1] for row in conn.execute("PRAGMA table_info(reminders)")]
                if "google_event_id" not in columns:
                    conn.execute("ALTER TABLE reminders ADD COLUMN google_event_id TEXT DEFAULT NULL")
                    conn.commit()
                    self.logger.info("Migrated: added google_event_id column")
                if "event_time" not in columns:
                    conn.execute("ALTER TABLE reminders ADD COLUMN event_time TEXT DEFAULT NULL")
                    conn.commit()
                    self.logger.info("Migrated: added event_time column")

                # Indexes (after migrations so all columns exist)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_reminders_status_time
                    ON reminders(status, reminder_time)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_reminders_priority
                    ON reminders(priority)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_reminders_google_event
                    ON reminders(google_event_id)
                """)
                conn.commit()

            finally:
                conn.close()
        self.logger.info(f"Reminder database ready at {self.db_path}")

    def _conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with row_factory."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_reminder(self, title: str, reminder_time: datetime,
                     priority: int = 3, reminder_type: str = "one_time",
                     recurrence_rule: str = None, description: str = "",
                     _skip_calendar_push: bool = False,
                     event_time: datetime = None) -> int:
        """Add a new reminder. Returns the reminder ID.

        _skip_calendar_push is used internally when creating from Google sync
        to avoid pushing back to Google in a loop.

        event_time: the actual event start time when reminder_time is offset
        (e.g., reminder fires 15 min before the event). Used for speech:
        "You have X in 15 minutes."
        """
        requires_ack = 1 if priority <= 2 else 0
        time_str = reminder_time.strftime("%Y-%m-%d %H:%M:%S")
        event_time_str = event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else None

        # Push to Google Calendar first (so we can store the event ID)
        google_event_id = None
        if not _skip_calendar_push and self._calendar_manager:
            # Push the actual event time, not the offset reminder time
            push_time = event_time if event_time else reminder_time
            google_event_id = self._calendar_manager.create_event(
                title, push_time, priority, description
            )

        with self._db_lock:
            conn = self._conn()
            try:
                cur = conn.execute("""
                    INSERT INTO reminders
                        (title, description, reminder_time, reminder_type,
                         recurrence_rule, priority, requires_ack, google_event_id,
                         event_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (title, description, time_str, reminder_type,
                      recurrence_rule, priority, requires_ack, google_event_id,
                      event_time_str))
                conn.commit()
                rid = cur.lastrowid
            finally:
                conn.close()

        self.logger.info(
            f"Reminder #{rid} created: '{title}' at {time_str} "
            f"{'(event at ' + event_time_str + ') ' if event_time_str else ''}"
            f"(priority={priority}, type={reminder_type}"
            f"{', gcal=' + google_event_id if google_event_id else ''})"
        )
        return rid

    def get_reminder(self, reminder_id: int) -> Optional[Dict]:
        """Get a single reminder by ID."""
        with self._db_lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_reminders(self, status: str = "pending", limit: int = 20) -> List[Dict]:
        """List reminders filtered by status."""
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status = ? "
                    "ORDER BY reminder_time ASC LIMIT ?",
                    (status, limit)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def list_today(self) -> List[Dict]:
        """List all pending/fired reminders for today."""
        today_start = datetime.now().replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        today_end = datetime.now().replace(hour=23, minute=59, second=59).strftime("%Y-%m-%d %H:%M:%S")
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status IN ('pending', 'fired') "
                    "AND reminder_time BETWEEN ? AND ? "
                    "ORDER BY reminder_time ASC",
                    (today_start, today_end)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def cancel_reminder(self, reminder_id: int) -> bool:
        """Cancel a reminder by ID."""
        # Get reminder first so we can clean up Google Calendar
        reminder = self.get_reminder(reminder_id)

        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE reminders SET status = 'cancelled', "
                    "updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (reminder_id,)
                )
                conn.commit()
            finally:
                conn.close()

        # Remove from Google Calendar (strip composite offset suffix)
        if reminder and self._calendar_manager and reminder.get("google_event_id"):
            base_id = self._base_google_event_id(reminder["google_event_id"])
            self._calendar_manager.delete_event(base_id)

        return True

    def cancel_by_title(self, title_fragment: str) -> Optional[Dict]:
        """Cancel the first pending reminder whose title contains the fragment.

        Returns the cancelled reminder dict, or None if not found.
        """
        fragment_lower = title_fragment.strip().lower()
        pending = self.list_reminders("pending", limit=100)
        # Also check fired (unacked) reminders
        pending.extend(self.list_reminders("fired", limit=100))

        for r in pending:
            if fragment_lower in r["title"].lower():
                self.cancel_reminder(r["id"])
                self.logger.info(f"Cancelled reminder #{r['id']}: {r['title']}")
                return r
        return None

    def _update_status(self, reminder_id: int, status: str, **extra):
        """Update a reminder's status and any extra fields."""
        sets = ["status = ?", "updated_at = datetime('now', 'localtime')"]
        vals = [status]
        for k, v in extra.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(reminder_id)

        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    f"UPDATE reminders SET {', '.join(sets)} WHERE id = ?",
                    tuple(vals)
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Time Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_natural_time(text: str) -> Optional[datetime]:
        """Parse a natural language time expression into a datetime.

        Uses dateutil for structured expressions and regex for relative ones.
        Returns None if parsing fails.
        """
        if not text:
            return None

        text = text.strip().lower()

        # Handle relative expressions: "in X minutes/hours/days"
        m = re.match(r"in\s+(\d+)\s+(minute|min|hour|hr|day)s?", text)
        if m:
            amount = int(m.group(1))
            unit = m.group(2)
            if unit in ("minute", "min"):
                return datetime.now() + timedelta(minutes=amount)
            elif unit in ("hour", "hr"):
                return datetime.now() + timedelta(hours=amount)
            elif unit == "day":
                return datetime.now() + timedelta(days=amount)

        now = datetime.now()

        # Handle "tomorrow [morning/afternoon/evening] [at TIME]"
        tomorrow = False
        if text.startswith("tomorrow"):
            tomorrow = True
            text = text.replace("tomorrow", "", 1).strip()

        # Handle "tonight [at TIME]"
        tonight = False
        if text.startswith("tonight"):
            tonight = True
            text = text.replace("tonight", "", 1).strip()

        # Handle "noon" and "midnight" keywords
        if "noon" in text:
            text = text.replace("noon", "12:00 PM")
        if "midnight" in text:
            text = text.replace("midnight", "12:00 AM")

        # Strip "morning/afternoon/evening" and map to default hours
        default_hour = None
        for period, hour in [("morning", 8), ("afternoon", 14), ("evening", 19), ("night", 21)]:
            if period in text:
                default_hour = hour
                text = text.replace(period, "").strip()
                break

        # Strip leading "at"
        if text.startswith("at "):
            text = text[3:].strip()

        # Try to parse whatever remains as a time/date
        parsed_hour = None
        parsed_minute = 0

        if text:
            # Try simple time patterns first: "6", "6 PM", "6:30 PM", "8.30 AM", "18:00"
            time_m = re.match(
                r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.?|p\.m\.?)?$", text, re.I
            )
            if time_m:
                parsed_hour = int(time_m.group(1))
                parsed_minute = int(time_m.group(2) or 0)
                ampm = (time_m.group(3) or "").lower().replace(".", "")
                if ampm == "pm" and parsed_hour < 12:
                    parsed_hour += 12
                elif ampm == "am" and parsed_hour == 12:
                    parsed_hour = 0
                elif not ampm and tonight and parsed_hour < 12:
                    parsed_hour += 12
            else:
                # Fall back to dateutil for complex expressions
                try:
                    from dateutil import parser as dateutil_parser
                    result = dateutil_parser.parse(text, fuzzy=True)
                    parsed_hour = result.hour
                    parsed_minute = result.minute
                    # If dateutil also parsed a date (not today), use it
                    if result.date() != now.date() and not tomorrow:
                        if result <= now:
                            result += timedelta(days=1)
                        return result
                except (ValueError, OverflowError):
                    pass

        # Build the final datetime
        if parsed_hour is not None:
            hour = parsed_hour
            minute = parsed_minute
        elif default_hour is not None:
            hour = default_hour
            minute = 0
        elif tonight:
            hour = 21  # Default "tonight" = 9 PM
            minute = 0
        else:
            # No time info at all, try full dateutil parse on original
            try:
                from dateutil import parser as dateutil_parser
                result = dateutil_parser.parse(text, fuzzy=True)
                if result <= now:
                    result += timedelta(days=1)
                return result
            except (ValueError, OverflowError):
                return None

        result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if tomorrow:
            result = result + timedelta(days=1)
        elif result <= now:
            # Time is in the past today, assume tomorrow
            result += timedelta(days=1)

        return result

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    def _check_due_reminders(self) -> List[Dict]:
        """Get all pending reminders whose time has arrived.

        Capped at 3 per poll cycle to prevent notification floods
        (e.g., historical calendar sync importing hundreds of past events).
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status = 'pending' "
                    "AND reminder_time <= ? ORDER BY priority ASC, reminder_time ASC "
                    "LIMIT 3",
                    (now_str,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def _fire_reminder(self, reminder: Dict):
        """Announce a reminder with appropriate tone and update status."""
        priority = reminder["priority"]
        title = reminder["title"]
        rid = reminder["id"]

        self.logger.info(f"Firing reminder #{rid}: '{title}' (priority={priority})")

        # Pause listening to prevent speaker-to-mic bleed
        if self._pause_listener_callback:
            self._pause_listener_callback()

        # Play priority tone
        self._play_priority_tone(priority)
        time.sleep(0.3)

        # Build announcement — use commas for natural speech flow
        cap_title = title[0].upper() + title[1:] if title else title
        if priority == 1:
            prefix = f"Urgent reminder, {get_honorific()}."
        elif priority == 2:
            prefix = f"Reminder, {get_honorific()}."
        elif priority == 3:
            prefix = f"Just a reminder, {get_honorific()}."
        else:
            prefix = f"By the way, {get_honorific()}."

        # When fired before event time, add "in X minutes" context
        time_phrase = None
        event_time_str = reminder.get("event_time")
        if event_time_str:
            try:
                event_dt = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M:%S")
                minutes_until = max(1, int((event_dt - datetime.now()).total_seconds() / 60))
                if minutes_until >= 60:
                    hours = minutes_until // 60
                    remaining = minutes_until % 60
                    if remaining:
                        time_phrase = f"in {hours} hour{'s' if hours != 1 else ''} and {remaining} minutes"
                    else:
                        time_phrase = f"in {hours} hour{'s' if hours != 1 else ''}"
                else:
                    time_phrase = f"in {minutes_until} minute{'s' if minutes_until != 1 else ''}"
            except (ValueError, TypeError):
                pass

        if time_phrase:
            self.tts.speak(f"{prefix} {cap_title}, {time_phrase}.")
        else:
            self.tts.speak(f"{prefix} {cap_title}.")

        # Desktop notification (visual companion to voice)
        try:
            from core.desktop_manager import get_desktop_manager
            dm = get_desktop_manager()
            if dm:
                urgency = "critical" if priority <= 1 else "normal" if priority <= 2 else "low"
                notif_body = f"{cap_title} ({time_phrase})" if time_phrase else cap_title
                dm.send_notification(f"JARVIS Reminder", notif_body, urgency)
        except Exception:
            pass  # notification is best-effort

        # Resume listening after announcement
        if self._resume_listener_callback:
            self._resume_listener_callback()

        # Track last announced for ack
        self._last_announced_id = rid

        # Update status
        new_fire_count = reminder["fire_count"] + 1
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if reminder["requires_ack"]:
            self._update_status(rid, "fired",
                                fire_count=new_fire_count,
                                last_fired_at=now_str)
            # Open conversation window for ack
            if self._ack_window_callback:
                self._ack_window_callback(rid)
        else:
            # Auto-clear for normal/low
            if reminder["reminder_type"] == "one_time":
                self._update_status(rid, "confirmed",
                                    fire_count=new_fire_count,
                                    last_fired_at=now_str)
            else:
                self._advance_recurring(reminder)

    def _play_priority_tone(self, priority: int):
        """Play the appropriate audio tone for a reminder priority."""
        if priority <= 2:
            tone = self._assets_dir / "urgent_reminder.wav"
        elif priority == 3:
            tone = self._assets_dir / "reminder.wav"
        else:
            return  # Low priority: no tone

        if not tone.exists():
            self.logger.warning(f"Tone file not found: {tone}")
            return

        try:
            subprocess.run(
                ["aplay", "-D", self._audio_device, str(tone)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3
            )
        except Exception as e:
            self.logger.error(f"Failed to play tone: {e}")

    def _play_rundown_tone(self, kind: str = "daily"):
        """Play the appropriate rundown chime (daily or weekly)."""
        filename = "weekly_run.wav" if kind == "weekly" else "daily_run.wav"
        tone = self._assets_dir / filename
        if not tone.exists():
            self.logger.warning(f"Rundown tone file not found: {tone}")
            return
        try:
            subprocess.run(
                ["aplay", "-D", self._audio_device, str(tone)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3
            )
        except Exception as e:
            self.logger.error(f"Failed to play rundown tone: {e}")

    # ------------------------------------------------------------------
    # Acknowledgment
    # ------------------------------------------------------------------

    def get_pending_acks(self) -> List[Dict]:
        """Get all reminders that have fired but await acknowledgment."""
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status = 'fired' "
                    "AND requires_ack = 1 ORDER BY priority ASC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def is_awaiting_ack(self) -> bool:
        """Check if any reminders are awaiting acknowledgment."""
        return len(self.get_pending_acks()) > 0

    def acknowledge_reminder(self, reminder_id: int) -> bool:
        """Acknowledge a fired reminder."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reminder = self.get_reminder(reminder_id)
        if not reminder:
            return False

        if reminder["reminder_type"] == "one_time":
            self._update_status(reminder_id, "confirmed", ack_at=now_str)
        else:
            # Recurring: advance to next occurrence
            self._advance_recurring(reminder)

        # Remove from Google Calendar (strip composite offset suffix)
        if self._calendar_manager and reminder.get("google_event_id"):
            base_id = self._base_google_event_id(reminder["google_event_id"])
            self._calendar_manager.delete_event(base_id)

        self.logger.info(f"Reminder #{reminder_id} acknowledged")
        return True

    def acknowledge_last(self) -> Optional[Dict]:
        """Acknowledge the most recently announced reminder.

        Returns the acknowledged reminder or None.
        """
        # Try the last announced first
        if self._last_announced_id:
            reminder = self.get_reminder(self._last_announced_id)
            if reminder and reminder["status"] == "fired":
                self.acknowledge_reminder(self._last_announced_id)
                return reminder

        # Fall back to oldest unacked
        pending = self.get_pending_acks()
        if pending:
            r = pending[0]
            self.acknowledge_reminder(r["id"])
            return r

        return None

    def snooze_reminder(self, reminder_id: int, minutes: int = None) -> bool:
        """Snooze a reminder for the given number of minutes."""
        if minutes is None:
            minutes = self.default_snooze
        snooze_time = datetime.now() + timedelta(minutes=minutes)
        snooze_until = snooze_time.strftime("%Y-%m-%d %H:%M:%S")
        self._update_status(reminder_id, "snoozed", snooze_until=snooze_until)
        self.logger.info(f"Reminder #{reminder_id} snoozed until {snooze_until}")

        # Update Google Calendar event to new time (strip composite offset suffix)
        reminder = self.get_reminder(reminder_id)
        if reminder and self._calendar_manager and reminder.get("google_event_id"):
            base_id = self._base_google_event_id(reminder["google_event_id"])
            self._calendar_manager.update_event(base_id, start_time=snooze_time)

        return True

    def snooze_last(self, minutes: int = None) -> Optional[Dict]:
        """Snooze the most recently announced reminder."""
        if self._last_announced_id:
            reminder = self.get_reminder(self._last_announced_id)
            if reminder and reminder["status"] == "fired":
                self.snooze_reminder(self._last_announced_id, minutes)
                return reminder

        pending = self.get_pending_acks()
        if pending:
            r = pending[0]
            self.snooze_reminder(r["id"], minutes)
            return r

        return None

    def _should_nag(self, reminder: Dict) -> bool:
        """Check if an unacked reminder should be re-announced."""
        last_fired = reminder.get("last_fired_at")
        if not last_fired:
            return True

        try:
            last = datetime.strptime(last_fired, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return True

        elapsed_min = (datetime.now() - last).total_seconds() / 60
        fire_count = reminder.get("fire_count", 0)

        if fire_count >= self.nag_backoff_count:
            return elapsed_min >= self.nag_backoff_min

        if reminder["priority"] == 1:
            return elapsed_min >= self.nag_critical_min
        else:
            return elapsed_min >= self.nag_high_min

    # ------------------------------------------------------------------
    # Recurring
    # ------------------------------------------------------------------

    def _advance_recurring(self, reminder: Dict):
        """Calculate and set the next fire time for a recurring reminder."""
        rule = reminder.get("recurrence_rule", "")
        if not rule:
            # No rule — just confirm it
            self._update_status(reminder["id"], "confirmed")
            return

        next_time = self._next_occurrence(rule)
        if next_time:
            time_str = next_time.strftime("%Y-%m-%d %H:%M:%S")
            with self._db_lock:
                conn = self._conn()
                try:
                    conn.execute(
                        "UPDATE reminders SET status = 'pending', "
                        "reminder_time = ?, fire_count = 0, last_fired_at = NULL, "
                        "updated_at = datetime('now', 'localtime') WHERE id = ?",
                        (time_str, reminder["id"])
                    )
                    conn.commit()
                finally:
                    conn.close()
            self.logger.info(f"Recurring reminder #{reminder['id']} advanced to {time_str}")
        else:
            self._update_status(reminder["id"], "confirmed")

    def _next_occurrence(self, rule: str) -> Optional[datetime]:
        """Calculate next occurrence from a recurrence rule.

        Rule formats:
            daily:HH:MM
            weekly:mon,wed,fri:HH:MM
            annual:MM-DD:HH:MM
        """
        now = datetime.now()
        parts = rule.split(":")

        if parts[0] == "daily":
            time_parts = parts[1].split(":")  # This won't work if HH:MM is split by our `:` delimiter
            # Re-parse: "daily:08:15" -> parts = ["daily", "08", "15"]
            hour, minute = int(parts[1]), int(parts[2])
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

        elif parts[0] == "weekly":
            # "weekly:tue,fri:19:00" -> parts = ["weekly", "tue,fri", "19", "00"]
            day_names = parts[1].split(",")
            hour, minute = int(parts[2]), int(parts[3])

            day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
                       "fri": 4, "sat": 5, "sun": 6}
            target_days = sorted([day_map[d.strip().lower()] for d in day_names])

            # Find next matching day
            for offset in range(1, 8):
                candidate = now + timedelta(days=offset)
                if candidate.weekday() in target_days:
                    return candidate.replace(hour=hour, minute=minute,
                                             second=0, microsecond=0)
            # Check today if time hasn't passed
            if now.weekday() in target_days:
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate > now:
                    return candidate

        elif parts[0] == "annual":
            # "annual:03-15:09:00" -> parts = ["annual", "03-15", "09", "00"]
            month_day = parts[1].split("-")
            month, day = int(month_day[0]), int(month_day[1])
            hour, minute = int(parts[2]), int(parts[3])

            candidate = now.replace(month=month, day=day, hour=hour,
                                    minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate = candidate.replace(year=now.year + 1)
            return candidate

        return None

    # ------------------------------------------------------------------
    # Missed Reminder Recovery
    # ------------------------------------------------------------------

    def scan_missed_reminders(self) -> List[Dict]:
        """Find reminders that should have fired while the system was down."""
        now = datetime.now()
        lookback = now - timedelta(hours=self.missed_lookback_hours)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        lookback_str = lookback.strftime("%Y-%m-%d %H:%M:%S")

        with self._db_lock:
            conn = self._conn()
            try:
                # Missed pending reminders
                missed = conn.execute(
                    "SELECT * FROM reminders WHERE status = 'pending' "
                    "AND reminder_time < ? AND reminder_time > ? "
                    "ORDER BY priority ASC, reminder_time ASC",
                    (now_str, lookback_str)
                ).fetchall()

                # Also get previously fired but unacked
                unacked = conn.execute(
                    "SELECT * FROM reminders WHERE status = 'fired' "
                    "AND requires_ack = 1 ORDER BY priority ASC"
                ).fetchall()

                result = [dict(r) for r in missed] + [dict(r) for r in unacked]
                return result
            finally:
                conn.close()

    def announce_missed_reminders(self):
        """Announce missed reminders on startup."""
        missed = self.scan_missed_reminders()
        if not missed:
            self.logger.info("No missed reminders")
            return

        self.logger.info(f"Found {len(missed)} missed reminder(s)")

        # Pause listening to prevent speaker-to-mic bleed
        if self._pause_listener_callback:
            self._pause_listener_callback()

        critical = [r for r in missed if r["priority"] <= 2]
        normal = [r for r in missed if r["priority"] > 2]

        if critical:
            self._play_priority_tone(1)
            time.sleep(0.3)

            if len(critical) == 1:
                self.tts.speak("Sir, you missed an urgent reminder while you were away.")
            else:
                self.tts.speak(
                    f"Sir, you missed {len(critical)} urgent reminders while you were away."
                )
            time.sleep(0.3)

            for r in critical:
                self.tts.speak(f"Did you remember to {r['title']}?")
                time.sleep(0.5)
                # Mark as fired (awaiting ack)
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._update_status(r["id"], "fired",
                                    fire_count=r["fire_count"] + 1,
                                    last_fired_at=now_str)
                self._last_announced_id = r["id"]

            # Open conversation window for ack
            if self._ack_window_callback and critical:
                self._ack_window_callback(critical[-1]["id"])

        if normal:
            time.sleep(0.5)
            if len(normal) == 1:
                self.tts.speak("You also had a reminder while you were away.")
            else:
                self.tts.speak(
                    f"You also had {len(normal)} other reminders while you were away."
                )

            for r in normal:
                self.tts.speak(r["title"])
                time.sleep(0.3)
                # Auto-clear one-time, advance recurring
                if r["reminder_type"] == "one_time":
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._update_status(r["id"], "confirmed",
                                        fire_count=r["fire_count"] + 1,
                                        last_fired_at=now_str)
                elif r["status"] == "pending":
                    self._advance_recurring(r)

        # Resume listening after announcements
        if self._resume_listener_callback:
            self._resume_listener_callback()

    # ------------------------------------------------------------------
    # Daily Rundown
    # ------------------------------------------------------------------

    def get_daily_rundown(self) -> str:
        """Format today's reminders and calendar events as natural flowing speech."""
        # Gather all items for today
        items = []

        today = self.list_today()
        for r in today:
            try:
                t = datetime.strptime(r["reminder_time"], "%Y-%m-%d %H:%M:%S")
                items.append({"time": t, "title": r["title"]})
            except ValueError:
                pass

        if self._calendar_manager:
            try:
                cal_events = self._calendar_manager.get_primary_events_today()
                for ev in cal_events:
                    items.append({"time": ev["start_time"], "title": ev["title"]})
            except Exception as e:
                self.logger.error(f"Failed to fetch calendar events for rundown: {e}")

        # Sort by time and format naturally
        if items:
            items.sort(key=lambda x: x["time"])
            rundown_text = self._format_items_naturally(items)
        else:
            rundown_text = f"You have no reminders or events for today, {get_honorific()}."

        # Append news summary if available
        try:
            from core.news_manager import get_news_manager
            news_mgr = get_news_manager()
            if news_mgr:
                news_summary = news_mgr.get_news_summary_for_rundown()
                if news_summary:
                    rundown_text += f" {news_summary}"
        except Exception:
            pass  # News system not available — no problem

        return rundown_text

    @staticmethod
    def _format_time_spoken(t: datetime) -> str:
        """Format a time for natural speech.

        Examples: "8:15 this morning", "2:30 this afternoon",
        "6 PM", "noon", "midnight"
        """
        hour = t.hour
        minute = t.minute

        # Special cases
        if hour == 12 and minute == 0:
            return "noon"
        if hour == 0 and minute == 0:
            return "midnight"

        # Format the base time
        if minute == 0:
            time_str = t.strftime("%-I %p")
        else:
            time_str = t.strftime("%-I:%M %p")

        # Remove trailing :00 and make PM/AM lowercase-ish for speech
        # Piper reads "PM" fine, but "6 PM sharp" sounds better than "6:00 PM"
        return time_str

    @staticmethod
    def _format_items_naturally(items: list, day_prefix: str = "") -> str:
        """Format a list of time+title items into natural flowing speech.

        Produces output like:
        "At 8:15 AM you have XYZ, followed by ABC at 9:30 AM,
         with 123 at 2:30 PM, and finally 456 at 6 PM."
        """
        if not items:
            return ""

        parts = []
        count = len(items)

        for i, item in enumerate(items):
            time_str = ReminderManager._format_time_spoken(item["time"])
            title = item["title"]

            if i == 0:
                # First item: "At 8:15 AM you have XYZ"
                prefix = f"{day_prefix}at " if day_prefix else "At "
                parts.append(f"{prefix}{time_str} you have {title}")
            elif i == count - 1:
                # Last item: "and finally 456 at 6 PM"
                if count == 2:
                    parts.append(f"and then {title} at {time_str}")
                else:
                    parts.append(f"and finally {title} at {time_str}")
            else:
                # Middle items: alternate connectors for variety
                connector = "followed by" if i % 2 == 1 else "with"
                parts.append(f"{connector} {title} at {time_str}")

        return ", ".join(parts) + "."

    def _do_daily_rundown(self):
        """Proactively announce the daily rundown (non-interactive fallback)."""
        rundown = self.get_daily_rundown()
        self.logger.info(f"Daily rundown: {rundown}")
        if self._pause_listener_callback:
            self._pause_listener_callback()
        self._play_rundown_tone("daily")
        time.sleep(0.3)
        self.tts.speak(f"Good morning, {get_honorific()}. Here's your rundown for today. {rundown}")
        if self._resume_listener_callback:
            self._resume_listener_callback()

    # ------------------------------------------------------------------
    # Interactive Rundown State Machine
    # ------------------------------------------------------------------

    def _offer_rundown(self):
        """Ask the user if they're ready for the rundown."""
        self._rundown_state = "offered"
        self._rundown_offered_at = datetime.now()
        if self._rundown_cycle == 0:
            self._rundown_cycle = 1

        kind = "weekly" if self._rundown_is_weekly else "daily"
        self.logger.info(f"Offering {kind} rundown (cycle {self._rundown_cycle})")

        if self._pause_listener_callback:
            self._pause_listener_callback()

        self._play_rundown_tone(kind)
        time.sleep(0.3)
        self.tts.speak(f"Good morning, {get_honorific()}. Are you ready for the {kind} rundown?")

        if self._resume_listener_callback:
            self._resume_listener_callback()

        # Open a 30-second conversation window for the response
        if self._open_window_callback:
            self._open_window_callback(self._offer_timeout)

    def _re_ask_rundown(self):
        """Re-ask after silence on first offer."""
        self._rundown_state = "re-asked"
        self._rundown_offered_at = datetime.now()

        self.logger.info(f"Re-asking rundown (cycle {self._rundown_cycle})")

        if self._pause_listener_callback:
            self._pause_listener_callback()

        self.tts.speak("Sir, shall I proceed with the rundown?")

        if self._resume_listener_callback:
            self._resume_listener_callback()

        if self._open_window_callback:
            self._open_window_callback(self._offer_timeout)

    def deliver_rundown(self):
        """Deliver the full rundown (called when user says yes).

        Public — called from jarvis_continuous.py.
        """
        is_weekly = self._rundown_is_weekly

        # Clear state first
        self._rundown_state = None
        self._rundown_cycle = 0
        self._rundown_pending_mention = False

        if is_weekly:
            self._last_weekly_rundown_date = datetime.now().date()
            rundown = self.get_weekly_rundown()
            self.logger.info(f"Weekly rundown: {rundown}")
            if self._pause_listener_callback:
                self._pause_listener_callback()
            self.tts.speak(f"Here's your weekly rundown, {get_honorific()}. {rundown}")
            if self._resume_listener_callback:
                self._resume_listener_callback()

            # After weekly, offer daily rundown too
            self._rundown_is_weekly = False
            self._rundown_cycle = 0
            self._offer_rundown()
        else:
            rundown = self.get_daily_rundown()
            self.logger.info(f"Daily rundown: {rundown}")
            if self._pause_listener_callback:
                self._pause_listener_callback()
            self.tts.speak(f"Here's your rundown for today, {get_honorific()}. {rundown}")
            if self._resume_listener_callback:
                self._resume_listener_callback()

    def defer_rundown(self):
        """User said no — mark as done for today, don't re-ask.

        Public — called from jarvis_continuous.py.
        """
        self.logger.info("Rundown deferred by user")
        self._rundown_state = None
        self._rundown_cycle = 0
        self._rundown_is_weekly = False
        # _last_rundown_date is already set, so it won't re-trigger today

    def is_rundown_pending(self) -> bool:
        """Check if we're waiting for a rundown response."""
        return self._rundown_state in ("offered", "re-asked")

    def has_rundown_mention(self) -> bool:
        """Check if we should mention the rundown on next wake word."""
        return self._rundown_pending_mention

    def clear_rundown_mention(self):
        """Clear the pending mention flag (called after mentioning once)."""
        self._rundown_pending_mention = False

    def get_weekly_rundown(self) -> str:
        """Format the week's reminders and calendar events as natural flowing speech."""
        now = datetime.now()
        today = now.date()
        # Monday of current week
        monday = now - timedelta(days=now.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]

        # Gather reminders for the week
        monday_str = monday.strftime("%Y-%m-%d %H:%M:%S")
        sunday_str = sunday.strftime("%Y-%m-%d %H:%M:%S")

        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE status IN ('pending', 'fired') "
                    "AND reminder_time BETWEEN ? AND ? "
                    "ORDER BY reminder_time ASC",
                    (monday_str, sunday_str)
                ).fetchall()
                reminders = [dict(r) for r in rows]
            finally:
                conn.close()

        # Gather calendar events for the week
        cal_events = []
        if self._calendar_manager:
            try:
                cal_events = self._calendar_manager.get_primary_events_week()
            except Exception as e:
                self.logger.error(f"Failed to fetch weekly calendar events: {e}")

        # Group all items by day
        days_with_items = {}  # day_offset -> list of {time, title}
        for day_offset in range(7):
            day_date = (monday + timedelta(days=day_offset)).date()
            items = []

            for r in reminders:
                try:
                    t = datetime.strptime(r["reminder_time"], "%Y-%m-%d %H:%M:%S")
                    if t.date() == day_date:
                        items.append({"time": t, "title": r["title"]})
                except ValueError:
                    pass

            for ev in cal_events:
                if ev["start_time"].date() == day_date:
                    items.append({"time": ev["start_time"], "title": ev["title"]})

            if items:
                items.sort(key=lambda x: x["time"])
                days_with_items[day_offset] = items

        if not days_with_items:
            return f"You have a clear week ahead, {get_honorific()}. No reminders or events scheduled."

        # Build natural speech
        sentences = []
        busy_days = list(days_with_items.keys())
        empty_streak = []

        for day_offset in range(7):
            day_date = (monday + timedelta(days=day_offset)).date()
            day_name = day_names[day_offset]

            # Use relative names for today/tomorrow
            if day_date == today:
                day_label = "Today"
            elif day_date == today + timedelta(days=1):
                day_label = "Tomorrow"
            else:
                day_label = day_name

            if day_offset in days_with_items:
                # Flush empty streak first
                if empty_streak:
                    self._append_empty_days(sentences, empty_streak, day_names,
                                            today, monday)
                    empty_streak = []

                items = days_with_items[day_offset]
                if len(items) == 1:
                    # Single item: "On Wednesday you just have XYZ at 10:45 AM."
                    item = items[0]
                    time_str = self._format_time_spoken(item["time"])
                    if day_label in ("Today", "Tomorrow"):
                        sentences.append(
                            f"{day_label} you just have {item['title']} at {time_str}."
                        )
                    else:
                        sentences.append(
                            f"On {day_label} you just have {item['title']} at {time_str}."
                        )
                else:
                    # Multiple items: use natural list formatting
                    if day_label in ("Today", "Tomorrow"):
                        prefix = f"{day_label}, "
                    else:
                        prefix = f"On {day_label}, "
                    formatted = self._format_items_naturally(items, day_prefix=prefix)
                    sentences.append(formatted)
            else:
                empty_streak.append(day_offset)

        # Flush trailing empty days
        if empty_streak and len(empty_streak) < 7:
            self._append_empty_days(sentences, empty_streak, day_names,
                                    today, monday)

        # Closing
        total = sum(len(v) for v in days_with_items.values())
        if total == 1:
            sentences.append(f"That's all for the week, {get_honorific()}.")
        else:
            sentences.append(f"That's the week's schedule so far, {get_honorific()}.")

        return " ".join(sentences)

    @staticmethod
    def _append_empty_days(sentences: list, day_offsets: list,
                           day_names: list, today, monday):
        """Add a natural mention of empty days to the rundown.

        Examples: "There's nothing tomorrow."
                  "Wednesday and Thursday are clear."
                  "There's nothing on the weekend."
        """
        if not day_offsets:
            return

        labels = []
        for offset in day_offsets:
            day_date = (monday + timedelta(days=offset)).date()
            if day_date == today:
                labels.append("today")
            elif day_date == today + timedelta(days=1):
                labels.append("tomorrow")
            else:
                labels.append(day_names[offset])

        # Collapse weekend
        if set(day_offsets) == {5, 6}:
            sentences.append("The weekend is clear.")
            return

        if len(labels) == 1:
            if labels[0] in ("today", "tomorrow"):
                sentences.append(f"There's nothing {labels[0]}.")
            else:
                sentences.append(f"There's nothing on {labels[0]}.")
        elif len(labels) == 2:
            sentences.append(f"{labels[0].capitalize()} and {labels[1]} are clear.")
        else:
            # Capitalize each day name (but not "today"/"tomorrow")
            capped = [l if l in ("today", "tomorrow") else l.capitalize()
                      for l in labels]
            joined = ", ".join(capped[:-1]) + f", and {capped[-1]}"
            sentences.append(f"{joined} are clear.")

    # ------------------------------------------------------------------
    # Background Thread
    # ------------------------------------------------------------------

    def set_calendar_manager(self, calendar_manager):
        """Set the Google Calendar manager for push sync."""
        self._calendar_manager = calendar_manager
        # Register callbacks for Google→JARVIS sync
        calendar_manager.set_sync_callbacks(
            on_new_event=self._on_google_new_event,
            on_cancel_event=self._on_google_cancel_event,
        )
        self.logger.info("Google Calendar integration active")

    def _on_google_new_event(self, title: str, start_time: datetime,
                             priority: int, google_event_id: str,
                             reminder_minutes: int = None) -> int:
        """Callback: a new event was added on Google Calendar → create local reminder.

        reminder_minutes: offset from Google Calendar reminders (e.g., 30 = fire 30 min before).
        When set, reminder_time = start_time - offset, and event_time = start_time.

        Composite key: google_event_id is stored as "base_id:offset" (e.g., "abc123:60")
        so multiple notifications for the same event each get their own reminder row.
        """
        # Skip past events — prevents notification storms from historical sync
        if start_time < datetime.now() - timedelta(hours=1):
            self.logger.debug(f"Skipping past Google event: '{title}' @ {start_time}")
            return -1

        # Apply reminder offset: fire BEFORE the event, not at event time
        if reminder_minutes and reminder_minutes > 0:
            reminder_time = start_time - timedelta(minutes=reminder_minutes)
            event_time = start_time
        else:
            reminder_time = start_time
            event_time = None

        # Skip if the computed reminder_time is already past (e.g., future event
        # with a large offset like 1 week — the notification window already passed)
        if reminder_time < datetime.now() - timedelta(hours=1):
            self.logger.debug(f"Skipping past reminder time for '{title}': "
                              f"reminder={reminder_time}, event={start_time}")
            return -1

        # Composite key: base_event_id:offset — allows multiple reminders per event
        composite_id = f"{google_event_id}:{reminder_minutes}" if reminder_minutes else google_event_id

        # Check if we already have this specific reminder (event + offset combo)
        existing = self._find_by_google_event_id(composite_id)
        if existing:
            # Update if time changed
            existing_time = datetime.strptime(existing["reminder_time"], "%Y-%m-%d %H:%M:%S")
            if existing_time != reminder_time:
                time_str = reminder_time.strftime("%Y-%m-%d %H:%M:%S")
                event_time_str = event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else None
                self._update_status(existing["id"], existing["status"],
                                    reminder_time=time_str, title=title, priority=priority)
                # Also update event_time
                if event_time_str:
                    with self._db_lock:
                        conn = self._conn()
                        try:
                            conn.execute("UPDATE reminders SET event_time = ? WHERE id = ?",
                                         (event_time_str, existing["id"]))
                            conn.commit()
                        finally:
                            conn.close()
                self.logger.info(f"Updated reminder #{existing['id']} from Google sync")
            return existing["id"]

        # Create new local reminder
        rid = self.add_reminder(title, reminder_time, priority,
                                _skip_calendar_push=True, event_time=event_time)
        # Store the composite google_event_id
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE reminders SET google_event_id = ? WHERE id = ?",
                             (composite_id, rid))
                conn.commit()
            finally:
                conn.close()
        self.logger.info(f"Created reminder #{rid} from Google Calendar event {google_event_id}"
                         f"{f' (fires {reminder_minutes}min before event)' if reminder_minutes else ''}")
        return rid

    def _on_google_cancel_event(self, google_event_id: str) -> bool:
        """Callback: an event was deleted on Google Calendar → cancel ALL local reminders.

        Uses prefix match to find all reminders for this event (base_id:offset pattern).
        """
        reminders = self._find_all_by_google_event_id(google_event_id)
        cancelled = 0
        for reminder in reminders:
            if reminder["status"] in ("pending", "fired"):
                self._update_status(reminder["id"], "cancelled")
                cancelled += 1
        if cancelled:
            self.logger.info(f"Cancelled {cancelled} reminder(s) for Google event {google_event_id}")
        return cancelled > 0

    @staticmethod
    def _base_google_event_id(composite_id: str) -> str:
        """Extract the base Google event ID from a composite key (strip ':offset' suffix)."""
        idx = composite_id.rfind(":")
        if idx > 0:
            suffix = composite_id[idx + 1:]
            if suffix.isdigit():
                return composite_id[:idx]
        return composite_id

    def _find_by_google_event_id(self, google_event_id: str) -> Optional[Dict]:
        """Find a reminder by its exact Google Calendar event ID (including composite key)."""
        with self._db_lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM reminders WHERE google_event_id = ?",
                    (google_event_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def _find_all_by_google_event_id(self, google_event_id: str) -> List[Dict]:
        """Find ALL reminders for a Google Calendar event (prefix match).

        Matches both exact ID and composite keys (base_id:offset pattern).
        Used by cancellation to remove all notification offsets for one event.
        """
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE google_event_id = ? "
                    "OR google_event_id LIKE ?",
                    (google_event_id, f"{google_event_id}:%")
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def set_ack_window_callback(self, callback: Callable):
        """Set the callback for opening a conversation window after reminder fires."""
        self._ack_window_callback = callback

    def set_window_callback(self, callback: Callable):
        """Set the callback for opening a conversation window with custom duration.

        callback(duration_seconds) -> opens conversation window for that duration.
        """
        self._open_window_callback = callback

    def set_listener_callbacks(self, pause: Callable, resume: Callable):
        """Set callbacks for pausing/resuming the listener during announcements."""
        self._pause_listener_callback = pause
        self._resume_listener_callback = resume

    def start(self):
        """Start the reminder system: scan missed, then begin polling."""
        if not self.config.get("reminders.enabled", True):
            self.logger.info("Reminder system disabled in config")
            return

        self.logger.info("Starting reminder system")

        # Scan for missed reminders (delayed to let audio initialize)
        missed = self.scan_missed_reminders()
        if missed:
            self.logger.info(f"Found {len(missed)} missed reminders, announcing in {self.startup_delay}s")
            threading.Timer(self.startup_delay, self.announce_missed_reminders).start()

        # Start polling thread
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                             name="reminder-poll")
        self._poll_thread.start()
        self.logger.info("Reminder polling started")

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        self.logger.info("Reminder system stopped")

    def _poll_loop(self):
        """Main polling loop: check for due reminders every poll_interval seconds."""
        while self._running:
            try:
                # 1. Check due reminders
                due = self._check_due_reminders()
                for reminder in due:
                    self._fire_reminder(reminder)

                # 2. Re-nag unacknowledged
                unacked = self.get_pending_acks()
                for reminder in unacked:
                    if self._should_nag(reminder):
                        self._fire_reminder(reminder)

                # 3. Check snoozed reminders
                self._check_snoozed()

                # 4. Daily/weekly rundown — interactive state machine
                if self.rundown_enabled:
                    self._poll_rundown()

            except Exception as e:
                self.logger.error(f"Reminder poll error: {e}")

            # Sleep in small increments for responsive shutdown
            for _ in range(self.poll_interval // 5):
                if not self._running:
                    break
                time.sleep(5)

    def _poll_rundown(self):
        """Handle the interactive rundown state machine each poll cycle."""
        now = datetime.now()
        timeout_with_buffer = self._offer_timeout + 5  # 35s: 30s window + 5s grace

        # --- Active state: check for timeouts ---
        if self._rundown_state == "offered" and self._rundown_offered_at:
            elapsed = (now - self._rundown_offered_at).total_seconds()
            if elapsed >= timeout_with_buffer:
                self._re_ask_rundown()
            return  # Don't trigger a new rundown while one is active

        if self._rundown_state == "re-asked" and self._rundown_offered_at:
            elapsed = (now - self._rundown_offered_at).total_seconds()
            if elapsed >= timeout_with_buffer:
                if self._rundown_cycle < 2:
                    # Defer and retry in 5 minutes
                    self._rundown_state = "deferred"
                    self._rundown_offered_at = datetime.now()
                    self.logger.info("Rundown deferred (silence), will retry in "
                                     f"{self._retry_delay} minutes")
                else:
                    # Both cycles exhausted — set pending mention
                    self._rundown_state = None
                    self._rundown_cycle = 0
                    self._rundown_pending_mention = True
                    self._rundown_is_weekly = False
                    self.logger.info("Rundown abandoned after 2 cycles, will mention on next wake")
            return

        if self._rundown_state == "deferred" and self._rundown_offered_at:
            elapsed = (now - self._rundown_offered_at).total_seconds()
            if elapsed >= self._retry_delay * 60:
                self._rundown_cycle = 2
                self._offer_rundown()
            return

        # --- No active state: check if it's time to start ---
        if self._rundown_state is not None:
            return  # Safety guard

        if (now.hour == self.rundown_hour and
                now.minute >= self.rundown_minute and
                now.minute < self.rundown_minute + 1 and
                self._last_rundown_date != now.date()):
            self._last_rundown_date = now.date()

            # Monday = weekly rundown first
            if (self.weekly_rundown_enabled and
                    now.weekday() == self._weekly_day_num and
                    self._last_weekly_rundown_date != now.date()):
                self._rundown_is_weekly = True

            self._offer_rundown()

    def _check_snoozed(self):
        """Reactivate snoozed reminders whose snooze period has expired."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE reminders SET status = 'pending', snooze_until = NULL, "
                    "updated_at = datetime('now', 'localtime') "
                    "WHERE status = 'snoozed' AND snooze_until <= ?",
                    (now_str,)
                )
                conn.commit()
            finally:
                conn.close()
