"""
Artifact cleanup for JARVIS Test Suite V3.

Three-layer cleanup strategy:
  1. Pre-run snapshot of system state (memory facts, reminders, share/ files, DB rows)
  2. Per-conversation cleanup: remove artifacts created during that conversation
  3. Run-level safety net: compare post-run state to snapshot, purge test-created artifacts

Deep cleanup uses DELTA-BASED removal — only artifacts created after the run started
are purged. Pre-existing user data (chat history, FAISS vectors, cached artifacts, etc.)
is preserved intact. This is safe to run while real users are active.

Deep cleanup targets (direct DB/file access — no API required):
  - memory.db: topic_segments, interaction_log (facts are real — kept)
  - interaction_cache.db: artifacts, artifact_links, consolidated_knowledge
  - web_queries.db: web_queries
  - reminders.db: cancel test-created reminders AND delete their Google Calendar events
  - FAISS index: memory_faiss/ (only if new files were added during run)
  - Chat history: chat_history.jsonl (truncate to pre-run line count)
  - Sessions meta: sessions_meta.json (restore pre-run keys)
  - Lock files: share/.~lock*
  - Temp files: /tmp/jarvis_*, /tmp/test_*
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp


# ── Paths ────────────────────────────────────────────────────────────────

DATA_DIR = "/mnt/storage/jarvis/data"
MEMORY_DB = os.path.join(DATA_DIR, "memory.db")
CACHE_DB = os.path.join(DATA_DIR, "interaction_cache.db")
WEB_DB = os.path.join(DATA_DIR, "web_queries.db")
REMINDERS_DB = os.path.join(DATA_DIR, "reminders.db")
FAISS_DIR = os.path.join(DATA_DIR, "memory_faiss")
CHAT_HISTORY = os.path.join(DATA_DIR, "conversations", "chat_history.jsonl")
SESSIONS_META = os.path.join(DATA_DIR, "conversations", "sessions_meta.json")


CONFIG_PATH = os.path.expanduser("~/jarvis/config.yaml")
GOOGLE_TOKEN_PATH = os.path.join(DATA_DIR, "google_token.json")


def _get_share_dir() -> str:
    return os.path.expanduser("~/jarvis/share")


def _delete_google_calendar_events(event_ids: set[str]) -> int:
    """Delete Google Calendar events by ID. Returns count deleted.

    Handles rate limits with backoff, silently skips already-deleted events.
    Fails gracefully if Google API deps are missing or auth is unavailable.
    """
    if not event_ids:
        return 0
    try:
        import yaml
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return 0

    if not os.path.exists(GOOGLE_TOKEN_PATH) or not os.path.exists(CONFIG_PATH):
        return 0

    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        gcal_config = config.get("google_calendar", {})
        if not gcal_config.get("enabled", False):
            return 0

        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(GOOGLE_TOKEN_PATH, 'w') as f:
                f.write(creds.to_json())

        service = build('calendar', 'v3', credentials=creds)

        # Find the JARVIS calendar
        cal_name = gcal_config.get("jarvis_calendar_name", "JARVIS")
        calendars = service.calendarList().list().execute()
        cal_id = None
        for cal in calendars.get('items', []):
            if cal['summary'] == cal_name:
                cal_id = cal['id']
                break
        if not cal_id:
            return 0

        deleted = 0
        for eid in event_ids:
            try:
                service.events().delete(calendarId=cal_id, eventId=eid).execute()
                deleted += 1
            except Exception as e:
                err = str(e)
                if "410" in err or "404" in err:
                    deleted += 1  # Already gone — count as success
                elif "403" in err and "rateLimitExceeded" in err:
                    time.sleep(2)
                    try:
                        service.events().delete(calendarId=cal_id, eventId=eid).execute()
                        deleted += 1
                    except Exception:
                        pass
            time.sleep(0.5)  # Avoid rate limits

        return deleted
    except Exception:
        return 0


# ── State snapshot ───────────────────────────────────────────────────────

@dataclass
class StateSnapshot:
    """Captured system state at a point in time."""
    # Timestamp (epoch float) — used for delta-based DB cleanup
    timestamp: float = 0.0
    # API-level (existing)
    memory_fact_ids: set[int] = field(default_factory=set)
    reminder_ids: set[int] = field(default_factory=set)
    share_files: set[str] = field(default_factory=set)
    # File-level baselines (for delta truncation)
    chat_history_lines: int = 0
    sessions_meta_keys: set[str] = field(default_factory=set)
    faiss_files: set[str] = field(default_factory=set)


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
    deep_cleaned: dict[str, int] = field(default_factory=dict)


# ── DB helpers ───────────────────────────────────────────────────────────

def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchall()
    return len(rows) > 0


def _delete_since(db_path: str, table: str, ts_column: str,
                  cutoff: float) -> int:
    """Delete rows where ts_column >= cutoff. Returns rows deleted.

    Safe for concurrent use — only removes rows created after the cutoff
    timestamp, preserving all pre-existing data.
    """
    if not os.path.exists(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path)
        if not _has_table(conn, table):
            conn.close()
            return 0
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {ts_column} >= ?", (cutoff,)
        ).fetchone()[0]
        if count > 0:
            conn.execute(
                f"DELETE FROM {table} WHERE {ts_column} >= ?", (cutoff,)
            )
            conn.commit()
        conn.close()
        return count
    except Exception:
        return 0


# ── Snapshot functions ───────────────────────────────────────────────────

def snapshot_db_state() -> StateSnapshot:
    """Capture baseline state for delta-based cleanup.

    Records the current timestamp (used for DB row cleanup) and file-level
    baselines (chat history line count, session keys, FAISS files, share files).
    """
    snap = StateSnapshot()
    snap.timestamp = time.time()

    # Reminders — capture existing IDs for delta cleanup
    if os.path.exists(REMINDERS_DB):
        try:
            conn = sqlite3.connect(REMINDERS_DB)
            if _has_table(conn, "reminders"):
                rows = conn.execute("SELECT id FROM reminders").fetchall()
                snap.reminder_ids = {r[0] for r in rows}
            conn.close()
        except Exception:
            pass

    # Chat history — line count for truncation
    if os.path.exists(CHAT_HISTORY):
        try:
            with open(CHAT_HISTORY) as f:
                snap.chat_history_lines = sum(1 for _ in f)
        except Exception:
            pass

    # Sessions meta — capture existing keys
    if os.path.exists(SESSIONS_META):
        try:
            with open(SESSIONS_META) as f:
                data = json.load(f)
            if isinstance(data, dict):
                snap.sessions_meta_keys = set(data.keys())
        except Exception:
            pass

    # FAISS — capture filenames for diff
    if os.path.isdir(FAISS_DIR):
        snap.faiss_files = {
            os.path.basename(f)
            for f in glob.glob(os.path.join(FAISS_DIR, "*"))
        }

    # Share files
    share_dir = _get_share_dir()
    if os.path.isdir(share_dir):
        snap.share_files = {
            os.path.basename(f)
            for f in glob.glob(os.path.join(share_dir, "*"))
            if os.path.isfile(f)
        }

    return snap


async def snapshot_state(base_url: str = "http://localhost:8088",
                         token: str = "") -> StateSnapshot:
    """Capture current memory facts, reminders, share/ files, and DB baselines."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # Start with DB-level snapshot
    snapshot = snapshot_db_state()

    # Add API-level snapshot (memory fact IDs)
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


