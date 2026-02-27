"""
Tool Executor — dispatches LLM tool calls to local handlers.

Phase 1 of the LLM-centric migration.  Each tool function takes the
tool arguments dict and returns a plain-text result string.  The LLM
then synthesizes this into a natural response via continue_after_tool_call().

Design principles:
    - NO dependency on skill instances, TTS, or BaseSkill
    - Pure data retrieval: read /proc, run safe commands, return text
    - The LLM formats the answer; these functions return raw data
    - Whitelisted commands only — no arbitrary shell execution
"""

import logging
import os
import platform
import shutil
import subprocess
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
# Handler registry
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "get_time": _handle_get_time,
    "get_system_info": _handle_get_system_info,
    "find_files": _handle_find_files,
}
