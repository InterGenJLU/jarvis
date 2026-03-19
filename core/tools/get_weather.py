"""Tool definition: get_weather — current weather, forecast, rain check, sunrise/sunset."""

import logging
import os
from datetime import datetime, date, timedelta

TOOL_NAME = "get_weather"
SKILL_NAME = "weather"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get current weather, forecast, rain check, or sunrise/sunset times. "
            "Use for ANY question about weather, temperature, conditions, "
            "rain, forecast, sunrise, or sunset. Covers current conditions and future forecasts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["current", "forecast", "tomorrow", "rain_check", "sunrise", "sunset", "period"],
                    "description": (
                        "current: current weather conditions. "
                        "forecast: 3-day forecast. "
                        "tomorrow: tomorrow's weather. "
                        "rain_check: will it rain tomorrow. "
                        "sunrise: today's sunrise time. "
                        "sunset: today's sunset time. "
                        "period: weather for a specific time period (use with 'period' parameter)."
                    )
                },
                "location": {
                    "type": "string",
                    "description": (
                        "City or location name (e.g. 'Paris', 'London', 'New York'). "
                        "Omit for the user's default location."
                    )
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Temporal phrase for period queries (e.g. 'this weekend', "
                        "'next week', 'next few days', 'next 5 days'). "
                        "Only used when query_type is 'period'."
                    )
                }
            },
            "required": ["query_type"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For ANY question about weather, temperature, forecast, rain, sunrise, or sunset, "
    "call get_weather. No location needed — it defaults to "
    "the user's home location. "
    "Examples: 'is it going to rain?' → rain_check, 'what's it like outside?' → current, "
    "'weather this week' → forecast, 'when is sunrise?' → sunrise, 'sunset today' → sunset, "
    "'weather this weekend' → period (period='this weekend'), "
    "'next week weather' → period (period='next week'). "
    "NOT for: climate change discussion, historical weather data, weather in fiction."
)

from core.logger import get_logger
logger = get_logger("jarvis.tools.get_weather")


# ---------------------------------------------------------------------------
# Location defaults
# ---------------------------------------------------------------------------

def _get_weather_api_key() -> str:
    """Lazy read — .env may not be loaded at import time."""
    return os.environ.get("OPENWEATHER_API_KEY", "")

_DEFAULT_LAT = 33.6662
_DEFAULT_LON = -86.8128
_DEFAULT_CITY = "Gardendale"


def _resolve_location(location: str | None) -> tuple[float, float, str]:
    """Geocode a location name or return the default coordinates."""
    if not location:
        return _DEFAULT_LAT, _DEFAULT_LON, _DEFAULT_CITY

    import requests
    try:
        resp = requests.get(
            "http://api.openweathermap.org/geo/1.0/direct",
            params={"q": location, "limit": 1, "appid": _get_weather_api_key()},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            return data[0]["lat"], data[0]["lon"], data[0]["name"]
    except Exception as e:
        logger.error(f"Geocoding error for '{location}': {e}")

    return _DEFAULT_LAT, _DEFAULT_LON, _DEFAULT_CITY


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_db():
    """Try to get the weather DB instance."""
    try:
        from core.weather_db import get_weather_db
        return get_weather_db()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(args: dict) -> str:
    """Route to the appropriate weather sub-handler."""
    query_type = args.get("query_type", "current")
    location = args.get("location")

    # Sunrise/sunset don't need location resolution
    if query_type == "sunrise":
        return _weather_sun("sunrise")
    elif query_type == "sunset":
        return _weather_sun("sunset")

    # Period queries use the temporal parser
    if query_type == "period":
        period_text = args.get("period", "")
        return _weather_period(period_text)

    lat, lon, city = _resolve_location(location)
    is_home = (location is None)

    if query_type == "forecast":
        return _weather_forecast(lat, lon, city, is_home)
    elif query_type == "tomorrow":
        return _weather_tomorrow(lat, lon, city, is_home)
    elif query_type == "rain_check":
        return _weather_rain_check(lat, lon, city, is_home)
    # Default: current
    return _weather_current(lat, lon, city, is_home)


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------

def _weather_current(lat: float, lon: float, city: str, is_home: bool) -> str:
    """Current weather conditions — raw data for LLM synthesis."""
    # Try DB for home location
    if is_home:
        db = _get_db()
        if db and not db.is_current_stale():
            current = db.get_current()
            if current:
                temp = round(current["temp"])
                feels = round(current["feels_like"])
                desc = current.get("description", "")
                wind = round(current.get("wind_speed", 0))
                result = f"Weather in {city}: {temp} degrees"
                if abs(temp - feels) > 3:
                    result += f" (feels like {feels})"
                result += f", {desc}."
                if wind >= 15:
                    result += f" Windy at {wind} mph."
                return result

    # Live API fallback (or named location)
    import requests
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": _get_weather_api_key(),
                    "units": "imperial"},
            timeout=5,
        )
        resp.raise_for_status()
        d = resp.json()

        temp = round(d["main"]["temp"])
        feels = round(d["main"]["feels_like"])
        desc = d["weather"][0]["description"]
        wind = round(d["wind"]["speed"])

        result = f"Weather in {city}: {temp} degrees"
        if abs(temp - feels) > 3:
            result += f" (feels like {feels})"
        result += f", {desc}."
        if wind >= 15:
            result += f" Windy at {wind} mph."
        return result

    except Exception as e:
        logger.error(f"Weather API error: {e}")
        return f"Error fetching weather for {city}: {e}"