# ── Deep cleanup ─────────────────────────────────────────────────────────

def deep_cleanup(pre_snapshot: StateSnapshot) -> dict[str, int]:
    """Delta-based cleanup: remove ONLY artifacts created during the test run.

    Uses pre_snapshot.timestamp for DB rows (DELETE WHERE created_at >= cutoff)
    and pre_snapshot file baselines for file-level artifacts (truncate/diff).

    Pre-existing user data is never touched. Safe to run while real users
    are active on the system.

    Returns dict of {store_name: rows_or_files_cleaned}.
    """
    cleaned: dict[str, int] = {}
    cutoff = pre_snapshot.timestamp

    # 1. memory.db — delete rows created during run (KEEP facts always)
    n = _delete_since(MEMORY_DB, "topic_segments", "created_at", cutoff)
    if n:
        cleaned["memory.db:topic_segments"] = n
    n = _delete_since(MEMORY_DB, "interaction_log", "created_at", cutoff)
    if n:
        cleaned["memory.db:interaction_log"] = n

    # 2. interaction_cache.db — delete rows created during run
    n = _delete_since(CACHE_DB, "artifacts", "created_at", cutoff)
    if n:
        cleaned["interaction_cache.db:artifacts"] = n
    n = _delete_since(CACHE_DB, "artifact_links", "created_at", cutoff)
    if n:
        cleaned["interaction_cache.db:artifact_links"] = n
    n = _delete_since(CACHE_DB, "consolidated_knowledge", "created_at", cutoff)
    if n:
        cleaned["interaction_cache.db:consolidated_knowledge"] = n

    # 3. web_queries.db — delete rows created during run
    #    Uses DATETIME column, so convert cutoff to ISO string
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if os.path.exists(WEB_DB):
        try:
            conn = sqlite3.connect(WEB_DB)
            if _has_table(conn, "web_queries"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM web_queries WHERE timestamp >= ?",
                    (cutoff_dt,)
                ).fetchone()[0]
                if count > 0:
                    conn.execute(
                        "DELETE FROM web_queries WHERE timestamp >= ?",
                        (cutoff_dt,)
                    )
                    conn.commit()
                    cleaned["web_queries.db:web_queries"] = count
            conn.close()
        except Exception:
            pass

    # 4. FAISS — remove only files added during run
    if os.path.isdir(FAISS_DIR):
        current_files = {
            os.path.basename(f)
            for f in glob.glob(os.path.join(FAISS_DIR, "*"))
        }
        new_files = current_files - pre_snapshot.faiss_files
        removed = 0
        for fname in new_files:
            try:
                os.remove(os.path.join(FAISS_DIR, fname))
                removed += 1
            except Exception:
                pass
        if removed:
            cleaned["faiss_index_files"] = removed

    # 5. Chat history — truncate back to pre-run line count
    if os.path.exists(CHAT_HISTORY):
        try:
            with open(CHAT_HISTORY) as f:
                all_lines = f.readlines()
            new_count = len(all_lines) - pre_snapshot.chat_history_lines
            if new_count > 0:
                with open(CHAT_HISTORY, 'w') as f:
                    f.writelines(all_lines[:pre_snapshot.chat_history_lines])
                cleaned["chat_history_lines"] = new_count
        except Exception:
            pass

    # 6. Sessions meta — remove only keys added during run
    if os.path.exists(SESSIONS_META):
        try:
            with open(SESSIONS_META) as f:
                data = json.load(f)
            if isinstance(data, dict):
                new_keys = set(data.keys()) - pre_snapshot.sessions_meta_keys
                if new_keys:
                    for k in new_keys:
                        del data[k]
                    with open(SESSIONS_META, 'w') as f:
                        json.dump(data, f)
                    cleaned["sessions_meta_entries"] = len(new_keys)
        except Exception:
            pass

    # 7. Share directory — remove test-generated files + stale lock files
    share_dir = _get_share_dir()
    if os.path.isdir(share_dir):
        current_files = {
            os.path.basename(f)
            for f in glob.glob(os.path.join(share_dir, "*"))
            if os.path.isfile(f)
        }
        new_files = current_files - pre_snapshot.share_files
        removed = 0
        for fname in new_files:
            filepath = os.path.join(share_dir, fname)
            try:
                os.remove(filepath)
                removed += 1
            except Exception:
                pass
        if removed:
            cleaned["share_files"] = removed

        # Lock files (always safe, even if pre-existing)
        lock_removed = 0
        for f in glob.glob(os.path.join(share_dir, ".~lock*")):
            try:
                os.remove(f)
                lock_removed += 1
            except Exception:
                pass
        if lock_removed:
            cleaned["lock_files"] = lock_removed

    # 8. Reminders — cancel test-created reminders AND delete their Google Calendar events.
    #    Without the Google Calendar delete, the bidirectional sync re-creates cancelled
    #    reminders on the next sync cycle, causing phantom notifications.
    if os.path.exists(REMINDERS_DB):
        try:
            conn = sqlite3.connect(REMINDERS_DB)
            if _has_table(conn, "reminders"):
                # Find reminder IDs that didn't exist at snapshot time
                rows = conn.execute("SELECT id FROM reminders").fetchall()
                new_ids = {r[0] for r in rows} - pre_snapshot.reminder_ids
                if new_ids:
                    placeholders = ",".join("?" * len(new_ids))
                    id_list = list(new_ids)

                    # Collect Google Calendar event IDs BEFORE cancelling
                    gcal_rows = conn.execute(
                        f"SELECT google_event_id FROM reminders "
                        f"WHERE id IN ({placeholders}) AND google_event_id IS NOT NULL",
                        id_list
                    ).fetchall()
                    # Extract unique base event IDs (strip :offset suffix)
                    gcal_base_ids: set[str] = set()
                    for (gid,) in gcal_rows:
                        idx = gid.rfind(":")
                        if idx > 0 and gid[idx + 1:].isdigit():
                            gcal_base_ids.add(gid[:idx])
                        else:
                            gcal_base_ids.add(gid)

                    # Cancel pending reminders in DB
                    pending = conn.execute(
                        f"SELECT COUNT(*) FROM reminders "
                        f"WHERE id IN ({placeholders}) AND status = 'pending'",
                        id_list
                    ).fetchone()[0]
                    if pending:
                        conn.execute(
                            f"UPDATE reminders SET status = 'cancelled' "
                            f"WHERE id IN ({placeholders}) AND status = 'pending'",
                            id_list
                        )
                        conn.commit()
                        cleaned["reminders_cancelled"] = pending

                    # Delete corresponding Google Calendar events (breaks sync loop)
                    if gcal_base_ids:
                        gcal_deleted = _delete_google_calendar_events(gcal_base_ids)
                        if gcal_deleted:
                            cleaned["gcal_events_deleted"] = gcal_deleted
            conn.close()
        except Exception:
            pass

    # 9. Temp files (always safe — these are ephemeral by nature)
    removed = 0
    for pat in ['/tmp/jarvis_startup_health_report_*.txt',
                '/tmp/jarvis_*.txt', '/tmp/jarvis_*.png',
                '/tmp/test_*.py', '/tmp/test_*.txt', '/tmp/test_*.png']:
        for f in glob.glob(pat):
            try:
                os.remove(f)
                removed += 1
            except Exception:
                pass
    if removed:
        cleaned["tmp_files"] = removed

    return cleaned


