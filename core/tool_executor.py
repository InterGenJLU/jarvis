"""
Tool Executor — dispatches LLM tool calls to local handlers.

LLM-centric migration.  Each tool function takes the tool arguments dict
and returns a plain-text result string.  The LLM then synthesizes this
into a natural response via continue_after_tool_call().

Design principles:
    - NO dependency on skill instances, TTS, or BaseSkill
    - Pure data retrieval: read /proc, run safe commands, return text
    - The LLM formats the answer; these functions return raw data
    - Safety-classified shell execution for developer_tools
"""

import logging
import os
import platform
import shutil
import subprocess
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("jarvis.tool_executor")


def execute_tool(tool_name: str, arguments: dict) -> str:
    """Dispatch a tool call to the appropriate handler.

    Args:
        tool_name: The tool function name from the LLM's tool_call.
        arguments: The parsed arguments dict.

    Returns:
        Plain-text result string for the LLM to synthesize.
        On error, returns an error description (not an exception).
    """
    handler = _TOOL_HANDLERS.get(tool_name)
    if not handler:
        logger.warning(f"Unknown tool: {tool_name}")
        return f"Error: unknown tool '{tool_name}'"
    try:
        return handler(arguments)
    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}")
        return f"Error executing {tool_name}: {e}"


# ---------------------------------------------------------------------------
# get_time
# ---------------------------------------------------------------------------

def _handle_get_time(args: dict) -> str:
    """Return current local time and optionally the date."""
    now = datetime.now()

    # Time in 12-hour format
    hour = now.hour % 12 or 12
    minute = now.minute
    period = "AM" if now.hour < 12 else "PM"
    if minute == 0:
        time_str = f"{hour}:{minute:02d} {period}"
    else:
        time_str = f"{hour}:{minute:02d} {period}"

    include_date = args.get("include_date", False)
    if include_date:
        day_name = now.strftime("%A")
        month_name = now.strftime("%B")
        day = now.day
        year = now.year
        return f"Current time: {time_str}. Date: {day_name}, {month_name} {day}, {year}."
    return f"Current time: {time_str}."


# ---------------------------------------------------------------------------
# get_system_info
# ---------------------------------------------------------------------------

_SYSTEM_INFO_HANDLERS = {}


def _register_sysinfo(category: str):
    """Decorator to register a system info category handler."""
    def decorator(fn):
        _SYSTEM_INFO_HANDLERS[category] = fn
        return fn
    return decorator


def _handle_get_system_info(args: dict) -> str:
    """Route to the appropriate system info sub-handler."""
    category = args.get("category", "")
    handler = _SYSTEM_INFO_HANDLERS.get(category)
    if not handler:
        available = ", ".join(sorted(_SYSTEM_INFO_HANDLERS.keys()))
        return f"Unknown category '{category}'. Available: {available}"
    return handler()


@_register_sysinfo("cpu")
def _sysinfo_cpu() -> str:
    with open("/proc/cpuinfo", "r") as f:
        cpuinfo = f.read()
    model = None
    for line in cpuinfo.split("\n"):
        if "model name" in line:
            model = line.split(":")[1].strip()
            model = model.replace("(R)", "").replace("(TM)", "").replace("  ", " ")
            break
    core_count = subprocess.check_output(["nproc"], text=True).strip()
    if model:
        return f"CPU: {model}, {core_count} cores."
    return f"CPU: {core_count} cores (model unknown)."


@_register_sysinfo("memory")
def _sysinfo_memory() -> str:
    with open("/proc/meminfo", "r") as f:
        meminfo = f.read()
    total_kb = 0
    for line in meminfo.split("\n"):
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
            break
    total_gb = total_kb / (1024 * 1024)

    result = subprocess.run(["free", "-m"], capture_output=True, text=True)
    if result.returncode == 0:
        lines = result.stdout.strip().split("\n")
        if len(lines) > 1:
            parts = lines[1].split()
            if len(parts) >= 3:
                used_mb = int(parts[2])
                used_gb = used_mb / 1024
                pct = (used_gb / total_gb) * 100
                return (
                    f"RAM: {total_gb:.1f} GB total, {used_gb:.1f} GB used "
                    f"({pct:.0f}% utilization)."
                )
    return f"RAM: {total_gb:.1f} GB total."


