"""
Weather Database Manager

Local SQLite cache for weather data, populated by a background polling routine.
Supports multi-location tracking: home base is always polled (full weather),
away users get NWS alert polling based on GPS divergence detection.

Follows the same singleton + threading-lock pattern as NewsManager and
ReminderManager.
"""

import math
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple

from core.logger import get_logger


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional["WeatherDB"] = None


def get_weather_db(config=None) -> Optional["WeatherDB"]:
    """Get or create the singleton WeatherDB.

    Call with config on first invocation (from startup).
    Call with no args from skills to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = WeatherDB(config)
    return _instance


# ---------------------------------------------------------------------------
# Haversine distance (miles)
# ---------------------------------------------------------------------------

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Temporal phrase parser
# ---------------------------------------------------------------------------

def parse_temporal_phrase(text: str) -> tuple[date, date] | None:
    """Resolve a natural-language time phrase to (start_date, end_date).

    Returns None if no temporal phrase is detected.
    """
    text_lower = text.lower().strip()
    today = date.today()
    weekday = today.weekday()  # 0=Monday … 6=Sunday

    # "this weekend" — upcoming Saturday + Sunday (or today if already Sat/Sun)
    if re.search(r'\bthis\s+weekend\b', text_lower):
        days_to_sat = (5 - weekday) % 7
        if days_to_sat == 0 and weekday != 5:
            days_to_sat = 7  # already past Saturday this week
        sat = today + timedelta(days=days_to_sat)
        if weekday in (5, 6):
            sat = today if weekday == 5 else today - timedelta(days=1)
        sun = sat + timedelta(days=1)
        return (sat, sun)

    # "next weekend"
    if re.search(r'\bnext\s+weekend\b', text_lower):
        days_to_sat = (5 - weekday) % 7
        if days_to_sat == 0:
            days_to_sat = 7
        sat = today + timedelta(days=days_to_sat + 7)
        # If we're before this Saturday, "next weekend" is the one after this
        if weekday < 5:
            sat = today + timedelta(days=(5 - weekday) + 7)
        else:
            sat = today + timedelta(days=(5 - weekday) % 7 + 7)
        sun = sat + timedelta(days=1)
        return (sat, sun)

    # "next week" — next Monday through Sunday
    if re.search(r'\bnext\s+week\b', text_lower):
        days_to_mon = (7 - weekday) % 7
        if days_to_mon == 0:
            days_to_mon = 7
        mon = today + timedelta(days=days_to_mon)
        sun = mon + timedelta(days=6)
        return (mon, sun)

    # "this week" — today through Sunday
    if re.search(r'\bthis\s+week\b', text_lower):
        days_to_sun = (6 - weekday) % 7
        if days_to_sun == 0 and weekday != 6:
            days_to_sun = 7
        sun = today + timedelta(days=days_to_sun)
        if weekday == 6:
            sun = today
        return (today, sun)

    # "end of the week" / "end of this week" — Friday through Sunday
    if re.search(r'\bend\s+of\s+(the|this)\s+week\b', text_lower):
        days_to_fri = (4 - weekday) % 7
        if weekday > 4:
            fri = today
        else:
            fri = today + timedelta(days=days_to_fri)
        sun = fri + timedelta(days=(6 - fri.weekday()) % 7)
        if fri.weekday() == 6:
            sun = fri
        return (fri, sun)

    # "next N days" / "next few days" / "coming days"
    m = re.search(r'\bnext\s+(\d+)\s+days?\b', text_lower)
    if m:
        n = int(m.group(1))
        return (today, today + timedelta(days=n))
    if re.search(r'\bnext\s+few\s+days\b', text_lower):
        return (today, today + timedelta(days=3))
    if re.search(r'\bcoming\s+days\b', text_lower):
        return (today, today + timedelta(days=3))

    return None


# ---------------------------------------------------------------------------
# WeatherDB
# ---------------------------------------------------------------------------

class WeatherDB:
    """SQLite weather cache with multi-location tracking."""

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # Database path
        self.db_path = Path(config.get(
            "weather.db_path",
            "/mnt/storage/jarvis/data/weather.db",
        ))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.Lock()

        # Home coordinates from config
        self.home_lat: float = config.get("location.home_lat", 33.6662)
        self.home_lon: float = config.get("location.home_lon", -86.8128)

        # Divergence threshold (miles)
        self.divergence_threshold: float = config.get(
            "weather.divergence_threshold_miles", 25.0
        )

        # Initialize schema
        self._init_db()
        self.logger.info("WeatherDB initialized — %s", self.db_path)

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Create a new connection with WAL mode and Row factory."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create all tables and indexes."""
        with self._db_lock:
            conn = self._conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS tracked_locations (
                        location_key    TEXT PRIMARY KEY,
                        label           TEXT,
                        lat             REAL NOT NULL,
                        lon             REAL NOT NULL,
                        user_id         TEXT,
                        source          TEXT NOT NULL DEFAULT 'config',
                        created_at      TEXT NOT NULL,
                        updated_at      TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS current_conditions (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        location_key    TEXT NOT NULL DEFAULT 'home',
                        temp            REAL,
                        feels_like      REAL,
                        humidity        INTEGER,
                        wind_speed      REAL,
                        wind_dir        TEXT,
                        description     TEXT,
                        weather_main    TEXT,
                        icon            TEXT,
                        fetched_at      TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS forecast (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        location_key    TEXT NOT NULL DEFAULT 'home',
                        date            TEXT NOT NULL,
                        temp_high       REAL,
                        temp_low        REAL,
                        description     TEXT,
                        weather_main    TEXT,
                        rain_chance     REAL,
                        wind_speed      REAL,
                        fetched_at      TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sun_times (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        date            TEXT NOT NULL UNIQUE,
                        sunrise         TEXT,
                        sunset          TEXT,
                        day_length      TEXT,
                        fetched_at      TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS alerts (
                        id              TEXT PRIMARY KEY,
                        location_key    TEXT NOT NULL,
                        event           TEXT NOT NULL,
                        severity        TEXT,
                        urgency         TEXT,
                        headline        TEXT,
                        description     TEXT,
                        onset           TEXT,
                        expires         TEXT,
                        fetched_at      TEXT NOT NULL,
                        notified        INTEGER NOT NULL DEFAULT 0,
                        notified_at     TEXT,
                        last_reminded   TEXT
                    );

                    -- Indexes
                    CREATE INDEX IF NOT EXISTS idx_current_location
                        ON current_conditions(location_key);
                    CREATE INDEX IF NOT EXISTS idx_forecast_location_date
                        ON forecast(location_key, date);
                    CREATE INDEX IF NOT EXISTS idx_sun_date
                        ON sun_times(date);
                    CREATE INDEX IF NOT EXISTS idx_alerts_location
                        ON alerts(location_key);
                    CREATE INDEX IF NOT EXISTS idx_alerts_notified
                        ON alerts(notified);
                    CREATE INDEX IF NOT EXISTS idx_alerts_expires
                        ON alerts(expires);
                """)
                conn.commit()

                # Seed home location if not present
                now = datetime.now().isoformat()
                conn.execute("""
                    INSERT OR IGNORE INTO tracked_locations
                        (location_key, label, lat, lon, user_id, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, 'config', ?, ?)
                """, ("home", "Home - Gardendale, AL",
                      self.home_lat, self.home_lon, now, now))
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Tracked Locations
    # ------------------------------------------------------------------

    def upsert_tracked_location(self, key: str, label: str,
                                lat: float, lon: float,
                                user_id: Optional[str] = None,
                                source: str = "gps") -> None:
        """Add or update a tracked location."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO tracked_locations
                        (location_key, label, lat, lon, user_id, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(location_key) DO UPDATE SET
                        label = excluded.label,
                        lat = excluded.lat,
                        lon = excluded.lon,
                        updated_at = excluded.updated_at
                """, (key, label, lat, lon, user_id, source, now, now))
                conn.commit()
            finally:
                conn.close()

    def remove_tracked_location(self, key: str) -> None:
        """Remove a tracked location. Cannot remove 'home'."""
        if key == "home":
            self.logger.warning("Cannot remove home location")
            return
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM tracked_locations WHERE location_key = ?", (key,))
                # Also clean up any alerts tied to this location
                conn.execute("DELETE FROM alerts WHERE location_key = ?", (key,))
                conn.commit()
            finally:
                conn.close()

    def get_tracked_locations(self) -> List[Dict]:
        """Return all tracked locations."""
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT * FROM tracked_locations").fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_away_locations(self) -> List[Dict]:
        """Return only non-home tracked locations."""
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM tracked_locations WHERE location_key != 'home'"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def is_away(self, lat: float, lon: float) -> bool:
        """Check if coordinates are beyond the divergence threshold from home."""
        return haversine_miles(self.home_lat, self.home_lon, lat, lon) > self.divergence_threshold

    # ------------------------------------------------------------------
    # Current Conditions
    # ------------------------------------------------------------------

    def upsert_current(self, data: Dict, location_key: str = "home") -> None:
        """Store current weather conditions. Replaces previous entry for this location."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                # Delete previous entry for this location
                conn.execute(
                    "DELETE FROM current_conditions WHERE location_key = ?",
                    (location_key,)
                )
                conn.execute("""
                    INSERT INTO current_conditions
                        (location_key, temp, feels_like, humidity, wind_speed,
                         wind_dir, description, weather_main, icon, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    location_key,
                    data.get("temp"),
                    data.get("feels_like"),
                    data.get("humidity"),
                    data.get("wind_speed"),
                    data.get("wind_dir"),
                    data.get("description"),
                    data.get("weather_main"),
                    data.get("icon"),
                    now,
                ))
                conn.commit()
            finally:
                conn.close()

    def get_current(self, location_key: str = "home") -> Optional[Dict]:
        """Read the most recent current conditions for a location."""
        with self._db_lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM current_conditions WHERE location_key = ? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (location_key,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def is_current_stale(self, max_age_seconds: int = 1800,
                         location_key: str = "home") -> bool:
        """Check if current conditions are older than max_age_seconds."""
        current = self.get_current(location_key)
        if not current:
            return True
        try:
            fetched = datetime.fromisoformat(current["fetched_at"])
            return (datetime.now() - fetched).total_seconds() > max_age_seconds
        except (ValueError, KeyError):
            return True

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def upsert_forecast(self, rows: List[Dict],
                        location_key: str = "home") -> None:
        """Store forecast rows. Replaces all forecast data for this location."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM forecast WHERE location_key = ?",
                    (location_key,)
                )
                for row in rows:
                    conn.execute("""
                        INSERT INTO forecast
                            (location_key, date, temp_high, temp_low, description,
                             weather_main, rain_chance, wind_speed, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        location_key,
                        row.get("date"),
                        row.get("temp_high"),
                        row.get("temp_low"),
                        row.get("description"),
                        row.get("weather_main"),
                        row.get("rain_chance"),
                        row.get("wind_speed"),
                        now,
                    ))
                conn.commit()
            finally:
                conn.close()

    def get_forecast(self, days: int = 5,
                     location_key: str = "home") -> List[Dict]:
        """Read forecast rows for a location, ordered by date."""
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM forecast WHERE location_key = ? "
                    "ORDER BY date ASC LIMIT ?",
                    (location_key, days)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Sun Times
    # ------------------------------------------------------------------

    def upsert_sun_times(self, rows: List[Dict]) -> None:
        """Batch insert/replace sunrise-sunset data."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                for row in rows:
                    conn.execute("""
                        INSERT INTO sun_times (date, sunrise, sunset, day_length, fetched_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(date) DO UPDATE SET
                            sunrise = excluded.sunrise,
                            sunset = excluded.sunset,
                            day_length = excluded.day_length,
                            fetched_at = excluded.fetched_at
                    """, (
                        row.get("date"),
                        row.get("sunrise"),
                        row.get("sunset"),
                        row.get("day_length"),
                        now,
                    ))
                conn.commit()
            finally:
                conn.close()

    def get_sun_times(self, date: str) -> Optional[Dict]:
        """Get sunrise/sunset for a specific date (YYYY-MM-DD)."""
        with self._db_lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM sun_times WHERE date = ?", (date,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_sun_times_count(self) -> int:
        """Return number of sun_times rows (for checking if pre-population needed)."""
        with self._db_lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT COUNT(*) as cnt FROM sun_times").fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def upsert_alerts(self, alerts: List[Dict],
                      location_key: str = "home") -> List[str]:
        """Store NWS alerts. Returns list of newly inserted alert IDs."""
        now = datetime.now().isoformat()
        new_ids = []
        with self._db_lock:
            conn = self._conn()
            try:
                for alert in alerts:
                    alert_id = alert.get("id")
                    if not alert_id:
                        continue
                    # Check if already exists
                    existing = conn.execute(
                        "SELECT id FROM alerts WHERE id = ?", (alert_id,)
                    ).fetchone()
                    if existing:
                        # Update expiry and description (NWS may update alerts)
                        conn.execute("""
                            UPDATE alerts SET
                                headline = ?, description = ?,
                                expires = ?, fetched_at = ?
                            WHERE id = ?
                        """, (
                            alert.get("headline"),
                            alert.get("description"),
                            alert.get("expires"),
                            now,
                            alert_id,
                        ))
                    else:
                        conn.execute("""
                            INSERT INTO alerts
                                (id, location_key, event, severity, urgency,
                                 headline, description, onset, expires,
                                 fetched_at, notified)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """, (
                            alert_id,
                            location_key,
                            alert.get("event"),
                            alert.get("severity"),
                            alert.get("urgency"),
                            alert.get("headline"),
                            alert.get("description"),
                            alert.get("onset"),
                            alert.get("expires"),
                            now,
                        ))
                        new_ids.append(alert_id)
                conn.commit()
            finally:
                conn.close()
        return new_ids

    def get_active_alerts(self, location_key: Optional[str] = None) -> List[Dict]:
        """Get unexpired alerts, optionally filtered by location."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                if location_key:
                    rows = conn.execute(
                        "SELECT * FROM alerts WHERE location_key = ? "
                        "AND (expires IS NULL OR expires > ?) "
                        "ORDER BY severity ASC, onset ASC",
                        (location_key, now)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM alerts "
                        "WHERE expires IS NULL OR expires > ? "
                        "ORDER BY severity ASC, onset ASC",
                        (now,)
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_unnotified_alerts(self) -> List[Dict]:
        """Get alerts that haven't been announced yet (across all locations)."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE notified = 0 "
                    "AND (expires IS NULL OR expires > ?) "
                    "ORDER BY severity ASC, onset ASC",
                    (now,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def mark_alert_notified(self, alert_id: str) -> None:
        """Mark an alert as announced via TTS."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE alerts SET notified = 1, notified_at = ? WHERE id = ?",
                    (now, alert_id)
                )
                conn.commit()
            finally:
                conn.close()

    def mark_alert_reminded(self, alert_id: str) -> None:
        """Update the last_reminded timestamp for periodic re-announcements."""
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE alerts SET last_reminded = ? WHERE id = ?",
                    (now, alert_id)
                )
                conn.commit()
            finally:
                conn.close()

    def cleanup_expired_alerts(self) -> int:
        """Remove alerts that expired more than 24 hours ago. Returns count removed."""
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        with self._db_lock:
            conn = self._conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM alerts WHERE expires IS NOT NULL AND expires < ?",
                    (cutoff,)
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()