def _weather_forecast(lat: float, lon: float, city: str, is_home: bool) -> str:
    """3-day forecast — raw data for LLM synthesis."""
    # Try DB for home location
    if is_home:
        db = _get_db()
        if db:
            rows = db.get_forecast(days=3)
            if rows:
                lines = [f"3-day forecast for {city}:"]
                for row in rows:
                    dt = datetime.strptime(row["date"], "%Y-%m-%d")
                    day = dt.strftime("%A")
                    high = round(row["temp_high"])
                    low = round(row["temp_low"])
                    wm = row.get("weather_main", "").lower()
                    cond = "thunderstorms" if "thunderstorm" in wm else (
                        "rain" if ("rain" in wm or "drizzle" in wm) else
                        row.get("description", wm))
                    rain = row.get("rain_chance", 0)
                    line = f"  {day}: High {high}, Low {low}, {cond}"
                    if rain > 0:
                        line += f" ({round(rain)}% precip)"
                    lines.append(line)
                return "\n".join(lines)

    # Live API fallback
    import requests
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": _get_weather_api_key(),
                    "units": "imperial"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        by_day = {}
        for item in data["list"][:32]:
            dt = datetime.fromtimestamp(item["dt"])
            day = dt.strftime("%A")
            w_main = item["weather"][0]["main"].lower()
            if day not in by_day:
                by_day[day] = {
                    "high": item["main"]["temp_max"],
                    "low": item["main"]["temp_min"],
                    "desc": item["weather"][0]["description"],
                    "rain": "rain" in w_main or "drizzle" in w_main,
                    "storm": "thunderstorm" in w_main,
                }
            else:
                by_day[day]["high"] = max(by_day[day]["high"],
                                          item["main"]["temp_max"])
                by_day[day]["low"] = min(by_day[day]["low"],
                                         item["main"]["temp_min"])
                if "rain" in w_main or "drizzle" in w_main:
                    by_day[day]["rain"] = True
                if "thunderstorm" in w_main:
                    by_day[day]["storm"] = True

        lines = [f"3-day forecast for {city}:"]
        for day, f in list(by_day.items())[:3]:
            high = round(f["high"])
            low = round(f["low"])
            cond = "thunderstorms" if f["storm"] else (
                "rain" if f["rain"] else f["desc"])
            lines.append(f"  {day}: High {high} degrees, Low {low} degrees, {cond}")
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Forecast API error: {e}")
        return f"Error fetching forecast for {city}: {e}"


def _weather_tomorrow(lat: float, lon: float, city: str, is_home: bool) -> str:
    """Tomorrow's weather — raw data for LLM synthesis."""
    # Try DB for home location
    if is_home:
        db = _get_db()
        if db:
            tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
            rows = db.get_forecast(days=5)
            row = next((r for r in rows if r["date"] == tomorrow_str), None)
            if row:
                high = round(row["temp_high"])
                low = round(row["temp_low"])
                wm = row.get("weather_main", "").lower()
                cond = "thunderstorms expected" if "thunderstorm" in wm else (
                    "rain expected" if ("rain" in wm or "drizzle" in wm) else
                    row.get("description", wm))
                return f"Tomorrow in {city}: High {high} degrees, Low {low} degrees, {cond}."

    # Live API fallback
    import requests
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": _get_weather_api_key(),
                    "units": "imperial"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        tomorrow_day = (datetime.now().day + 1)
        temps = []
        conditions = []
        for item in data["list"][:16]:
            dt = datetime.fromtimestamp(item["dt"])
            if dt.day == tomorrow_day:
                temps.append(item["main"]["temp"])
                conditions.append(item["weather"][0]["main"].lower())

        if not temps:
            return f"Tomorrow's forecast for {city} is not yet available."

        high = round(max(temps))
        low = round(min(temps))
        has_rain = any("rain" in c or "drizzle" in c for c in conditions)
        has_storm = any("thunderstorm" in c for c in conditions)

        cond = "thunderstorms expected" if has_storm else (
            "rain expected" if has_rain else
            data["list"][0]["weather"][0]["description"])

        return f"Tomorrow in {city}: High {high} degrees, Low {low} degrees, {cond}."

    except Exception as e:
        logger.error(f"Tomorrow weather error: {e}")
        return f"Error fetching tomorrow's weather for {city}: {e}"


