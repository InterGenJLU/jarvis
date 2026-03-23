"""Modification Engine — Safe self-modification with rollback.

The single gateway through which ALL autonomous changes flow.
Supports config changes (Phase 1), prompt changes (Phase 2),
and code changes (Phase 3). Each phase adds one safety layer
on top of the previous.

Architecture:
  Governance.approve() → this engine → validate → snapshot → apply → verify
  If verify fails → automatic rollback from snapshot

No change is applied without:
  1. Governance approval (confirmed via 2FA)
  2. Validation (schema, syntax, protected file check)
  3. Snapshot (rollback point created BEFORE the change)
  4. Verification (smoke test / import check after applying)
"""

import ast
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

from core.logger import get_logger

logger = get_logger(__name__)

_instance: Optional["ModificationEngine"] = None


def get_modification_engine(config=None):
    """Get or create the singleton ModificationEngine."""
    global _instance
    if _instance is None and config is not None:
        _instance = ModificationEngine(config)
    return _instance


# -----------------------------------------------------------------------
# Protected files — JARVIS can NEVER modify these
# -----------------------------------------------------------------------

PROTECTED_FILES = frozenset({
    "core/governance.py",
    "core/modification_engine.py",
    "core/event_logger.py",
    "core/trace_context.py",
    "governance/commandments.md",
    "governance/.governance_hash",
    "scripts/jarvis-approve",
    "scripts/jarvis-confirm",
    "scripts/jarvis-governance-setup",
    ".env",
    ".gitignore",
})

# -----------------------------------------------------------------------
# Whitelisted config keys — the ONLY config values JARVIS can modify
# -----------------------------------------------------------------------

WHITELISTED_CONFIG_KEYS = {
    # VAD tuning
    "vad.speech_frames_threshold": {"type": int, "min": 5, "max": 60},
    "vad.silence_frames_threshold": {"type": int, "min": 10, "max": 120},
    # LLM temperature
    "llm.local.temperature": {"type": float, "min": 0.1, "max": 1.5},
    "llm.local.top_p": {"type": float, "min": 0.1, "max": 1.0},
    "llm.local.top_k": {"type": int, "min": 1, "max": 100},
    "llm.small.temperature": {"type": float, "min": 0.1, "max": 1.5},
    "llm.small.max_tokens": {"type": int, "min": 50, "max": 2000},
    # TTS
    "tts.kokoro_speed": {"type": float, "min": 0.5, "max": 2.0},
    "tts.kokoro_blend_ratio": {"type": float, "min": 0.0, "max": 1.0},
    "tts.kokoro_pronunciations": {"type": dict},
    # Conversation window
    "conversation.follow_up_window.default_duration": {"type": float, "min": 2.0, "max": 15.0},
    "conversation.follow_up_window.extended_duration": {"type": float, "min": 4.0, "max": 30.0},
    "conversation.max_history_turns": {"type": int, "min": 5, "max": 100},
    # Speaker ID
    "user_profiles.similarity_threshold": {"type": float, "min": 0.1, "max": 0.9},
    # Wake word
    "wake_word.sensitivity": {"type": float, "min": 0.1, "max": 1.0},
}

# Maximum lines a single code change can touch (Phase 3)
MAX_CODE_CHANGE_LINES = 50
MAX_CODE_CHANGE_FILES = 3


class ModificationResult:
    """Result of a modification attempt."""

    def __init__(self, success: bool, change_type: str, detail: str,
                 rollback_id: str = None, error: str = None):
        self.success = success
        self.change_type = change_type
        self.detail = detail
        self.rollback_id = rollback_id
        self.error = error
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "change_type": self.change_type,
            "detail": self.detail,
            "rollback_id": self.rollback_id,
            "error": self.error,
            "timestamp": self.timestamp,
        }


