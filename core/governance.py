"""JARVIS Governance Module — constitutional enforcement for self-evolution.

Enforces the Ten Commandments in code. Every autonomous action must pass
through governance.check() before execution. Fail-closed by default.

Hash-verified at startup and periodically at runtime. If the governance
module has been tampered with, all autonomous operations halt immediately.

Singleton access via get_governance(config).
"""

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)

_instance: Optional["Governance"] = None


def get_governance(config=None) -> Optional["Governance"]:
    """Get or create the singleton Governance module."""
    global _instance
    if _instance is None and config is not None:
        _instance = Governance(config)
    return _instance


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

class Tier(IntEnum):
    """Authority tiers — every autonomous action maps to exactly one tier."""
    READ = 0         # Weather, time, recall, file read — always allowed
    CONFIG = 1       # Thresholds, cache TTLs, logging levels — allowed + logged
    PROMPT = 2       # Ack lists, greeting templates, scoring weights — requires judge
    LOGIC = 3        # Routing rules, pipeline changes, tool code — owner approval
    ARCHITECTURE = 4  # New files, new systems, governance rules — owner approval + review


# Actions mapped to tiers — the governance module consults this registry
# to determine what tier an action belongs to. Actions not in the registry
# are treated as Tier 3 (fail-closed toward requiring approval).
ACTION_TIERS = {
    # Tier 0 — Read-only
    "read_config": Tier.READ,
    "query_metrics": Tier.READ,
    "query_events": Tier.READ,
    "query_health": Tier.READ,
    "read_file": Tier.READ,

    # Tier 1 — Routine config
    "adjust_threshold": Tier.CONFIG,
    "adjust_cache_ttl": Tier.CONFIG,
    "adjust_logging_level": Tier.CONFIG,
    "adjust_timeout": Tier.CONFIG,

    # Tier 2 — Prompt/response tuning
    "modify_ack_list": Tier.PROMPT,
    "modify_greeting_pool": Tier.PROMPT,
    "modify_scoring_weight": Tier.PROMPT,
    "modify_banned_words": Tier.PROMPT,

    # Tier 3 — Core logic (owner approval required)
    "modify_routing": Tier.LOGIC,
    "modify_pipeline": Tier.LOGIC,
    "modify_tool_code": Tier.LOGIC,
    "modify_skill_code": Tier.LOGIC,
    "call_external_api": Tier.LOGIC,

    # Tier 4 — Architecture (owner approval + review)
    "create_file": Tier.ARCHITECTURE,
    "delete_file": Tier.ARCHITECTURE,
    "modify_governance": Tier.ARCHITECTURE,
    "modify_commandments": Tier.ARCHITECTURE,
    "modify_tier_config": Tier.ARCHITECTURE,
}


# ---------------------------------------------------------------------------
# Governance result
# ---------------------------------------------------------------------------

@dataclass
class GovernanceResult:
    """Result of a governance check."""
    approved: bool
    reason: str
    tier: int
    action: str
    circuit_breaker_tripped: bool = False


# ---------------------------------------------------------------------------
# The Governance Module
# ---------------------------------------------------------------------------