def _weather_rain_check(lat: float, lon: float, city: str, is_home: bool) -> str:
    """Rain check for tomorrow — raw data for LLM synthesis."""
    # Try DB for home location
    if is_home:
        db = _get_db()
        if db:
            tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
            rows = db.get_forecast(days=5)
            row = next((r for r in rows if r["date"] == tomorrow_str), None)
            if row:
                wm = row.get("weather_main", "").lower()
                rain_chance = round(row.get("rain_chance", 0))
                has_storm = "thunderstorm" in wm
                has_rain = "rain" in wm or "drizzle" in wm
                if has_storm:
                    return (f"Rain check for {city}: Thunderstorms likely tomorrow, "
                            f"{rain_chance}% precipitation chance.")
                elif has_rain:
                    return (f"Rain check for {city}: Rain expected tomorrow, "
                            f"{rain_chance}% precipitation chance.")
                else:
                    return f"Rain check for {city}: No rain expected tomorrow."

    # Live API fallback
    import requests
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": _get_weather_api_key(),
                    "units": "imperial"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        tomorrow_day = (datetime.now().day + 1)
        will_rain = False
        rain_chance = 0
        has_storm = False

        for item in data["list"][:16]:
            dt = datetime.fromtimestamp(item["dt"])
            if dt.day == tomorrow_day:
                w_main = item["weather"][0]["main"].lower()
                if "thunderstorm" in w_main:
                    has_storm = True
                    will_rain = True
                elif "rain" in w_main or "drizzle" in w_main:
                    will_rain = True
                if "pop" in item:
                    rain_chance = max(rain_chance, item["pop"] * 100)

        if has_storm:
            return (f"Rain check for {city}: Thunderstorms likely tomorrow, "
                    f"{round(rain_chance)}% precipitation chance.")
        elif will_rain:
            return (f"Rain check for {city}: Rain expected tomorrow, "
                    f"{round(rain_chance)}% precipitation chance.")
        else:
            return f"Rain check for {city}: No rain expected tomorrow."

    except Exception as e:
        logger.error(f"Rain check error: {e}")
        return f"Error checking rain forecast for {city}: {e}"


def _weather_period(period_text: str) -> str:
    """Weather for a date range parsed from a temporal phrase — raw data for LLM synthesis."""
    from core.weather_db import parse_temporal_phrase

    period = parse_temporal_phrase(period_text) if period_text else None
    if not period:
        # Can't parse — fall back to 3-day forecast
        return _weather_forecast(_DEFAULT_LAT, _DEFAULT_LON, _DEFAULT_CITY, True)

    start_date, end_date = period
    today = date.today()
    max_forecast = today + timedelta(days=15)

    if start_date > max_forecast:
        return (f"That period is beyond the 16-day forecast window. "
                f"Forecast data is available through {max_forecast.strftime('%A, %B %d')}.")
    if end_date > max_forecast:
        end_date = max_forecast

    db = _get_db()
    if db:
        rows = db.get_forecast(days=16)
        filtered = [
            r for r in rows
            if start_date.isoformat() <= r["date"] <= end_date.isoformat()
        ]
        if filtered:
            lines = [f"Weather for {start_date.strftime('%A %b %d')} – {end_date.strftime('%A %b %d')}:"]
            for row in filtered:
                dt = datetime.strptime(row["date"], "%Y-%m-%d")
                day = dt.strftime("%A")
                high = round(row["temp_high"])
                low = round(row["temp_low"])
                wm = row.get("weather_main", "").lower()
                cond = "thunderstorms" if "thunderstorm" in wm else (
                    "rain" if ("rain" in wm or "drizzle" in wm) else
                    row.get("description", wm))
                rain = row.get("rain_chance", 0)
                line = f"  {day}: High {high}, Low {low}, {cond}"
                if rain and rain > 0:
                    line += f" ({round(rain)}% precip)"
                lines.append(line)
            return "\n".join(lines)

    # Fallback to generic forecast
    return _weather_forecast(_DEFAULT_LAT, _DEFAULT_LON, _DEFAULT_CITY, True)


def _weather_sun(which: str) -> str:
    """Sunrise or sunset time from DB."""
    db = _get_db()
    if db:
        today_str = date.today().isoformat()
        sun = db.get_sun_times(today_str)
        if sun:
            time_val = sun.get(which, "")
            if time_val:
                return f"{which.capitalize()} today: {time_val}."

    return f"{which.capitalize()} time is not available yet — data is still being populated."
