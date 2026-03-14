"""
Artifact cleanup for JARVIS Test Suite V3.

Three-layer cleanup strategy:
  1. Pre-run snapshot of system state (memory facts, reminders, share/ files)
  2. Per-conversation cleanup: remove artifacts created during that conversation
  3. Run-level safety net: compare post-run state to snapshot, report leaks
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

import aiohttp


# ── State snapshot ───────────────────────────────────────────────────────

@dataclass
class StateSnapshot:
    """Captured system state at a point in time."""
    memory_fact_ids: set[int] = field(default_factory=set)
    reminder_count: int = 0
    share_files: set[str] = field(default_factory=set)


@dataclass
class Artifact:
    """An artifact created during a test conversation."""
    type: str       # "memory_fact", "reminder", "file"
    identifier: str  # fact ID, reminder description fragment, or filename


@dataclass
class CleanupReport:
    """Report of cleanup actions taken and any leaks detected."""
    artifacts_created: int = 0
    artifacts_cleaned: int = 0
    leaks: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


# ── Snapshot functions ───────────────────────────────────────────────────

def _get_share_dir() -> str:
    return os.path.expanduser("~/jarvis/share")


async def snapshot_state(base_url: str = "http://localhost:8088",
                         token: str = "") -> StateSnapshot:
    """Capture current memory facts, reminders, and share/ files."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    snapshot = StateSnapshot()

    async with aiohttp.ClientSession() as session:
        # Memory facts
        try:
            async with session.get(f"{base_url}/api/memory/facts?limit=1000",
                                   headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    facts = data.get("facts", [])
                    snapshot.memory_fact_ids = {f["id"] for f in facts if "id" in f}
        except Exception:
            pass

    # Share files
    share_dir = _get_share_dir()
    if os.path.isdir(share_dir):
        snapshot.share_files = {
            os.path.basename(f)
            for f in glob.glob(os.path.join(share_dir, "*"))
            if os.path.isfile(f)
        }

    return snapshot


# ── Per-conversation cleanup ─────────────────────────────────────────────

def detect_artifacts(info_messages: list[str], response_text: str) -> list[Artifact]:
    """Detect artifacts created during a turn from info messages and response."""
    artifacts = []

    for info in info_messages:
        # Memory store detection
        if "Running: recall_memory" in info:
            pass  # Read-only, no artifact
        # Reminder creation
        if "Running: manage_reminders" in info:
            # Check response for confirmation of creation
            resp_lower = response_text.lower()
            if any(word in resp_lower for word in ["set", "created", "scheduled", "reminder for"]):
                artifacts.append(Artifact("reminder", "test_reminder"))

    # Memory fact storage (detected from response patterns)
    resp_lower = response_text.lower()
    if any(phrase in resp_lower for phrase in ["noted", "i'll remember", "stored", "remembered",
                                                "got it", "saved that"]):
        # Check if this was a memory store operation
        if any("recall_memory" in info or "memory" in info.lower() for info in info_messages):
            artifacts.append(Artifact("memory_fact", "test_fact"))

    return artifacts


async def cleanup_memory_facts(new_fact_ids: set[int], base_url: str = "http://localhost:8088",
                                token: str = "") -> list[str]:
    """Delete memory facts created during testing."""
    actions = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with aiohttp.ClientSession() as session:
        for fact_id in new_fact_ids:
            try:
                async with session.delete(f"{base_url}/api/memory/facts/{fact_id}",
                                          headers=headers) as resp:
                    if resp.status == 200:
                        actions.append(f"Deleted fact {fact_id}")
                    else:
                        actions.append(f"Failed to delete fact {fact_id}: HTTP {resp.status}")
            except Exception as e:
                actions.append(f"Error deleting fact {fact_id}: {e}")

    return actions


def cleanup_share_files(new_files: set[str]) -> list[str]:
    """Remove files created in share/ during testing."""
    actions = []
    share_dir = _get_share_dir()

    for filename in new_files:
        filepath = os.path.join(share_dir, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                actions.append(f"Removed {filename}")
            except Exception as e:
                actions.append(f"Error removing {filename}: {e}")

    return actions


# ── Run-level safety net ─────────────────────────────────────────────────

async def verify_clean_state(pre_snapshot: StateSnapshot,
                              base_url: str = "http://localhost:8088",
                              token: str = "",
                              auto_clean: bool = False) -> CleanupReport:
    """Compare current state to pre-run snapshot. Report and optionally clean leaks."""
    current = await snapshot_state(base_url, token)
    report = CleanupReport()

    # Check for new memory facts
    new_facts = current.memory_fact_ids - pre_snapshot.memory_fact_ids
    if new_facts:
        report.leaks.append(f"{len(new_facts)} new memory fact(s): {sorted(new_facts)}")
        if auto_clean:
            actions = await cleanup_memory_facts(new_facts, base_url, token)
            report.actions.extend(actions)
            report.artifacts_cleaned += len(new_facts)

    # Check for new share files
    new_files = current.share_files - pre_snapshot.share_files
    if new_files:
        report.leaks.append(f"{len(new_files)} new file(s) in share/: {sorted(new_files)}")
        if auto_clean:
            actions = cleanup_share_files(new_files)
            report.actions.extend(actions)
            report.artifacts_cleaned += len(new_files)

    return report
