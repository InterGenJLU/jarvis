"""GPU Swap Manager — orchestrates exclusive VRAM access between services.

The RX 7900 XT (20GB) can only hold one large model at a time.
This manager handles the stop → start → health-check cycle for swapping
between llama-server (LLM) and flux-server (image generation).

Thread-safe: only one swap can be in progress at a time.
"""

import logging
import subprocess
import threading
import time

import requests

logger = logging.getLogger("jarvis.gpu_swap")

# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------

SERVICES = {
    "llama": {
        "unit": "llama-server.service",
        "health_url": "http://127.0.0.1:8080/health",
        "startup_timeout": 30,
    },
    "flux": {
        "unit": "flux-server.service",
        "health_url": "http://127.0.0.1:8190/health",
        "startup_timeout": 90,  # model load takes 30-60s
    },
}

DEFAULT_SERVICE = "llama"


class GPUSwapManager:
    """Manages VRAM by swapping between GPU-bound services."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = DEFAULT_SERVICE
        self._swapping = False
        logger.info("GPUSwapManager initialized, active service: %s", self._active)

    # -- Public properties --------------------------------------------------

    @property
    def active_service(self) -> str:
        """Name of the currently active GPU service."""
        return self._active

    @property
    def is_llm_available(self) -> bool:
        """True when llama-server is active and VRAM is available for LLM."""
        return self._active == "llama" and not self._swapping

    @property
    def is_swapping(self) -> bool:
        return self._swapping

    # -- Service control (private) ------------------------------------------

    def _systemctl(self, action: str, unit: str) -> bool:
        """Run a systemctl command. Returns True on success."""
        cmd = ["sudo", "systemctl", action, unit]
        logger.info("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.error("systemctl %s %s failed: %s", action, unit, result.stderr.strip())
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("systemctl %s %s timed out", action, unit)
            return False

    def _wait_for_stop(self, unit: str, timeout: int = 15) -> bool:
        """Wait until a systemd unit is no longer active."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = subprocess.run(
                ["sudo", "systemctl", "is-active", unit],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() != "active":
                return True
            time.sleep(0.5)
        logger.error("Timed out waiting for %s to stop", unit)
        return False

    def _wait_for_health(self, url: str, timeout: int) -> bool:
        """Poll a health endpoint until it responds with 'ready'."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = requests.get(url, timeout=3)
                if r.status_code == 200:
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    status = data.get("status", "ready")
                    if status == "ready":
                        return True
                    logger.debug("Health check %s: status=%s", url, status)
            except (requests.ConnectionError, requests.Timeout):
                pass
            time.sleep(2)
        logger.error("Timed out waiting for %s to become healthy", url)
        return False

    # -- Public API ---------------------------------------------------------

    def swap_to(self, target: str) -> bool:
        """Stop the current GPU service and start the target.

        Args:
            target: Service name ("llama" or "flux").

        Returns:
            True if swap succeeded and target is healthy.
        """
        if target not in SERVICES:
            logger.error("Unknown swap target: %s", target)
            return False

        if target == self._active and not self._swapping:
            logger.info("Already on %s, no swap needed", target)
            return True

        if not self._lock.acquire(blocking=False):
            logger.warning("Swap already in progress, rejecting swap_to(%s)", target)
            return False

        try:
            self._swapping = True
            current = self._active
            current_svc = SERVICES.get(current, {})
            target_svc = SERVICES[target]

            logger.info("GPU swap: %s → %s", current, target)

            # 1. Stop current service
            if current_svc:
                if not self._systemctl("stop", current_svc["unit"]):
                    logger.error("Failed to stop %s, aborting swap", current)
                    return False
                if not self._wait_for_stop(current_svc["unit"]):
                    logger.error("%s did not stop cleanly, aborting swap", current)
                    return False
                logger.info("Stopped %s", current)

            # 2. Start target service
            if not self._systemctl("start", target_svc["unit"]):
                logger.error("Failed to start %s", target)
                # Try to recover by restarting the original
                self._recover(current)
                return False

            # 3. Wait for health
            if not self._wait_for_health(target_svc["health_url"], target_svc["startup_timeout"]):
                logger.error("%s started but not healthy, recovering", target)
                self._systemctl("stop", target_svc["unit"])
                self._recover(current)
                return False

            self._active = target
            logger.info("GPU swap complete: now on %s", target)
            return True

        finally:
            self._swapping = False
            self._lock.release()

    def swap_back(self) -> bool:
        """Return to the default service (llama-server)."""
        return self.swap_to(DEFAULT_SERVICE)

    def _recover(self, service_name: str):
        """Best-effort recovery: restart the given service."""
        svc = SERVICES.get(service_name)
        if not svc:
            return
        logger.warning("Attempting recovery: restarting %s", service_name)
        if self._systemctl("start", svc["unit"]):
            if self._wait_for_health(svc["health_url"], svc["startup_timeout"]):
                self._active = service_name
                logger.info("Recovery successful: %s is active", service_name)
                return
        logger.error("Recovery FAILED for %s — GPU may be in unknown state", service_name)
        self._active = "unknown"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance = None
_instance_lock = threading.Lock()


def get_gpu_swap_manager() -> GPUSwapManager:
    """Get or create the singleton GPUSwapManager."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = GPUSwapManager()
    return _instance