@_register_sysinfo("disk")
def _sysinfo_disk() -> str:
    usage = shutil.disk_usage("/")
    total_gb = usage.total / (1024**3)
    used_gb = usage.used / (1024**3)
    free_gb = usage.free / (1024**3)
    pct = (usage.used / usage.total) * 100
    return (
        f"Root partition: {used_gb:.1f} GB used / {total_gb:.1f} GB total "
        f"({free_gb:.1f} GB free, {pct:.0f}% used)."
    )


@_register_sysinfo("gpu")
def _sysinfo_gpu() -> str:
    # Try nvidia-smi
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"GPU: NVIDIA {result.stdout.strip()}."
    except FileNotFoundError:
        pass

    # Fall back to lspci
    result = subprocess.run(["lspci"], capture_output=True, text=True)
    if result.returncode == 0:
        for line in result.stdout.split("\n"):
            if "VGA" in line or "Display" in line or "3D" in line:
                if ":" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        info = parts[2].strip()
                        info = info.replace("[AMD/ATI]", "AMD").replace("[NVIDIA]", "NVIDIA")
                        return f"GPU: {info}."
    return "GPU: unable to detect."


@_register_sysinfo("uptime")
def _sysinfo_uptime() -> str:
    with open("/proc/uptime", "r") as f:
        uptime_seconds = float(f.readline().split()[0])
    delta = timedelta(seconds=int(uptime_seconds))
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return f"Uptime: {', '.join(parts) if parts else 'less than a minute'}."


@_register_sysinfo("hostname")
def _sysinfo_hostname() -> str:
    return f"Hostname: {platform.node()}."


@_register_sysinfo("username")
def _sysinfo_username() -> str:
    username = os.getenv("USER") or os.getenv("USERNAME") or "unknown"
    return f"Username: {username}."