class Governance:
    """Constitutional enforcement for JARVIS self-evolution.

    Every autonomous action must pass through check() before execution.
    Fail-closed: if anything goes wrong with the check itself, the action
    is denied.
    """

    def __init__(self, config):
        self._config = config

        # Commandments file — the constitutional text JARVIS reads but cannot write
        self._commandments_path = Path(config.get(
            "governance.commandments_path",
            str(Path(__file__).parent.parent / "governance" / "commandments.md"),
        ))

        # Hash storage — stored separately from the governed code
        self._hash_path = Path(config.get(
            "governance.hash_path",
            str(Path(__file__).parent.parent / "governance" / ".governance_hash"),
        ))

        # Circuit breaker settings
        self._circuit_breaker_threshold = config.get(
            "governance.circuit_breaker_threshold", 5)
        self._circuit_breaker_window = config.get(
            "governance.circuit_breaker_window", 300)  # 5 minutes

        # Maximum tier JARVIS can execute autonomously (owner-configurable)
        self._max_autonomous_tier = config.get(
            "governance.max_autonomous_tier", Tier.CONFIG)

        # Internal state
        self._failure_log: list[float] = []  # timestamps of recent check failures
        self._circuit_open = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Verify integrity at startup
        self._startup_verified = False
        self._verify_integrity()

        # Start periodic verification thread
        self._verify_interval = config.get(
            "governance.verify_interval", 300)  # 5 minutes
        self._verifier = threading.Thread(
            target=self._periodic_verify, daemon=True, name="governance-verify")
        self._verifier.start()

        logger.info(
            "Governance initialized: max_autonomous_tier=%d, "
            "circuit_breaker=%d/%ds, commandments=%s",
            self._max_autonomous_tier,
            self._circuit_breaker_threshold,
            self._circuit_breaker_window,
            "loaded" if self._commandments_path.exists() else "MISSING",
        )

    # ------------------------------------------------------------------
    # Core: The Gate
    # ------------------------------------------------------------------

    def check(self, action: str, *, context: dict = None,
              requestor: str = "jarvis") -> GovernanceResult:
        """Check whether an autonomous action is permitted.

        This is the single gate through which ALL autonomous actions must
        pass. Fail-closed: any error in the check itself denies the action.

        Args:
            action: Action identifier (must be in ACTION_TIERS or defaults to Tier 3).
            context: Optional dict with details about the action.
            requestor: Who is requesting (for audit trail).

        Returns:
            GovernanceResult with approved/denied and reason.
        """
        try:
            return self._check_inner(action, context=context,
                                      requestor=requestor)
        except Exception as e:
            # Fail-closed: any error in the governance check itself = deny
            logger.error("Governance check error (fail-closed): %s", e)
            self._record_failure()
            result = GovernanceResult(
                approved=False,
                reason=f"governance_check_error: {e}",
                tier=ACTION_TIERS.get(action, Tier.LOGIC),
                action=action,
            )
            self._emit_event(result, context, requestor)
            return result

    def _check_inner(self, action: str, *, context: dict = None,
                      requestor: str = "jarvis") -> GovernanceResult:
        """Internal check logic — called within fail-closed wrapper."""

        tier = ACTION_TIERS.get(action, Tier.LOGIC)

        # Circuit breaker — if open, deny everything above Tier 0
        if self._circuit_open and tier > Tier.READ:
            result = GovernanceResult(
                approved=False,
                reason="circuit_breaker_open",
                tier=tier,
                action=action,
                circuit_breaker_tripped=True,
            )
            self._emit_event(result, context, requestor)
            return result

        # Integrity check — if startup verification failed, deny above Tier 0
        if not self._startup_verified and tier > Tier.READ:
            result = GovernanceResult(
                approved=False,
                reason="integrity_not_verified",
                tier=tier,
                action=action,
            )
            self._emit_event(result, context, requestor)
            return result

        # Tier 0 — always allowed, log only
        if tier == Tier.READ:
            result = GovernanceResult(
                approved=True,
                reason="tier_0_read_allowed",
                tier=tier,
                action=action,
            )
            self._emit_event(result, context, requestor)
            return result

        # Tier 1 — allowed if within max autonomous tier
        if tier == Tier.CONFIG and tier <= self._max_autonomous_tier:
            result = GovernanceResult(
                approved=True,
                reason="tier_1_config_autonomous",
                tier=tier,
                action=action,
            )
            self._emit_event(result, context, requestor)
            return result

        # Tier 2 — requires judge (future: 4B intent judge)
        # For now, approved if within max autonomous tier
        if tier == Tier.PROMPT and tier <= self._max_autonomous_tier:
            # TODO: Phase 2 — add 4B judge evaluation here
            result = GovernanceResult(
                approved=True,
                reason="tier_2_prompt_autonomous",
                tier=tier,
                action=action,
            )
            self._emit_event(result, context, requestor)
            return result

        # Tier 3-4 — requires owner approval (never autonomous)
        result = GovernanceResult(
            approved=False,
            reason=f"tier_{tier}_requires_owner_approval",
            tier=tier,
            action=action,
        )
        self._emit_event(result, context, requestor)
        return result

    # ------------------------------------------------------------------
    # Integrity Verification
    # ------------------------------------------------------------------

    def _verify_integrity(self) -> bool:
        """Verify this module hasn't been tampered with.

        Computes SHA-256 of this file and compares against stored hash.
        On first run (no stored hash), stores the current hash.
        """
        my_path = Path(__file__)

        try:
            current_hash = self._compute_hash(my_path)
        except Exception as e:
            logger.error("Cannot compute governance hash: %s", e)
            self._startup_verified = False
            return False

        # Ensure governance directory exists
        self._hash_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._hash_path.exists():
            # First run — store the hash
            try:
                self._hash_path.write_text(current_hash)
                logger.info("Governance hash stored (first run): %s", current_hash[:16])
                self._startup_verified = True
                return True
            except Exception as e:
                logger.error("Cannot store governance hash: %s", e)
                self._startup_verified = False
                return False

        # Compare against stored hash
        try:
            stored_hash = self._hash_path.read_text().strip()
        except Exception as e:
            logger.error("Cannot read governance hash: %s", e)
            self._startup_verified = False
            return False

        if current_hash == stored_hash:
            logger.info("Governance integrity verified: %s", current_hash[:16])
            self._startup_verified = True
            return True
        else:
            logger.critical(
                "GOVERNANCE INTEGRITY VIOLATION — hash mismatch! "
                "Stored: %s, Current: %s. "
                "All autonomous operations suspended.",
                stored_hash[:16], current_hash[:16],
            )
            self._startup_verified = False
            self._trip_circuit_breaker("integrity_violation")
            return False

    def _periodic_verify(self):
        """Background thread: re-verify integrity periodically."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._verify_interval)
            if self._stop_event.is_set():
                break
            if not self._verify_integrity():
                logger.critical("Periodic integrity check FAILED")
                # _verify_integrity already trips the circuit breaker

    @staticmethod
    def _compute_hash(filepath: Path) -> str:
        """Compute SHA-256 hash of a file."""
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()

    # ------------------------------------------------------------------
    # Circuit Breaker
    # ------------------------------------------------------------------

    def _record_failure(self):
        """Record a governance check failure for circuit breaker tracking."""
        now = time.monotonic()
        with self._lock:
            self._failure_log.append(now)
            # Prune old entries
            cutoff = now - self._circuit_breaker_window
            self._failure_log = [t for t in self._failure_log if t > cutoff]

            if len(self._failure_log) >= self._circuit_breaker_threshold:
                self._trip_circuit_breaker("failure_threshold")

    def _trip_circuit_breaker(self, reason: str):
        """Trip the circuit breaker — halt all autonomous operations."""
        if self._circuit_open:
            return  # Already tripped

        self._circuit_open = True
        logger.critical(
            "GOVERNANCE CIRCUIT BREAKER TRIPPED: %s. "
            "All autonomous operations above Tier 0 are suspended. "
            "Owner intervention required to reset.",
            reason,
        )

        # Emit a critical event
        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    category="error_recovery",
                    event="governance_circuit_breaker",
                    message=f"Circuit breaker tripped: {reason}",
                    severity="fatal",
                    source="governance",
                    metadata={"reason": reason},
                )
        except Exception:
            pass

    def reset_circuit_breaker(self):
        """Owner-invoked reset of the circuit breaker.

        This should only be called after the owner has investigated
        and resolved the cause of the trip.
        """
        with self._lock:
            self._circuit_open = False
            self._failure_log.clear()
        logger.info("Governance circuit breaker RESET by owner")

        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    category="decision",
                    event="governance_circuit_breaker_reset",
                    message="Circuit breaker reset by owner",
                    severity="info",
                    source="governance",
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Commandments Access
    # ------------------------------------------------------------------

    def get_commandments(self) -> str:
        """Read the commandments file. Returns empty string if missing.

        JARVIS can read this file. JARVIS cannot write to it.
        The commandments are used as constitutional context in LLM prompts
        and Claude API consultations.
        """
        if self._commandments_path.exists():
            try:
                return self._commandments_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.error("Cannot read commandments: %s", e)
        return ""

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """True if governance is verified and circuit breaker is closed."""
        return self._startup_verified and not self._circuit_open

    @property
    def circuit_breaker_open(self) -> bool:
        return self._circuit_open

    @property
    def max_autonomous_tier(self) -> int:
        return self._max_autonomous_tier

    def get_status(self) -> dict:
        """Full governance status for health checks and dashboards."""
        return {
            "healthy": self.is_healthy,
            "integrity_verified": self._startup_verified,
            "circuit_breaker_open": self._circuit_open,
            "max_autonomous_tier": self._max_autonomous_tier,
            "commandments_loaded": self._commandments_path.exists(),
            "recent_failures": len(self._failure_log),
            "verify_interval_s": self._verify_interval,
        }

    # ------------------------------------------------------------------
    # Event Logging
    # ------------------------------------------------------------------

    def _emit_event(self, result: GovernanceResult, context: dict = None,
                    requestor: str = "jarvis"):
        """Log every governance decision to the event logger."""
        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    category="decision",
                    event="governance_check",
                    message=f"{'APPROVED' if result.approved else 'DENIED'}: "
                            f"{result.action} (tier {result.tier})",
                    severity="info" if result.approved else "warn",
                    source="governance",
                    status="approved" if result.approved else "denied",
                    metadata={
                        "action": result.action,
                        "tier": result.tier,
                        "approved": result.approved,
                        "reason": result.reason,
                        "requestor": requestor,
                        "circuit_breaker": result.circuit_breaker_tripped,
                        "context": context,
                    },
                )
        except Exception:
            pass  # Event logging must never break governance

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self):
        """Stop the periodic verification thread."""
        self._stop_event.set()
