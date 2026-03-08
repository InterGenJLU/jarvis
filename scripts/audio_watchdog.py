#!/usr/bin/env python3
"""
Audio Watchdog for JARVIS Voice Assistant

Monitors audio input/output health and auto-recovers from common failures:
- Mic device disappearing (USB disconnect, PipeWire suspend)
- Output sink dropping (Realtek analog sink)
- Listener silent freeze (stream active but no frames flowing)
- PipeWire/PulseAudio state corruption

Runs as a systemd user service alongside jarvis.service.
"""

import subprocess
import time
import sys
import os
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Configurable constants
CHECK_INTERVAL = 180          # seconds between checks (3 min)
FRAME_SILENCE_THRESHOLD = 900 # seconds with no VAD activity = frozen (15 min)
RESTART_COOLDOWN = 300       # seconds between restart attempts
MAX_RESTARTS_PER_HOUR = 3

LOG_DIR = Path(os.path.expanduser("~/.local/share/jarvis/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - audio-watchdog - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "audio_watchdog.log"),
    ],
)
logger = logging.getLogger("audio-watchdog")

# Expected devices (from config.yaml)
EXPECTED_MIC = "USB PnP Audio Device"
EXPECTED_SINK = "analog-stereo"  # Realtek


def run_cmd(cmd: str, timeout: int = 10) -> tuple[int, str]:
    """Run a shell command, return (exit_code, output)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)


class AudioWatchdog:
    def __init__(self):
        self.restart_times: list[datetime] = []
        self.last_restart: datetime | None = None
        self.consecutive_silence = 0

    def check_jarvis_running(self) -> bool:
        """Is jarvis.service active?"""
        code, out = run_cmd("systemctl --user is-active jarvis.service")
        return code == 0 and "active" in out

    def check_mic_present(self) -> dict:
        """Check if expected mic is visible to ALSA and PipeWire."""
        result = {"alsa": False, "pipewire": False, "state": "unknown"}

        # ALSA check
        code, out = run_cmd("cat /proc/asound/cards")
        if code == 0 and "USB PnP" in out:
            result["alsa"] = True

        # PipeWire/PulseAudio check
        code, out = run_cmd("pactl list sources short")
        if code == 0:
            for line in out.splitlines():
                if "usb" in line.lower() and "pnp" in line.lower():
                    result["pipewire"] = True
                    # Check state (RUNNING, SUSPENDED, IDLE)
                    parts = line.split()
                    if parts:
                        result["state"] = parts[-1]
                    break

        return result

    def check_sink_present(self) -> dict:
        """Check if expected output sink is available."""
        result = {"present": False, "state": "unknown"}

        code, out = run_cmd("pactl list sinks short")
        if code == 0:
            for line in out.splitlines():
                if EXPECTED_SINK in line:
                    result["present"] = True
                    parts = line.split()
                    if parts:
                        result["state"] = parts[-1]
                    break

        return result

    def check_listener_activity(self) -> dict:
        """Check recent VAD/speech activity from jarvis journal logs."""
        result = {"active": False, "last_activity_ago": None, "frames_recent": 0}

        # Look at last 2 minutes of logs for any speech processing
        code, out = run_cmd(
            "journalctl --user -u jarvis.service --since '2 min ago' "
            "--no-pager -q 2>/dev/null"
        )
        if code != 0:
            return result

        # Count speech processing events
        speech_lines = [
            l for l in out.splitlines()
            if "Processing speech" in l or "Speech detected" in l
        ]
        result["frames_recent"] = len(speech_lines)

        # Find the most recent activity of any kind from the listener
        code2, out2 = run_cmd(
            "journalctl --user -u jarvis.service --since '5 min ago' "
            "--no-pager -q 2>/dev/null | "
            "grep -E 'Processing speech|Audio callback|Speech detected|VAD' | tail -1"
        )
        if code2 == 0 and out2:
            # Parse timestamp from journal line
            match = re.match(r"(\w+ \d+ \d+:\d+:\d+)", out2)
            if match:
                try:
                    ts_str = match.group(1)
                    now = datetime.now()
                    ts = datetime.strptime(
                        f"{now.year} {ts_str}", "%Y %b %d %H:%M:%S"
                    )
                    delta = (now - ts).total_seconds()
                    result["last_activity_ago"] = delta
                    result["active"] = delta < FRAME_SILENCE_THRESHOLD
                except ValueError:
                    pass

        return result

    def try_recover_mic(self) -> bool:
        """Attempt to recover a suspended or missing mic."""
        logger.info("Attempting mic recovery...")

        # Try unsuspending via PipeWire/PulseAudio
        code, out = run_cmd("pactl list sources short")
        if code == 0:
            for line in out.splitlines():
                if "usb" in line.lower() and "pnp" in line.lower():
                    source_id = line.split()[0]
                    run_cmd(f"pactl suspend-source {source_id} 0")
                    logger.info(f"Unsuspended source {source_id}")
                    return True

        # If not found in PipeWire, try triggering USB re-enumeration
        logger.warning("Mic not found in PipeWire — may need physical reconnect")
        return False

    def try_recover_sink(self) -> bool:
        """Attempt to recover a dropped output sink."""
        logger.info("Attempting sink recovery...")

        code, out = run_cmd("pactl list sinks short")
        if code == 0:
            for line in out.splitlines():
                if EXPECTED_SINK in line:
                    sink_id = line.split()[0]
                    run_cmd(f"pactl suspend-sink {sink_id} 0")
                    logger.info(f"Unsuspended sink {sink_id}")
                    return True

        logger.warning("Output sink not found — Realtek may have dropped")
        return False

    def restart_jarvis(self, reason: str) -> bool:
        """Restart jarvis.service with cooldown and rate limiting."""
        now = datetime.now()

        # Cooldown check
        if self.last_restart and (now - self.last_restart).total_seconds() < RESTART_COOLDOWN:
            remaining = RESTART_COOLDOWN - (now - self.last_restart).total_seconds()
            logger.warning(
                f"Restart cooldown active ({remaining:.0f}s remaining) — skipping"
            )
            return False

        # Rate limit check
        hour_ago = now - timedelta(hours=1)
        self.restart_times = [t for t in self.restart_times if t > hour_ago]
        if len(self.restart_times) >= MAX_RESTARTS_PER_HOUR:
            logger.error(
                f"Rate limit hit ({MAX_RESTARTS_PER_HOUR} restarts/hour) — "
                "manual intervention needed"
            )
            self._notify_user(
                "Audio watchdog: JARVIS restart limit reached. Manual check needed."
            )
            return False

        logger.warning(f"Restarting jarvis.service — reason: {reason}")
        code, out = run_cmd("systemctl --user restart jarvis.service")
        if code == 0:
            self.last_restart = now
            self.restart_times.append(now)
            logger.info("jarvis.service restarted successfully")
            return True
        else:
            logger.error(f"Failed to restart jarvis.service: {out}")
            return False

    def _notify_user(self, message: str):
        """Send desktop notification."""
        run_cmd(
            f'notify-send -u critical "JARVIS Audio Watchdog" "{message}"'
        )

    def run_check(self):
        """Run one full health check cycle."""
        # 1. Is Jarvis running at all?
        if not self.check_jarvis_running():
            logger.info("jarvis.service not active — nothing to monitor")
            self.consecutive_silence = 0
            return

        # 2. Check mic
        mic = self.check_mic_present()
        if not mic["alsa"]:
            logger.error("Mic not found in ALSA — hardware disconnected?")
            self._notify_user("Mic disconnected — check USB cable")
            return

        if mic["state"] == "SUSPENDED":
            logger.warning("Mic suspended in PipeWire — attempting recovery")
            self.try_recover_mic()

        # 3. Check output sink
        sink = self.check_sink_present()
        if not sink["present"]:
            logger.warning("Output sink missing — attempting recovery")
            if not self.try_recover_sink():
                self._notify_user("Audio output lost — Realtek sink dropped")

        # 4. Check listener frame flow (the silent freeze detector)
        activity = self.check_listener_activity()

        if activity["last_activity_ago"] is not None:
            if activity["active"]:
                self.consecutive_silence = 0
            else:
                self.consecutive_silence += 1
                logger.warning(
                    f"No listener activity for {activity['last_activity_ago']:.0f}s "
                    f"(consecutive silent checks: {self.consecutive_silence})"
                )
        else:
            # No activity data at all — could be normal (no one talking)
            # or could be frozen. Increment cautiously.
            self.consecutive_silence += 1

        # 5 consecutive silent checks (5 min) with Jarvis running = likely frozen
        if self.consecutive_silence >= 5:
            logger.error(
                f"Listener appears frozen ({self.consecutive_silence} silent checks)"
            )
            # First try mic recovery
            if mic["pipewire"] and mic["state"] != "RUNNING":
                self.try_recover_mic()
                time.sleep(5)
                # Re-check
                recheck = self.check_listener_activity()
                if recheck.get("active"):
                    logger.info("Recovery successful — listener resumed")
                    self.consecutive_silence = 0
                    return

            # If still frozen, restart the service
            if self.restart_jarvis("listener silent freeze detected"):
                self.consecutive_silence = 0

    def run(self):
        """Main loop."""
        logger.info(
            f"Audio watchdog started (interval={CHECK_INTERVAL}s, "
            f"silence_threshold={FRAME_SILENCE_THRESHOLD}s)"
        )

        while True:
            try:
                self.run_check()
            except Exception as e:
                logger.error(f"Watchdog check failed: {e}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    watchdog = AudioWatchdog()
    watchdog.run()
