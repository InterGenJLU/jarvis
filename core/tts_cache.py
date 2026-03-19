"""TTSCache — Persistent cataloged TTS audio cache.

Stores pre-generated PCM audio on disk with a SQLite index. Startup loads
from disk (~10ms) instead of regenerating via Kokoro (~200s).

Storage:
    Index:  /mnt/storage/jarvis/data/tts_cache.db
    Audio:  /mnt/storage/jarvis/data/tts_cache/<hash>.pcm

Usage:
    cache = TTSCache(config)
    cache.load_all()                     # Load existing cache from disk
    pcm = cache.get("You're welcome, sir.")  # Lookup
    cache.put("New phrase.", pcm, ...)   # Add new entry
    cache.generate_missing(templates, honorifics, synth_fn)  # Fill gaps
"""

import hashlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.logger import get_logger

logger = get_logger("jarvis.tts_cache")

_DEFAULT_DB_PATH = "/mnt/storage/jarvis/data/tts_cache.db"
_DEFAULT_CACHE_DIR = "/mnt/storage/jarvis/data/tts_cache"


class TTSCache:
    """Persistent TTS audio cache with SQLite catalog."""

    def __init__(self, config: dict = None):
        if config:
            db_path = config.get("tts_cache.db_path", _DEFAULT_DB_PATH)
            cache_dir = config.get("tts_cache.cache_dir", _DEFAULT_CACHE_DIR)
        else:
            db_path = _DEFAULT_DB_PATH
            cache_dir = _DEFAULT_CACHE_DIR

        self._db_path = db_path
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # In-memory lookup: phrase text -> raw PCM bytes
        self._memory: dict[str, bytes] = {}

        # Initialize database
        self._init_db()

    def _init_db(self):
        """Create the index table if it doesn't exist."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_entries (
                phrase TEXT PRIMARY KEY,
                template TEXT,
                honorific TEXT,
                mood TEXT DEFAULT 'neutral',
                filename TEXT NOT NULL,
                sample_rate INTEGER DEFAULT 24000,
                duration_ms INTEGER,
                size_bytes INTEGER,
                generated_at TEXT,
                version TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info("TTS cache DB initialized: %s", self._db_path)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def get(self, phrase: str) -> Optional[bytes]:
        """Lookup a cached phrase. Returns raw PCM bytes or None."""
        return self._memory.get(phrase)

    def put(self, phrase: str, pcm: bytes, template: str = "",
            honorific: str = "", mood: str = "neutral",
            sample_rate: int = 24000, version: str = "") -> None:
        """Add or update a cached phrase — saves to disk + memory."""
        filename = self._hash_filename(phrase)
        filepath = self._cache_dir / filename

        # Write PCM to disk
        filepath.write_bytes(pcm)

        # Calculate duration
        duration_ms = int(len(pcm) / (sample_rate * 2) * 1000)  # 16-bit = 2 bytes/sample

        # Update index
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            INSERT OR REPLACE INTO cache_entries
            (phrase, template, honorific, mood, filename, sample_rate,
             duration_ms, size_bytes, generated_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            phrase, template, honorific, mood, filename, sample_rate,
            duration_ms, len(pcm),
            datetime.now(timezone.utc).isoformat(),
            version,
        ))
        conn.commit()
        conn.close()

        # Update memory
        self._memory[phrase] = pcm

    def remove(self, phrase: str) -> bool:
        """Remove a cached phrase from disk + memory + index."""
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT filename FROM cache_entries WHERE phrase = ?", (phrase,)
        ).fetchone()

        if row:
            filename = row[0]
            filepath = self._cache_dir / filename
            if filepath.exists():
                filepath.unlink()
            conn.execute("DELETE FROM cache_entries WHERE phrase = ?", (phrase,))
            conn.commit()
            conn.close()
            self._memory.pop(phrase, None)
            logger.info("TTS cache: removed '%s'", phrase[:50])
            return True

        conn.close()
        return False

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def load_all(self) -> int:
        """Load all cached PCM files from disk into memory. Returns count."""
        t0 = time.time()
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute("SELECT phrase, filename FROM cache_entries").fetchall()
        conn.close()

        loaded = 0
        missing = 0
        for phrase, filename in rows:
            filepath = self._cache_dir / filename
            if filepath.exists():
                self._memory[phrase] = filepath.read_bytes()
                loaded += 1
            else:
                missing += 1

        elapsed = (time.time() - t0) * 1000
        logger.info(
            "TTS cache loaded: %d phrases in %.1fms (%d missing files)",
            loaded, elapsed, missing,
        )
        return loaded

    def generate_missing(self, templates: list[str], honorifics: list[str],
                         synthesize_fn: Callable[[str], Optional[bytes]],
                         version: str = "1",
                         throttle_sleep: float = 0.05) -> int:
        """Generate PCM for any template×honorific combos not already cached.

        Args:
            templates: List of response templates with {honorific} placeholders.
            honorifics: List of honorific strings to generate for.
            synthesize_fn: Callable that takes text and returns PCM bytes or None.
            version: Version string for cache invalidation.
            throttle_sleep: Seconds to sleep between generations (CPU relief).

        Returns:
            Number of new phrases generated.
        """
        # Build the full set of phrases we need
        needed = {}
        for honorific in honorifics:
            for template in templates:
                if "{honorific}" in template:
                    phrase = template.replace("{honorific}", honorific)
                    needed[phrase] = (template, honorific)
                elif template not in needed:
                    needed[template] = (template, "")

        # Check what's already cached
        existing = set(self._memory.keys())
        to_generate = {p: v for p, v in needed.items() if p not in existing}

        if not to_generate:
            logger.info("TTS cache: all %d phrases already cached", len(needed))
            return 0

        logger.info(
            "TTS cache: generating %d missing phrases (%d already cached)",
            len(to_generate), len(existing),
        )

        t0 = time.time()
        generated = 0
        total = len(to_generate)

        for phrase, (template, honorific) in to_generate.items():
            pcm = synthesize_fn(phrase)
            if pcm:
                self.put(phrase, pcm, template=template,
                         honorific=honorific, version=version)
                generated += 1

            # Progress log every 25 phrases
            if generated > 0 and generated % 25 == 0:
                elapsed = time.time() - t0
                logger.info(
                    "TTS cache generation: %d/%d (%.1fs elapsed)",
                    generated, total, elapsed,
                )

            # Throttle to avoid starving audio callback thread
            if throttle_sleep > 0:
                time.sleep(throttle_sleep)

        elapsed = time.time() - t0
        logger.info(
            "TTS cache generation complete: %d new phrases in %.1fs",
            generated, elapsed,
        )
        return generated

    # ------------------------------------------------------------------
    # Catalog queries
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return total number of cached phrases."""
        return len(self._memory)

    def list_honorifics(self) -> list[str]:
        """Return list of unique honorifics in the cache."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT DISTINCT honorific FROM cache_entries WHERE honorific != ''"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def list_phrases(self, honorific: str = None, mood: str = None) -> list[str]:
        """Return list of cached phrases, optionally filtered."""
        conn = sqlite3.connect(self._db_path)
        query = "SELECT phrase FROM cache_entries WHERE 1=1"
        params = []
        if honorific is not None:
            query += " AND honorific = ?"
            params.append(honorific)
        if mood is not None:
            query += " AND mood = ?"
            params.append(mood)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def get_stats(self) -> dict:
        """Return cache statistics."""
        conn = sqlite3.connect(self._db_path)
        row = conn.execute("""
            SELECT COUNT(*), SUM(size_bytes), SUM(duration_ms),
                   COUNT(DISTINCT honorific), COUNT(DISTINCT mood)
            FROM cache_entries
        """).fetchone()
        conn.close()
        return {
            "total_phrases": row[0] or 0,
            "total_bytes": row[1] or 0,
            "total_duration_ms": row[2] or 0,
            "honorifics": row[3] or 0,
            "moods": row[4] or 0,
            "memory_loaded": len(self._memory),
        }

    def clear_all(self) -> int:
        """Remove all cached entries. Returns count removed."""
        conn = sqlite3.connect(self._db_path)
        count = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
        conn.execute("DELETE FROM cache_entries")
        conn.commit()
        conn.close()

        # Remove PCM files
        for f in self._cache_dir.glob("*.pcm"):
            f.unlink()

        self._memory.clear()
        logger.info("TTS cache cleared: %d entries removed", count)
        return count

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_filename(phrase: str) -> str:
        """Generate a stable filename from phrase text."""
        h = hashlib.sha256(phrase.encode('utf-8')).hexdigest()[:16]
        return f"{h}.pcm"