@_register_sysinfo("all_drives")
def _sysinfo_all_drives() -> str:
    result = subprocess.run(
        ["lsblk", "-d", "-o", "NAME,SIZE,MODEL,TYPE", "-n"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "Error listing drives."
    drives = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split(None, 3)
        if len(parts) >= 3 and "disk" in line and not parts[0].startswith("loop"):
            name, size = parts[0], parts[1]
            model = parts[2].strip() if len(parts) > 2 else "Unknown"
            # Model might include "disk" from the TYPE column
            if model == "disk":
                model = "Unknown"
            drives.append(f"{name}: {size} {model}")
    if not drives:
        return "No drives detected."
    return "Drives:\n" + "\n".join(f"  - {d}" for d in drives)


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------

def _handle_find_files(args: dict) -> str:
    """Search for files, count files in a directory, or count code lines."""
    action = args.get("action", "search")
    if action == "count_code":
        return _find_count_code()
    elif action == "count_files":
        return _find_count_files(args.get("directory", "home"))
    elif action == "search":
        pattern = args.get("pattern", "")
        if not pattern:
            return "Error: 'pattern' is required for file search."
        return _find_search(pattern)
    return f"Unknown find_files action: {action}"


def _find_search(pattern: str) -> str:
    """Search for files matching a name pattern."""
    search_paths = [
        str(Path.home()),
        str(Path.home() / "Documents"),
        str(Path.home() / "Downloads"),
        str(Path.home() / "Desktop"),
    ]
    all_matches = []
    for search_path in search_paths:
        if not Path(search_path).exists():
            continue
        try:
            result = subprocess.run(
                ["find", search_path, "-name", f"*{pattern}*", "-type", "f",
                 "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
                 "-not", "-path", "*/venv/*", "-not", "-path", "*/.cache/*"],
                capture_output=True, text=True, timeout=10,
            )
            matches = [f for f in result.stdout.strip().split("\n") if f]
            all_matches.extend(matches)
        except subprocess.TimeoutExpired:
            continue
        if all_matches:
            break  # Stop after first path with results

    if not all_matches:
        return f"No files found matching '{pattern}'."
    # Deduplicate and limit
    seen = set()
    unique = []
    for m in all_matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    if len(unique) == 1:
        return f"Found: {unique[0]}"
    display = unique[:10]
    result_text = f"Found {len(unique)} files matching '{pattern}':\n"
    result_text += "\n".join(f"  - {f}" for f in display)
    if len(unique) > 10:
        result_text += f"\n  ... and {len(unique) - 10} more"
    return result_text


def _find_count_files(directory: str) -> str:
    """Count files in a named directory."""
    dir_map = {
        "documents": Path.home() / "Documents",
        "downloads": Path.home() / "Downloads",
        "desktop": Path.home() / "Desktop",
        "home": Path.home(),
        "pictures": Path.home() / "Pictures",
        "videos": Path.home() / "Videos",
        "music": Path.home() / "Music",
        "scripts": Path.home() / "scripts",
        "jarvis": Path.home() / "jarvis",
        "core": Path.home() / "jarvis" / "core",
        "skills": Path("/mnt/storage/jarvis/skills"),
        "models": Path("/mnt/models"),
    }
    target = dir_map.get(directory.lower())
    if not target:
        # Try as literal path
        target = Path(directory).expanduser()
    if not target.exists():
        return f"Directory '{directory}' does not exist."
    try:
        file_count = sum(1 for item in target.iterdir() if item.is_file())
        dir_count = sum(1 for item in target.iterdir() if item.is_dir())
    except PermissionError:
        return f"Permission denied accessing '{directory}'."
    if file_count == 0 and dir_count == 0:
        return f"'{directory}' is empty."
    parts = []
    if file_count:
        parts.append(f"{file_count:,} files")
    if dir_count:
        parts.append(f"{dir_count:,} folders")
    return f"'{directory}' contains {' and '.join(parts)}."


def _find_count_code() -> str:
    """Count lines of Python code in the JARVIS codebase."""
    jarvis_path = Path.home() / "jarvis"
    if not jarvis_path.exists():
        return "JARVIS codebase not found."
    result = subprocess.run(
        ["find", str(jarvis_path), "-name", "*.py", "-type", "f",
         "-not", "-path", "*/venv*", "-not", "-path", "*/__pycache__/*"],
        capture_output=True, text=True,
    )
    py_files = [f for f in result.stdout.strip().split("\n") if f]
    if not py_files:
        return "No Python files found."
    total_lines = 0
    for py_file in py_files:
        try:
            with open(py_file, "r") as f:
                total_lines += sum(1 for _ in f)
        except Exception:
            continue
    return f"Codebase: {total_lines:,} lines of Python across {len(py_files)} files."


# ---------------------------------------------------------------------------
# get_weather
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


def _handle_get_weather(args: dict) -> str:
    """Route to the appropriate weather sub-handler."""
    query_type = args.get("query_type", "current")
    location = args.get("location")
    lat, lon, city = _resolve_location(location)

    if query_type == "forecast":
        return _weather_forecast(lat, lon, city)
    elif query_type == "tomorrow":
        return _weather_tomorrow(lat, lon, city)
    elif query_type == "rain_check":
        return _weather_rain_check(lat, lon, city)
    # Default: current
    return _weather_current(lat, lon, city)


def _weather_current(lat: float, lon: float, city: str) -> str:
    """Current weather conditions — raw data for LLM synthesis."""
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


def _weather_forecast(lat: float, lon: float, city: str) -> str:
    """3-day forecast — raw data for LLM synthesis."""
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

        # Aggregate by day
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


def _weather_tomorrow(lat: float, lon: float, city: str) -> str:
    """Tomorrow's weather — raw data for LLM synthesis."""
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

        return (f"Tomorrow in {city}: High {high} degrees, Low {low} degrees, {cond}.")

    except Exception as e:
        logger.error(f"Tomorrow weather error: {e}")
        return f"Error fetching tomorrow's weather for {city}: {e}"


def _weather_rain_check(lat: float, lon: float, city: str) -> str:
    """Rain check for tomorrow — raw data for LLM synthesis."""
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


# ---------------------------------------------------------------------------
# manage_reminders
# ---------------------------------------------------------------------------

_reminder_manager = None


def set_reminder_manager(mgr):
    """Wire the reminder manager singleton for tool dispatch.

    Called during init in pipeline.py, jarvis_console.py, jarvis_web.py.
    """
    global _reminder_manager
    _reminder_manager = mgr


def _handle_manage_reminders(args: dict) -> str:
    """Dispatch reminder actions to sub-handlers."""
    if not _reminder_manager:
        return "Error: reminder system not initialized."

    action = args.get("action", "")
    if action == "add":
        return _reminders_add(_reminder_manager, args)
    elif action == "list":
        return _reminders_list(_reminder_manager)
    elif action == "cancel":
        return _reminders_cancel(_reminder_manager, args)
    elif action == "acknowledge":
        return _reminders_acknowledge(_reminder_manager)
    elif action == "snooze":
        return _reminders_snooze(_reminder_manager, args)
    else:
        return f"Error: unknown reminder action '{action}'."


def _reminders_add(mgr, args: dict) -> str:
    """Add a new one-time reminder."""
    title = args.get("title", "").strip()
    time_text = args.get("time_text", "").strip()

    if not title:
        return "Error: title is required to set a reminder."
    if not time_text:
        return "Error: time_text is required (e.g. 'tomorrow at 6 PM')."

    priority_str = args.get("priority", "normal").lower()
    priority_map = {"urgent": 1, "high": 2, "normal": 3}
    priority = priority_map.get(priority_str, 3)

    # Use the manager's existing natural-time parser
    from core.reminder_manager import ReminderManager
    reminder_time = ReminderManager.parse_natural_time(time_text)
    if not reminder_time:
        return (f"Error: couldn't parse time '{time_text}'. "
                "Try formats like 'tomorrow at 6 PM' or 'in 30 minutes'.")

    rid = mgr.add_reminder(
        title=title,
        reminder_time=reminder_time,
        priority=priority,
    )

    time_desc = _format_reminder_time(reminder_time)
    priority_note = " (marked urgent)" if priority <= 2 else ""
    return f"Reminder #{rid} set: '{title}' {time_desc}{priority_note}."


def _reminders_list(mgr) -> str:
    """List upcoming and fired reminders."""
    pending = mgr.list_reminders("pending", limit=10)
    fired = mgr.list_reminders("fired", limit=5)
    all_reminders = fired + pending

    if not all_reminders:
        return "No upcoming reminders."

    lines = []
    for r in all_reminders:
        try:
            rt = datetime.strptime(r["reminder_time"], "%Y-%m-%d %H:%M:%S")
            time_desc = _format_reminder_time(rt)
        except (ValueError, KeyError):
            time_desc = r.get("reminder_time", "unknown time")
        status_note = " [awaiting acknowledgment]" if r["status"] == "fired" else ""
        lines.append(f"- {r['title']}{status_note}, {time_desc}")

    count = len(all_reminders)
    header = f"{count} reminder{'s' if count != 1 else ''}:"
    return header + "\n" + "\n".join(lines)


def _reminders_cancel(mgr, args: dict) -> str:
    """Cancel a reminder by title fragment."""
    fragment = args.get("cancel_fragment", "").strip()
    if not fragment:
        return "Error: cancel_fragment is required (e.g. 'dentist')."

    cancelled = mgr.cancel_by_title(fragment)
    if cancelled:
        return f"Cancelled: '{cancelled['title']}'."
    return f"No reminder found matching '{fragment}'."


def _reminders_acknowledge(mgr) -> str:
    """Acknowledge the last-fired reminder."""
    if not mgr.is_awaiting_ack():
        return "No reminders currently awaiting acknowledgment."

    reminder = mgr.acknowledge_last()
    if reminder:
        return f"Acknowledged: '{reminder['title']}' marked as done."
    return "Error acknowledging reminder."


def _reminders_snooze(mgr, args: dict) -> str:
    """Snooze the last-fired reminder."""
    if not mgr.is_awaiting_ack():
        return "No reminder to snooze at the moment."

    minutes = args.get("snooze_minutes")
    reminder = mgr.snooze_last(minutes)
    if reminder:
        snooze_min = minutes or mgr.default_snooze
        return f"Snoozed '{reminder['title']}' for {snooze_min} minutes."
    return "Error snoozing reminder."


def _format_reminder_time(dt) -> str:
    """Format a reminder datetime for LLM context (relative when possible)."""
    now = datetime.now()
    diff = dt - now

    total_seconds = diff.total_seconds()
    if total_seconds < 0:
        return f"at {dt.strftime('%-I:%M %p')} (past)"
    elif total_seconds < 90:
        return "in about a minute"
    elif total_seconds < 3600:
        minutes = int(total_seconds / 60)
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    elif total_seconds < 7200:
        return "in about an hour"

    if dt.date() == now.date():
        return f"today at {dt.strftime('%-I:%M %p')}"
    elif (dt.date() - now.date()).days == 1:
        return f"tomorrow at {dt.strftime('%-I:%M %p')}"
    elif (dt.date() - now.date()).days < 7:
        return f"{dt.strftime('%A')} at {dt.strftime('%-I:%M %p')}"
    else:
        return f"on {dt.strftime('%B %-d')} at {dt.strftime('%-I:%M %p')}"


# ---------------------------------------------------------------------------
# developer_tools
# ---------------------------------------------------------------------------

_config = None


def set_config(config):
    """Wire the config object for tool dispatch (health check, etc.).

    Called during init in pipeline.py, jarvis_console.py, jarvis_web.py.
    """
    global _config
    _config = config


# Lazy-loaded safety module (from developer_tools skill)
_safety_module = None


def _get_safety():
    """Lazy-load _safety.py from the developer_tools skill directory."""
    global _safety_module
    if _safety_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_safety',
            '/mnt/storage/jarvis/skills/system/developer_tools/_safety.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _safety_module = mod
    return _safety_module


# Git repo paths
_GIT_REPOS = {
    'main': '/home/user/jarvis',
    'skills': '/mnt/storage/jarvis/skills',
    'models': '/mnt/models',
}


def _resolve_repos(repo: str) -> dict:
    """Return dict of repo_name→path for the requested repo(s)."""
    if repo == 'all' or not repo:
        return dict(_GIT_REPOS)
    if repo in _GIT_REPOS:
        return {repo: _GIT_REPOS[repo]}
    return dict(_GIT_REPOS)


def _run_cmd(cmd: str, cwd: str = None, timeout: int = 15) -> str:
    """Run a shell command and return stdout (or error message)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            output += f"\n{result.stderr.strip()}" if output else result.stderr.strip()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# Pending confirmation state for run_command → confirm_pending flow
_pending_command = None  # (command_str, expiry_time) or None


_DEVTOOLS_HANDLERS = {}


def _register_devtool(action: str):
    """Decorator to register a developer_tools action handler."""
    def decorator(fn):
        _DEVTOOLS_HANDLERS[action] = fn
        return fn
    return decorator


def _handle_developer_tools(args: dict) -> str:
    """Route to the appropriate developer_tools action handler."""
    action = args.get("action", "")
    handler = _DEVTOOLS_HANDLERS.get(action)
    if not handler:
        available = ", ".join(sorted(_DEVTOOLS_HANDLERS.keys()))
        return f"Unknown developer_tools action '{action}'. Available: {available}"
    return handler(args)


@_register_devtool("git_status")
def _devtools_git_status(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    lines = []
    for name, path in repos.items():
        output = _run_cmd("git status --short", cwd=path)
        if output == "(no output)":
            output = "clean"
        lines.append(f"[{name}] ({path}):\n{output}")
    return "\n\n".join(lines)


@_register_devtool("git_log")
def _devtools_git_log(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    count = max(1, min(50, int(args.get("count", 10))))
    lines = []
    for name, path in repos.items():
        output = _run_cmd(f"git log --oneline --decorate -{count}", cwd=path)
        lines.append(f"[{name}]:\n{output}")
    return "\n\n".join(lines)


@_register_devtool("git_diff")
def _devtools_git_diff(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    lines = []
    for name, path in repos.items():
        output = _run_cmd("git diff", cwd=path)
        if output == "(no output)":
            output = "no changes"
        lines.append(f"[{name}]:\n{output}")
    return "\n\n".join(lines)


@_register_devtool("git_branch")
def _devtools_git_branch(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    lines = []
    for name, path in repos.items():
        output = _run_cmd("git branch -a", cwd=path)
        lines.append(f"[{name}]:\n{output}")
    return "\n\n".join(lines)


@_register_devtool("codebase_search")
def _devtools_codebase_search(args: dict) -> str:
    pattern = args.get("pattern", "").strip()
    if not pattern:
        return "Error: 'pattern' is required for codebase search."
    search_dirs = [
        '/home/user/jarvis/core',
        '/mnt/storage/jarvis/skills',
    ]
    all_matches = []
    for d in search_dirs:
        output = _run_cmd(
            f"grep -rn --include='*.py' "
            f"--exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=venv "
            f"-- {subprocess.list2cmdline([pattern])} {d}",
            timeout=10,
        )
        if output and output != "(no output)" and not output.startswith("Error"):
            all_matches.extend(output.split('\n'))
    if not all_matches:
        return f"No matches found for '{pattern}'."
    if len(all_matches) > 30:
        truncated = all_matches[:30]
        truncated.append(f"... ({len(all_matches) - 30} more matches)")
        return "\n".join(truncated)
    return "\n".join(all_matches)


@_register_devtool("process_info")
def _devtools_process_info(args: dict) -> str:
    sort_by = args.get("sort_by", "cpu")
    sort_key = "-%mem" if sort_by == "memory" else "-%cpu"
    return _run_cmd(f"ps aux --sort={sort_key} | head -15")


@_register_devtool("service_status")
def _devtools_service_status(args: dict) -> str:
    service_name = args.get("service_name", "").strip()
    if service_name:
        # Try user service first, then system
        output = _run_cmd(f"systemctl --user status {subprocess.list2cmdline([service_name])}")
        if "could not be found" in output.lower() or "not loaded" in output.lower():
            output = _run_cmd(f"systemctl status {subprocess.list2cmdline([service_name])}")
        return output
    # List JARVIS-related services
    lines = ["User services:"]
    for svc in ["jarvis", "jarvis-web", "llama-server"]:
        status = _run_cmd(f"systemctl --user is-active {svc} 2>/dev/null")
        lines.append(f"  {svc}: {status}")
    lines.append("\nSystem services:")
    status = _run_cmd("systemctl is-active llama-server 2>/dev/null")
    lines.append(f"  llama-server: {status}")
    return "\n".join(lines)


@_register_devtool("network_info")
def _devtools_network_info(args: dict) -> str:
    info_type = args.get("info_type", "addresses")
    target = args.get("target", "")
    if info_type == "ports":
        return _run_cmd("ss -tlnp")
    elif info_type == "ping" and target:
        return _run_cmd(f"ping -c 4 {subprocess.list2cmdline([target])}", timeout=10)
    elif info_type == "interfaces":
        return _run_cmd("ip link show")
    # Default: addresses
    return _run_cmd("ip -brief addr show")


@_register_devtool("package_info")
def _devtools_package_info(args: dict) -> str:
    package_name = args.get("package_name", "").strip()
    if package_name:
        lines = []
        which = _run_cmd(f"which {subprocess.list2cmdline([package_name])}")
        if which and not which.startswith("Error") and which != "(no output)":
            lines.append(f"Location: {which}")
        version = _run_cmd(f"{subprocess.list2cmdline([package_name])} --version 2>&1 | head -1")
        if version and not version.startswith("Error") and version != "(no output)":
            lines.append(f"Version: {version}")
        pip_info = _run_cmd(f"pip show {subprocess.list2cmdline([package_name])} 2>/dev/null")
        if pip_info and not pip_info.startswith("Error") and pip_info != "(no output)":
            lines.append(f"Pip info:\n{pip_info}")
        return "\n".join(lines) if lines else f"Package '{package_name}' not found."
    # General info
    return _run_cmd("python3 --version && pip --version")


@_register_devtool("system_health")
def _devtools_system_health(args: dict) -> str:
    if not _config:
        return "Error: config not initialized. Cannot run health check."
    try:
        from core.health_check import get_full_health
        results = get_full_health(_config)
        lines = []
        for layer_name, checks in results.items():
            lines.append(f"=== {layer_name.upper()} ===")
            for check in checks:
                status = check.get('status', 'unknown')
                name = check.get('name', 'unknown')
                detail = check.get('detail', '')
                icon = {'green': 'OK', 'yellow': 'WARN', 'red': 'FAIL'}.get(status, '??')
                lines.append(f"  [{icon}] {name}: {detail}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error running health check: {e}"


@_register_devtool("check_logs")
def _devtools_check_logs(args: dict) -> str:
    log_filter = args.get("filter", "recent")
    minutes = max(1, min(1440, int(args.get("minutes", 15))))
    cmd = f'journalctl --user -u jarvis --since "{minutes} min ago" --no-pager'
    if log_filter == "errors":
        cmd += " | grep -i error"
    elif log_filter == "warnings":
        cmd += " | grep -iE '(warn|error)'"
    output = _run_cmd(cmd, timeout=10)
    safety = _get_safety()
    return safety.sanitize_output(output)


@_register_devtool("run_command")
def _devtools_run_command(args: dict) -> str:
    global _pending_command
    command = args.get("command", "").strip()
    if not command:
        return "Error: 'command' is required."
    safety = _get_safety()
    tier, reason = safety.classify_command(command)
    if tier == 'blocked':
        return f"BLOCKED: {reason}. This command is not allowed."
    if tier == 'confirmation':
        _pending_command = (command, _time.time() + 30)
        return f"CONFIRMATION REQUIRED: `{command}` — {reason}. Shall I proceed?"
    # Tier 1 (allowed) or Tier 2 (safe_write) — execute
    output = _run_cmd(command, cwd='/home/user/jarvis', timeout=30)
    return safety.sanitize_output(output)


@_register_devtool("confirm_pending")
def _devtools_confirm_pending(args: dict) -> str:
    global _pending_command
    if _pending_command is None:
        return "No pending command to confirm."
    command, expiry = _pending_command
    if _time.time() > expiry:
        _pending_command = None
        return "That confirmation has expired. Please issue the command again."
    _pending_command = None
    safety = _get_safety()
    output = _run_cmd(command, cwd='/home/user/jarvis', timeout=30)
    return safety.sanitize_output(output)


# ---------------------------------------------------------------------------
# get_news
# ---------------------------------------------------------------------------

def _handle_get_news(args: dict) -> str:
    """Read or count RSS news headlines from the local feed monitor."""
    from core.news_manager import get_news_manager
    mgr = get_news_manager()
    if not mgr:
        return "News system is not available."
    action = args.get("action", "read")
    category = args.get("category")
    max_priority = args.get("max_priority")
    if action == "count":
        return mgr.get_headline_count_response()
    return mgr.read_headlines(category=category, limit=5, max_priority=max_priority)


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "get_time": _handle_get_time,
    "get_system_info": _handle_get_system_info,
    "find_files": _handle_find_files,
    "get_weather": _handle_get_weather,
    "manage_reminders": _handle_manage_reminders,
    "developer_tools": _handle_developer_tools,
    "get_news": _handle_get_news,
}
