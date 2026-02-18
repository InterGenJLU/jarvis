"""
JARVIS System Health Check — 5-layer diagnostic module.

Layers:
    1. Bare Metal   — CPU, GPU, RAM, disks, network
    2. Services     — jarvis.service, llama-server, recent errors
    3. Internals    — skills, STT, TTS, pipeline, semantic matcher
    4. Data Stores  — databases, chat history, FAISS index
    5. Self-Assessment — uptime, session stats, mood

Usage:
    from core.health_check import register_coordinator, get_full_health
    from core.health_check import format_voice_brief, format_visual_report

    register_coordinator(coordinator)   # once after init
    health = get_full_health(config)
    print(format_visual_report(health))
    tts.speak(format_voice_brief(health))
"""

import os
import re
import time
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import psutil

from core.logger import get_logger

# ---------------------------------------------------------------------------
# Module-level coordinator reference
# ---------------------------------------------------------------------------

_coordinator_ref = None
_logger = None


def register_coordinator(coordinator):
    """Register the Coordinator for internal state checks."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _get_logger(config=None):
    global _logger
    if _logger is None:
        _logger = get_logger("health_check", config)
    return _logger


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _check(name, status, summary, detail=""):
    """Build a check result dict."""
    return {"name": name, "status": status, "summary": summary, "detail": detail}


def _run(command, timeout=10):
    """Run a shell command, return (success, stdout)."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return (result.returncode == 0, result.stdout.strip())
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    except Exception as e:
        return (False, str(e))


# ---------------------------------------------------------------------------
# Layer 1 — Bare Metal
# ---------------------------------------------------------------------------