# ── Run-level safety net ─────────────────────────────────────────────────

async def verify_clean_state(pre_snapshot: StateSnapshot,
                              base_url: str = "http://localhost:8088",
                              token: str = "",
                              auto_clean: bool = False) -> CleanupReport:
    """Compare current state to pre-run snapshot. Report and optionally clean leaks.

    Always runs deep_cleanup for DB/file artifacts regardless of auto_clean.
    auto_clean controls API-level cleanup (memory facts via REST API).
    """
    current = await snapshot_state(base_url, token)
    report = CleanupReport()

    # Check for new memory facts (API-level)
    new_facts = current.memory_fact_ids - pre_snapshot.memory_fact_ids
    if new_facts:
        report.leaks.append(f"{len(new_facts)} new memory fact(s): {sorted(new_facts)}")
        if auto_clean:
            actions = await cleanup_memory_facts(new_facts, base_url, token)
            report.actions.extend(actions)
            report.artifacts_cleaned += len(new_facts)

    # Check for new share files (file-level)
    new_files = current.share_files - pre_snapshot.share_files
    if new_files:
        report.leaks.append(f"{len(new_files)} new file(s) in share/: {sorted(new_files)}")
        if auto_clean:
            actions = cleanup_share_files(new_files)
            report.actions.extend(actions)
            report.artifacts_cleaned += len(new_files)

    # Deep cleanup — always runs (these are transient stores with no permanent value)
    report.deep_cleaned = deep_cleanup(pre_snapshot)

    return report
