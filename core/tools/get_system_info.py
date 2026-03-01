"""Tool definition: get_system_info — local hardware and OS information."""

import os
import platform
import shutil
import subprocess
from datetime import timedelta

TOOL_NAME = "get_system_info"
SKILL_NAME = "system_info"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_system_info",
        "description": (
            "Get information about THIS computer's hardware or OS. "
            "Use for questions about the local machine's CPU, RAM, GPU, "
            "disk space, drives, uptime, hostname, or username."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["cpu", "memory", "disk", "gpu", "uptime",
                             "hostname", "username", "all_drives"],
                    "description": "Which system info to retrieve"
                }
            },
            "required": ["category"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For questions about THIS COMPUTER's hardware or OS (CPU, RAM, GPU, "
    "disk, uptime, hostname), call get_system_info. "
    "Examples: 'how much RAM do I have?' → memory, 'disk space?' → disk, "
    "'what GPU?' → gpu. "
    "NOT for: general tech questions, hardware shopping advice, other people's computers."
)


# ---------------------------------------------------------------------------
# Sub-handler registry
# ---------------------------------------------------------------------------

_SYSTEM_INFO_HANDLERS = {}


def _register_sysinfo(category: str):
    """Decorator to register a system info category handler."""
    def decorator(fn):
        _SYSTEM_INFO_HANDLERS[category] = fn
        return fn
    return decorator


def handler(args: dict) -> str:
    """Route to the appropriate system info sub-handler."""
    category = args.get("category", "")
    sub_handler = _SYSTEM_INFO_HANDLERS.get(category)
    if not sub_handler:
        available = ", ".join(sorted(_SYSTEM_INFO_HANDLERS.keys()))
        return f"Unknown category '{category}'. Available: {available}"
    return sub_handler()


# ---------------------------------------------------------------------------
# Category handlers
# ---------------------------------------------------------------------------

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