def check_bare_metal(config=None):
    """CPU, GPU, RAM, disks, network, audio input."""
    results = []

    # --- CPU ---
    try:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count()
        # CPU temp from sensors (Ryzen k10temp sensor)
        ok, sensors_out = _run("sensors 2>/dev/null | grep 'Tctl'")
        cpu_temp = None
        if ok and sensors_out:
            m = re.search(r'\+(\d+\.?\d*)°C', sensors_out)
            if m:
                cpu_temp = float(m.group(1))

        temp_str = f"{cpu_temp:.0f} C" if cpu_temp else "unknown"
        status = "green"
        if cpu_temp and cpu_temp > 90:
            status = "red"
        elif cpu_temp and cpu_temp > 80:
            status = "yellow"
        elif cpu_percent > 90:
            status = "yellow"

        results.append(_check(
            "CPU", status,
            f"Ryzen 9 5900X — {temp_str}, {cpu_percent:.0f}% load, {cpu_count} cores",
            f"temp={temp_str} load={cpu_percent}% cores={cpu_count}",
        ))
    except Exception as e:
        results.append(_check("CPU", "red", f"Error: {e}"))

    # --- GPU ---
    try:
        ok, smi_out = _run("rocm-smi 2>/dev/null")
        if ok and smi_out:
            # Parse the data line (line after the header with DID, GUID)
            for line in smi_out.splitlines():
                if line.strip().startswith("0"):
                    parts = line.split()
                    # Format: Device Node DID GUID Temp Power ... VRAM% GPU%
                    gpu_temp_str = next((p for p in parts if "°C" in p), None)
                    gpu_temp = float(gpu_temp_str.replace("°C", "")) if gpu_temp_str else None
                    gpu_vram = next((p for p in parts if p.endswith("%") and parts.index(p) >= len(parts) - 2), None)
                    gpu_util = parts[-1] if parts[-1].endswith("%") else None
                    gpu_vram_pct = parts[-2] if len(parts) >= 2 and parts[-2].endswith("%") else None

                    status = "green"
                    if gpu_temp and gpu_temp > 95:
                        status = "red"
                    elif gpu_temp and gpu_temp > 85:
                        status = "yellow"

                    temp_disp = f"{gpu_temp:.0f} C" if gpu_temp else "unknown"
                    results.append(_check(
                        "GPU", status,
                        f"RX 7900 XT — {temp_disp}, {gpu_util or '?'} util, {gpu_vram_pct or '?'} VRAM",
                        f"temp={temp_disp} util={gpu_util} vram={gpu_vram_pct}",
                    ))
                    break
            else:
                results.append(_check("GPU", "yellow", "rocm-smi returned no device data"))
        else:
            results.append(_check("GPU", "red", "rocm-smi not available or failed"))
    except Exception as e:
        results.append(_check("GPU", "red", f"GPU error: {e}"))

    # --- RAM ---
    try:
        mem = psutil.virtual_memory()
        used_gb = mem.used / (1024 ** 3)
        total_gb = mem.total / (1024 ** 3)
        swap = psutil.swap_memory()
        status = "green"
        if mem.percent > 95:
            status = "red"
        elif mem.percent > 85:
            status = "yellow"

        swap_str = f", swap {swap.percent:.0f}%" if swap.total > 0 else ""
        results.append(_check(
            "RAM", status,
            f"{used_gb:.1f} / {total_gb:.1f} GB ({mem.percent:.0f}%){swap_str}",
            f"used={used_gb:.1f}GB total={total_gb:.1f}GB percent={mem.percent}%",
        ))
    except Exception as e:
        results.append(_check("RAM", "red", f"RAM error: {e}"))

    # --- Disks ---
    disk_mounts = {
        "/": "Root",
        "/mnt/storage": "Storage",
        "/mnt/models": "Models",
    }
    for mount, label in disk_mounts.items():
        try:
            usage = psutil.disk_usage(mount)
            pct = usage.percent
            free_gb = usage.free / (1024 ** 3)
            status = "green"
            if pct > 90:
                status = "red"
            elif pct > 80:
                status = "yellow"
            results.append(_check(
                f"Disk ({label})", status,
                f"{mount} — {pct:.0f}% used, {free_gb:.0f} GB free",
                f"mount={mount} percent={pct}% free={free_gb:.0f}GB",
            ))
        except FileNotFoundError:
            results.append(_check(f"Disk ({label})", "red", f"{mount} — not mounted"))
        except Exception as e:
            results.append(_check(f"Disk ({label})", "red", f"{mount} — {e}"))

    # --- Network ---
    try:
        ok, _ = _run("ping -c 1 -W 2 8.8.8.8", timeout=5)
        addrs = psutil.net_if_addrs()
        local_ip = None
        for iface, addr_list in addrs.items():
            if iface == "lo":
                continue
            for addr in addr_list:
                if addr.family.name == "AF_INET" and not addr.address.startswith("127."):
                    local_ip = addr.address
                    break
            if local_ip:
                break

        if ok:
            results.append(_check(
                "Network", "green",
                f"Connected ({local_ip or 'IP unknown'})",
                f"ip={local_ip} internet=ok",
            ))
        else:
            results.append(_check(
                "Network", "yellow",
                f"No internet ({local_ip or 'no IP'})",
                f"ip={local_ip} internet=failed",
            ))
    except Exception as e:
        results.append(_check("Network", "red", f"Network error: {e}"))

    # --- Audio Input ---
    try:
        import sounddevice as _sd
        devices = _sd.query_devices()
        mic_name = config.get("audio.mic_device", "") if config else ""
        found_configured = False
        any_input = False

        for dev in devices:
            has_input = dev.get('max_input_channels', 0) > 0
            if has_input:
                any_input = True
            if mic_name and mic_name in dev.get('name', '') and has_input:
                found_configured = True
                results.append(_check(
                    "Audio Input", "green",
                    f"{dev['name']}",
                    f"device={dev['name']} configured=yes",
                ))
                break

        if not found_configured:
            if any_input:
                results.append(_check(
                    "Audio Input", "yellow",
                    f"'{mic_name}' not found — fallback device available",
                    f"configured_missing={mic_name} fallback=yes",
                ))
            else:
                results.append(_check(
                    "Audio Input", "red",
                    "No audio input device detected",
                    "no_input_device",
                ))
    except Exception as e:
        results.append(_check("Audio Input", "yellow", f"Could not check: {e}"))

    return results


# ---------------------------------------------------------------------------
# Layer 2 — Services & Processes
# ---------------------------------------------------------------------------

