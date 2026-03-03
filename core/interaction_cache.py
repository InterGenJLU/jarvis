"""Interaction Artifact Cache — unified cache for typed, addressable interaction data.

Phase 1: In-memory hot tier + SQLite warm tier. Every skill/tool/LLM response
can produce artifacts that are stored, referenced by ordinal or type, and
demoted to warm storage on conversation window close.

See docs/INTERACTION_ARTIFACT_CACHE_DESIGN.md for the full vision.
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from core.logger import get_logger


@dataclass
class Artifact:
    """A discrete, typed, addressable item produced during an interaction."""

    artifact_id: str            # uuid4().hex[:16]
    turn_id: int                # conversation turn number
    item_index: int             # position within turn (for ordinal addressing)
    artifact_type: str          # free string: "search_result_set", "synthesis", etc.
    content: str                # the actual data
    summary: str                # one-line TTS-friendly summary
    source: str                 # skill/tool that produced it
    provenance: dict = field(default_factory=dict)   # URL, query, tool args
    metadata: dict = field(default_factory=dict)     # flexible bag
    parent_id: Optional[str] = None   # for sub-items (Phase 3)
    user_id: str = "primary_user"
    window_id: str = ""
    tier: str = "hot"           # "hot" / "warm" / "cold"
    created_at: float = 0.0


def _serialize_artifact(art: Artifact) -> tuple:
    """Convert an Artifact to a SQLite INSERT tuple."""
    return (
        art.artifact_id,
        art.turn_id,
        art.item_index,
        art.artifact_type,
        art.content,
        art.summary,
        art.source,
        json.dumps(art.provenance),
        json.dumps(art.metadata),
        art.parent_id,
        art.user_id,
        art.window_id,
        art.tier,
        art.created_at,
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    """Convert a SQLite Row to an Artifact."""
    return Artifact(
        artifact_id=row["artifact_id"],
        turn_id=row["turn_id"],
        item_index=row["item_index"],
        artifact_type=row["artifact_type"],
        content=row["content"],
        summary=row["summary"],
        source=row["source"],
        provenance=json.loads(row["provenance"]) if row["provenance"] else {},
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        parent_id=row["parent_id"],
        user_id=row["user_id"],
        window_id=row["window_id"],
        tier=row["tier"],
        created_at=row["created_at"],
    )


class InteractionCache:
    """Unified cache for interaction artifacts.

    Hot tier: in-memory dict keyed by window_id for fast access during
    active conversation. All writes also persist to SQLite for crash safety.

    Warm tier: SQLite rows with tier='warm', queryable across sessions.
    """

    def __init__(self, config):
        storage_path = Path(config.get(
            "system.storage_path",
            "/mnt/storage/jarvis",
        ))
        self.db_path = storage_path / "data" / "interaction_cache.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db_lock = threading.Lock()
        self._hot: dict[str, list[Artifact]] = {}  # window_id → artifacts

        self.logger = get_logger(__name__, config)
        self._init_db()
        self.logger.info("InteractionCache initialized (db=%s)", self.db_path)

    def _init_db(self):
        """Create the artifacts table and indexes if they don't exist."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS artifacts (
                        artifact_id   TEXT PRIMARY KEY,
                        turn_id       INTEGER NOT NULL,
                        item_index    INTEGER NOT NULL DEFAULT 0,
                        artifact_type TEXT NOT NULL,
                        content       TEXT NOT NULL,
                        summary       TEXT NOT NULL DEFAULT '',
                        source        TEXT NOT NULL,
                        provenance    TEXT NOT NULL DEFAULT '{}',
                        metadata      TEXT NOT NULL DEFAULT '{}',
                        parent_id     TEXT,
                        user_id       TEXT NOT NULL DEFAULT 'user',
                        window_id     TEXT NOT NULL,
                        tier          TEXT NOT NULL DEFAULT 'hot',
                        created_at    REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_window
                    ON artifacts(window_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_window_turn
                    ON artifacts(window_id, turn_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_window_type
                    ON artifacts(window_id, artifact_type)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_tier
                    ON artifacts(tier)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_user
                    ON artifacts(user_id)
                """)
                conn.commit()
            finally:
                conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with row_factory set."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def store(self, artifact: Artifact) -> str:
        """Store an artifact in hot tier (in-memory) + SQLite (crash safety).

        Returns the artifact_id.
        """
        if not artifact.created_at:
            artifact.created_at = time.time()

        # Hot tier
        if artifact.window_id not in self._hot:
            self._hot[artifact.window_id] = []
        self._hot[artifact.window_id].append(artifact)

        # SQLite (crash safety)
        with self._db_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO artifacts
                       (artifact_id, turn_id, item_index, artifact_type,
                        content, summary, source, provenance, metadata,
                        parent_id, user_id, window_id, tier, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    _serialize_artifact(artifact),
                )
                conn.commit()
            except Exception as e:
                self.logger.warning("Failed to persist artifact %s: %s",
                                    artifact.artifact_id, e)
            finally:
                conn.close()

        self.logger.debug(
            "Stored artifact %s [%s] turn=%d idx=%d window=%s",
            artifact.artifact_id, artifact.artifact_type,
            artifact.turn_id, artifact.item_index, artifact.window_id,
        )
        return artifact.artifact_id

    def get_by_id(self, artifact_id: str) -> Optional[Artifact]:
        """Retrieve a single artifact by ID. Checks hot first, then SQLite."""
        # Hot tier scan
        for artifacts in self._hot.values():
            for art in artifacts:
                if art.artifact_id == artifact_id:
                    return art

        # SQLite fallback
        with self._db_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if row:
                    return _row_to_artifact(row)
            finally:
                conn.close()
        return None

    def get_by_turn(self, window_id: str, turn_id: int,
                    artifact_type: Optional[str] = None) -> list[Artifact]:
        """Get all artifacts for a given turn, optionally filtered by type."""
        # Hot tier first
        results = []
        for art in self._hot.get(window_id, []):
            if art.turn_id == turn_id:
                if artifact_type is None or art.artifact_type == artifact_type:
                    results.append(art)
        if results:
            return sorted(results, key=lambda a: a.item_index)

        # SQLite fallback
        with self._db_lock:
            conn = self._get_conn()
            try:
                if artifact_type:
                    rows = conn.execute(
                        """SELECT * FROM artifacts
                           WHERE window_id = ? AND turn_id = ?
                             AND artifact_type = ?
                           ORDER BY item_index""",
                        (window_id, turn_id, artifact_type),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM artifacts
                           WHERE window_id = ? AND turn_id = ?
                           ORDER BY item_index""",
                        (window_id, turn_id),
                    ).fetchall()
                return [_row_to_artifact(r) for r in rows]
            finally:
                conn.close()

    def get_latest(self, window_id: str,
                   artifact_type: Optional[str] = None,
                   source: Optional[str] = None) -> Optional[Artifact]:
        """Get the most recent artifact matching filters for a window."""
        # Hot tier (reverse scan for recency)
        hot_list = self._hot.get(window_id, [])
        for art in reversed(hot_list):
            if artifact_type and art.artifact_type != artifact_type:
                continue
            if source and art.source != source:
                continue
            return art

        # SQLite fallback
        with self._db_lock:
            conn = self._get_conn()
            try:
                conditions = ["window_id = ?"]
                params: list = [window_id]
                if artifact_type:
                    conditions.append("artifact_type = ?")
                    params.append(artifact_type)
                if source:
                    conditions.append("source = ?")
                    params.append(source)
                where = " AND ".join(conditions)
                row = conn.execute(
                    f"""SELECT * FROM artifacts
                        WHERE {where}
                        ORDER BY created_at DESC LIMIT 1""",
                    params,
                ).fetchone()
                if row:
                    return _row_to_artifact(row)
            finally:
                conn.close()
        return None

    def get_hot_artifacts(self, window_id: str) -> list[Artifact]:
        """Get all hot-tier artifacts for a window."""
        return list(self._hot.get(window_id, []))

    def find_by_keyword(self, window_id: str,
                        keyword: str) -> Optional[Artifact]:
        """Find the most recent artifact matching a keyword.

        Searches summary, provenance.query, and first 500 chars of content
        (case-insensitive). Checks hot tier first, then SQLite warm tier.
        """
        kw = keyword.lower()

        # Hot tier (reverse for most-recent-first)
        for art in reversed(self._hot.get(window_id, [])):
            if self._keyword_match(art, kw):
                return art

        # SQLite fallback (warm tier)
        with self._db_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT * FROM artifacts
                       WHERE window_id = ?
                       ORDER BY created_at DESC""",
                    (window_id,),
                ).fetchall()
                for row in rows:
                    art = _row_to_artifact(row)
                    if self._keyword_match(art, kw):
                        return art
            finally:
                conn.close()
        return None

    @staticmethod
    def _keyword_match(art: Artifact, kw: str) -> bool:
        """Check if an artifact matches a keyword (case-insensitive)."""
        if kw in art.summary.lower():
            return True
        query = art.provenance.get("query", "")
        if query and kw in query.lower():
            return True
        if kw in art.content[:500].lower():
            return True
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def demote_window(self, window_id: str):
        """Move all hot artifacts for a window to warm tier.

        Called on conversation window close. Updates tier in SQLite,
        removes from in-memory hot dict.
        """
        if not window_id:
            return

        # Remove from hot dict
        removed = self._hot.pop(window_id, [])

        # Update tier in SQLite
        if removed:
            with self._db_lock:
                conn = self._get_conn()
                try:
                    conn.execute(
                        """UPDATE artifacts SET tier = 'warm'
                           WHERE window_id = ? AND tier = 'hot'""",
                        (window_id,),
                    )
                    conn.commit()
                except Exception as e:
                    self.logger.warning("Failed to demote window %s: %s",
                                        window_id, e)
                finally:
                    conn.close()

            self.logger.info(
                "Demoted %d artifacts for window %s to warm tier",
                len(removed), window_id,
            )

    def ensure_window_id(self, conv_state) -> str:
        """Ensure conv_state has a window_id; generate one if empty.

        Used by web UI which lacks explicit window open/close.
        """
        if not conv_state.window_id:
            conv_state.window_id = uuid.uuid4().hex[:12]
            self.logger.debug("Generated window_id: %s", conv_state.window_id)
        return conv_state.window_id


# ----------------------------------------------------------------------
# Singleton factory
# ----------------------------------------------------------------------

_instance: Optional[InteractionCache] = None


def get_interaction_cache(config=None) -> Optional[InteractionCache]:
    """Get or create the singleton InteractionCache.

    Call with config on first invocation (from frontend init).
    Call with no args from other modules to retrieve existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        try:
            _instance = InteractionCache(config)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "InteractionCache init failed (non-fatal): %s", e,
            )
    return _instance
