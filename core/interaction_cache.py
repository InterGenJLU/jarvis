"""Interaction Artifact Cache — unified cache for typed, addressable interaction data.

Phase 1: In-memory hot tier + SQLite warm tier. Every skill/tool/LLM response
can produce artifacts that are stored, referenced by ordinal or type, and
demoted to warm storage on conversation window close.

See docs/INTERACTION_ARTIFACT_CACHE_DESIGN.md for the full vision.
"""

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
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
    importance_score: float = 1.0  # engagement accumulator (CMA selective retention)
    last_accessed_at: float = 0.0  # timestamp of most recent record_access()
    access_count: int = 0          # total record_access() calls


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
        art.importance_score,
        art.last_accessed_at,
        art.access_count,
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    """Convert a SQLite Row to an Artifact."""
    keys = row.keys() if hasattr(row, "keys") else []
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
        importance_score=row["importance_score"] if "importance_score" in keys else 1.0,
        last_accessed_at=row["last_accessed_at"] if "last_accessed_at" in keys else 0.0,
        access_count=row["access_count"] if "access_count" in keys else 0,
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
        self._hot_lock = threading.Lock()  # protects _hot dict mutations/iterations
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
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_parent
                    ON artifacts(parent_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_tier_window
                    ON artifacts(tier, window_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_cold_user_date
                    ON artifacts(tier, user_id, created_at DESC)
                """)
                # Migration: add importance_score column if missing
                try:
                    conn.execute("SELECT importance_score FROM artifacts LIMIT 1")
                except sqlite3.OperationalError:
                    conn.execute(
                        "ALTER TABLE artifacts "
                        "ADD COLUMN importance_score REAL NOT NULL DEFAULT 1.0"
                    )
                    self.logger.info(
                        "Migrated artifacts table: added importance_score column"
                    )
                # Migration: add retrieval-driven mutation columns
                try:
                    conn.execute(
                        "SELECT last_accessed_at FROM artifacts LIMIT 1"
                    )
                except sqlite3.OperationalError:
                    conn.execute(
                        "ALTER TABLE artifacts "
                        "ADD COLUMN last_accessed_at REAL DEFAULT 0.0"
                    )
                    conn.execute(
                        "ALTER TABLE artifacts "
                        "ADD COLUMN access_count INTEGER DEFAULT 0"
                    )
                    self.logger.info(
                        "Migrated artifacts table: added last_accessed_at, "
                        "access_count columns"
                    )
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_artifacts_cold_importance
                    ON artifacts(tier, user_id, importance_score DESC, created_at DESC)
                """)
                # Associative linking — graph edges between artifacts
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS artifact_links (
                        source_id   TEXT NOT NULL,
                        target_id   TEXT NOT NULL,
                        link_type   TEXT NOT NULL,
                        strength    REAL NOT NULL DEFAULT 1.0,
                        created_at  REAL NOT NULL,
                        PRIMARY KEY (source_id, target_id, link_type)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_links_source
                    ON artifact_links(source_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_links_target
                    ON artifact_links(target_id)
                """)

                # -- Consolidated knowledge (CMA consolidation & abstraction) --
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS consolidated_knowledge (
                        knowledge_id    TEXT PRIMARY KEY,
                        user_id         TEXT NOT NULL DEFAULT 'user',
                        pattern_type    TEXT NOT NULL,
                        content         TEXT NOT NULL,
                        evidence_count  INTEGER NOT NULL DEFAULT 1,
                        evidence_ids    TEXT NOT NULL DEFAULT '[]',
                        first_seen      REAL NOT NULL,
                        last_seen       REAL NOT NULL,
                        confidence      REAL NOT NULL DEFAULT 0.5,
                        promoted        INTEGER NOT NULL DEFAULT 0,
                        superseded_by   TEXT,
                        created_at      REAL NOT NULL,
                        updated_at      REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ck_user
                    ON consolidated_knowledge(user_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ck_type
                    ON consolidated_knowledge(user_id, pattern_type)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ck_promoted
                    ON consolidated_knowledge(promoted)
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

        self.logger.debug("store: type=%s id=%s window=%s%s",
                          artifact.artifact_type, artifact.artifact_id,
                          artifact.window_id,
                          f" image={artifact.metadata.get('image_path')}" if artifact.metadata and artifact.metadata.get('image_path') else "")

        # Hot tier
        with self._hot_lock:
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
                        parent_id, user_id, window_id, tier, created_at,
                        importance_score, last_accessed_at, access_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

        # Auto-link to co-occurring artifacts in the same turn
        if artifact.window_id and artifact.turn_id is not None:
            self._auto_link_co_occurrence(artifact)

        # Evict lowest-scoring hot artifacts if over capacity
        self._evict_hot_if_needed()

        return artifact.artifact_id

    def _evict_hot_if_needed(self):
        """Evict lowest effective_score hot artifacts when over _MAX_HOT_ARTIFACTS."""
        with self._hot_lock:
            total = sum(len(arts) for arts in self._hot.values())
            if total <= self._MAX_HOT_ARTIFACTS:
                return

            # Collect all hot artifacts with effective scores
            now = time.time()
            all_hot = []
            for wid, arts in self._hot.items():
                for art in arts:
                    all_hot.append((self.effective_score(art, now), wid, art))

            # Sort by effective score ascending — evict lowest
            all_hot.sort(key=lambda x: x[0])
            to_evict = total - self._MAX_HOT_ARTIFACTS
            for _, wid, art in all_hot[:to_evict]:
                self._hot[wid].remove(art)
                if not self._hot[wid]:
                    del self._hot[wid]

        self.logger.debug("Evicted %d hot artifacts (cap=%d)",
                          to_evict, self._MAX_HOT_ARTIFACTS)

    # ------------------------------------------------------------------
    # Importance scoring + retrieval-driven mutation (CMA)
    # ------------------------------------------------------------------

    # Time decay half-life: artifact loses half its effective score
    # every N days without access. 7 days balances responsiveness
    # with stability — weekly-unused artifacts fade, daily-used persist.
    HALF_LIFE_DAYS = 7.0

    # Hard cap on importance_score to prevent unbounded parent bubble.
    _MAX_IMPORTANCE_SCORE = 100.0

    # Maximum hot-tier artifacts across all windows before LRU eviction.
    _MAX_HOT_ARTIFACTS = 200

    # Frequency weight: how much repeated recall compounds.
    # 0.15 * log2(1 + count) gives gentle boost:
    #   1 access → +15%, 3 → +30%, 7 → +45%, 15 → +60%
    FREQUENCY_WEIGHT = 0.15

    @classmethod
    def effective_score(cls, art: 'Artifact', now: float = None) -> float:
        """Compute time-decayed, frequency-boosted effective score.

        Used for cross-session retrieval ranking (search_cold). Combines:
        - Base importance (cumulative engagement weight)
        - Recency decay (exponential, 7-day half-life from last access)
        - Frequency boost (log-scaled access count)
        """
        if now is None:
            now = time.time()
        last_touch = art.last_accessed_at or art.created_at
        age_days = max(0.0, (now - last_touch) / 86400.0)
        decay = 0.5 ** (age_days / cls.HALF_LIFE_DAYS)
        freq_boost = 1.0 + cls.FREQUENCY_WEIGHT * math.log2(
            1 + art.access_count
        )
        return art.importance_score * decay * freq_boost

    ACCESS_WEIGHTS = {
        # Cross-session recall — strongest signal
        "rehydrate": 5.0,
        # Direct reference — user explicitly pointed at this artifact
        "ordinal_reference": 3.0,
        "nav_jump": 3.0,
        "nav_section_drill": 3.0,
        "generic_followup": 2.5,
        "type_reference": 2.0,
        "readback_recall": 2.0,
        "readback_section": 2.0,
        "recency_reference": 1.5,
        "readback_repeat": 1.5,
        # Sequential engagement
        "nav_advance": 1.0,
        "nav_retreat": 1.0,
        "readback_continue": 1.0,
        # Structural navigation
        "nav_reset": 0.5,
        "nav_drill_out": 0.5,
    }

    # Associative link strengths by relationship type
    LINK_STRENGTHS = {
        "co_occurrence": 1.0,       # same (window, turn) — automatic
        "rehydrated_with": 1.5,     # recalled together across sessions
        "user_associated": 2.0,     # explicit user/LLM association (future)
    }

    def record_access(self, artifact_id: str, access_type: str):
        """Increment importance score for an artifact on user interaction.

        Also updates last_accessed_at and access_count for retrieval-driven
        mutation (time decay + recall frequency tracking).

        Bubbles half the weight to the parent artifact (if any),
        so top-level artifacts accumulate engagement from sub-item nav.
        """
        weight = self.ACCESS_WEIGHTS.get(access_type, 1.0)
        now = time.time()

        # Update in-memory hot tier
        target_art = None
        with self._hot_lock:
            for artifacts in self._hot.values():
                for art in artifacts:
                    if art.artifact_id == artifact_id:
                        art.importance_score = min(
                            art.importance_score + weight,
                            self._MAX_IMPORTANCE_SCORE,
                        )
                        art.last_accessed_at = now
                        art.access_count += 1
                        target_art = art
                        break
                if target_art:
                    break

        # Update SQLite
        with self._db_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE artifacts "
                    "SET importance_score = MIN(importance_score + ?, ?), "
                    "    last_accessed_at = ?, "
                    "    access_count = access_count + 1 "
                    "WHERE artifact_id = ?",
                    (weight, self._MAX_IMPORTANCE_SCORE, now, artifact_id),
                )
                # Bubble to parent — sub-item engagement reflects on parent
                if target_art and target_art.parent_id:
                    half = weight * 0.5
                    conn.execute(
                        "UPDATE artifacts "
                        "SET importance_score = MIN(importance_score + ?, ?), "
                        "    last_accessed_at = ?, "
                        "    access_count = access_count + 1 "
                        "WHERE artifact_id = ?",
                        (half, self._MAX_IMPORTANCE_SCORE, now,
                         target_art.parent_id),
                    )
                    # Hot tier parent too
                    with self._hot_lock:
                        for artifacts in self._hot.values():
                            for art in artifacts:
                                if art.artifact_id == target_art.parent_id:
                                    art.importance_score = min(
                                        art.importance_score + half,
                                        self._MAX_IMPORTANCE_SCORE,
                                    )
                                    art.last_accessed_at = now
                                    art.access_count += 1
                                    break
                conn.commit()
            except Exception as e:
                self.logger.warning("record_access failed for %s: %s",
                                    artifact_id, e)
            finally:
                conn.close()

        self.logger.debug("Recorded access: %s +%.1f (%s) count=%d",
                          artifact_id, weight, access_type,
                          target_art.access_count if target_art else 0)

    # ------------------------------------------------------------------
    # Associative linking (CMA associative routing)
    # ------------------------------------------------------------------

    def create_link(self, id_a: str, id_b: str,
                    link_type: str = "co_occurrence") -> bool:
        """Create an undirected link between two artifacts.

        Normalizes IDs (smaller = source) to prevent duplicate edges.
        Returns True if a new link was created, False if it already existed
        or the IDs are identical.
        """
        if id_a == id_b:
            return False

        source_id, target_id = sorted([id_a, id_b])
        strength = self.LINK_STRENGTHS.get(link_type, 1.0)
        now = time.time()

        with self._db_lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO artifact_links
                       (source_id, target_id, link_type, strength, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (source_id, target_id, link_type, strength, now),
                )
                conn.commit()
                created = cursor.rowcount > 0
            except Exception as e:
                self.logger.warning("create_link failed %s<->%s: %s",
                                    id_a, id_b, e)
                return False
            finally:
                conn.close()

        if created:
            self.logger.debug("Linked %s <-> %s [%s, %.1f]",
                              source_id, target_id, link_type, strength)
        return created

    def get_linked(self, artifact_id: str,
                   link_type: str = None) -> list[Artifact]:
        """Retrieve all artifacts linked to the given artifact ID.

        Returns Artifact objects (from any tier). Optionally filter by
        link_type. Results ordered by link strength DESC.
        """
        with self._db_lock:
            conn = self._get_conn()
            try:
                if link_type:
                    rows = conn.execute("""
                        SELECT a.* FROM artifacts a
                        JOIN artifact_links l ON (
                            (l.source_id = ? AND l.target_id = a.artifact_id)
                            OR
                            (l.target_id = ? AND l.source_id = a.artifact_id)
                        )
                        WHERE l.link_type = ?
                        ORDER BY l.strength DESC, l.created_at DESC
                    """, (artifact_id, artifact_id, link_type)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT a.* FROM artifacts a
                        JOIN artifact_links l ON (
                            (l.source_id = ? AND l.target_id = a.artifact_id)
                            OR
                            (l.target_id = ? AND l.source_id = a.artifact_id)
                        )
                        ORDER BY l.strength DESC, l.created_at DESC
                    """, (artifact_id, artifact_id)).fetchall()
                return [_row_to_artifact(r) for r in rows]
            except Exception as e:
                self.logger.warning("get_linked failed for %s: %s",
                                    artifact_id, e)
                return []
            finally:
                conn.close()

    def get_link_count(self, artifact_id: str) -> int:
        """Count the number of links for an artifact (any direction)."""
        with self._db_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT COUNT(*) FROM artifact_links
                       WHERE source_id = ? OR target_id = ?""",
                    (artifact_id, artifact_id),
                ).fetchone()
                return row[0] if row else 0
            except Exception:
                return 0
            finally:
                conn.close()

    def _auto_link_co_occurrence(self, new_artifact: Artifact):
        """Link a newly stored artifact to others in the same turn.

        Only links top-level artifacts (skips sub-items) to avoid noisy
        edges between a parent and its own decomposed children.
        """
        if new_artifact.parent_id:
            return

        siblings = []
        for art in self._hot.get(new_artifact.window_id, []):
            if (art.artifact_id != new_artifact.artifact_id
                    and art.turn_id == new_artifact.turn_id
                    and art.parent_id is None):
                siblings.append(art.artifact_id)

        for sib_id in siblings:
            self.create_link(new_artifact.artifact_id, sib_id,
                             "co_occurrence")

    def _auto_link_rehydrated(self, rehydrated: list[Artifact]):
        """Link all pairs of co-rehydrated artifacts.

        Uses the original cold-tier IDs (from provenance.rehydrated_from)
        since clones are ephemeral. The durable link is between the
        persistent cold-tier originals.
        """
        original_ids = []
        for art in rehydrated:
            orig_id = art.provenance.get("rehydrated_from")
            if orig_id:
                original_ids.append(orig_id)

        for i in range(len(original_ids)):
            for j in range(i + 1, len(original_ids)):
                self.create_link(original_ids[i], original_ids[j],
                                 "rehydrated_with")

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
    # Sub-item decomposition (Phase 3)
    # ------------------------------------------------------------------

    # Regex patterns for structured content extraction
    # Section headers: **Bold Headers** or ### Markdown Headers on their own line
    # Uses [ \t]* (not \s*) to avoid consuming newlines needed as next-match anchors
    _SECTION_HEADER = re.compile(
        r'(?:^|\n)\*\*([^*\n]+)\*\*[ \t]*\n|(?:^|\n)(#{1,3})\s+(.+?)\n',
    )
    # Numbered steps — tolerates bold wrapping: "1. Text" or "**1.** Text" or "**1. Text**"
    _NUMBERED_STEP = re.compile(
        r'(?:^|\n)\*{0,2}(\d+)[\.\)]\*{0,2}\s+(.*?)(?=\n\*{0,2}\d+[\.\)]\*{0,2}\s|\Z)',
        re.DOTALL,
    )
    _BULLET_ITEM = re.compile(
        r'(?:^|\n)[-*]\s+(.*?)(?=\n[-*]\s|\n\n|\Z)', re.DOTALL,
    )
    _MD_SECTION = re.compile(
        r'(?:^|\n)(#{1,3})\s+(.+?)\n(.*?)(?=\n#{1,3}\s|\Z)', re.DOTALL,
    )

    # Minimum content size for decomposition (skip short prose)
    _MIN_DECOMPOSE_CHARS = 200
    _MIN_DECOMPOSE_NEWLINES = 3
    _MAX_DECOMPOSE_DEPTH = 5    # prevent unbounded recursive decomposition
    _MAX_CHILDREN_PER_ROOT = 200

    # Bold-wrapped numbered step (NOT a section header)
    _BOLD_NUMBERED = re.compile(r'^\d+[\.\)]')

    @classmethod
    def _detect_sections(cls, content: str) -> list[tuple[str, str, str]]:
        """Detect top-level section boundaries in content.

        Returns list of (label, body, item_type) 3-tuples where item_type
        is "section". Section headers are bold (**Header**) or markdown (## Header).
        Only returns sections if >= 2 are found.

        Skips: title (first bold line before real content), bold numbered items
        (e.g., **1. Make the Dough** — those are steps, not sections).
        """
        # Find all section header positions
        headers: list[tuple[int, int, str]] = []  # (match_start, body_start, name)
        first_match = True
        for m in cls._SECTION_HEADER.finditer(content):
            name = m.group(1) or m.group(3)
            if not name:
                continue
            name = name.strip()

            # Skip bold-wrapped numbered items (e.g., "2. Portion & Roll")
            if cls._BOLD_NUMBERED.match(name):
                continue

            # Skip title: first bold line at/near start of content
            if first_match:
                first_match = False
                # If the match starts within the first few chars, it's likely a title
                pre_content = content[:m.start()].strip()
                if not pre_content:
                    continue  # Skip the title

            headers.append((m.start(), m.end(), name))

        if len(headers) < 2:
            return []

        # Extract body text between consecutive headers
        sections: list[tuple[str, str, str]] = []
        for i, (match_start, body_start, name) in enumerate(headers):
            if i + 1 < len(headers):
                body = content[body_start:headers[i + 1][0]].strip()
            else:
                body = content[body_start:].strip()

            if body:  # Skip empty sections
                sections.append((name, body, "section"))

        return sections if len(sections) >= 2 else []

    _DECOMPOSE_PROMPT = (
        "Break the following text into navigable sub-items for a voice assistant.\n"
        "Identify the natural items (steps, ingredients, sections, tips, etc.).\n\n"
        "Text:\n{content}\n\n"
        "Respond with ONLY a JSON array. Each item: {{\"label\": \"...\", \"text\": \"...\"}}.\n"
        "\"label\" is a short spoken identifier (e.g. \"Step 1\", \"Ingredients\").\n"
        "\"text\" is the full content of that item.\n"
        "No markdown fences, no explanation — just the JSON array."
    )

    def get_children(self, parent_id: str,
                     window_id: str) -> list[Artifact]:
        """Get all child sub-items of a parent artifact, ordered by item_index."""
        # Hot tier first
        results = [
            a for a in self._hot.get(window_id, [])
            if a.parent_id == parent_id
        ]
        if results:
            return sorted(results, key=lambda a: a.item_index)

        # SQLite fallback
        with self._db_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT * FROM artifacts
                       WHERE parent_id = ? AND window_id = ?
                       ORDER BY item_index""",
                    (parent_id, window_id),
                ).fetchall()
                return [_row_to_artifact(r) for r in rows]
            finally:
                conn.close()

    def _get_ancestor_depth(self, artifact_id: str) -> int:
        """Count how many parent_id hops to reach a root (no parent)."""
        depth = 0
        current_id = artifact_id
        visited = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            parent = self.get_by_id(current_id)
            if not parent or not parent.parent_id:
                break
            current_id = parent.parent_id
            depth += 1
        return depth

    def decompose(self, parent_id: str, window_id: str,
                  llm=None) -> list[Artifact]:
        """Decompose a parent artifact into child sub-item artifacts.

        Idempotent — returns existing children if already decomposed.
        Regex-first (numbered steps, bullets, sections), LLM fallback.
        Respects _MAX_DECOMPOSE_DEPTH to prevent unbounded nesting.
        """
        existing = self.get_children(parent_id, window_id)
        if existing:
            return existing

        # Depth guard
        depth = self._get_ancestor_depth(parent_id)
        if depth >= self._MAX_DECOMPOSE_DEPTH:
            self.logger.info(
                "decompose: depth %d >= max %d for %s, skipping",
                depth, self._MAX_DECOMPOSE_DEPTH, parent_id,
            )
            return []

        parent = self.get_by_id(parent_id)
        if not parent:
            self.logger.warning("decompose: parent %s not found", parent_id)
            return []

        items = self._extract_sub_items(parent.content)
        self.logger.debug("decompose: parent=%s type=%s regex_items=%d",
                          parent_id, parent.artifact_type, len(items))

        if not items and llm:
            llm_items = self._llm_decompose(parent.content, llm)
            # LLM returns 2-tuples — wrap as sub_item type
            items = [(label, text, "sub_item") for label, text in llm_items]

        if not items:
            self.logger.info("decompose: no sub-items found for %s", parent_id)
            return []

        # Cap total children per decomposition
        if len(items) > self._MAX_CHILDREN_PER_ROOT:
            items = items[:self._MAX_CHILDREN_PER_ROOT]

        children = []
        for idx, (label, text, item_type) in enumerate(items):
            child = Artifact(
                artifact_id=uuid.uuid4().hex[:16],
                turn_id=parent.turn_id,
                item_index=idx,
                artifact_type=item_type,
                content=text.strip(),
                summary=label,
                source=parent.source,
                provenance={"parent_id": parent_id},
                metadata={"label": label},
                parent_id=parent_id,
                user_id=parent.user_id,
                window_id=window_id,
                tier="hot",
            )
            self.store(child)
            children.append(child)

        self.logger.info(
            "Decomposed artifact %s into %d sub-items", parent_id, len(children),
        )
        return children

    @classmethod
    def _extract_sub_items(cls, content: str) -> list[tuple[str, str, str]]:
        """Regex extraction of sub-items from structured content.

        Priority: sections (bold/markdown headers) > numbered steps > bullets.
        Returns list of (label, text, item_type) 3-tuples.
        item_type is "section", "sub_item", or "sub_item".
        Empty if content is too short or nothing matched.
        """
        # Guard: skip short prose that isn't navigable
        if (len(content) < cls._MIN_DECOMPOSE_CHARS
                or content.count('\n') < cls._MIN_DECOMPOSE_NEWLINES):
            return []

        # 1. Sections: **Bold Headers** or ## Markdown Headers
        sections = cls._detect_sections(content)
        if sections:
            return sections

        # 2. Numbered steps: "1. Preheat oven..." / "**1.** Mix flour..."
        matches = cls._NUMBERED_STEP.findall(content)
        if len(matches) >= 2:
            return [(f"Step {num}", text.strip(), "sub_item")
                    for num, text in matches]

        # 3. Bullet items: "- 2 cups flour" / "* 1 tsp salt"
        matches = cls._BULLET_ITEM.findall(content)
        if len(matches) >= 2:
            return [(f"Item {i+1}", text.strip(), "sub_item")
                    for i, text in enumerate(matches)]

        # 4. Markdown sections (legacy ## style)
        matches = cls._MD_SECTION.findall(content)
        if len(matches) >= 2:
            return [(f"Section: {header.strip()}", body.strip(), "section")
                    for _hashes, header, body in matches if body.strip()]

        return []

    def _llm_decompose(self, content: str,
                       llm) -> list[tuple[str, str]]:
        """LLM-based decomposition fallback for unstructured content."""
        try:
            prompt = self._DECOMPOSE_PROMPT.format(content=content[:3000])
            raw = llm.chat(user_message=prompt, max_tokens=800)
            # Strip markdown code fences if present
            raw = re.sub(r'^```(?:json)?\n?', '', raw.strip())
            raw = re.sub(r'\n?```$', '', raw)
            items_data = json.loads(raw)
            if not isinstance(items_data, list):
                return []
            return [(item["label"], item["text"])
                    for item in items_data
                    if isinstance(item, dict) and "label" in item and "text" in item]
        except Exception as e:
            self.logger.warning("LLM decompose failed: %s", e)
            return []

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
        with self._hot_lock:
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

    def promote_window(self, window_id: str) -> list[Artifact]:
        """Select promotable warm-tier artifacts and advance them to cold tier.

        Called after demote_window() during window close. Filters out:
        - Sub-items (parent_id set) — parent artifact is sufficient
        - Very short content (<50 chars) — trivial/empty
        - Duplicate content within the window (by content hash)

        Returns the list of promoted Artifact objects for session summary.
        """
        if not window_id:
            return []

        with self._db_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT * FROM artifacts
                       WHERE tier = 'warm' AND window_id = ?
                       ORDER BY turn_id, item_index""",
                    (window_id,),
                ).fetchall()
            finally:
                conn.close()

        if not rows:
            return []

        all_artifacts = [_row_to_artifact(r) for r in rows]

        # Filter: skip sub-items, short content, duplicates
        seen_hashes: set[int] = set()
        promoted: list[Artifact] = []
        skipped = 0

        for art in all_artifacts:
            # Skip sub-items — parent artifact carries enough context
            if art.parent_id:
                skipped += 1
                continue
            # Skip trivially short content
            if len(art.content) < 50:
                skipped += 1
                continue
            # Skip duplicate content (by hash of first 200 chars)
            content_hash = hash(art.content[:200])
            if content_hash in seen_hashes:
                skipped += 1
                continue
            seen_hashes.add(content_hash)
            promoted.append(art)

        if not promoted:
            self.logger.debug(
                "promote_window %s: all %d artifacts filtered out",
                window_id, len(all_artifacts),
            )
            return []

        # Rank by importance — highest-engagement artifacts first in summary
        promoted.sort(key=lambda a: a.importance_score, reverse=True)

        # Update promoted artifacts to cold tier
        promoted_ids = [a.artifact_id for a in promoted]
        with self._db_lock:
            conn = self._get_conn()
            try:
                placeholders = ",".join("?" for _ in promoted_ids)
                conn.execute(
                    f"""UPDATE artifacts SET tier = 'cold'
                        WHERE artifact_id IN ({placeholders})""",
                    promoted_ids,
                )
                conn.commit()
            except Exception as e:
                self.logger.warning("Failed to promote artifacts for %s: %s",
                                    window_id, e)
            finally:
                conn.close()

        self.logger.info(
            "Promoted %d artifacts for window %s to cold tier (skipped %d)",
            len(promoted), window_id, skipped,
        )
        return promoted

    # ------------------------------------------------------------------
    # Cross-session retrieval (Phase 5)
    # ------------------------------------------------------------------

    def search_cold(self, *, keyword: str = None,
                    artifact_type: str = None,
                    user_id: str = "primary_user",
                    days: int = 30, limit: int = 10,
                    artifact_ids: list = None,
                    include_linked: bool = False) -> list[Artifact]:
        """Search cold-tier artifacts across all windows.

        Two modes:
        - By artifact_ids: direct lookup (for rehydrating from session summary metadata)
        - By keyword/type/date: filtered scan across cold tier

        Keyword search checks summary, content[:500], and provenance.query
        (case-insensitive).

        If include_linked=True, expands results with directly linked
        cold-tier artifacts (deduplicated, re-ranked by effective_score).
        """
        with self._db_lock:
            conn = self._get_conn()
            try:
                if artifact_ids:
                    # Direct lookup by ID — any tier (cold artifacts may have
                    # been rehydrated to hot in a previous recall).
                    # Still scoped to user_id to prevent cross-user leakage.
                    placeholders = ",".join("?" for _ in artifact_ids)
                    rows = conn.execute(
                        f"""SELECT * FROM artifacts
                            WHERE artifact_id IN ({placeholders})
                              AND user_id = ?
                            ORDER BY created_at DESC""",
                        [*artifact_ids, user_id],
                    ).fetchall()
                    return [_row_to_artifact(r) for r in rows]

                # Filtered search across cold tier
                cutoff = time.time() - (days * 86400)
                conditions = ["tier = 'cold'", "user_id = ?",
                              "created_at > ?", "parent_id IS NULL"]
                params: list = [user_id, cutoff]

                if artifact_type:
                    conditions.append("artifact_type = ?")
                    params.append(artifact_type)

                where = " AND ".join(conditions)
                rows = conn.execute(
                    f"""SELECT * FROM artifacts
                        WHERE {where}
                        ORDER BY importance_score DESC, created_at DESC
                        LIMIT ?""",
                    params + [limit * 3],  # over-fetch for keyword filter
                ).fetchall()

                # Convert all rows, apply keyword filter if needed
                all_arts = [_row_to_artifact(r) for r in rows]
                if keyword:
                    kw = keyword.lower()
                    all_arts = [a for a in all_arts
                                if self._keyword_match(a, kw)]

                # Re-rank by effective score (time decay + frequency)
                now = time.time()
                all_arts.sort(
                    key=lambda a: self.effective_score(a, now),
                    reverse=True,
                )
                primary = all_arts[:limit]
            finally:
                conn.close()

        if not include_linked or not primary:
            return primary

        # Expand with directly linked cold-tier artifacts
        seen_ids = {a.artifact_id for a in primary}
        linked_extras = []
        for art in primary:
            linked = self.get_linked(art.artifact_id)
            for la in linked:
                if la.artifact_id not in seen_ids and la.tier == "cold":
                    seen_ids.add(la.artifact_id)
                    linked_extras.append(la)

        if not linked_extras:
            return primary

        combined = primary + linked_extras
        now = time.time()
        combined.sort(
            key=lambda a: self.effective_score(a, now),
            reverse=True,
        )
        return combined[:limit]

    def rehydrate(self, artifact_ids: list[str],
                  window_id: str) -> list[Artifact]:
        """Load cold-tier artifacts and clone them into the current window.

        Creates new hot-tier copies with fresh IDs so P3.5 navigation
        (readback, step-through, section drill) works on recalled content.
        Returns the rehydrated artifacts (new copies).
        """
        if not artifact_ids or not window_id:
            return []

        originals = self.search_cold(artifact_ids=artifact_ids)
        if not originals:
            return []

        # Boost original cold-tier importance (retrieval-driven mutation seed)
        for orig in originals:
            if not orig.parent_id:
                self.record_access(orig.artifact_id, "rehydrate")

        rehydrated = []
        for orig in originals:
            # Skip sub-items — parent will be decomposed on demand
            if orig.parent_id:
                continue

            clone = Artifact(
                artifact_id=uuid.uuid4().hex[:16],
                turn_id=orig.turn_id,
                item_index=orig.item_index,
                artifact_type=orig.artifact_type,
                content=orig.content,
                summary=orig.summary,
                source=orig.source,
                provenance={**orig.provenance,
                            "rehydrated_from": orig.artifact_id},
                metadata={**orig.metadata,
                          "rehydrated": True,
                          "original_window": orig.window_id},
                parent_id=None,
                user_id=orig.user_id,
                window_id=window_id,
                tier="hot",
            )
            self.store(clone)
            rehydrated.append(clone)

        # Auto-link co-rehydrated artifacts (original cold-tier IDs)
        if len(rehydrated) > 1:
            self._auto_link_rehydrated(rehydrated)

        if rehydrated:
            self.logger.info(
                "Rehydrated %d artifacts into window %s from cold tier",
                len(rehydrated), window_id,
            )
        return rehydrated

    def ensure_window_id(self, conv_state) -> str:
        """Ensure conv_state has a window_id; generate one if empty.

        Used by web UI which lacks explicit window open/close.
        """
        if not conv_state.window_id:
            conv_state.window_id = uuid.uuid4().hex[:12]
            self.logger.debug("Generated window_id: %s", conv_state.window_id)
        return conv_state.window_id

    # ------------------------------------------------------------------
    # Consolidation & Abstraction (CMA requirement 5/6)
    # ------------------------------------------------------------------

    # Regex strips common prefixes from summaries/queries for topic extraction
    _TOPIC_STRIP_RE = re.compile(
        r"^(?:Web search:|Weather|File|Developer:|Reminders:|News:|"
        r"System info:)\s*",
        re.IGNORECASE,
    )
    # Stop words to ignore during keyword overlap detection
    _STOP_WORDS = frozenset({
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "about", "between",
        "through", "after", "before", "during", "without", "and", "or",
        "but", "not", "no", "nor", "so", "yet", "both", "either", "neither",
        "each", "every", "all", "any", "few", "more", "most", "other", "some",
        "such", "than", "too", "very", "just", "also", "now", "then", "here",
        "there", "when", "where", "how", "what", "which", "who", "whom",
        "this", "that", "these", "those", "it", "its", "i", "me", "my",
        "you", "your", "he", "him", "his", "she", "her", "we", "our",
        "they", "them", "their", "up", "out", "if",
    })

    def _extract_topic(self, row) -> str:
        """Normalize an artifact row to a topic string for grouping.

        Extracts from provenance.query first (web searches), falls back
        to summary field. Returns lowercase keyword string, or "" if too short.
        """
        # Try provenance.query first (web search, weather, etc.)
        provenance = row["provenance"]
        if isinstance(provenance, str):
            try:
                provenance = json.loads(provenance) if provenance else {}
            except (json.JSONDecodeError, TypeError):
                provenance = {}
        query = provenance.get("query", "")
        if query and len(query.strip()) >= 3:
            return self._normalize_topic(query)

        # Fall back to summary
        summary = row["summary"] or ""
        if summary and len(summary.strip()) >= 3:
            return self._normalize_topic(summary)

        return ""

    def _normalize_topic(self, text: str) -> str:
        """Strip prefixes, lowercase, remove stop words, return keyword core."""
        text = self._TOPIC_STRIP_RE.sub("", text).strip()
        # Keep only word characters and spaces
        text = re.sub(r"[^\w\s]", " ", text.lower())
        words = [self._stem(w) for w in text.split()
                 if w not in self._STOP_WORDS and len(w) > 1]
        return " ".join(sorted(words))

    @staticmethod
    def _stem(word: str) -> str:
        """Minimal English stemmer — strip common suffixes for grouping."""
        if len(word) <= 3:
            return word
        # Plural: flies -> fly, dishes -> dish, containers -> container
        if word.endswith("ies") and len(word) > 4:
            return word[:-3] + "y"
        if word.endswith(("shes", "ches", "xes", "ses", "zes")):
            return word[:-2]
        if word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        # Gerund: running -> run (double-letter), networking -> network
        if word.endswith("ing") and len(word) > 5:
            base = word[:-3]
            if len(base) > 2 and base[-1] == base[-2]:
                return base[:-1]  # running -> run
            return base
        return word

    def _extract_keywords(self, row) -> set:
        """Extract keyword set from an artifact row for overlap detection."""
        topic = self._extract_topic(row)
        if not topic:
            # Fall back to first 200 chars of content
            content = (row["content"] or "")[:200].lower()
            content = re.sub(r"[^\w\s]", " ", content)
            words = {self._stem(w) for w in content.split()
                     if w not in self._STOP_WORDS and len(w) > 2}
            return words
        return set(topic.split())

    def _detect_frequency_patterns(self, user_id, conn):
        """Find topics queried 3+ times across 2+ distinct windows."""
        rows = conn.execute("""
            SELECT artifact_type, summary, provenance, window_id,
                   artifact_id, created_at
            FROM artifacts
            WHERE tier = 'cold' AND user_id = ? AND parent_id IS NULL
            ORDER BY created_at DESC
            LIMIT 500
        """, (user_id,)).fetchall()

        topic_groups = defaultdict(list)
        for row in rows:
            topic = self._extract_topic(row)
            if topic:
                topic_groups[topic].append(row)

        insights = []
        for topic, entries in topic_groups.items():
            windows = {e["window_id"] for e in entries}
            if len(entries) >= 3 and len(windows) >= 2:
                # Pick a readable label from the first entry's raw text
                raw = self._extract_raw_label(entries[0])
                insights.append({
                    "pattern_type": "interest",
                    "content": f"frequently searches for {raw}",
                    "evidence_count": len(entries),
                    "evidence_ids": [e["artifact_id"] for e in entries[:20]],
                    "first_seen": min(e["created_at"] for e in entries),
                    "last_seen": max(e["created_at"] for e in entries),
                    "distinct_windows": len(windows),
                    "topic_key": topic,
                })
        return insights

    def _extract_raw_label(self, row) -> str:
        """Get a human-readable label from a row (un-normalized)."""
        provenance = row["provenance"]
        if isinstance(provenance, str):
            try:
                provenance = json.loads(provenance) if provenance else {}
            except (json.JSONDecodeError, TypeError):
                provenance = {}
        query = provenance.get("query", "")
        if query and len(query.strip()) >= 3:
            return self._TOPIC_STRIP_RE.sub("", query).strip().lower()
        summary = row["summary"] or ""
        return self._TOPIC_STRIP_RE.sub("", summary).strip().lower()

    def _detect_temporal_patterns(self, user_id, conn):
        """Find actions repeated at similar times across 5+ distinct days."""
        rows = conn.execute("""
            SELECT artifact_type, source, created_at, artifact_id
            FROM artifacts
            WHERE tier = 'cold' AND user_id = ? AND parent_id IS NULL
            ORDER BY created_at DESC
            LIMIT 500
        """, (user_id,)).fetchall()

        type_groups = defaultdict(list)
        for row in rows:
            key = (row["artifact_type"], row["source"])
            type_groups[key].append(row)

        insights = []
        for (art_type, source), entries in type_groups.items():
            # Need 5+ entries across distinct days
            days_seen = {
                datetime.fromtimestamp(e["created_at"]).date()
                for e in entries
            }
            if len(entries) < 5 or len(days_seen) < 5:
                continue

            hours = [datetime.fromtimestamp(e["created_at"]).hour
                     for e in entries]

            # Circular mean for hours (handles wrap-around at midnight)
            angles = [h * (2 * math.pi / 24) for h in hours]
            sin_sum = sum(math.sin(a) for a in angles)
            cos_sum = sum(math.cos(a) for a in angles)
            mean_angle = math.atan2(sin_sum / len(angles),
                                    cos_sum / len(angles))
            if mean_angle < 0:
                mean_angle += 2 * math.pi
            mean_hour = mean_angle * 24 / (2 * math.pi)

            # Circular variance
            R = math.sqrt((sin_sum / len(angles)) ** 2 +
                          (cos_sum / len(angles)) ** 2)
            # R close to 1 = tightly clustered, close to 0 = dispersed
            # Convert to approximate std dev in hours
            if R > 0.01:
                circular_std = math.sqrt(-2 * math.log(R)) * (24 / (2 * math.pi))
            else:
                circular_std = 12.0  # maximally dispersed

            if circular_std < 2.0:
                period = self._hour_to_period(mean_hour)
                label = self._source_to_label(source, art_type)
                insights.append({
                    "pattern_type": "habit",
                    "content": f"checks {label} every {period}",
                    "evidence_count": len(entries),
                    "evidence_ids": [e["artifact_id"] for e in entries[:20]],
                    "first_seen": min(e["created_at"] for e in entries),
                    "last_seen": max(e["created_at"] for e in entries),
                    "distinct_windows": len(days_seen),
                    "topic_key": f"habit_{source}_{art_type}",
                })
        return insights

    @staticmethod
    def _hour_to_period(hour: float) -> str:
        """Convert hour (0-24) to human period label."""
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        else:
            return "night"

    @staticmethod
    def _source_to_label(source: str, art_type: str) -> str:
        """Convert source/type to a human-readable action label."""
        labels = {
            "get_weather": "weather",
            "get_news": "news",
            "get_system_info": "system info",
            "find_files": "files",
            "developer_tools": "developer tools",
            "manage_reminders": "reminders",
            "web_search": "web search",
        }
        return labels.get(source, art_type.replace("_", " "))

    def _detect_interest_clusters(self, user_id, conn):
        """Find clusters of linked cold-tier artifacts with keyword overlap."""
        # Get cold-tier artifacts that have links
        rows = conn.execute("""
            SELECT DISTINCT a.artifact_id, a.summary, a.provenance,
                   a.artifact_type, a.content, a.created_at, a.window_id
            FROM artifacts a
            WHERE a.tier = 'cold' AND a.user_id = ? AND a.parent_id IS NULL
            AND (
                EXISTS (SELECT 1 FROM artifact_links l
                        WHERE l.source_id = a.artifact_id)
                OR
                EXISTS (SELECT 1 FROM artifact_links l
                        WHERE l.target_id = a.artifact_id)
            )
        """, (user_id,)).fetchall()

        if len(rows) < 3:
            return []

        # Build adjacency map
        row_by_id = {r["artifact_id"]: r for r in rows}
        adj = defaultdict(set)
        link_rows = conn.execute("""
            SELECT source_id, target_id FROM artifact_links
        """).fetchall()
        linked_ids = set(row_by_id.keys())
        for lr in link_rows:
            s, t = lr["source_id"], lr["target_id"]
            if s in linked_ids and t in linked_ids:
                adj[s].add(t)
                adj[t].add(s)

        # BFS depth-2 to find clusters
        visited_clusters = []
        globally_seen = set()
        for start_id in row_by_id:
            if start_id in globally_seen:
                continue
            # BFS
            cluster = set()
            frontier = {start_id}
            for _depth in range(2):
                next_frontier = set()
                for nid in frontier:
                    cluster.add(nid)
                    next_frontier |= adj.get(nid, set())
                frontier = next_frontier - cluster
            cluster |= frontier

            if len(cluster) < 3:
                continue

            # Check keyword overlap across cluster members
            all_keywords = []
            for cid in cluster:
                if cid in row_by_id:
                    kw = self._extract_keywords(row_by_id[cid])
                    all_keywords.append(kw)

            if len(all_keywords) < 3:
                continue

            # Find words that appear in at least half the cluster
            word_counts = defaultdict(int)
            for kw_set in all_keywords:
                for w in kw_set:
                    word_counts[w] += 1

            threshold = max(2, len(all_keywords) // 2)
            shared = {w for w, c in word_counts.items() if c >= threshold}

            if not shared:
                continue

            # This cluster has a coherent theme
            globally_seen |= cluster
            cluster_rows = [row_by_id[cid] for cid in cluster
                            if cid in row_by_id]
            label = " ".join(sorted(shared)[:4])
            visited_clusters.append({
                "pattern_type": "interest",
                "content": f"interested in {label}",
                "evidence_count": len(cluster_rows),
                "evidence_ids": [r["artifact_id"] for r in cluster_rows[:20]],
                "first_seen": min(r["created_at"] for r in cluster_rows),
                "last_seen": max(r["created_at"] for r in cluster_rows),
                "distinct_windows": len({r["window_id"]
                                         for r in cluster_rows}),
                "topic_key": f"cluster_{label}",
            })

        return visited_clusters

    def _detect_engagement_patterns(self, user_id, conn):
        """High-engagement artifacts (access_count >= 3) indicate important topics."""
        rows = conn.execute("""
            SELECT artifact_type, summary, provenance, artifact_id,
                   importance_score, access_count, created_at, window_id,
                   content
            FROM artifacts
            WHERE tier = 'cold' AND user_id = ? AND parent_id IS NULL
                  AND access_count >= 3
            ORDER BY importance_score DESC
            LIMIT 50
        """, (user_id,)).fetchall()

        if not rows:
            return []

        # Group by topic
        topic_groups = defaultdict(list)
        for row in rows:
            topic = self._extract_topic(row)
            if topic:
                topic_groups[topic].append(row)

        insights = []
        for topic, entries in topic_groups.items():
            raw = self._extract_raw_label(entries[0])
            insights.append({
                "pattern_type": "interest",
                "content": f"frequently engages with {raw}",
                "evidence_count": len(entries),
                "evidence_ids": [e["artifact_id"] for e in entries[:20]],
                "first_seen": min(e["created_at"] for e in entries),
                "last_seen": max(e["created_at"] for e in entries),
                "distinct_windows": len({e["window_id"] for e in entries}),
                "topic_key": f"engagement_{topic}",
            })
        return insights

    def _upsert_knowledge(self, conn, insight: dict) -> str:
        """Insert or update a consolidated knowledge row. Returns knowledge_id."""
        now = time.time()
        topic_key = insight["topic_key"]
        pattern_type = insight["pattern_type"]

        # Check for existing row with same pattern_type + topic_key
        existing = conn.execute("""
            SELECT knowledge_id, evidence_count, evidence_ids,
                   first_seen, confidence
            FROM consolidated_knowledge
            WHERE user_id = ? AND pattern_type = ? AND superseded_by IS NULL
            AND knowledge_id IN (
                SELECT knowledge_id FROM consolidated_knowledge
                WHERE content LIKE ? OR knowledge_id LIKE ?
            )
        """, (
            insight.get("user_id", "primary_user"),
            pattern_type,
            f"%{topic_key[:30]}%",
            f"%{topic_key[:16]}%",
        )).fetchone()

        # Better dedup: search by topic_key stored in evidence_ids metadata
        if not existing:
            # Look for a row whose topic_key matches (stored as prefix of kid)
            kid_prefix = f"ck_{pattern_type}_{topic_key}"[:32]
            existing = conn.execute("""
                SELECT knowledge_id, evidence_count, evidence_ids,
                       first_seen, confidence
                FROM consolidated_knowledge
                WHERE user_id = ? AND pattern_type = ?
                AND knowledge_id LIKE ?
                AND superseded_by IS NULL
            """, (
                insight.get("user_id", "primary_user"),
                pattern_type,
                f"{kid_prefix}%",
            )).fetchone()

        evidence_ids = json.dumps(insight["evidence_ids"][:20])
        distinct_windows = insight.get("distinct_windows", 2)
        evidence_count = insight["evidence_count"]
        confidence = min(0.95, 0.3 + 0.1 * evidence_count
                         + 0.05 * distinct_windows)

        if existing:
            # Update existing row
            kid = existing["knowledge_id"]
            old_ids = json.loads(existing["evidence_ids"] or "[]")
            new_ids = list(dict.fromkeys(
                old_ids + insight["evidence_ids"]
            ))[:20]
            new_count = max(existing["evidence_count"], evidence_count)
            conn.execute("""
                UPDATE consolidated_knowledge
                SET evidence_count = ?, evidence_ids = ?,
                    last_seen = ?, confidence = ?, content = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
            """, (
                new_count, json.dumps(new_ids),
                insight["last_seen"], confidence, insight["content"],
                now, kid,
            ))
            return kid
        else:
            # Insert new row — use topic_key as deterministic ID prefix
            kid = f"ck_{pattern_type}_{topic_key}"[:32]
            # Ensure uniqueness with hash suffix
            kid_hash = uuid.uuid5(uuid.NAMESPACE_DNS, kid).hex[:8]
            kid = f"{kid}_{kid_hash}"[:48]
            conn.execute("""
                INSERT OR REPLACE INTO consolidated_knowledge
                    (knowledge_id, user_id, pattern_type, content,
                     evidence_count, evidence_ids, first_seen, last_seen,
                     confidence, promoted, superseded_by,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
            """, (
                kid,
                insight.get("user_id", "primary_user"),
                pattern_type,
                insight["content"],
                evidence_count,
                evidence_ids,
                insight["first_seen"],
                insight["last_seen"],
                confidence,
                now, now,
            ))
            return kid

    def _promote_mature_insights(self, conn, user_id, memory_manager=None):
        """Push high-confidence consolidated knowledge to memory_manager facts."""
        if not memory_manager:
            return

        rows = conn.execute("""
            SELECT * FROM consolidated_knowledge
            WHERE user_id = ? AND promoted = 0
            AND confidence >= 0.75 AND evidence_count >= 5
            AND superseded_by IS NULL
        """, (user_id,)).fetchall()

        for row in rows:
            fact_id = memory_manager.store_fact({
                "user_id": user_id,
                "category": row["pattern_type"],
                "subject": row["pattern_type"],
                "content": row["content"],
                "source": "consolidated",
                "confidence": row["confidence"],
            })
            if fact_id:
                conn.execute("""
                    UPDATE consolidated_knowledge
                    SET promoted = 1, updated_at = ?
                    WHERE knowledge_id = ?
                """, (time.time(), row["knowledge_id"]))
                self.logger.info(
                    "Promoted consolidated insight to facts: %s (kid=%s)",
                    row["content"][:60], row["knowledge_id"][:16],
                )

    def consolidate(self, user_id="user", memory_manager=None):
        """Scan cold-tier artifacts and extract/update consolidated knowledge.

        Called from background thread at window close. Safe to run frequently
        as it's idempotent (updates existing insights, doesn't duplicate).
        """
        with self._db_lock:
            conn = self._get_conn()
            try:
                # Run all four detectors
                all_insights = []
                all_insights.extend(
                    self._detect_frequency_patterns(user_id, conn))
                all_insights.extend(
                    self._detect_temporal_patterns(user_id, conn))
                all_insights.extend(
                    self._detect_interest_clusters(user_id, conn))
                all_insights.extend(
                    self._detect_engagement_patterns(user_id, conn))

                if not all_insights:
                    return

                # Upsert each insight
                for insight in all_insights:
                    insight["user_id"] = user_id
                    self._upsert_knowledge(conn, insight)

                # Promote mature insights to memory_manager
                self._promote_mature_insights(conn, user_id, memory_manager)

                conn.commit()
                self.logger.info(
                    "Consolidation complete: %d insights processed for %s",
                    len(all_insights), user_id,
                )
            except Exception as e:
                self.logger.warning("Consolidation failed: %s", e)
                conn.rollback()
            finally:
                conn.close()

    def get_consolidated_knowledge(self, user_id="user",
                                   pattern_type=None,
                                   min_confidence=0.0) -> list:
        """Retrieve consolidated knowledge, optionally filtered."""
        with self._db_lock:
            conn = self._get_conn()
            try:
                query = """
                    SELECT * FROM consolidated_knowledge
                    WHERE user_id = ? AND superseded_by IS NULL
                    AND confidence >= ?
                """
                params = [user_id, min_confidence]
                if pattern_type:
                    query += " AND pattern_type = ?"
                    params.append(pattern_type)
                query += " ORDER BY confidence DESC, evidence_count DESC"

                rows = conn.execute(query, params).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()


# ----------------------------------------------------------------------
# Tool artifact storage helper
# ----------------------------------------------------------------------

_TOOL_ARTIFACT_META = {
    "get_weather": (
        "weather_report",
        lambda args, _: "Weather {}{}".format(
            args.get("query_type", "report"),
            f" for {args['location']}" if args.get("location") else "",
        ),
    ),
    "get_system_info": (
        "system_info",
        lambda args, _: f"System info: {args.get('category', 'general')}",
    ),
    "find_files": (
        "file_search",
        lambda args, _: "File {}{}".format(
            args.get("action", "search"),
            f": {args['pattern']}" if args.get("pattern") else "",
        ),
    ),
    "developer_tools": (
        "dev_tool_output",
        lambda args, _: f"Developer: {args.get('action', 'unknown')}",
    ),
    "manage_reminders": (
        "reminder_result",
        lambda args, _: f"Reminders: {args.get('action', 'unknown')}",
    ),
    "get_news": (
        "news_headlines",
        lambda args, _: "News: {}{}".format(
            args.get("action", "read"),
            f" ({args['category']})" if args.get("category") else "",
        ),
    ),
    "capture_webcam": ("webcam_capture", lambda args, _: "Webcam capture"),
    "take_screenshot": (
        "screenshot",
        lambda args, _: f"Screenshot ({args.get('target', 'monitor')})",
    ),
}

# Results starting with these prefixes are transient — don't cache
_SKIP_PREFIXES = ("Error", "BLOCKED", "CONFIRMATION REQUIRED")


def store_tool_artifact(tool_name: str, tool_args: dict, tool_result: str,
                        cache: 'InteractionCache', conv_state,
                        user_id: str = 'christopher',
                        image_path: str = None) -> Optional[str]:
    """Create and store an artifact from a tool execution result.

    Returns the artifact_id if stored, None if skipped.
    """
    if not cache or not conv_state or not tool_result:
        return None

    # Skip errors, blocked results, and trivially short output
    if any(tool_result.startswith(p) for p in _SKIP_PREFIXES):
        return None
    if len(tool_result) < 30:
        return None

    meta = _TOOL_ARTIFACT_META.get(tool_name)
    if not meta:
        return None

    artifact_type, summary_fn = meta
    summary = summary_fn(tool_args, tool_result)

    metadata = {"tool_name": tool_name}
    if image_path:
        metadata["image_path"] = image_path

    wid = cache.ensure_window_id(conv_state)
    art = Artifact(
        artifact_id=uuid.uuid4().hex[:16],
        turn_id=conv_state.turn_count,
        item_index=0,
        artifact_type=artifact_type,
        content=tool_result,
        summary=summary,
        source=tool_name,
        provenance={"tool_args": tool_args},
        metadata=metadata,
        parent_id=None,
        user_id=user_id,
        window_id=wid,
        tier="hot",
        created_at=time.time(),
    )
    return cache.store(art)


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
