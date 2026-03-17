"""
JARVIS Internal Watchdog — proactive self-healing for the voice pipeline.

Runs as a daemon thread inside the JARVIS process.  Every `check_interval`
seconds it runs lightweight health checks against the coordinator, listener,
queues, and llama-server, and takes graduated recovery actions:

    1. Silent self-fix  (clear stuck flags, drain queues)
    2. Logged warning   (for post-mortem analysis)
    3. Spoken announcement (last resort, rate-limited)

Usage:
    from core.watchdog import Watchdog

    wd = Watchdog(config=config, coordinator=coordinator,
                  listener=listener, tts=tts,
                  event_queue=eq, audio_queue=aq, tts_queue=tq)
    wd.start()   # daemon thread — dies with the process
"""

import queue
import subprocess
import threading
import time

import requests

from core.events import Event, EventType, PipelineState
from core.logger import get_logger


class Watchdog(threading.Thread):
    """Background self-healing monitor for the JARVIS voice pipeline."""

    def __init__(self, *, config, coordinator, listener, tts,
                 event_queue, audio_queue, tts_queue):
        super().__init__(daemon=True, name="watchdog")
        self.logger = get_logger("core.watchdog", config)

        self._coordinator = coordinator
        self._listener = listener
        self._tts = tts
        self._event_queue = event_queue
        self._audio_queue = audio_queue
        self._tts_queue = tts_queue

        # Configuration
        self._check_interval = config.get("watchdog.check_interval", 10)
        self._listener_stuck_threshold = config.get("watchdog.listener_stuck_threshold", 60)
        self._command_hung_threshold = config.get("watchdog.command_hung_threshold", 120)
        self._queue_backlog_threshold = config.get("watchdog.queue_backlog_threshold", 10)
        self._llm_health_interval = config.get("watchdog.llm_health_interval", 30)
        self._recovery_cooldown = config.get("watchdog.recovery_cooldown", 300)
        self._announce_failures = config.get("watchdog.announce_failures", True)
        self._max_announcements_per_hour = config.get("watchdog.max_announcements_per_hour", 3)

        # Internal state
        self._recovery_log: dict[str, float] = {}        # check_name → last recovery time
        self._announcement_times: list[float] = []        # monotonic timestamps
        self._last_llm_check_ts: float = 0.0
        self._llm_status: str | None = None               # None = healthy
        self._llm_unhealthy_count: int = 0
        self._flux_detected_ts: float = 0.0
        self._flux_grace_period: int = 400  # seconds — covers 300s generation + 90s startup + buffer
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self.logger.info(
            "Watchdog started (interval=%ds, listener_stuck=%ds, command_hung=%ds)",
            self._check_interval, self._listener_stuck_threshold,
            self._command_hung_threshold,
        )
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._check_interval)
            if self._stop_event.is_set():
                break
            if not self._coordinator.running:
                break
            try:
                self._run_checks()
            except Exception as e:
                self.logger.error("Watchdog check cycle error: %s", e, exc_info=True)
        self.logger.info("Watchdog stopped")

    def stop(self):
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Check dispatch
    # ------------------------------------------------------------------

    def _run_checks(self):
        # Order matters: clear cheap false-positive sources first
        if self._check_streaming_orphan():
            self._recover_streaming_orphan()

        if self._check_speaking_stuck():
            self._recover_speaking_stuck()

        if self._check_listener_stuck():
            self._recover_listener_stuck()

        if self._check_command_hung():
            self._recover_command_hung()

        if self._check_stt_backlog():
            self._recover_stt_backlog()

        self._check_llm_health()

    # ------------------------------------------------------------------
    # Check 1: Streaming orphan — _streaming_active=True but IDLE
    # ------------------------------------------------------------------

    def _check_streaming_orphan(self) -> bool:
        if not self._coordinator._streaming_active:
            return False
        return self._coordinator.state == PipelineState.IDLE

    def _recover_streaming_orphan(self):
        self._coordinator._streaming_active = False
        self.logger.warning("Cleared orphaned _streaming_active flag")

    # ------------------------------------------------------------------
    # Check 2: Speaking flags stuck — listener paused but TTS idle
    # ------------------------------------------------------------------

    def _check_speaking_stuck(self) -> bool:
        listener = self._listener
        if not (listener.speaking or listener._speaking_event.is_set()):
            return False
        # TTS actively playing?
        with self._tts._active_procs_lock:
            tts_active = bool(self._tts._active_procs)
        if tts_active or self._tts_queue.qsize() > 0:
            return False  # legitimately speaking
        return True

    def _recover_speaking_stuck(self):
        if not self._can_recover("speaking_stuck"):
            return
        self._listener.speaking = False
        self._listener._speaking_event.clear()
        self._listener.resume_listening()
        self._record_recovery("speaking_stuck")
        self.logger.warning("Cleared stuck speaking flags, resumed listening")

    # ------------------------------------------------------------------
    # Check 3: Listener stuck — no transcription while IDLE for too long
    # ------------------------------------------------------------------

    def _check_listener_stuck(self) -> bool:
        if not self._listener.running:
            return False
        if self._coordinator.state != PipelineState.IDLE:
            return False
        if self._listener.speaking or self._listener._speaking_event.is_set():
            return False
        # Only consider "stuck" if VAD has detected speech activity since the
        # last transcription — otherwise it's just silence (nobody talking),
        # which is normal and doesn't need recovery.
        last_vad = getattr(self._listener, '_last_vad_activity_ts', 0.0)
        last_tx = self._coordinator._last_transcription_ts
        if last_vad <= last_tx:
            return False  # No VAD activity since last transcription — just silence
        idle_duration = time.monotonic() - last_tx
        return idle_duration > self._listener_stuck_threshold

    def _recover_listener_stuck(self):
        if not self._can_recover("listener_stuck"):
            return
        self.logger.warning(
            "Listener appears stuck (no transcription for %.0fs) — attempting soft reset",
            time.monotonic() - self._coordinator._last_transcription_ts,
        )
        # Soft reset: clear any stuck state flags
        self._listener.speaking = False
        self._listener._speaking_event.clear()
        self._listener.collecting_speech = False
        self._listener.speech_buffer = []
        self._coordinator._streaming_active = False
        self._coordinator.state = PipelineState.IDLE
        self._listener.resume_listening()
        # Reset the timestamp so we don't immediately re-trigger
        self._coordinator._last_transcription_ts = time.monotonic()
        self._record_recovery("listener_stuck")
        self.logger.info("Listener soft reset complete")

    # ------------------------------------------------------------------
    # Check 4: Command processing hung
    # ------------------------------------------------------------------

    def _check_command_hung(self) -> bool:
        if self._coordinator.state == PipelineState.IDLE:
            return False
        if self._coordinator._last_command_start_ts == 0.0:
            return False
        elapsed = time.monotonic() - self._coordinator._last_command_start_ts
        return elapsed > self._command_hung_threshold

    def _recover_command_hung(self):
        if not self._can_recover("command_hung"):
            return
        elapsed = time.monotonic() - self._coordinator._last_command_start_ts
        self.logger.warning(
            "Command processing hung for %.0fs — forcing IDLE", elapsed
        )
        self._coordinator._streaming_active = False
        self._coordinator._llm_responded = True
        self._coordinator.state = PipelineState.IDLE
        self._coordinator._last_idle_ts = time.monotonic()
        self._listener.speaking = False
        self._listener._speaking_event.clear()
        self._listener.resume_listening()
        self._record_recovery("command_hung")
        self._announce(
            "I'm sorry, I got stuck processing your last request. I'm back and listening."
        )

    # ------------------------------------------------------------------
    # Check 5: STT audio queue backlog
    # ------------------------------------------------------------------

    def _check_stt_backlog(self) -> bool:
        return self._audio_queue.qsize() > self._queue_backlog_threshold

    def _recover_stt_backlog(self):
        if not self._can_recover("stt_backlog"):
            return
        drained = 0
        while self._audio_queue.qsize() > 1:
            try:
                self._audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        self._record_recovery("stt_backlog")
        self.logger.warning("Drained %d stale audio frames from queue", drained)

    # ------------------------------------------------------------------
    # Check 6: llama-server health (periodic, not every cycle)
    # ------------------------------------------------------------------

    def _check_llm_health(self):
        now = time.monotonic()
        if now - self._last_llm_check_ts < self._llm_health_interval:
            return
        self._last_llm_check_ts = now

        # Respect GPU swap — expected downtime
        try:
            from core.gpu_swap import get_gpu_swap_manager
            swap = get_gpu_swap_manager()
            if swap and swap.is_swapping:
                if self._llm_status != "swapping":
                    self.logger.info("LLM offline — GPU swap in progress")
                self._llm_status = "swapping"
                self._llm_unhealthy_count = 0
                return
            if swap and not swap.is_llm_available:
                if self._llm_status != "gpu_swapped":
                    self.logger.info("LLM offline — GPU allocated to another service")
                self._llm_status = "gpu_swapped"
                self._llm_unhealthy_count = 0
                return
        except Exception:
            pass  # gpu_swap not initialized or import error

        # Cross-process check: flux-server may be running from web service GPU swap
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "flux-server.service"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "active":
                if self._llm_status != "flux_generating":
                    self._flux_detected_ts = time.monotonic()
                    self.logger.info("LLM offline — flux-server active (image generation in progress)")
                elif time.monotonic() - self._flux_detected_ts > self._flux_grace_period:
                    self.logger.warning(
                        "flux-server still running after %ds grace period — possible stuck generation",
                        self._flux_grace_period,
                    )
                    # Fall through to normal health check / announcement
                else:
                    # Within grace period — suppress
                    pass
                if time.monotonic() - self._flux_detected_ts <= self._flux_grace_period:
                    self._llm_status = "flux_generating"
                    self._llm_unhealthy_count = 0
                    return
        except Exception:
            pass

        try:
            r = requests.get("http://127.0.0.1:8080/health", timeout=3)
            if r.status_code == 200:
                data = r.json() if "json" in r.headers.get("content-type", "") else {}
                status = data.get("status", "ok")
                if status == "ok":
                    if self._llm_status and self._llm_status not in ("swapping", "gpu_swapped"):
                        self.logger.info("LLM back online (was: %s)", self._llm_status)
                    self._llm_status = None
                    self._llm_unhealthy_count = 0
                    return
                new_status = status  # e.g. "loading model"
            else:
                new_status = f"http_{r.status_code}"
        except (requests.ConnectionError, requests.Timeout):
            new_status = "unreachable"
        except Exception as e:
            new_status = f"error: {e}"

        # Track consecutive unhealthy checks
        if new_status != self._llm_status:
            self.logger.warning("LLM status changed: %s → %s", self._llm_status, new_status)
        self._llm_status = new_status
        self._llm_unhealthy_count += 1

        # Announce after 2 consecutive unhealthy checks (not transient)
        if self._llm_unhealthy_count == 2 and new_status == "unreachable":
            self._announce(
                "System notification, sir. My language model is currently off line. "
                "I can still handle skill-based commands."
            )

    @property
    def llm_status(self) -> str | None:
        """Current LLM status. None = healthy."""
        return self._llm_status

    # ------------------------------------------------------------------
    # Recovery cooldown
    # ------------------------------------------------------------------

    def _can_recover(self, check_name: str) -> bool:
        last = self._recovery_log.get(check_name, 0.0)
        return (time.monotonic() - last) >= self._recovery_cooldown

    def _record_recovery(self, check_name: str):
        self._recovery_log[check_name] = time.monotonic()

    # ------------------------------------------------------------------
    # TTS announcements (rate-limited)
    # ------------------------------------------------------------------

    def _can_announce(self) -> bool:
        if not self._announce_failures:
            return False
        now = time.monotonic()
        self._announcement_times = [
            t for t in self._announcement_times if now - t < 3600
        ]
        return len(self._announcement_times) < self._max_announcements_per_hour

    def _announce(self, message: str):
        if not self._can_announce():
            self.logger.warning("Suppressed announcement (rate limit): %s", message)
            return
        self._announcement_times.append(time.monotonic())
        self.logger.info("Announcing: %s", message)
        self._tts_queue.put(Event(
            EventType.SPEAK_REQUEST,
            data={"text": message},
            source="watchdog",
        ))
