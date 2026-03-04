"""
CalDAV Calendar Manager

Handles CalDAV authentication and event retrieval for iCloud Calendar.
Provides the same interface as GoogleCalendarManager for daily/weekly
rundowns so it can be used interchangeably by ReminderManager.

Singleton pattern — access via get_caldav_manager().
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Callable

from core.logger import get_logger

# Singleton instance
_instance: Optional["CalDAVManager"] = None


def get_caldav_manager(config=None) -> Optional["CalDAVManager"]:
    """Get or create the singleton CalDAVManager.

    Call with config on first invocation (from jarvis_continuous.py).
    Call with no args from other modules to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = CalDAVManager(config)
    return _instance


class CalDAVManager:
    """Handles CalDAV authentication and event retrieval for iCloud Calendar."""

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # CalDAV connection settings
        self._url = config.get("caldav.url", "https://caldav.icloud.com")
        self._username = config.get("caldav.username", "")
        self._password = config.get("caldav.password", "")
        self._calendar_name = config.get("caldav.calendar_name", None)
        self._timezone = config.get("caldav.timezone", "America/Your_Timezone")
        self._sync_interval = config.get("caldav.sync_interval_seconds", 300)

        # State
        self._client = None
        self._calendar = None
        self._authenticated = False
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Sync callbacks (same pattern as GoogleCalendarManager)
        self._on_new_event: Optional[Callable] = None
        self._on_cancel_event: Optional[Callable] = None

    def _connect(self) -> bool:
        """Establish CalDAV connection and find the target calendar."""
        if not self._username or not self._password:
            self.logger.warning("CalDAV credentials not configured — skipping")
            return False

        try:
            import caldav
            self._client = caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
            )
            principal = self._client.principal()
            calendars = principal.calendars()

            if not calendars:
                self.logger.warning("No CalDAV calendars found")
                return False

            # Find target calendar by name, or use first available
            if self._calendar_name:
                for cal in calendars:
                    if str(cal.name).lower() == self._calendar_name.lower():
                        self._calendar = cal
                        break
                if not self._calendar:
                    self.logger.warning(
                        f"Calendar '{self._calendar_name}' not found, "
                        f"using first: {calendars[0].name}"
                    )
                    self._calendar = calendars[0]
            else:
                self._calendar = calendars[0]

            self._authenticated = True
            self.logger.info(f"CalDAV connected: {self._calendar.name}")
            return True

        except Exception as e:
            self.logger.error(f"CalDAV connection failed: {e}")
            self._authenticated = False
            return False

    def start(self):
        """Start background sync thread."""
        if not self._connect():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="caldav-sync"
        )
        self._thread.start()
        self.logger.info("CalDAV sync started")

    def stop(self):
        """Stop background sync."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.logger.info("CalDAV sync stopped")

    def set_sync_callbacks(self, on_new_event=None, on_cancel_event=None):
        """Set callbacks for CalDAV→JARVIS sync (same interface as Google)."""
        self._on_new_event = on_new_event
        self._on_cancel_event = on_cancel_event

    # ------------------------------------------------------------------
    # Event Retrieval
    # ------------------------------------------------------------------

    def get_primary_events_today(self) -> List[Dict]:
        """Get today's events for daily rundown."""
        if not self._authenticated:
            return []
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return self._fetch_events(start, end)

    def get_primary_events_week(self) -> List[Dict]:
        """Get this week's events (Mon-Sun) for weekly rundown."""
        if not self._authenticated:
            return []
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=6)
        sunday = sunday.replace(hour=23, minute=59, second=59, microsecond=0)
        return self._fetch_events(monday, sunday)

    def _fetch_events(self, start: datetime, end: datetime) -> List[Dict]:
        """Fetch events from CalDAV calendar within a time range."""
        with self._lock:
            try:
                events = self._calendar.search(
                    start=start, end=end, event=True, expand=True,
                )
                results = []
                for event in events:
                    parsed = self._parse_event(event)
                    if parsed:
                        results.append(parsed)
                results.sort(key=lambda e: e.get("start_time", ""))
                return results
            except Exception as e:
                self.logger.error(f"CalDAV event fetch failed: {e}")
                return []

    def _parse_event(self, event) -> Optional[Dict]:
        """Parse a CalDAV event into a dict matching Google's format."""
        try:
            vevent = event.vobject_instance.vevent
            summary = str(vevent.summary.value) if hasattr(vevent, 'summary') else ""
            if not summary:
                return None

            dtstart = vevent.dtstart.value
            dtend = vevent.dtend.value if hasattr(vevent, 'dtend') else None

            # Handle date vs datetime
            if hasattr(dtstart, 'hour'):
                start_str = dtstart.strftime("%Y-%m-%d %H:%M:%S")
                all_day = False
            else:
                start_str = dtstart.strftime("%Y-%m-%d") + " 00:00:00"
                all_day = True

            end_str = None
            if dtend:
                if hasattr(dtend, 'hour'):
                    end_str = dtend.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    end_str = dtend.strftime("%Y-%m-%d") + " 23:59:59"

            location = ""
            if hasattr(vevent, 'location'):
                location = str(vevent.location.value)

            return {
                "title": summary,
                "start_time": start_str,
                "end_time": end_str,
                "all_day": all_day,
                "location": location,
                "source": "caldav",
            }
        except Exception as e:
            self.logger.debug(f"Failed to parse CalDAV event: {e}")
            return None

    # ------------------------------------------------------------------
    # Event CRUD (for reminder push-back)
    # ------------------------------------------------------------------

    def create_event(self, title: str, start_time: datetime,
                     priority: int = 3, description: str = "") -> Optional[str]:
        """Create a CalDAV event. Returns the event UID or None."""
        if not self._authenticated:
            return None
        with self._lock:
            try:
                import uuid
                uid = str(uuid.uuid4())
                end_time = start_time + timedelta(minutes=30)
                vcal = (
                    "BEGIN:VCALENDAR\r\n"
                    "VERSION:2.0\r\n"
                    "PRODID:-//JARVIS//CalDAV//EN\r\n"
                    "BEGIN:VEVENT\r\n"
                    f"UID:{uid}\r\n"
                    f"DTSTART:{start_time.strftime('%Y%m%dT%H%M%S')}\r\n"
                    f"DTEND:{end_time.strftime('%Y%m%dT%H%M%S')}\r\n"
                    f"SUMMARY:{title}\r\n"
                    f"DESCRIPTION:{description}\r\n"
                    "END:VEVENT\r\n"
                    "END:VCALENDAR\r\n"
                )
                self._calendar.save_event(vcal)
                self.logger.info(f"CalDAV event created: {title}")
                return uid
            except Exception as e:
                self.logger.error(f"CalDAV create event failed: {e}")
                return None

    def delete_event(self, event_uid: str) -> bool:
        """Delete a CalDAV event by UID."""
        if not self._authenticated or not event_uid:
            return False
        with self._lock:
            try:
                event = self._calendar.event_by_uid(event_uid)
                event.delete()
                self.logger.info(f"CalDAV event deleted: {event_uid}")
                return True
            except Exception as e:
                self.logger.warning(f"CalDAV delete event failed: {e}")
                return False

    def update_event(self, event_uid: str, start_time: datetime = None) -> bool:
        """Update a CalDAV event's start time (for snooze)."""
        if not self._authenticated or not event_uid:
            return False
        with self._lock:
            try:
                event = self._calendar.event_by_uid(event_uid)
                vevent = event.vobject_instance.vevent
                if start_time:
                    vevent.dtstart.value = start_time
                    vevent.dtend.value = start_time + timedelta(minutes=30)
                event.save()
                self.logger.info(f"CalDAV event updated: {event_uid}")
                return True
            except Exception as e:
                self.logger.warning(f"CalDAV update event failed: {e}")
                return False

    # ------------------------------------------------------------------
    # Background Sync
    # ------------------------------------------------------------------

    def _sync_loop(self):
        """Background thread: periodically sync CalDAV events."""
        while self._running:
            try:
                self._sync_events()
            except Exception as e:
                self.logger.error(f"CalDAV sync error: {e}")
            # Sleep in small chunks so stop() is responsive
            for _ in range(self._sync_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _sync_events(self):
        """Sync today's CalDAV events into local reminders via callback."""
        if not self._on_new_event:
            return

        events = self.get_primary_events_today()
        for event in events:
            if event.get("all_day"):
                continue
            try:
                start = datetime.strptime(event["start_time"], "%Y-%m-%d %H:%M:%S")
                if start < datetime.now():
                    continue
                self._on_new_event(
                    title=event["title"],
                    start_time=start,
                    event_id=f"caldav_{event['title']}_{event['start_time']}",
                    source="caldav",
                )
            except Exception as e:
                self.logger.debug(f"CalDAV sync skip: {e}")
