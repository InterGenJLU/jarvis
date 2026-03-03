"""Interaction Artifact Cache — unified cache for typed, addressable interaction data.

Phase 1: In-memory hot tier + SQLite warm tier. Every skill/tool/LLM response
can produce artifacts that are stored, referenced by ordinal or type, and
demoted to warm storage on conversation window close.

See docs/INTERACTION_ARTIFACT_CACHE_DESIGN.md for the full vision.
"""

import json
import re
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

    def decompose(self, parent_id: str, window_id: str,
                  llm=None) -> list[Artifact]:
        """Decompose a parent artifact into child sub-item artifacts.

        Idempotent — returns existing children if already decomposed.
        Regex-first (numbered steps, bullets, sections), LLM fallback.
        """
        existing = self.get_children(parent_id, window_id)
        if existing:
            return existing

        parent = self.get_by_id(parent_id)
        if not parent:
            self.logger.warning("decompose: parent %s not found", parent_id)
            return []

        items = self._extract_sub_items(parent.content)

        if not items and llm:
            llm_items = self._llm_decompose(parent.content, llm)
            # LLM returns 2-tuples — wrap as sub_item type
            items = [(label, text, "sub_item") for label, text in llm_items]

        if not items:
            self.logger.info("decompose: no sub-items found for %s", parent_id)
            return []

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
                    artifact_ids: list = None) -> list[Artifact]:
        """Search cold-tier artifacts across all windows.

        Two modes:
        - By artifact_ids: direct lookup (for rehydrating from session summary metadata)
        - By keyword/type/date: filtered scan across cold tier

        Keyword search checks summary, content[:500], and provenance.query
        (case-insensitive).
        """
        with self._db_lock:
            conn = self._get_conn()
            try:
                if artifact_ids:
                    # Direct lookup by ID — any tier (cold artifacts may have
                    # been rehydrated to hot in a previous recall)
                    placeholders = ",".join("?" for _ in artifact_ids)
                    rows = conn.execute(
                        f"""SELECT * FROM artifacts
                            WHERE artifact_id IN ({placeholders})
                            ORDER BY created_at DESC""",
                        artifact_ids,
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
                        ORDER BY created_at DESC
                        LIMIT ?""",
                    params + [limit * 3],  # over-fetch for keyword filter
                ).fetchall()

                if not keyword:
                    return [_row_to_artifact(r) for r in rows[:limit]]

                # Apply keyword filter in Python (summary, content, provenance)
                kw = keyword.lower()
                results = []
                for row in rows:
                    art = _row_to_artifact(row)
                    if self._keyword_match(art, kw):
                        results.append(art)
                        if len(results) >= limit:
                            break
                return results
            finally:
                conn.close()

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
}

# Results starting with these prefixes are transient — don't cache
_SKIP_PREFIXES = ("Error", "BLOCKED", "CONFIRMATION REQUIRED")


def store_tool_artifact(tool_name: str, tool_args: dict, tool_result: str,
                        cache: 'InteractionCache', conv_state,
                        user_id: str = 'christopher') -> Optional[str]:
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
        metadata={"tool_name": tool_name},
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
