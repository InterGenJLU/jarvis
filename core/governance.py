"""JARVIS Governance Module — constitutional enforcement for self-evolution.

Enforces the Ten Commandments in code. Every autonomous action must pass
through governance.check() before execution. Fail-closed by default.

Hash-verified at startup and periodically at runtime. If the governance
module has been tampered with, all autonomous operations halt immediately.

Singleton access via get_governance(config).
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
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
        self._proposals: dict[str, dict] = {}  # proposal_id -> proposal
        self._denial_log: dict[str, list[float]] = {}  # action -> denial timestamps
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
    # Approval Queue — proposals requiring owner authorization
    # ------------------------------------------------------------------

    def propose(self, action: str, *, description: str,
                diff: str = None, justification: str = None,
                risk_tier: int = None, rollback_plan: str = None,
                context: dict = None, requestor: str = "jarvis") -> str:
        """Submit a proposal for owner approval.

        Creates a structured proposal in the queue. The owner reviews it
        via the web dashboard, then completes approval via console password.

        Returns the proposal ID.
        """
        tier = risk_tier if risk_tier is not None else ACTION_TIERS.get(action, Tier.LOGIC)

        proposal_id = secrets.token_hex(8)
        confirmation_code = self._generate_confirmation_code()

        proposal = {
            "id": proposal_id,
            "action": action,
            "tier": int(tier),
            "description": description,
            "diff": diff,
            "justification": justification,
            "rollback_plan": rollback_plan,
            "context": context,
            "requestor": requestor,
            "status": "pending",
            "confirmation_code": confirmation_code,
            "created_at": time.time(),
            "expires_at": time.time() + self._proposal_ttl(tier),
            "reviewed_at": None,
            "review_decision": None,
            "review_comment": None,
            "confirmed_at": None,
        }

        with self._lock:
            self._proposals[proposal_id] = proposal

        logger.info("Proposal queued: %s — %s (tier %d)", proposal_id, action, tier)

        self._emit_proposal_event(proposal, "proposal_queued")
        return proposal_id

    def get_proposals(self, status: str = None) -> list[dict]:
        """Get proposals, optionally filtered by status.

        Returns proposals with confirmation codes REDACTED (those only
        appear after web review, displayed on screen for console entry).
        """
        now = time.time()
        with self._lock:
            # Expire old proposals
            for pid, p in list(self._proposals.items()):
                if p["status"] == "pending" and now > p["expires_at"]:
                    p["status"] = "expired"
                    self._emit_proposal_event(p, "proposal_expired")

            results = []
            for p in self._proposals.values():
                if status and p["status"] != status:
                    continue
                # Redact confirmation code — only shown after web review
                safe = dict(p)
                safe.pop("confirmation_code", None)
                results.append(safe)

        return sorted(results, key=lambda x: x["created_at"], reverse=True)

    def get_proposal(self, proposal_id: str) -> dict | None:
        """Get a single proposal by ID (code redacted)."""
        with self._lock:
            p = self._proposals.get(proposal_id)
            if not p:
                return None
            safe = dict(p)
            safe.pop("confirmation_code", None)
            return safe

    def review_proposal(self, proposal_id: str, decision: str,
                         comment: str = None) -> dict | None:
        """Owner reviews a proposal via web UI.

        decision: 'approve', 'reject', 'defer', 'edit'
        Returns the proposal with confirmation code VISIBLE (for console entry)
        if approved, or None if not found.
        """
        valid_decisions = {"approve", "reject", "defer", "edit"}
        if decision not in valid_decisions:
            return None

        with self._lock:
            p = self._proposals.get(proposal_id)
            if not p or p["status"] != "pending":
                return None

            p["reviewed_at"] = time.time()
            p["review_decision"] = decision
            p["review_comment"] = comment

            if decision == "reject":
                p["status"] = "rejected"
                self._emit_proposal_event(p, "proposal_rejected")
                self._record_denial(p["action"])
                safe = dict(p)
                safe.pop("confirmation_code", None)
                return safe

            if decision == "defer":
                p["status"] = "deferred"
                self._emit_proposal_event(p, "proposal_deferred")
                safe = dict(p)
                safe.pop("confirmation_code", None)
                return safe

            if decision == "edit":
                p["status"] = "editing"
                self._emit_proposal_event(p, "proposal_editing")
                safe = dict(p)
                safe.pop("confirmation_code", None)
                return safe

            # Approved — return WITH confirmation code for console entry
            p["status"] = "awaiting_confirmation"
            self._emit_proposal_event(p, "proposal_approved_awaiting_confirmation")

            # Regenerate code and set a short expiry for the confirmation step
            p["confirmation_code"] = self._generate_confirmation_code()
            p["confirmation_expires"] = time.time() + 300  # 5 minutes to enter code

            return dict(p)  # Includes confirmation_code

    def confirm_proposal(self, proposal_id: str, confirmation_code: str,
                          password: str) -> GovernanceResult:
        """Owner confirms a proposal via console with code + password.

        This is the out-of-band second factor. The confirmation code was
        displayed on the web UI after review. The password is entered in
        the console. Both must match.

        Returns GovernanceResult with approved=True if confirmed.
        """
        with self._lock:
            p = self._proposals.get(proposal_id)
            if not p:
                return GovernanceResult(
                    approved=False, reason="proposal_not_found",
                    tier=0, action="confirm_proposal")

            if p["status"] != "awaiting_confirmation":
                return GovernanceResult(
                    approved=False, reason=f"proposal_status_{p['status']}",
                    tier=p["tier"], action=p["action"])

            # Check confirmation code expiry
            if time.time() > p.get("confirmation_expires", 0):
                p["status"] = "confirmation_expired"
                self._emit_proposal_event(p, "proposal_confirmation_expired")
                return GovernanceResult(
                    approved=False, reason="confirmation_expired",
                    tier=p["tier"], action=p["action"])

            # Verify confirmation code (constant-time comparison)
            if not hmac.compare_digest(confirmation_code, p["confirmation_code"]):
                self._emit_proposal_event(p, "proposal_bad_confirmation_code")
                return GovernanceResult(
                    approved=False, reason="invalid_confirmation_code",
                    tier=p["tier"], action=p["action"])

            # Verify governance password
            if not self._verify_password(password):
                self._emit_proposal_event(p, "proposal_bad_password")
                return GovernanceResult(
                    approved=False, reason="invalid_password",
                    tier=p["tier"], action=p["action"])

            # Both factors verified — approve
            p["status"] = "confirmed"
            p["confirmed_at"] = time.time()
            self._emit_proposal_event(p, "proposal_confirmed")

            logger.info("Proposal CONFIRMED: %s — %s", proposal_id, p["action"])

            return GovernanceResult(
                approved=True,
                reason="owner_confirmed",
                tier=p["tier"],
                action=p["action"],
            )

    # ------------------------------------------------------------------
    # Approval helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_confirmation_code() -> str:
        """Generate a short, human-typeable confirmation code."""
        # 6 alphanumeric characters, uppercase for readability
        return secrets.token_hex(3).upper()

    def _proposal_ttl(self, tier: int) -> float:
        """Time-to-live for a proposal based on tier (seconds)."""
        ttls = {
            Tier.CONFIG: 86400,       # 24 hours
            Tier.PROMPT: 86400,       # 24 hours
            Tier.LOGIC: 43200,        # 12 hours
            Tier.ARCHITECTURE: 21600, # 6 hours
        }
        return ttls.get(tier, 43200)

    def _record_denial(self, action: str):
        """Record a denial for escalating cooldown tracking."""
        with self._lock:
            if not hasattr(self, '_denial_log'):
                self._denial_log = {}
            now = time.time()
            if action not in self._denial_log:
                self._denial_log[action] = []
            self._denial_log[action].append(now)
            # Prune old denials (keep last 7 days)
            cutoff = now - 604800
            self._denial_log[action] = [
                t for t in self._denial_log[action] if t > cutoff]

    def get_denial_count(self, action: str, hours: float = 24) -> int:
        """How many times an action has been denied recently."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            denials = getattr(self, '_denial_log', {}).get(action, [])
            return sum(1 for t in denials if t > cutoff)

    def _verify_password(self, password: str) -> bool:
        """Verify the governance password.

        The password hash is stored in the governance directory.
        If no password is set, this returns False (fail-closed).
        """
        pw_hash_path = self._hash_path.parent / ".governance_pw"
        if not pw_hash_path.exists():
            logger.warning("No governance password set — confirm_proposal will fail")
            return False

        try:
            stored_hash = pw_hash_path.read_text().strip()
            computed = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return hmac.compare_digest(computed, stored_hash)
        except Exception as e:
            logger.error("Password verification error: %s", e)
            return False

    def set_password(self, password: str) -> bool:
        """Set the governance password. Called once during initial setup.

        Stores SHA-256 hash in the governance directory.
        """
        try:
            pw_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            pw_path = self._hash_path.parent / ".governance_pw"
            pw_path.write_text(pw_hash)
            logger.info("Governance password set")
            return True
        except Exception as e:
            logger.error("Failed to set governance password: %s", e)
            return False

    def _emit_proposal_event(self, proposal: dict, event_name: str):
        """Log proposal lifecycle events."""
        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    category="decision",
                    event=event_name,
                    message=f"{proposal['action']} (tier {proposal['tier']}): "
                            f"{proposal.get('description', '')[:100]}",
                    severity="info",
                    source="governance",
                    metadata={
                        "proposal_id": proposal["id"],
                        "action": proposal["action"],
                        "tier": proposal["tier"],
                        "status": proposal["status"],
                    },
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self):
        """Stop the periodic verification thread."""
        self._stop_event.set()
