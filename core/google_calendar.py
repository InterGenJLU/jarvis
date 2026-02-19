"""
Google Calendar Manager

Handles OAuth2 authentication, event CRUD, and two-way sync between
JARVIS's local reminder database and Google Calendar.

Uses a dedicated "JARVIS" secondary calendar for voice-created reminders.
Primary calendar events are read for daily rundown but not auto-imported
as local reminders.

Singleton pattern — access via get_calendar_manager().
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Callable

from core.logger import get_logger

# Singleton instance
_instance: Optional["GoogleCalendarManager"] = None


def get_calendar_manager(config=None) -> Optional["GoogleCalendarManager"]:
    """Get or create the singleton GoogleCalendarManager.

    Call with config on first invocation (from jarvis_continuous.py).
    Call with no args from other modules to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = GoogleCalendarManager(config)
    return _instance


class GoogleCalendarManager:
    """Handles OAuth, event CRUD, and background sync with Google Calendar."""

    # Google Calendar API scopes
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # Paths
        self._credentials_path = os.path.expanduser(
            config.get("google_calendar.credentials_path", "~/jarvis/credentials.json")
        )
        self._token_path = os.path.expanduser(
            config.get("google_calendar.token_path",
                        "/mnt/storage/jarvis/data/google_token.json")
        )
        self._sync_token_path = os.path.expanduser(
            config.get("google_calendar.sync_token_path",
                        "/mnt/storage/jarvis/data/google_sync_token.json")
        )

        # Config
        self._sync_interval = config.get("google_calendar.sync_interval_seconds", 300)
        self._calendar_name = config.get("google_calendar.jarvis_calendar_name", "JARVIS")
        self._include_primary = config.get("google_calendar.include_primary_in_rundown", True)
        self._timezone = config.get("google_calendar.timezone", "America/Your_Timezone")

        # State
        self.creds = None
        self.service = None
        self._jarvis_calendar_id = None
        self._sync_token = None
        self._running = False
        self._thread = None

        # Callback for creating local reminders from Google events
        self._on_new_event: Optional[Callable] = None
        self._on_cancel_event: Optional[Callable] = None

        # Authenticate
        self._authenticated = False
        try:
            self._authenticate()
            self._authenticated = True
            self._load_sync_token()
            self._ensure_jarvis_calendar()
            self.logger.info("Google Calendar authenticated and ready")
        except FileNotFoundError:
            self.logger.warning(
                f"Google Calendar credentials not found at {self._credentials_path}. "
                "Calendar sync disabled. Download credentials.json from Google Cloud Console."
            )
        except Exception as e:
            self.logger.error(f"Google Calendar auth failed: {e}")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self):
        """Load token.json or trigger first-time OAuth browser flow."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not os.path.exists(self._credentials_path):
            raise FileNotFoundError(f"No credentials.json at {self._credentials_path}")

        creds = None
        if os.path.exists(self._token_path):
            creds = Credentials.from_authorized_user_file(self._token_path, self.SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                self.logger.info("Refreshing Google Calendar token...")
                creds.refresh(Request())
            else:
                self.logger.info("Starting Google Calendar OAuth flow (browser)...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, self.SCOPES
                )
                creds = flow.run_local_server(port=0)
                self.logger.info("Google Calendar OAuth completed")

            # Persist token
            os.makedirs(os.path.dirname(self._token_path), exist_ok=True)
            with open(self._token_path, "w") as f:
                f.write(creds.to_json())

        self.creds = creds
        self._build_service()

    def _build_service(self):
        """Build the Google Calendar API service object."""
        from googleapiclient.discovery import build

        self.service = build("calendar", "v3", credentials=self.creds)

    def _ensure_valid(self):
        """Refresh access token if expired. Persist refreshed token."""
        if not self._authenticated or not self.creds:
            return False

        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError

        if self.creds.expired:
            try:
                self.creds.refresh(Request())
                with open(self._token_path, "w") as f:
                    f.write(self.creds.to_json())
                self._build_service()
                return True
            except RefreshError:
                self.logger.error(
                    "Google Calendar token revoked! Re-authorization required. "
                    "Delete token.json and restart JARVIS."
                )
                self._authenticated = False
                return False
        return True

    # ------------------------------------------------------------------
    # Calendar Management
    # ------------------------------------------------------------------

    def _ensure_jarvis_calendar(self):
        """Find or create the dedicated JARVIS secondary calendar."""
        if not self._authenticated:
            return

        try:
            # List existing calendars
            calendar_list = self.service.calendarList().list().execute()
            for cal in calendar_list.get("items", []):
                if cal.get("summary") == self._calendar_name:
                    self._jarvis_calendar_id = cal["id"]
                    self.logger.info(f"Found JARVIS calendar: {self._jarvis_calendar_id}")
                    return

            # Create new calendar
            body = {
                "summary": self._calendar_name,
                "description": "Reminders managed by JARVIS voice assistant",
                "timeZone": self._timezone,
            }
            created = self.service.calendars().insert(body=body).execute()
            self._jarvis_calendar_id = created["id"]
            self.logger.info(f"Created JARVIS calendar: {self._jarvis_calendar_id}")

        except Exception as e:
            self.logger.error(f"Failed to ensure JARVIS calendar: {e}")

    # ------------------------------------------------------------------
    # Event CRUD
    # ------------------------------------------------------------------

    def create_event(self, title: str, start_time: datetime,
                     priority: int = 3, description: str = "") -> Optional[str]:
        """Create an event on the JARVIS calendar.

        Returns the Google event ID, or None on failure.
        """
        if not self._authenticated or not self._jarvis_calendar_id:
            return None

        self._ensure_valid()

        # Build reminder overrides based on priority
        if priority <= 2:
            reminders = {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 30},
                    {"method": "popup", "minutes": 10},
                    {"method": "popup", "minutes": 0},
                ],
            }
        else:
            reminders = {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                ],
            }

        # Events need an end time — use 15 min duration for reminders
        end_time = start_time + timedelta(minutes=15)

        priority_label = {1: "[URGENT] ", 2: "[IMPORTANT] ", 3: "", 4: "[low] "}
        event_body = {
            "summary": f"{priority_label.get(priority, '')}{title}",
            "description": description or f"JARVIS reminder (priority {priority})",
            "start": {
                "dateTime": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": self._timezone,
            },
            "end": {
                "dateTime": end_time.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": self._timezone,
            },
            "reminders": reminders,
        }

        try:
            created = self.service.events().insert(
                calendarId=self._jarvis_calendar_id,
                body=event_body,
            ).execute()
            event_id = created["id"]
            self.logger.info(f"Google Calendar event created: {event_id} ({title})")
            return event_id
        except Exception as e:
            self.logger.error(f"Failed to create Google Calendar event: {e}")
            return None

    def update_event(self, event_id: str, **kwargs) -> bool:
        """Update an existing event (title, start_time, description)."""
        if not self._authenticated or not self._jarvis_calendar_id or not event_id:
            return False

        self._ensure_valid()

        try:
            # Fetch current event
            event = self.service.events().get(
                calendarId=self._jarvis_calendar_id,
                eventId=event_id,
            ).execute()

            # Apply updates
            if "title" in kwargs:
                event["summary"] = kwargs["title"]
            if "start_time" in kwargs:
                start = kwargs["start_time"]
                end = start + timedelta(minutes=15)
                event["start"]["dateTime"] = start.strftime("%Y-%m-%dT%H:%M:%S")
                event["end"]["dateTime"] = end.strftime("%Y-%m-%dT%H:%M:%S")
            if "description" in kwargs:
                event["description"] = kwargs["description"]

            self.service.events().update(
                calendarId=self._jarvis_calendar_id,
                eventId=event_id,
                body=event,
            ).execute()
            self.logger.info(f"Google Calendar event updated: {event_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to update Google Calendar event: {e}")
            return False

    def delete_event(self, event_id: str) -> bool:
        """Delete an event from the JARVIS calendar."""
        if not self._authenticated or not self._jarvis_calendar_id or not event_id:
            return False

        self._ensure_valid()

        try:
            self.service.events().delete(
                calendarId=self._jarvis_calendar_id,
                eventId=event_id,
            ).execute()
            self.logger.info(f"Google Calendar event deleted: {event_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to delete Google Calendar event: {e}")
            return False

    # ------------------------------------------------------------------
    # Sync: Google → JARVIS
    # ------------------------------------------------------------------

    def sync_from_google(self) -> Dict[str, List[Dict]]:
        """Pull new/modified/deleted events from the JARVIS calendar.

        Uses incremental syncToken for efficiency.

        Returns dict with keys: 'new', 'updated', 'deleted'
        Each value is a list of event dicts.
        """
        if not self._authenticated or not self._jarvis_calendar_id:
            return {"new": [], "updated": [], "deleted": []}

        self._ensure_valid()

        result = {"new": [], "updated": [], "deleted": []}

        try:
            kwargs = {"calendarId": self._jarvis_calendar_id, "singleEvents": True}

            if self._sync_token:
                kwargs["syncToken"] = self._sync_token
            else:
                # First sync: get events from today forward
                now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + self._tz_offset()
                kwargs["timeMin"] = now
                kwargs["orderBy"] = "startTime"

            events_result = self.service.events().list(**kwargs).execute()

            for event in events_result.get("items", []):
                status = event.get("status", "confirmed")
                event_id = event["id"]

                if status == "cancelled":
                    result["deleted"].append({"google_event_id": event_id})
                else:
                    parsed = self._parse_google_event(event)
                    if parsed:
                        # Determine if new or updated based on whether we've seen it
                        parsed["google_event_id"] = event_id
                        result["new"].append(parsed)  # Caller resolves new vs update

            # Save sync token for next incremental call
            new_token = events_result.get("nextSyncToken")
            if new_token:
                self._sync_token = new_token
                self._save_sync_token()

            if result["new"] or result["deleted"]:
                self.logger.info(
                    f"Google sync: {len(result['new'])} new/updated, "
                    f"{len(result['deleted'])} deleted"
                )

        except Exception as e:
            err_str = str(e)
            if "410" in err_str or "fullSyncRequired" in err_str:
                # Sync token expired — do a full re-sync
                self.logger.warning("Sync token expired, performing full sync")
                self._sync_token = None
                self._save_sync_token()
                return self.sync_from_google()
            self.logger.error(f"Google Calendar sync failed: {e}")

        return result

    def get_primary_events_today(self) -> List[Dict]:
        """Get today's events from the primary calendar for daily rundown."""
        if not self._authenticated or not self._include_primary:
            return []

        self._ensure_valid()

        try:
            now = datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0)
            end_of_day = now.replace(hour=23, minute=59, second=59)

            tz = self._tz_offset()
            events_result = self.service.events().list(
                calendarId="primary",
                timeMin=start_of_day.strftime("%Y-%m-%dT%H:%M:%S") + tz,
                timeMax=end_of_day.strftime("%Y-%m-%dT%H:%M:%S") + tz,
                singleEvents=True,
                orderBy="startTime",
                maxResults=20,
            ).execute()

            results = []
            for event in events_result.get("items", []):
                parsed = self._parse_google_event(event)
                if parsed:
                    results.append(parsed)
            return results

        except Exception as e:
            self.logger.error(f"Failed to get primary calendar events: {e}")
            return []

    def get_primary_events_week(self) -> List[Dict]:
        """Get this week's events (Mon–Sun) from the primary calendar for weekly rundown."""
        if not self._authenticated or not self._include_primary:
            return []

        self._ensure_valid()

        try:
            now = datetime.now()
            # Monday of current week
            monday = now - timedelta(days=now.weekday())
            monday = monday.replace(hour=0, minute=0, second=0)
            sunday = monday + timedelta(days=6)
            sunday = sunday.replace(hour=23, minute=59, second=59)

            tz = self._tz_offset()
            events_result = self.service.events().list(
                calendarId="primary",
                timeMin=monday.strftime("%Y-%m-%dT%H:%M:%S") + tz,
                timeMax=sunday.strftime("%Y-%m-%dT%H:%M:%S") + tz,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()

            results = []
            for event in events_result.get("items", []):
                parsed = self._parse_google_event(event)
                if parsed:
                    results.append(parsed)
            return results

        except Exception as e:
            self.logger.error(f"Failed to get primary calendar events for week: {e}")
            return []

    def _parse_google_event(self, event: Dict) -> Optional[Dict]:
        """Parse a Google Calendar event into a JARVIS-friendly dict."""
        summary = event.get("summary", "")
        if not summary:
            return None

        # Extract priority from title prefix
        priority = 3
        clean_title = summary
        if summary.startswith("[URGENT] "):
            priority = 1
            clean_title = summary[9:]
        elif summary.startswith("[IMPORTANT] "):
            priority = 2
            clean_title = summary[12:]
        elif summary.startswith("[low] "):
            priority = 4
            clean_title = summary[6:]

        # Parse start time
        start = event.get("start", {})
        start_str = start.get("dateTime") or start.get("date")
        if not start_str:
            return None

        try:
            # Handle ISO format with timezone offset
            start_str_clean = re.sub(r"[+-]\d{2}:\d{2}$", "", start_str)
            start_time = datetime.fromisoformat(start_str_clean)
        except ValueError:
            return None

        # Extract reminder offset (minutes before event)
        # Google API: reminders.overrides = [{"method": "popup", "minutes": 15}, ...]
        # Use the earliest (largest minutes value) so JARVIS reminds at first alert
        reminder_minutes = None
        reminders_data = event.get("reminders", {})
        if reminders_data.get("useDefault"):
            # Default reminders are typically 10-30 min; use 15 as safe default
            reminder_minutes = 15
        else:
            overrides = reminders_data.get("overrides", [])
            popup_minutes = [
                r["minutes"] for r in overrides
                if r.get("method") in ("popup", "email") and "minutes" in r
            ]
            if popup_minutes:
                reminder_minutes = max(popup_minutes)  # earliest alert

        return {
            "title": clean_title,
            "start_time": start_time,
            "priority": priority,
            "description": event.get("description", ""),
            "google_event_id": event.get("id"),
            "reminder_minutes": reminder_minutes,
        }

    def _tz_offset(self) -> str:
        """Get the local timezone offset string (e.g., '-06:00')."""
        now = datetime.now()
        utc_now = datetime.utcnow()
        diff = now - utc_now
        total_seconds = int(diff.total_seconds())
        hours = total_seconds // 3600
        minutes = abs(total_seconds) % 3600 // 60
        return f"{hours:+03d}:{minutes:02d}"

    # ------------------------------------------------------------------
    # Sync Token Persistence
    # ------------------------------------------------------------------

    def _load_sync_token(self):
        """Load sync token from disk."""
        if os.path.exists(self._sync_token_path):
            try:
                with open(self._sync_token_path) as f:
                    data = json.load(f)
                    self._sync_token = data.get("sync_token")
                    self.logger.info("Loaded Google Calendar sync token")
            except Exception:
                self._sync_token = None

    def _save_sync_token(self):
        """Persist sync token to disk."""
        try:
            os.makedirs(os.path.dirname(self._sync_token_path), exist_ok=True)
            with open(self._sync_token_path, "w") as f:
                json.dump({"sync_token": self._sync_token}, f)
        except Exception as e:
            self.logger.error(f"Failed to save sync token: {e}")

    # ------------------------------------------------------------------
    # Background Sync Thread
    # ------------------------------------------------------------------

    def set_sync_callbacks(self, on_new_event: Callable, on_cancel_event: Callable):
        """Set callbacks for when events are synced from Google.

        on_new_event(title, start_time, priority, google_event_id, reminder_minutes) -> int
        on_cancel_event(google_event_id) -> bool
        """
        self._on_new_event = on_new_event
        self._on_cancel_event = on_cancel_event

    def start(self):
        """Start the background sync thread."""
        if not self._authenticated:
            self.logger.warning("Google Calendar not authenticated — sync disabled")
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.logger.info(f"Google Calendar sync started (interval={self._sync_interval}s)")

    def stop(self):
        """Stop the background sync thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        self.logger.info("Google Calendar sync stopped")

    def _poll_loop(self):
        """Background thread: sync from Google periodically."""
        while self._running:
            try:
                changes = self.sync_from_google()

                # Process new events from JARVIS calendar → create local reminders
                if self._on_new_event:
                    for event in changes["new"]:
                        self._on_new_event(
                            title=event["title"],
                            start_time=event["start_time"],
                            priority=event["priority"],
                            google_event_id=event["google_event_id"],
                            reminder_minutes=event.get("reminder_minutes"),
                        )

                # Process deleted events → cancel local reminders
                if self._on_cancel_event:
                    for event in changes["deleted"]:
                        self._on_cancel_event(event["google_event_id"])

            except Exception as e:
                self.logger.error(f"Google Calendar poll error: {e}")

            # Sleep in small increments for responsive shutdown
            for _ in range(self._sync_interval):
                if not self._running:
                    return
                time.sleep(1)

    # ------------------------------------------------------------------
    # Health Check
    # ------------------------------------------------------------------

    def check_connection(self) -> bool:
        """Verify Google Calendar connection is active."""
        if not self._authenticated:
            return False

        self._ensure_valid()
        try:
            self.service.calendarList().list(maxResults=1).execute()
            return True
        except Exception:
            return False

    @property
    def is_connected(self) -> bool:
        return self._authenticated and self.creds is not None and not self.creds.expired