def check_services():
    """JARVIS service, llama-server, recent errors."""
    results = []

    # --- jarvis.service ---
    try:
        ok, active_out = _run("systemctl --user is-active jarvis 2>/dev/null")
        is_active = active_out.strip() == "active"

        uptime_str = "unknown"
        if is_active:
            ok2, prop_out = _run(
                "systemctl --user show jarvis --property=ActiveEnterTimestampMonotonic 2>/dev/null"
            )
            if ok2 and "=" in prop_out:
                # ActiveEnterTimestampMonotonic is in microseconds
                mono_str = prop_out.split("=", 1)[1].strip()
                if mono_str and mono_str.isdigit():
                    try:
                        # Compare with current monotonic time
                        import ctypes
                        CLOCK_MONOTONIC = 1
                        class Timespec(ctypes.Structure):
                            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]
                        librt = ctypes.CDLL("librt.so.1", use_errno=True)
                        ts = Timespec()
                        librt.clock_gettime(CLOCK_MONOTONIC, ctypes.byref(ts))
                        now_us = ts.tv_sec * 1_000_000 + ts.tv_nsec // 1_000
                        start_us = int(mono_str)
                        delta_secs = (now_us - start_us) / 1_000_000
                        hours = int(delta_secs // 3600)
                        minutes = int((delta_secs % 3600) // 60)
                        if hours > 0:
                            uptime_str = f"{hours}h {minutes}m"
                        else:
                            uptime_str = f"{minutes}m"
                    except Exception:
                        uptime_str = "unknown"

        if is_active:
            results.append(_check(
                "jarvis.service", "green",
                f"Active (uptime: {uptime_str})",
            ))
        else:
            results.append(_check(
                "jarvis.service", "red",
                f"Not active ({active_out.strip()})",
            ))
    except Exception as e:
        results.append(_check("jarvis.service", "red", f"Error: {e}"))

    # --- llama-server ---
    try:
        ok, health_out = _run("curl -s --max-time 3 http://localhost:8080/health")
        if ok and "ok" in health_out.lower():
            results.append(_check("llama-server", "green", "Responsive (healthy)"))
        else:
            results.append(_check("llama-server", "red", f"Unhealthy: {health_out[:60]}"))
    except Exception as e:
        results.append(_check("llama-server", "red", f"Error: {e}"))

    # --- Recent errors ---
    try:
        ok, err_out = _run(
            'journalctl --user -u jarvis --since "15 min ago" '
            '--priority=err --no-pager -q 2>/dev/null | wc -l'
        )
        err_count = int(err_out.strip()) if ok and err_out.strip().isdigit() else 0
        if err_count == 0:
            results.append(_check("Recent Errors", "green", "0 errors (last 15 min)"))
        elif err_count <= 3:
            results.append(_check("Recent Errors", "yellow", f"{err_count} error(s) (last 15 min)"))
        else:
            results.append(_check("Recent Errors", "red", f"{err_count} errors (last 15 min)"))
    except Exception as e:
        results.append(_check("Recent Errors", "yellow", f"Could not check: {e}"))

    return results


# ---------------------------------------------------------------------------
# Layer 3 — JARVIS Internals
# ---------------------------------------------------------------------------

def check_internals(coordinator=None):
    """Skills, STT, TTS, pipeline, semantic matcher."""
    coord = coordinator or _coordinator_ref
    results = []

    if coord is None:
        results.append(_check("Pipeline", "yellow", "Coordinator not available (limited check)"))
        return results

    try:
        health = coord.get_health()

        # Pipeline state
        results.append(_check(
            "Pipeline", "green" if health['running'] else "red",
            f"State: {health['state']}, "
            f"event queue: {health['event_queue_size']}, "
            f"TTS queue: {health['tts_queue_size']}",
        ))

        # Skills
        results.append(_check(
            "Skills", "green" if health['skills_loaded'] > 0 else "red",
            f"{health['skills_loaded']} skills loaded, "
            f"{health['semantic_intents']} semantic intents",
        ))

        # TTS
        engine = health.get('tts_engine', 'unknown')
        results.append(_check(
            "TTS", "green",
            f"Engine: {engine}",
        ))

        # STT (check from listener state)
        listener = health.get('listener', {})
        stt_status = "green" if listener.get('stream_active') else "yellow"
        results.append(_check(
            "STT", stt_status,
            f"Mic: {listener.get('device', 'unknown')}, "
            f"stream: {'active' if listener.get('stream_active') else 'inactive'}",
        ))

        # Wake word
        results.append(_check(
            "Wake Word", "green" if listener.get('running') else "yellow",
            f"Porcupine {'active' if listener.get('running') else 'inactive'}",
        ))

        # LLM
        llm = health.get('llm', {})
        results.append(_check(
            "LLM", "green" if llm.get('local_model') else "yellow",
            f"Local: {'yes' if llm.get('local_model') else 'no'}, "
            f"API fallback: {'enabled' if llm.get('fallback_enabled') else 'disabled'}, "
            f"API calls: {llm.get('api_call_count', 0)}",
        ))

        # Managers
        mgrs = health.get('managers', {})
        enabled = [k for k, v in mgrs.items() if v]
        disabled = [k for k, v in mgrs.items() if not v]
        results.append(_check(
            "Managers", "green" if len(enabled) >= 3 else "yellow",
            f"Active: {', '.join(enabled) if enabled else 'none'}"
            + (f" | Disabled: {', '.join(disabled)}" if disabled else ""),
        ))

    except Exception as e:
        results.append(_check("Internals", "red", f"Error collecting state: {e}"))

    return results


# ---------------------------------------------------------------------------
# Layer 4 — Data Stores
# ---------------------------------------------------------------------------

def check_data_stores(config):
    """Check databases, chat history, FAISS index."""
    results = []
    data_dir = Path(config.get("system.storage_path", "/mnt/storage/jarvis")) / "data"

    # --- reminders.db ---
    db_path = config.get("reminders.db_path", str(data_dir / "reminders.db"))
    try:
        p = Path(db_path)
        if p.exists():
            size_kb = p.stat().st_size / 1024
            conn = sqlite3.connect(str(p))
            cursor = conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE status = 'pending'"
            )
            pending = cursor.fetchone()[0]
            # Next reminder time
            cursor = conn.execute(
                "SELECT MIN(reminder_time) FROM reminders WHERE status = 'pending'"
            )
            next_time = cursor.fetchone()[0]
            conn.close()
            next_str = f", next: {next_time}" if next_time else ""
            results.append(_check(
                "reminders.db", "green",
                f"{size_kb:.0f} KB, {pending} pending{next_str}",
            ))
        else:
            results.append(_check("reminders.db", "yellow", "File not found"))
    except Exception as e:
        results.append(_check("reminders.db", "yellow", f"Error: {e}"))

    # --- news_headlines.db ---
    news_db = config.get("news.db_path", str(data_dir / "news_headlines.db"))
    try:
        p = Path(news_db)
        if p.exists():
            size_kb = p.stat().st_size / 1024
            conn = sqlite3.connect(str(p))
            cursor = conn.execute("SELECT COUNT(*) FROM news_headlines")
            count = cursor.fetchone()[0]
            conn.close()
            results.append(_check(
                "news_headlines.db", "green",
                f"{size_kb:.0f} KB, {count} headlines",
            ))
        else:
            results.append(_check("news_headlines.db", "yellow", "File not found"))
    except Exception as e:
        results.append(_check("news_headlines.db", "yellow", f"Error: {e}"))

    # --- memory.db (conversational memory) ---
    if config.get("conversational_memory.enabled", False):
        mem_db = config.get("conversational_memory.db_path", str(data_dir / "memory.db"))
        try:
            p = Path(mem_db)
            if p.exists():
                size_kb = p.stat().st_size / 1024
                conn = sqlite3.connect(str(p))
                cursor = conn.execute("SELECT COUNT(*) FROM facts")
                count = cursor.fetchone()[0]
                conn.close()
                results.append(_check(
                    "memory.db", "green",
                    f"{size_kb:.0f} KB, {count} facts stored",
                ))
            else:
                results.append(_check("memory.db", "yellow", "File not found"))
        except Exception as e:
            results.append(_check("memory.db", "yellow", f"Error: {e}"))

        # FAISS index
        faiss_path = config.get("conversational_memory.faiss_index_path", str(data_dir / "memory_faiss"))
        try:
            p = Path(faiss_path)
            if p.exists() and p.is_dir():
                total_size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
                size_kb = total_size / 1024
                results.append(_check(
                    "FAISS Index", "green",
                    f"{size_kb:.0f} KB",
                ))
            elif p.exists():
                size_kb = p.stat().st_size / 1024
                results.append(_check("FAISS Index", "green", f"{size_kb:.0f} KB"))
            else:
                results.append(_check("FAISS Index", "yellow", "Not found"))
        except Exception as e:
            results.append(_check("FAISS Index", "yellow", f"Error: {e}"))

    # --- profiles.db ---
    profiles_db = str(data_dir / "profiles" / "profiles.db")
    try:
        p = Path(profiles_db)
        if p.exists():
            size_kb = p.stat().st_size / 1024
            conn = sqlite3.connect(str(p))
            cursor = conn.execute("SELECT COUNT(*) FROM profiles")
            count = cursor.fetchone()[0]
            conn.close()
            results.append(_check(
                "profiles.db", "green",
                f"{size_kb:.0f} KB, {count} enrolled",
            ))
        else:
            results.append(_check("profiles.db", "yellow", "File not found"))
    except Exception as e:
        results.append(_check("profiles.db", "yellow", f"Error: {e}"))

    # --- chat_history.jsonl ---
    chat_path = data_dir / "conversations" / "chat_history.jsonl"
    try:
        p = Path(chat_path)
        if p.exists():
            size_kb = p.stat().st_size / 1024
            # Count lines efficiently
            with open(p) as f:
                line_count = sum(1 for _ in f)
            results.append(_check(
                "chat_history.jsonl", "green",
                f"{size_kb:.0f} KB, {line_count} messages",
            ))
        else:
            results.append(_check("chat_history.jsonl", "yellow", "File not found"))
    except Exception as e:
        results.append(_check("chat_history.jsonl", "yellow", f"Error: {e}"))

    return results


# ---------------------------------------------------------------------------
# Layer 5 — Self-Assessment
# ---------------------------------------------------------------------------

def check_self_assessment(coordinator=None):
    """Uptime, session stats, mood."""
    coord = coordinator or _coordinator_ref
    results = []

    if coord is None:
        results.append(_check("Self-Assessment", "yellow", "Coordinator not available"))
        return results

    try:
        stats = coord.stats
        start_time = stats.get('start_time', time.time())
        uptime_secs = time.time() - start_time
        hours = int(uptime_secs // 3600)
        minutes = int((uptime_secs % 3600) // 60)

        if hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m"

        commands = stats.get('commands_processed', 0)
        errors = stats.get('errors', 0)

        # LLM stats
        api_calls = 0
        if coord.llm:
            api_calls = getattr(coord.llm, 'api_call_count', 0)

        results.append(_check(
            "Session", "green",
            f"Uptime: {uptime_str}, {commands} commands, {api_calls} API calls",
            f"uptime={uptime_str} commands={commands} api_calls={api_calls}",
        ))

        # Errors
        if errors == 0:
            results.append(_check("Errors", "green", "No errors this session"))
        elif errors <= 3:
            last_err = stats.get('last_error_msg', 'unknown')
            results.append(_check(
                "Errors", "yellow",
                f"{errors} error(s) — last: {last_err[:80]}",
            ))
        else:
            last_err = stats.get('last_error_msg', 'unknown')
            results.append(_check(
                "Errors", "red",
                f"{errors} errors — last: {last_err[:80]}",
            ))

        # Mood
        if errors == 0 and uptime_secs > 3600:
            mood = "Feeling quite sharp today, sir."
            mood_status = "green"
        elif errors == 0:
            mood = "All systems nominal, sir. Ready and waiting."
            mood_status = "green"
        elif errors <= 3:
            mood = "Running well, though I did stumble once or twice."
            mood_status = "yellow"
        else:
            mood = "I've had better days, sir, if I'm being honest."
            mood_status = "red"

        results.append(_check("Mood", mood_status, mood))

    except Exception as e:
        results.append(_check("Self-Assessment", "red", f"Error: {e}"))

    return results


# ---------------------------------------------------------------------------
# Full health check
# ---------------------------------------------------------------------------

def get_full_health(config) -> dict:
    """Run all 5 layers. Returns structured results keyed by layer name."""
    return {
        'bare_metal': check_bare_metal(config),
        'services': check_services(),
        'internals': check_internals(),
        'data_stores': check_data_stores(config),
        'self_assessment': check_self_assessment(),
    }


# ---------------------------------------------------------------------------
# Voice summary formatter
# ---------------------------------------------------------------------------

def format_voice_brief(health: dict) -> str:
    """Build a brief voice summary — just overall status + any errors."""
    from core.honorific import get_honorific
    h = get_honorific()

    all_checks = []
    for layer_results in health.values():
        all_checks.extend(layer_results)
    red_count = sum(1 for c in all_checks if c['status'] == 'red')
    yellow_count = sum(1 for c in all_checks if c['status'] == 'yellow')

    parts = []
    if red_count == 0 and yellow_count == 0:
        parts.append(f"All systems nominal, {h}. Full report is on your screen.")
    elif red_count > 0:
        parts.append(f"I've found {red_count} critical issue{'s' if red_count != 1 else ''}"
                     f" and {yellow_count} warning{'s' if yellow_count != 1 else ''}, {h}.")
        # Name the critical items
        red_names = [c['name'] for c in all_checks if c['status'] == 'red']
        parts.append(f"Critical: {', '.join(red_names)}. Details are on your screen.")
    else:
        parts.append(f"Systems are green with {yellow_count} minor warning{'s' if yellow_count != 1 else ''}, {h}."
                     " Report is on your screen.")

    return " ".join(parts)


def format_voice_summary(health: dict) -> str:
    """Build a butler-style voice summary from health results."""
    from core.honorific import get_honorific
    h = get_honorific()

    parts = []

    # Count issues
    all_checks = []
    for layer_results in health.values():
        all_checks.extend(layer_results)
    red_count = sum(1 for c in all_checks if c['status'] == 'red')
    yellow_count = sum(1 for c in all_checks if c['status'] == 'yellow')

    # Opening
    if red_count == 0 and yellow_count == 0:
        parts.append(f"All systems are nominal, {h}.")
    elif red_count > 0:
        parts.append(f"I've found {red_count + yellow_count} issue{'s' if red_count + yellow_count != 1 else ''}, {h}.")
    else:
        parts.append(f"Nearly everything looks good, {h}, with {yellow_count} minor note{'s' if yellow_count != 1 else ''}.")

    # CPU + GPU temps
    bare = health.get('bare_metal', [])
    for check in bare:
        if check['name'] == 'CPU':
            m = re.search(r'(\d+)\s*C', check['summary'])
            if m:
                parts.append(f"CPU is at {m.group(1)} degrees.")
        if check['name'] == 'GPU':
            m = re.search(r'(\d+)\s*C', check['summary'])
            if m:
                parts.append(f"GPU is at {m.group(1)} degrees.")

    # RAM
    for check in bare:
        if check['name'] == 'RAM':
            m = re.search(r'(\d+)%', check['summary'])
            if m:
                parts.append(f"Memory is at {m.group(1)} percent.")

    # Disk highlights (only mention issues or highest usage)
    disk_checks = [c for c in bare if c['name'].startswith('Disk')]
    problem_disks = [c for c in disk_checks if c['status'] != 'green']
    if problem_disks:
        for d in problem_disks:
            parts.append(f"{d['summary']}.")
    else:
        parts.append("All three drives are healthy.")

    # Services
    services = health.get('services', [])
    for check in services:
        if check['name'] == 'llama-server' and check['status'] != 'green':
            parts.append("Qwen's language server is not responding.")
        if check['name'] == 'jarvis.service':
            m = re.search(r'uptime:\s*(.+?)\)', check['summary'])
            if m:
                raw = m.group(1)
                # Convert compact "6h 23m" to spoken "about 6 hours"
                hm = re.match(r'(\d+)h\s*(\d+)m', raw)
                mm = re.match(r'(\d+)m$', raw)
                if hm:
                    h_val, m_val = int(hm.group(1)), int(hm.group(2))
                    if h_val == 1:
                        parts.append("I've been running for about an hour.")
                    else:
                        parts.append(f"I've been running for about {h_val} hours.")
                elif mm:
                    parts.append(f"I've been running for about {mm.group(1)} minutes.")

    # Skills
    internals = health.get('internals', [])
    for check in internals:
        if check['name'] == 'Skills':
            m = re.search(r'(\d+) skills', check['summary'])
            if m:
                parts.append(f"All {m.group(1)} skills are loaded.")

    # Data stores — just pending reminders
    data = health.get('data_stores', [])
    for check in data:
        if check['name'] == 'reminders.db':
            m = re.search(r'(\d+) pending', check['summary'])
            if m and int(m.group(1)) > 0:
                parts.append(f"I have {m.group(1)} pending reminder{'s' if int(m.group(1)) != 1 else ''}.")

    # Mood (last item in self_assessment)
    assessment = health.get('self_assessment', [])
    for check in assessment:
        if check['name'] == 'Mood':
            parts.append(check['summary'])

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Visual report formatter
# ---------------------------------------------------------------------------

# ANSI color codes
_RESET = '\033[0m'
_BOLD = '\033[1m'
_DIM = '\033[2m'
_GREEN = '\033[32m'
_YELLOW = '\033[33m'
_RED = '\033[31m'
_CYAN = '\033[36m'
_WHITE = '\033[37m'

_STATUS_COLORS = {
    'green': _GREEN,
    'yellow': _YELLOW,
    'red': _RED,
}

_STATUS_ICONS = {
    'green': f'{_GREEN}\u25cf{_RESET}',   # ● green
    'yellow': f'{_YELLOW}\u25cf{_RESET}',  # ● yellow
    'red': f'{_RED}\u2716{_RESET}',        # ✖ red
}

# Plain icons for summary line (color applied to whole phrase)
_STATUS_ICONS_PLAIN = {
    'green': '\u25cf',
    'yellow': '\u25cf',
    'red': '\u2716',
}


def format_visual_report(health: dict) -> str:
    """Build an ANSI-colored visual report for terminal display."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    width = 72

    lines = []
    lines.append(f"{_BOLD}{_CYAN}{'=' * width}{_RESET}")
    title = "JARVIS System Health Report"
    lines.append(f"{_BOLD}{_WHITE}{title.center(width)}{_RESET}")
    lines.append(f"{_DIM}{timestamp.center(width)}{_RESET}")
    lines.append(f"{_BOLD}{_CYAN}{'=' * width}{_RESET}")

    layer_names = {
        'bare_metal': 'LAYER 1 — BARE METAL',
        'services': 'LAYER 2 — SERVICES & PROCESSES',
        'internals': 'LAYER 3 — JARVIS INTERNALS',
        'data_stores': 'LAYER 4 — DATA STORES',
        'self_assessment': 'LAYER 5 — SELF-ASSESSMENT',
    }

    for layer_key, layer_title in layer_names.items():
        checks = health.get(layer_key, [])
        if not checks:
            continue

        lines.append("")
        lines.append(f"  {_BOLD}{_CYAN}{layer_title}{_RESET}")
        lines.append(f"  {_DIM}{'─' * (width - 4)}{_RESET}")

        for check in checks:
            icon = _STATUS_ICONS.get(check['status'], '?')
            color = _STATUS_COLORS.get(check['status'], '')
            name = check['name'].ljust(20)
            lines.append(f"  {icon} {_BOLD}{name}{_RESET} {color}{check['summary']}{_RESET}")

    lines.append("")
    lines.append(f"{_BOLD}{_CYAN}{'=' * width}{_RESET}")

    # Count summary
    all_checks = []
    for layer_results in health.values():
        all_checks.extend(layer_results)
    green = sum(1 for c in all_checks if c['status'] == 'green')
    yellow = sum(1 for c in all_checks if c['status'] == 'yellow')
    red = sum(1 for c in all_checks if c['status'] == 'red')
    summary = f"  {_GREEN}{_STATUS_ICONS_PLAIN['green']} {green} passed{_RESET}"
    if yellow:
        summary += f"  {_YELLOW}{_STATUS_ICONS_PLAIN['yellow']} {yellow} warning(s){_RESET}"
    if red:
        summary += f"  {_RED}{_STATUS_ICONS_PLAIN['red']} {red} critical{_RESET}"
    lines.append(summary)
    lines.append(f"{_BOLD}{_CYAN}{'=' * width}{_RESET}")

    return "\n".join(lines)