class ModificationEngine:
    """Gateway for all autonomous self-modification."""

    def __init__(self, config):
        self.config = config
        self.project_root = Path(__file__).parent.parent
        self.config_path = self.project_root / "config.yaml"
        self.snapshot_dir = Path(config.get(
            "system.storage_path",
            "/mnt/storage/jarvis",
        )) / "data" / "config_snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._history: list[dict] = []
        self._max_snapshots = 20
        logger.info(
            "ModificationEngine initialized: %d whitelisted config keys, "
            "%d protected files, snapshots at %s",
            len(WHITELISTED_CONFIG_KEYS), len(PROTECTED_FILES), self.snapshot_dir,
        )

    # -------------------------------------------------------------------
    # Phase 1: Config modification
    # -------------------------------------------------------------------

    def modify_config(self, key: str, value, proposal_id: str = None) -> ModificationResult:
        """Modify a whitelisted config value with validation and rollback.

        Args:
            key: dot-separated config key (e.g., "vad.speech_frames_threshold")
            value: new value to set
            proposal_id: governance proposal ID for audit trail

        Returns:
            ModificationResult with success/failure and rollback ID
        """
        # Step 1: Validate the key is whitelisted
        if key not in WHITELISTED_CONFIG_KEYS:
            return ModificationResult(
                success=False, change_type="config",
                detail=f"Key '{key}' is not whitelisted for autonomous modification",
                error="key_not_whitelisted",
            )

        # Step 2: Validate the value
        schema = WHITELISTED_CONFIG_KEYS[key]
        validation = self._validate_config_value(key, value, schema)
        if validation:
            return ModificationResult(
                success=False, change_type="config",
                detail=f"Validation failed for '{key}': {validation}",
                error="validation_failed",
            )

        # Step 3: Read current config
        try:
            _yaml = YAML()
            _yaml.preserve_quotes = True
            with open(self.config_path, "r") as f:
                config_data = _yaml.load(f)
        except Exception as e:
            return ModificationResult(
                success=False, change_type="config",
                detail=f"Failed to read config: {e}",
                error="config_read_failed",
            )

        # Step 4: Get current value
        current_value = self._get_nested(config_data, key)

        # Step 5: Snapshot before changing
        snapshot_id = self._create_config_snapshot(config_data, key, current_value, value, proposal_id)

        # Step 6: Apply the change
        try:
            self._set_nested(config_data, key, value)
            _yaml_w = YAML()
            _yaml_w.preserve_quotes = True
            with open(self.config_path, "w") as f:
                _yaml_w.dump(config_data, f)
        except Exception as e:
            return ModificationResult(
                success=False, change_type="config",
                detail=f"Failed to write config: {e}",
                rollback_id=snapshot_id,
                error="config_write_failed",
            )

        # Step 7: Verify the change persisted
        try:
            with open(self.config_path, "r") as f:
                _yaml_v = YAML()
                verify_data = _yaml_v.load(f)
            verify_value = self._get_nested(verify_data, key)
            if verify_value != value:
                # Rollback
                self.rollback(snapshot_id)
                return ModificationResult(
                    success=False, change_type="config",
                    detail=f"Verification failed: expected {value}, got {verify_value}",
                    rollback_id=snapshot_id,
                    error="verification_failed",
                )
        except Exception as e:
            self.rollback(snapshot_id)
            return ModificationResult(
                success=False, change_type="config",
                detail=f"Verification read failed: {e}",
                rollback_id=snapshot_id,
                error="verification_read_failed",
            )

        # Step 8: Log the event
        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    event="config_modified",
                    category="self_assessment",
                    status="success",
                    message=f"Config changed: {key} = {current_value} → {value}",
                    metadata={
                        "key": key,
                        "old_value": str(current_value),
                        "new_value": str(value),
                        "proposal_id": proposal_id,
                        "snapshot_id": snapshot_id,
                    },
                )
        except Exception:
            pass

        result = ModificationResult(
            success=True, change_type="config",
            detail=f"{key}: {current_value} → {value}",
            rollback_id=snapshot_id,
        )

        self._history.append(result.to_dict())
        logger.info(
            "Config modified: %s = %s → %s (snapshot=%s, proposal=%s)",
            key, current_value, value, snapshot_id, proposal_id,
        )

        return result

    # -------------------------------------------------------------------
    # Rollback
    # -------------------------------------------------------------------

    def rollback(self, snapshot_id: str) -> ModificationResult:
        """Restore config from a snapshot (raw file copy — preserves comments)."""
        raw_path = self.snapshot_dir / f"{snapshot_id}.config.yaml"
        meta_path = self.snapshot_dir / f"{snapshot_id}.meta.json"

        if not raw_path.exists():
            return ModificationResult(
                success=False, change_type="rollback",
                detail=f"Snapshot {snapshot_id} not found",
                error="snapshot_not_found",
            )

        try:
            # Restore the raw config file (comments and formatting intact)
            shutil.copy2(raw_path, self.config_path)

            # Read metadata for logging
            meta = {}
            if meta_path.exists():
                with open(meta_path, "r") as f:
                    meta = json.load(f)

            # Log the rollback
            try:
                from core.event_logger import get_event_logger
                el = get_event_logger()
                if el:
                    el.emit(
                        event="config_rolled_back",
                        category="self_assessment",
                        status="success",
                        message=f"Config rolled back to snapshot {snapshot_id}",
                        metadata={
                            "snapshot_id": snapshot_id,
                            "key": meta.get("key"),
                            "restored_value": meta.get("old_value"),
                        },
                    )
            except Exception:
                pass

            logger.info("Config rolled back to snapshot %s", snapshot_id)
            return ModificationResult(
                success=True, change_type="rollback",
                detail=f"Restored from snapshot {snapshot_id}",
                rollback_id=snapshot_id,
            )

        except Exception as e:
            return ModificationResult(
                success=False, change_type="rollback",
                detail=f"Rollback failed: {e}",
                error="rollback_failed",
            )

    # -------------------------------------------------------------------
    # Validation helpers
    # -------------------------------------------------------------------

    def _validate_config_value(self, key: str, value, schema: dict) -> str | None:
        """Validate a config value against its schema. Returns error or None."""
        expected_type = schema.get("type")
        if expected_type and not isinstance(value, expected_type):
            # Allow int where float is expected
            if expected_type == float and isinstance(value, int):
                pass
            else:
                return f"Expected {expected_type.__name__}, got {type(value).__name__}"

        if "min" in schema and value < schema["min"]:
            return f"Value {value} below minimum {schema['min']}"
        if "max" in schema and value > schema["max"]:
            return f"Value {value} above maximum {schema['max']}"

        return None

    def _get_nested(self, data: dict, key: str):
        """Get a nested value by dot-separated key."""
        parts = key.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def _set_nested(self, data: dict, key: str, value):
        """Set a nested value by dot-separated key."""
        parts = key.split(".")
        current = data
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    # -------------------------------------------------------------------
    # Snapshot management
    # -------------------------------------------------------------------

    def _create_config_snapshot(self, config_data: dict, key: str,
                                old_value, new_value,
                                proposal_id: str = None) -> str:
        """Create a config snapshot before modification.

        Stores: raw config file (preserving comments/formatting) + metadata.
        """
        snapshot_id = f"snap_{int(time.time())}_{key.replace('.', '_')}"

        # Copy the raw config file (preserves comments and formatting)
        raw_path = self.snapshot_dir / f"{snapshot_id}.config.yaml"
        shutil.copy2(self.config_path, raw_path)

        # Store metadata separately as JSON
        meta_path = self.snapshot_dir / f"{snapshot_id}.meta.json"
        meta = {
            "snapshot_id": snapshot_id,
            "timestamp": time.time(),
            "key": key,
            "old_value": str(old_value),
            "new_value": str(new_value),
            "proposal_id": proposal_id,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        # Prune old snapshots
        self._prune_snapshots()

        logger.debug("Config snapshot created: %s", snapshot_id)
        return snapshot_id

    def _prune_snapshots(self):
        """Keep only the most recent N snapshots."""
        snapshots = sorted(self.snapshot_dir.glob("snap_*.config.yaml"),
                          key=lambda p: p.stat().st_mtime)
        while len(snapshots) > self._max_snapshots:
            old = snapshots.pop(0)
            old.unlink()
            # Also remove metadata file
            meta = old.with_suffix("").with_suffix(".meta.json")
            if meta.exists():
                meta.unlink()
            logger.debug("Pruned old snapshot: %s", old.name)

    # -------------------------------------------------------------------
    # Phase 2 placeholder: File modification with safety checks
    # -------------------------------------------------------------------

    def is_protected(self, filepath: str) -> bool:
        """Check if a file is on the protected list."""
        rel = os.path.relpath(filepath, self.project_root)
        return rel in PROTECTED_FILES

    def validate_python_syntax(self, code: str) -> str | None:
        """Validate Python syntax. Returns error or None."""
        try:
            ast.parse(code)
            return None
        except SyntaxError as e:
            return f"Syntax error at line {e.lineno}: {e.msg}"

    # -------------------------------------------------------------------
    # Query methods
    # -------------------------------------------------------------------

    def get_whitelisted_keys(self) -> dict:
        """Return the whitelist with current values."""
        try:
            _yaml = YAML()
            _yaml.preserve_quotes = True
            with open(self.config_path, "r") as f:
                config_data = _yaml.load(f)
        except Exception:
            config_data = {}

        result = {}
        for key, schema in WHITELISTED_CONFIG_KEYS.items():
            current = self._get_nested(config_data, key)
            result[key] = {
                "current_value": current,
                "type": schema["type"].__name__,
                "min": schema.get("min"),
                "max": schema.get("max"),
            }
        return result

    def get_snapshots(self, limit: int = 10) -> list[dict]:
        """Return recent snapshots."""
        snapshots = sorted(self.snapshot_dir.glob("snap_*.meta.json"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for s in snapshots[:limit]:
            try:
                with open(s, "r") as f:
                    data = json.load(f)
                result.append(data)
            except Exception:
                pass
        return result

    def get_history(self, limit: int = 20) -> list[dict]:
        """Return modification history."""
        return self._history[-limit:]
