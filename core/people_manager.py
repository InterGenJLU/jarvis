"""
People Manager — structured contact storage with TTS pronunciation.

Stores people JARVIS has been introduced to, with name, relationship,
pronunciation overrides, and associated facts. Integrates with the
TTS normalizer to speak names correctly.

Uses a singleton pattern (matches memory_manager.py, reminder_manager.py).
"""

import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from core.logger import get_logger


# Singleton instance
_instance: Optional["PeopleManager"] = None


def get_people_manager(config=None) -> Optional["PeopleManager"]:
    """Get or create the singleton PeopleManager.

    Call with config on first invocation (from pipeline.py / jarvis_console.py).
    Call with no args from skills to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = PeopleManager(config)
    return _instance


class PeopleManager:
    """Structured contact storage with TTS pronunciation integration."""

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # Database
        self.db_path = Path(config.get("people.db_path",
            "/mnt/storage/jarvis/data/people.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.Lock()
        self._init_db()

        # Pronunciation cache: {name_lower: (compiled_pattern, replacement)}
        self._pronunciation_cache: dict[str, tuple[re.Pattern, str]] = {}
        self._load_pronunciation_cache()
        self._register_tts_normalizer()

        count = len(self.get_all_people())
        self.logger.info(f"People manager initialized ({count} contacts)")

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS people (
                        person_id   TEXT PRIMARY KEY,
                        user_id     TEXT NOT NULL DEFAULT 'user',
                        name        TEXT NOT NULL,
                        pronunciation TEXT,
                        relationship TEXT,
                        created_at  REAL NOT NULL,
                        updated_at  REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS person_facts (
                        fact_id     TEXT PRIMARY KEY,
                        person_id   TEXT NOT NULL,
                        content     TEXT NOT NULL,
                        created_at  REAL NOT NULL,
                        FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_people_user ON people(user_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_people_name ON people(user_id, name COLLATE NOCASE)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_person_facts ON person_facts(person_id)")
                # Enable foreign key enforcement (for CASCADE)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def add_person(self, name: str, relationship: str = None,
                   pronunciation: str = None,
                   user_id: str = "primary_user") -> str:
        """Add a new person. Returns person_id."""
        person_id = uuid.uuid4().hex
        now = time.time()
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "INSERT INTO people (person_id, user_id, name, pronunciation, "
                    "relationship, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (person_id, user_id, name, pronunciation, relationship, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        if pronunciation:
            self._update_pronunciation_cache(name, pronunciation)

        self.logger.info(f"Added person: {name} ({relationship or 'contact'})")
        return person_id

    def get_person_by_name(self, name: str,
                           user_id: str = "primary_user") -> Optional[dict]:
        """Case-insensitive lookup by name."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM people WHERE name = ? COLLATE NOCASE AND user_id = ?",
                    (name, user_id),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_person_with_facts(self, name: str,
                              user_id: str = "primary_user") -> Optional[dict]:
        """Get person record plus all their facts."""
        person = self.get_person_by_name(name, user_id)
        if not person:
            return None

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM person_facts WHERE person_id = ? ORDER BY created_at",
                    (person["person_id"],),
                ).fetchall()
                person["facts"] = [dict(r) for r in rows]
                return person
            finally:
                conn.close()

    def get_all_people(self, user_id: str = "primary_user") -> list[dict]:
        """List all known people for a user."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM people WHERE user_id = ? ORDER BY name COLLATE NOCASE",
                    (user_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def update_pronunciation(self, person_id: str, pronunciation: str):
        """Update phonetic respelling and refresh TTS cache."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    "UPDATE people SET pronunciation = ?, updated_at = ? WHERE person_id = ?",
                    (pronunciation, time.time(), person_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT name FROM people WHERE person_id = ?", (person_id,)
                ).fetchone()
            finally:
                conn.close()

        if row:
            self._update_pronunciation_cache(row["name"], pronunciation)
            self.logger.info(f"Updated pronunciation for {row['name']}: {pronunciation}")

    def update_relationship(self, person_id: str, relationship: str):
        """Update a person's relationship label."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "UPDATE people SET relationship = ?, updated_at = ? WHERE person_id = ?",
                    (relationship, time.time(), person_id),
                )
                conn.commit()
            finally:
                conn.close()

    def add_person_fact(self, person_id: str, content: str) -> str:
        """Add a fact about a person. Returns fact_id."""
        fact_id = uuid.uuid4().hex
        now = time.time()
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "INSERT INTO person_facts (fact_id, person_id, content, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (fact_id, person_id, content, now),
                )
                conn.commit()
            finally:
                conn.close()
        self.logger.info(f"Added fact for person {person_id}: {content[:50]}")
        return fact_id

    def delete_person(self, person_id: str):
        """Delete person and all their facts (CASCADE)."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                # Get name before deleting (for cache cleanup)
                row = conn.execute(
                    "SELECT name FROM people WHERE person_id = ?", (person_id,)
                ).fetchone()
                conn.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
                conn.commit()
            finally:
                conn.close()

        if row:
            name_lower = row[0].lower()
            self._pronunciation_cache.pop(name_lower, None)
            self.logger.info(f"Deleted person: {row[0]}")

    # ------------------------------------------------------------------
    # TTS pronunciation integration
    # ------------------------------------------------------------------

    def _load_pronunciation_cache(self):
        """Load all stored pronunciations into the in-memory cache."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute(
                    "SELECT name, pronunciation FROM people WHERE pronunciation IS NOT NULL"
                ).fetchall()
            finally:
                conn.close()

        for name, pronunciation in rows:
            self._update_pronunciation_cache(name, pronunciation)

    def _update_pronunciation_cache(self, name: str, pronunciation: str):
        """Add or update a pronunciation in the cache."""
        pattern = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
        self._pronunciation_cache[name.lower()] = (pattern, pronunciation)

    def _register_tts_normalizer(self):
        """Register the name substitution function with the TTS normalizer."""
        try:
            from core.tts_normalizer import get_normalizer
            normalizer = get_normalizer()
            normalizer.register_normalization("people_names", self._name_substitution)
            self.logger.debug("Registered people_names TTS normalizer")
        except Exception as e:
            self.logger.warning(f"Could not register TTS normalizer: {e}")

    def _name_substitution(self, text: str) -> str:
        """Replace known names with their phonetic respellings for TTS."""
        if not self._pronunciation_cache:
            return text
        for _name_lower, (pattern, replacement) in self._pronunciation_cache.items():
            text = pattern.sub(replacement, text)
        return text

    # ------------------------------------------------------------------
    # LLM context injection
    # ------------------------------------------------------------------

    def get_people_context(self, utterance: str,
                           user_id: str = "primary_user") -> Optional[str]:
        """Check if any known people are mentioned in the utterance.

        Returns formatted context for LLM system prompt injection, or None.
        """
        if not utterance:
            return None

        people = self.get_all_people(user_id)
        if not people:
            return None

        utterance_lower = utterance.lower()
        mentioned = []

        for person in people:
            # Word-boundary match for the person's name
            if re.search(r'\b' + re.escape(person["name"]) + r'\b',
                         utterance_lower, re.IGNORECASE):
                mentioned.append(person)

        if not mentioned:
            return None

        # Build context for each mentioned person
        parts = []
        for person in mentioned:
            full = self.get_person_with_facts(person["name"], user_id)
            if not full:
                continue
            rel = full.get("relationship") or "contact"
            line = f"You know that {full['name']} is the user's {rel}."
            facts = full.get("facts", [])
            if facts:
                fact_strs = [f["content"] for f in facts[:5]]
                line += " " + ". ".join(fact_strs) + "."
            parts.append(line)

        if not parts:
            return None

        return "PEOPLE YOU KNOW: " + " ".join(parts)
