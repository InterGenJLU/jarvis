"""
User Profile Manager

SQLite-backed profile storage for JARVIS multi-user support.
Stores user identity, preferred honorific, role, and links to
speaker embeddings for voice identification.

Uses the same singleton pattern as reminder_manager.py.
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from core.logger import get_logger


# Singleton instance
_instance: Optional["ProfileManager"] = None


def get_profile_manager(config=None) -> Optional["ProfileManager"]:
    """Get or create the singleton ProfileManager.

    Call with config on first invocation (from startup code).
    Call with no args from skills/modules to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = ProfileManager(config)
    return _instance


class ProfileManager:
    """Manages user profiles with SQLite storage."""

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # Database path
        storage_path = Path(config.get("system.storage_path"))
        self.profiles_dir = storage_path / "data" / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        self.embeddings_dir = self.profiles_dir / "embeddings"
        self.embeddings_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.profiles_dir / "profiles.db"
        self._db_lock = threading.Lock()
        self._init_db()

        self.logger.info(f"ProfileManager initialized (db: {self.db_path})")

    def _init_db(self):
        """Create the profiles table if it doesn't exist."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS profiles (
                        id          TEXT PRIMARY KEY,
                        name        TEXT NOT NULL,
                        honorific   TEXT NOT NULL DEFAULT 'sir',
                        role        TEXT NOT NULL DEFAULT 'user',
                        embedding_path TEXT,
                        created_at  TEXT NOT NULL,
                        updated_at  TEXT NOT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def create_profile(self, user_id: str, name: str, honorific: str = "sir",
                       role: str = "user") -> Dict[str, Any]:
        """Create a new user profile.

        Args:
            user_id: Unique identifier (e.g., "primary_user", "secondary_user")
            name: Display name
            honorific: Preferred form of address ("sir", "ma'am", etc.)
            role: Permission role ("admin", "user", "guest")

        Returns:
            The created profile dict.
        """
        now = datetime.now().isoformat()
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "INSERT INTO profiles (id, name, honorific, role, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, name, honorific, role, now, now),
                )
                conn.commit()
                self.logger.info(f"Created profile: {user_id} ({name}, {honorific})")
            finally:
                conn.close()

        return self.get_profile(user_id)

    def get_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get a profile by user_id."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM profiles WHERE id = ?", (user_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a profile by display name (case-insensitive)."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM profiles WHERE LOWER(name) = LOWER(?)", (name,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all profiles."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM profiles ORDER BY created_at"
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def update_profile(self, user_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update profile fields.

        Accepts keyword arguments for any column: name, honorific, role,
        embedding_path.
        """
        allowed = {"name", "honorific", "role", "embedding_path"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_profile(user_id)

        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    f"UPDATE profiles SET {set_clause} WHERE id = ?", values
                )
                conn.commit()
                self.logger.info(f"Updated profile {user_id}: {list(updates.keys())}")
            finally:
                conn.close()

        return self.get_profile(user_id)

    def delete_profile(self, user_id: str) -> bool:
        """Delete a profile and its embedding file."""
        profile = self.get_profile(user_id)
        if not profile:
            return False

        # Remove embedding file if it exists
        if profile.get("embedding_path"):
            emb_path = Path(profile["embedding_path"])
            if emb_path.exists():
                emb_path.unlink()
                self.logger.info(f"Deleted embedding for {user_id}")

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("DELETE FROM profiles WHERE id = ?", (user_id,))
                conn.commit()
                self.logger.info(f"Deleted profile: {user_id}")
            finally:
                conn.close()

        return True

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_honorific_for(self, user_id: str) -> str:
        """Get the honorific for a user, defaulting to 'sir'."""
        profile = self.get_profile(user_id)
        return profile["honorific"] if profile else "sir"

    def get_profiles_with_embeddings(self) -> List[Dict[str, Any]]:
        """Get all profiles that have speaker embeddings enrolled."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM profiles WHERE embedding_path IS NOT NULL "
                    "ORDER BY created_at"
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
