"""
Weather Poller

Background polling routine that fetches weather data from three sources
and stores it in the weather DB:
  - OpenWeatherMap (current conditions + 5-day forecast)
  - sunrise-sunset.org (sunrise/sunset times, batch pre-populated yearly)
  - NWS Alerts API (severe weather alerts for all tracked locations)

Follows the same daemon-thread pattern as NewsManager.
"""

import os
import subprocess
import threading
import time
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Set

import requests

from core.logger import get_logger
from core.weather_db import get_weather_db, WeatherDB


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional["WeatherPoller"] = None


def get_weather_poller(config=None) -> Optional["WeatherPoller"]:
    """Get or create the singleton WeatherPoller.

    Call with config on first invocation (from startup).
    Call with no args to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = WeatherPoller(config)
    return _instance


# ---------------------------------------------------------------------------
# Wind degree → compass direction
# ---------------------------------------------------------------------------

_COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _wind_direction(degrees: float) -> str:
    """Convert wind degrees to compass direction."""
    idx = round(degrees / 22.5) % 16
    return _COMPASS[idx]


# ---------------------------------------------------------------------------
# WeatherPoller
# ---------------------------------------------------------------------------

class WeatherPoller:
    """Background weather data fetcher."""

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # API keys / config
        self.owm_key = os.environ.get("OPENWEATHER_API_KEY", "")
        self.home_lat: float = config.get("location.home_lat", 33.6662)
        self.home_lon: float = config.get("location.home_lon", -86.8128)

        # Polling intervals (seconds)
        self.poll_interval: int = config.get("weather.poll_interval_seconds", 900)
        self.poll_interval_alert: int = config.get(
            "weather.poll_interval_alert_seconds", 300  # 5 minutes during active alerts
        )

        # NWS User-Agent (required by api.weather.gov)
        self.nws_headers = {
            "User-Agent": "(JARVIS Personal Assistant, contact@example.com)",
            "Accept": "application/geo+json",
        }

        # NWS zone cache: location_key → {"forecast_zone": "ALZ024", "county": "ALC073"}
        self._nws_zones: Dict[str, Dict[str, str]] = {}

        # Background thread
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

        # DB reference (must be initialized before poller starts)
        self._db: Optional[WeatherDB] = None

        # Alert notification callbacks
        self._tts_callback: Optional[callable] = None  # tts_proxy.speak
        self._alert_banner_callback: Optional[callable] = None  # WS push

        # Alert re-announcement interval (seconds) for critical alerts
        self.alert_remind_interval: int = config.get(
            "weather.alert_remind_interval_seconds", 300  # 5 minutes
        )

        # YouTube TV auto-launch on severe home alerts
        self.youtube_tv_channel_code: str = config.get(
            "weather.youtube_tv_channel_code", ""
        )
        self._tv_launched_alerts: Set[str] = set()  # alert IDs that already triggered TV

        self.logger.info("WeatherPoller initialized (interval=%ds)", self.poll_interval)

    @property
    def db(self) -> WeatherDB:
        """Lazy-load DB reference."""
        if self._db is None:
            self._db = get_weather_db()
        return self._db

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_tts_callback(self, callback: callable):
        """Set the TTS callback for announcing alerts (tts_proxy.speak)."""
        self._tts_callback = callback

    def set_alert_banner_callback(self, callback: callable):
        """Set the callback for pushing alert banners to web UI."""
        self._alert_banner_callback = callback

    def start(self):
        """Start background polling thread."""
        if not self.owm_key:
            self.logger.warning("OPENWEATHER_API_KEY not set — weather polling disabled")
            return
        if not self.config.get("weather.enabled", True):
            self.logger.info("Weather polling disabled in config")
            return

        self.logger.info("Starting weather poller (interval=%ds)", self.poll_interval)
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="weather-poll"
        )
        self._poll_thread.start()

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        self.logger.info("Weather poller stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """Main polling loop."""
        # Initial delay (30s) to let other systems initialize
        for _ in range(6):
            if not self._running:
                return
            time.sleep(5)

        # Check if sun_times needs pre-population
        try:
            self._maybe_populate_sun_times()
        except Exception as e:
            self.logger.error("Sun times pre-population failed: %s", e, exc_info=True)

        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                self.logger.error("Weather poll error: %s", e, exc_info=True)

            # Adaptive polling: shorter interval when alerts are active
            try:
                active_alerts = self.db.get_active_alerts()
                has_active = bool(active_alerts)
            except Exception:
                has_active = False

            interval = self.poll_interval_alert if has_active else self.poll_interval
            if has_active:
                self.logger.info("Active alerts detected — polling every %ds", interval)

            # Sleep in small increments for responsive shutdown
            for _ in range(interval // 5):
                if not self._running:
                    return
                time.sleep(5)

    def _poll_once(self):
        """Single poll cycle: current conditions, forecast, alerts."""
        self.logger.info("Weather poll cycle starting")

        # 1. Current conditions (home)
        try:
            self._fetch_current_conditions()
        except Exception as e:
            self.logger.error("Failed to fetch current conditions: %s", e)

        # 2. Forecast (home)
        try:
            self._fetch_forecast()
        except Exception as e:
            self.logger.error("Failed to fetch forecast: %s", e)

        # 3. NWS alerts for ALL tracked locations
        try:
            self._fetch_alerts_all_locations()
        except Exception as e:
            self.logger.error("Failed to fetch alerts: %s", e)

        # 4. Process new and active alerts — announce via TTS + push banners
        try:
            self._process_alert_notifications()
        except Exception as e:
            self.logger.error("Alert notification processing failed: %s", e)

        # 5. Cleanup expired alerts (older than 24h)
        try:
            removed = self.db.cleanup_expired_alerts()
            if removed:
                self.logger.info("Cleaned up %d expired alerts", removed)
        except Exception as e:
            self.logger.error("Alert cleanup failed: %s", e)

        self.logger.info("Weather poll cycle complete")

    # ------------------------------------------------------------------
    # OpenWeatherMap: Current Conditions
    # ------------------------------------------------------------------

    def _fetch_current_conditions(self):
        """Fetch current weather from OpenWeatherMap and store in DB."""
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {
            "lat": self.home_lat,
            "lon": self.home_lon,
            "appid": self.owm_key,
            "units": "imperial",
        }

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        weather_data = {
            "temp": round(data["main"]["temp"], 1),
            "feels_like": round(data["main"]["feels_like"], 1),
            "humidity": data["main"]["humidity"],
            "wind_speed": round(data["wind"].get("speed", 0), 1),
            "wind_dir": _wind_direction(data["wind"].get("deg", 0)),
            "description": data["weather"][0]["description"],
            "weather_main": data["weather"][0]["main"].lower(),
            "icon": data["weather"][0].get("icon", ""),
        }

        self.db.upsert_current(weather_data, location_key="home")
        self.logger.debug("Current conditions updated: %s°F, %s",
                          weather_data["temp"], weather_data["description"])

    # ------------------------------------------------------------------
    # Open-Meteo: 16-Day Forecast
    # ------------------------------------------------------------------

    # WMO weather code → (weather_main, description)
    _WMO_CODES = {
        0: ("clear", "clear sky"),
        1: ("clear", "mainly clear"),
        2: ("clouds", "partly cloudy"),
        3: ("clouds", "overcast"),
        45: ("fog", "fog"),
        48: ("fog", "depositing rime fog"),
        51: ("drizzle", "light drizzle"),
        53: ("drizzle", "moderate drizzle"),
        55: ("drizzle", "dense drizzle"),
        56: ("drizzle", "light freezing drizzle"),
        57: ("drizzle", "dense freezing drizzle"),
        61: ("rain", "slight rain"),
        63: ("rain", "moderate rain"),
        65: ("rain", "heavy rain"),
        66: ("rain", "light freezing rain"),
        67: ("rain", "heavy freezing rain"),
        71: ("snow", "slight snow"),
        73: ("snow", "moderate snow"),
        75: ("snow", "heavy snow"),
        77: ("snow", "snow grains"),
        80: ("rain", "slight rain showers"),
        81: ("rain", "moderate rain showers"),
        82: ("rain", "violent rain showers"),
        85: ("snow", "slight snow showers"),
        86: ("snow", "heavy snow showers"),
        95: ("thunderstorm", "thunderstorm"),
        96: ("thunderstorm", "thunderstorm with slight hail"),
        99: ("thunderstorm", "thunderstorm with heavy hail"),
    }

    def _fetch_forecast(self):
        """Fetch 16-day forecast from Open-Meteo and store in DB."""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.home_lat,
            "longitude": self.home_lon,
            "daily": ",".join([
                "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max",
                "weather_code", "wind_speed_10m_max",
            ]),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "forecast_days": 16,
            "timezone": "auto",
        }

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        rain_chances = daily.get("precipitation_probability_max", [])
        weather_codes = daily.get("weather_code", [])
        wind_speeds = daily.get("wind_speed_10m_max", [])

        rows = []
        for i, day_str in enumerate(dates):
            wmo_code = weather_codes[i] if i < len(weather_codes) else 0
            weather_main, description = self._WMO_CODES.get(
                wmo_code, ("clouds", f"WMO code {wmo_code}"))

            rows.append({
                "date": day_str,
                "temp_high": round(highs[i], 1) if i < len(highs) and highs[i] is not None else None,
                "temp_low": round(lows[i], 1) if i < len(lows) and lows[i] is not None else None,
                "description": description,
                "weather_main": weather_main,
                "rain_chance": round(rain_chances[i], 1) if i < len(rain_chances) and rain_chances[i] is not None else 0,
                "wind_speed": round(wind_speeds[i], 1) if i < len(wind_speeds) and wind_speeds[i] is not None else 0,
            })

        self.db.upsert_forecast(rows, location_key="home")
        self.logger.debug("Forecast updated: %d days (Open-Meteo)", len(rows))

    # ------------------------------------------------------------------
    # NWS Alerts (all tracked locations)
    # ------------------------------------------------------------------

    def _fetch_alerts_all_locations(self):
        """Fetch NWS alerts for every tracked location.

        Queries both zone-based and point-based endpoints for each location.
        Zone queries surface alerts faster (no geo-indexing delay), while
        point queries catch hyper-local alerts that may not map to a zone.
        """
        locations = self.db.get_tracked_locations()
        total_new = 0

        for loc in locations:
            loc_key = loc["location_key"]
            lat, lon = loc["lat"], loc["lon"]

            # 1. Zone-based query (faster — no geo-indexing delay)
            try:
                zones = self._resolve_nws_zones(lat, lon, loc_key)
                for zone_type in ("forecast_zone", "county"):
                    zone_id = zones.get(zone_type, "")
                    if zone_id:
                        new_ids = self._fetch_alerts_for_zone(zone_id, loc_key)
                        if new_ids:
                            total_new += len(new_ids)
                            self.logger.info("New alerts for %s (zone %s): %d",
                                            loc_key, zone_id, len(new_ids))
            except Exception as e:
                self.logger.error("NWS zone alert fetch failed for %s: %s",
                                  loc_key, e)

            # 2. Point-based query (catches hyper-local alerts)
            try:
                new_ids = self._fetch_alerts_for_point(lat, lon, loc_key)
                if new_ids:
                    total_new += len(new_ids)
                    self.logger.info("New alerts for %s (point): %d (%s)",
                                    loc_key, len(new_ids),
                                    ", ".join(a[:30] for a in new_ids))
            except Exception as e:
                self.logger.error("NWS point alert fetch failed for %s: %s",
                                  loc_key, e)

        if total_new:
            self.logger.info("Total new alerts this cycle: %d", total_new)

    def _fetch_alerts_for_point(self, lat: float, lon: float,
                                location_key: str) -> List[str]:
        """Fetch active NWS alerts for a single lat/lon point."""
        url = f"https://api.weather.gov/alerts/active"
        params = {"point": f"{lat:.4f},{lon:.4f}"}

        resp = requests.get(url, params=params, headers=self.nws_headers,
                            timeout=15)
        resp.raise_for_status()
        data = resp.json()

        alerts = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            alert_id = props.get("id") or feature.get("id", "")
            if not alert_id:
                continue
            alerts.append({
                "id": alert_id,
                "event": props.get("event", "Unknown"),
                "severity": props.get("severity", "Unknown"),
                "urgency": props.get("urgency", "Unknown"),
                "headline": props.get("headline", ""),
                "description": props.get("description", ""),
                "onset": props.get("onset", ""),
                "expires": props.get("expires", ""),
            })

        return self.db.upsert_alerts(alerts, location_key=location_key)

    def _resolve_nws_zones(self, lat: float, lon: float,
                           location_key: str) -> Dict[str, str]:
        """Look up NWS forecast zone and county for a lat/lon (cached)."""
        if location_key in self._nws_zones:
            return self._nws_zones[location_key]

        try:
            url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
            resp = requests.get(url, headers=self.nws_headers, timeout=10)
            resp.raise_for_status()
            props = resp.json().get("properties", {})

            # Extract zone IDs from URLs like ".../zones/forecast/ALZ024"
            fz_url = props.get("forecastZone", "")
            county_url = props.get("county", "")
            zones = {
                "forecast_zone": fz_url.rsplit("/", 1)[-1] if fz_url else "",
                "county": county_url.rsplit("/", 1)[-1] if county_url else "",
            }
            self._nws_zones[location_key] = zones
            self.logger.info("Resolved NWS zones for %s: %s", location_key, zones)
            return zones
        except Exception as e:
            self.logger.warning("NWS zone lookup failed for %s: %s", location_key, e)
            return {}

    def _fetch_alerts_for_zone(self, zone_id: str,
                               location_key: str) -> List[str]:
        """Fetch active NWS alerts for a forecast zone (faster than point query)."""
        if not zone_id:
            return []

        url = "https://api.weather.gov/alerts/active"
        params = {"zone": zone_id}

        resp = requests.get(url, params=params, headers=self.nws_headers,
                            timeout=15)
        resp.raise_for_status()
        data = resp.json()

        alerts = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            alert_id = props.get("id") or feature.get("id", "")
            if not alert_id:
                continue
            alerts.append({
                "id": alert_id,
                "event": props.get("event", "Unknown"),
                "severity": props.get("severity", "Unknown"),
                "urgency": props.get("urgency", "Unknown"),
                "headline": props.get("headline", ""),
                "description": props.get("description", ""),
                "onset": props.get("onset", ""),
                "expires": props.get("expires", ""),
            })

        return self.db.upsert_alerts(alerts, location_key=location_key)

    # ------------------------------------------------------------------
    # Alert Notification Processing
    # ------------------------------------------------------------------

    # Alerts that trigger immediate TTS announcement
    _IMMEDIATE_EVENTS = {
        "Tornado Warning",
        "Severe Thunderstorm Warning",
        "Flash Flood Warning",
        "Extreme Wind Warning",
        "Hurricane Warning",
        "Storm Surge Warning",
        "Fire Weather Warning",
        "Tsunami Warning",
    }

    # Alerts worth mentioning but not urgent
    _NOTABLE_EVENTS = {
        "Tornado Watch",
        "Severe Thunderstorm Watch",
        "Flash Flood Watch",
        "Winter Storm Warning",
        "Winter Storm Watch",
        "Ice Storm Warning",
        "Blizzard Warning",
        "Hurricane Watch",
        "Tropical Storm Warning",
        "Flood Warning",
        "Freeze Warning",
        "Freeze Watch",
        "Wind Chill Warning",
        "Wind Chill Watch",
        "Fire Weather Watch",
        "Heat Advisory",
        "Excessive Heat Warning",
        "Excessive Heat Watch",
        "Wind Advisory",
    }

    def _process_alert_notifications(self):
        """Check for unnotified alerts and announce critical ones."""
        unnotified = self.db.get_unnotified_alerts()
        if not unnotified:
            # Check for active critical alerts that need periodic re-announcement
            self._check_alert_reminders()
            return

        from core.honorific import get_honorific
        honorific = get_honorific()

        for alert in unnotified:
            event = alert.get("event", "")
            severity = alert.get("severity", "")
            headline = alert.get("headline", "")
            location_key = alert.get("location_key", "home")
            alert_id = alert.get("id", "")

            is_immediate = event in self._IMMEDIATE_EVENTS
            is_notable = event in self._NOTABLE_EVENTS

            if not is_immediate and not is_notable:
                # Low-priority alert — mark notified but don't announce
                self.db.mark_alert_notified(alert_id)
                continue

            # Build location-aware announcement
            if location_key == "home":
                location_phrase = "for the Gardendale area"
            else:
                # Away user — extract label from tracked locations
                locations = self.db.get_tracked_locations()
                loc_info = next((l for l in locations if l["location_key"] == location_key), None)
                if loc_info:
                    location_phrase = f"in your area near {loc_info['label'].split(' - ', 1)[-1]}"
                else:
                    location_phrase = "in your area"

            # Craft TTS announcement
            tv_launched = False
            if is_immediate and location_key == "home":
                # Launch YouTube TV to Fox 6 for severe home alerts
                self._launch_youtube_tv(alert_id)
                tv_launched = alert_id in self._tv_launched_alerts

            if is_immediate:
                text = (f"{honorific.capitalize()}, severe weather alert {location_phrase}. "
                        f"{headline}")
                if tv_launched:
                    text += " I've turned on Fox 6 for live coverage."
            else:
                text = (f"{honorific.capitalize()}, weather advisory {location_phrase}. "
                        f"{headline}")

            # Announce via TTS
            if self._tts_callback:
                self._tts_callback(text)
                self.logger.info("Weather alert announced: %s — %s", event, headline[:80])

            # Push banner to web UI
            if self._alert_banner_callback:
                self._alert_banner_callback({
                    "type": "weather_alert",
                    "event": event,
                    "severity": severity,
                    "headline": headline,
                    "location_key": location_key,
                    "alert_id": alert_id,
                    "is_immediate": is_immediate,
                })

            self.db.mark_alert_notified(alert_id)

    def _check_alert_reminders(self):
        """Re-announce critical active alerts periodically."""
        active = self.db.get_active_alerts()
        now = datetime.now()

        for alert in active:
            event = alert.get("event", "")
            if event not in self._IMMEDIATE_EVENTS:
                continue

            # Check if enough time has passed since last reminder
            last_reminded = alert.get("last_reminded") or alert.get("notified_at")
            if not last_reminded:
                continue

            try:
                last_dt = datetime.fromisoformat(last_reminded)
                elapsed = (now - last_dt).total_seconds()
            except (ValueError, TypeError):
                continue

            if elapsed < self.alert_remind_interval:
                continue

            # Re-announce
            from core.honorific import get_honorific
            honorific = get_honorific()
            headline = alert.get("headline", "")
            location_key = alert.get("location_key", "home")

            if location_key == "home":
                location_phrase = "for the Gardendale area"
            else:
                location_phrase = "in your area"

            text = (f"{honorific.capitalize()}, reminder — the {event.lower()} "
                    f"{location_phrase} remains in effect. {headline}")

            if self._tts_callback:
                self._tts_callback(text)
                self.logger.info("Weather alert reminder: %s", event)

            self.db.mark_alert_reminded(alert.get("id", ""))

    # ------------------------------------------------------------------
    # YouTube TV Auto-Launch
    # ------------------------------------------------------------------

    def _launch_youtube_tv(self, alert_id: str):
        """Launch YouTube TV to Local News Station for severe home weather.

        Only fires once per alert (tracked in _tv_launched_alerts).
        Opens Brave in fullscreen, non-incognito (YouTube TV needs auth).
        """
        if not self.youtube_tv_channel_code:
            return
        if alert_id in self._tv_launched_alerts:
            return

        url = f"https://tv.youtube.com/watch/{self.youtube_tv_channel_code}"

        try:
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")
            subprocess.Popen(
                [
                    "brave-browser",
                    "--ozone-platform=x11",
                    "--start-fullscreen",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            self._tv_launched_alerts.add(alert_id)
            self.logger.info("YouTube TV launched for alert %s → Local News Station",
                             alert_id[:30])
        except Exception as e:
            self.logger.error("Failed to launch YouTube TV: %s", e)

    # ------------------------------------------------------------------
    # Sunrise-Sunset Pre-population
    # ------------------------------------------------------------------

    def _maybe_populate_sun_times(self):
        """Pre-populate sun_times for the full year if needed."""
        count = self.db.get_sun_times_count()
        if count >= 300:
            self.logger.debug("Sun times already populated (%d rows)", count)
            return

        self.logger.info("Pre-populating sunrise/sunset times for the year "
                         "(%d existing rows)...", count)
        self._populate_sun_times_year()

    def _populate_sun_times_year(self):
        """Fetch sunrise/sunset for every day of the current year."""
        today = date.today()
        year_start = date(today.year, 1, 1)
        year_end = date(today.year, 12, 31)

        current = year_start
        batch = []
        batch_size = 30  # Commit every 30 days to avoid huge transactions

        while current <= year_end:
            if not self._running:
                self.logger.info("Sun times population interrupted by shutdown")
                break

            try:
                sun_data = self._fetch_sun_times_for_date(current)
                if sun_data:
                    batch.append(sun_data)
            except Exception as e:
                self.logger.warning("Sun times fetch failed for %s: %s", current, e)

            current += timedelta(days=1)

            # Commit in batches
            if len(batch) >= batch_size:
                self.db.upsert_sun_times(batch)
                self.logger.info("Sun times: %d days committed (through %s)",
                                 len(batch), batch[-1]["date"])
                batch = []

            # Be polite to the API — 0.3s delay between calls
            time.sleep(0.3)

        # Commit remaining
        if batch:
            self.db.upsert_sun_times(batch)
            self.logger.info("Sun times: final batch of %d days committed", len(batch))

        final_count = self.db.get_sun_times_count()
        self.logger.info("Sun times pre-population complete: %d total rows", final_count)

    def _fetch_sun_times_for_date(self, d: date) -> Optional[Dict]:
        """Fetch sunrise/sunset for a single date from sunrise-sunset.org."""
        url = "https://api.sunrise-sunset.org/json"
        params = {
            "lat": self.home_lat,
            "lng": self.home_lon,
            "date": d.isoformat(),
            "formatted": 0,  # ISO 8601 format
            "tzid": "America/Your_Timezone",
        }

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK":
            self.logger.warning("sunrise-sunset.org returned status=%s for %s",
                                data.get("status"), d)
            return None

        results = data.get("results", {})
        sunrise_raw = results.get("sunrise", "")
        sunset_raw = results.get("sunset", "")
        day_length_secs = results.get("day_length", 0)

        # Parse ISO times to just HH:MM AM/PM for readability
        sunrise_str = self._format_sun_time(sunrise_raw)
        sunset_str = self._format_sun_time(sunset_raw)

        # Format day length
        if isinstance(day_length_secs, (int, float)):
            hours = int(day_length_secs) // 3600
            minutes = (int(day_length_secs) % 3600) // 60
            day_length_str = f"{hours}:{minutes:02d}"
        else:
            day_length_str = str(day_length_secs)

        return {
            "date": d.isoformat(),
            "sunrise": sunrise_str,
            "sunset": sunset_str,
            "day_length": day_length_str,
        }

    @staticmethod
    def _format_sun_time(iso_str: str) -> str:
        """Convert ISO datetime string to '6:42 AM' style local time."""
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%-I:%M %p")
        except (ValueError, TypeError):
            return iso_str

    # ------------------------------------------------------------------
    # On-demand fetch (for away users asking about weather)
    # ------------------------------------------------------------------

    def fetch_current_for_coords(self, lat: float, lon: float) -> Optional[Dict]:
        """Fetch current weather for arbitrary coordinates (live API, not cached).

        Used when an away mobile user asks 'what's the weather?' and their
        GPS coords don't match home.
        """
        if not self.owm_key:
            return None

        try:
            url = "https://api.openweathermap.org/data/2.5/weather"
            params = {
                "lat": lat, "lon": lon,
                "appid": self.owm_key, "units": "imperial",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            return {
                "temp": round(data["main"]["temp"], 1),
                "feels_like": round(data["main"]["feels_like"], 1),
                "humidity": data["main"]["humidity"],
                "wind_speed": round(data["wind"].get("speed", 0), 1),
                "wind_dir": _wind_direction(data["wind"].get("deg", 0)),
                "description": data["weather"][0]["description"],
                "weather_main": data["weather"][0]["main"].lower(),
                "icon": data["weather"][0].get("icon", ""),
                "location_name": data.get("name", ""),
            }
        except Exception as e:
            self.logger.error("Live weather fetch for (%.4f, %.4f) failed: %s",
                              lat, lon, e)
            return None

    def fetch_forecast_for_coords(self, lat: float, lon: float) -> Optional[List[Dict]]:
        """Fetch forecast for arbitrary coordinates (live API, not cached)."""
        if not self.owm_key:
            return None

        try:
            url = "https://api.openweathermap.org/data/2.5/forecast"
            params = {
                "lat": lat, "lon": lon,
                "appid": self.owm_key, "units": "imperial",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            # Aggregate into daily summaries (same logic as _fetch_forecast)
            daily: Dict[str, dict] = {}
            for item in data.get("list", []):
                dt = datetime.fromtimestamp(item["dt"])
                day_str = dt.strftime("%Y-%m-%d")
                weather_main = item["weather"][0]["main"].lower()

                if day_str not in daily:
                    daily[day_str] = {
                        "date": day_str,
                        "temp_high": item["main"]["temp_max"],
                        "temp_low": item["main"]["temp_min"],
                        "description": item["weather"][0]["description"],
                        "weather_main": weather_main,
                        "rain_chance": item.get("pop", 0) * 100,
                        "wind_speed": round(item["wind"].get("speed", 0), 1),
                    }
                else:
                    d = daily[day_str]
                    d["temp_high"] = max(d["temp_high"], item["main"]["temp_max"])
                    d["temp_low"] = min(d["temp_low"], item["main"]["temp_min"])
                    d["rain_chance"] = max(d["rain_chance"], item.get("pop", 0) * 100)
                    d["wind_speed"] = max(d["wind_speed"],
                                          round(item["wind"].get("speed", 0), 1))
                    if "thunderstorm" in weather_main:
                        d["weather_main"] = "thunderstorm"
                        d["description"] = item["weather"][0]["description"]
                    elif ("rain" in weather_main or "drizzle" in weather_main) and \
                            "thunderstorm" not in d["weather_main"]:
                        d["weather_main"] = weather_main
                        d["description"] = item["weather"][0]["description"]

            rows = []
            for day_str in sorted(daily.keys()):
                d = daily[day_str]
                d["temp_high"] = round(d["temp_high"], 1)
                d["temp_low"] = round(d["temp_low"], 1)
                d["rain_chance"] = round(d["rain_chance"], 1)
                rows.append(d)

            return rows
        except Exception as e:
            self.logger.error("Live forecast fetch for (%.4f, %.4f) failed: %s",
                              lat, lon, e)
            return None
