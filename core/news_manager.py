"""
News Headlines Manager

Background RSS feed monitoring with urgency classification, semantic
deduplication, and voice-driven headline delivery.  Follows the same
singleton + background-thread pattern as ReminderManager.
"""

import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Callable, Tuple

import feedparser
import numpy as np
import requests

from core.logger import get_logger
from core.honorific import get_honorific


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional["NewsManager"] = None


def get_news_manager(config=None, tts=None, conversation=None, llm=None) -> Optional["NewsManager"]:
    """Get or create the singleton NewsManager.

    Call with all args on first invocation (from jarvis_continuous.py).
    Call with no args from skills to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = NewsManager(config, tts, conversation, llm)
    return _instance


# ---------------------------------------------------------------------------
# Priority keyword lists
# ---------------------------------------------------------------------------

_PRIORITY_1_KEYWORDS = [
    "zero-day", "zero day", "0-day", "0day",
    "major breach", "data breach", "massive breach",
    "ransomware attack", "ransomware campaign",
    "terror attack", "terrorist attack", "terrorism",
    "mass shooting", "mass casualty", "casualties reported",
    "earthquake", "hurricane", "tornado warning", "tsunami",
    "market crash", "stock market crash", "flash crash",
    "invasion", "declares war", "military strike",
    "nuclear", "pandemic", "state of emergency",
]

_PRIORITY_2_KEYWORDS = [
    "vulnerability", "exploit", "CVE-", "cve-",
    "data leak", "data exposed", "credentials leaked",
    "outage", "service down", "major outage",
    "indictment", "arrested", "charged with",
    "shooting", "active shooter",
    "emergency", "evacuation",
    "critical update", "security advisory",
    "supply chain attack", "backdoor",
    "phishing campaign", "malware",
]

# Category display names
_CATEGORY_LABELS = {
    "tech": "tech",
    "politics": "politics",
    "general": "general",
    "cyber": "cybersecurity",
    "local": "local",
}


class NewsManager:
    """Core news engine with background RSS polling and priority classification."""

    def __init__(self, config, tts, conversation, llm=None):
        self.config = config
        self.tts = tts
        self.conversation = conversation
        self.llm = llm
        self.logger = get_logger(__name__, config)

        # Database
        self.db_path = Path(config.get(
            "news.db_path",
            "/mnt/storage/jarvis/data/news_headlines.db",
        ))
        self._db_lock = threading.Lock()
        self._init_db()

        # Feed config
        self.feeds: List[Dict] = config.get("news.feeds", [])
        self.poll_interval: int = config.get("news.poll_interval_seconds", 900)
        self.max_per_feed: int = config.get("news.max_headlines_per_feed", 20)
        self.retention_days: int = config.get("news.headline_retention_days", 7)
        self.dedup_threshold: float = config.get("news.dedup_threshold", 0.82)

        # Background thread
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

        # Callbacks (set by jarvis_continuous.py)
        self._pause_listener_callback: Optional[Callable] = None
        self._resume_listener_callback: Optional[Callable] = None
        self._open_window_callback: Optional[Callable] = None

        # Pending critical announcements
        self._pending_announcements: List[Dict] = []
        self._pending_lock = threading.Lock()

        # "Pull that up" state
        self._last_read_url: Optional[str] = None
        self._last_read_headline: Optional[str] = None

        # Semantic dedup — embedding cache (rolling window)
        self._embedding_model = None
        self._recent_embeddings: deque = deque(maxlen=200)  # (headline, embedding)

        self.logger.info("NewsManager initialized")

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create the news_headlines table and indexes."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS news_headlines (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        source          TEXT NOT NULL,
                        category        TEXT NOT NULL,
                        headline        TEXT NOT NULL,
                        summary         TEXT,
                        url             TEXT,
                        published_date  DATETIME,
                        detected_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                        priority        INTEGER DEFAULT 4,
                        announced       BOOLEAN DEFAULT 0,
                        read            BOOLEAN DEFAULT 0
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_news_priority ON news_headlines(priority)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_news_announced ON news_headlines(announced)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_news_category ON news_headlines(category)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_news_detected ON news_headlines(detected_at)")
                conn.commit()
            finally:
                conn.close()
        self.logger.info(f"News database ready at {self.db_path}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _store_headline(self, source: str, category: str, headline: str,
                        summary: str, url: str, published: Optional[str],
                        priority: int) -> Optional[int]:
        """Store a headline and return its row ID, or None if duplicate."""
        with self._db_lock:
            conn = self._conn()
            try:
                # Check exact URL duplicate first (fast)
                if url:
                    existing = conn.execute(
                        "SELECT id FROM news_headlines WHERE url = ?", (url,)
                    ).fetchone()
                    if existing:
                        return None

                cur = conn.execute("""
                    INSERT INTO news_headlines
                        (source, category, headline, summary, url, published_date, priority)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (source, category, headline, summary, url, published, priority))
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def get_unread_count(self) -> Dict[str, int]:
        """Get count of unread headlines per category (last 24h)."""
        cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT category, COUNT(*) as cnt
                FROM news_headlines
                WHERE read = 0 AND detected_at >= ?
                GROUP BY category
            """, (cutoff,)).fetchall()
            return {row["category"]: row["cnt"] for row in rows}
        finally:
            conn.close()

    def get_unread_by_category(self, category: str = None, limit: int = 5) -> List[Dict]:
        """Get unread headlines, optionally filtered by category, newest first."""
        cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._conn()
        try:
            if category:
                rows = conn.execute("""
                    SELECT * FROM news_headlines
                    WHERE read = 0 AND detected_at >= ? AND category = ?
                    ORDER BY priority ASC, detected_at DESC
                    LIMIT ?
                """, (cutoff, category, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM news_headlines
                    WHERE read = 0 AND detected_at >= ?
                    ORDER BY priority ASC, detected_at DESC
                    LIMIT ?
                """, (cutoff, limit)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_read(self, ids: List[int]):
        """Mark headlines as read."""
        if not ids:
            return
        with self._db_lock:
            conn = self._conn()
            try:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE news_headlines SET read = 1 WHERE id IN ({placeholders})",
                    ids,
                )
                conn.commit()
            finally:
                conn.close()

    def mark_announced(self, ids: List[int]):
        """Mark headlines as announced."""
        if not ids:
            return
        with self._db_lock:
            conn = self._conn()
            try:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE news_headlines SET announced = 1 WHERE id IN ({placeholders})",
                    ids,
                )
                conn.commit()
            finally:
                conn.close()

    def _cleanup_old(self):
        """Delete headlines older than retention period."""
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._db_lock:
            conn = self._conn()
            try:
                deleted = conn.execute(
                    "DELETE FROM news_headlines WHERE detected_at < ?", (cutoff,)
                ).rowcount
                conn.commit()
                if deleted:
                    self.logger.info(f"Cleaned up {deleted} old headlines")
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # RSS Fetching
    # ------------------------------------------------------------------

    def _fetch_all_feeds(self) -> List[Dict]:
        """Fetch all configured RSS feeds and return new entries."""
        all_entries = []
        for feed_cfg in self.feeds:
            try:
                entries = self._fetch_feed(
                    url=feed_cfg["url"],
                    source_name=feed_cfg["name"],
                    category=feed_cfg["category"],
                )
                all_entries.extend(entries)
            except Exception as e:
                self.logger.warning(f"Failed to fetch {feed_cfg['name']}: {e}")
        return all_entries

    def _fetch_feed(self, url: str, source_name: str, category: str) -> List[Dict]:
        """Parse a single RSS feed and return normalized entries."""
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            self.logger.warning(f"feedparser error for {source_name}: {e}")
            return []

        if feed.bozo and not feed.entries:
            self.logger.warning(f"Feed error for {source_name}: {feed.bozo_exception}")
            return []

        entries = []
        for entry in feed.entries[:self.max_per_feed]:
            headline = entry.get("title", "").strip()
            if not headline:
                continue

            summary = entry.get("summary", entry.get("description", "")).strip()
            # Strip HTML tags from summary
            if summary:
                import re
                summary = re.sub(r"<[^>]+>", "", summary)
                summary = summary[:300]  # Cap length

            url_link = entry.get("link", "")
            published = self._extract_published_date(entry)

            entries.append({
                "source": source_name,
                "category": category,
                "headline": headline,
                "summary": summary,
                "url": url_link,
                "published_date": published,
            })

        return entries

    def _extract_published_date(self, entry) -> Optional[str]:
        """Extract and normalize published date from a feed entry."""
        for field in ("published_parsed", "updated_parsed"):
            parsed = entry.get(field)
            if parsed:
                try:
                    dt = datetime(*parsed[:6])
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    pass
        return None

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_priority(self, headline: str, summary: str, category: str) -> int:
        """Classify headline priority: 1=critical, 2=high, 3=normal, 4=low."""
        combined = f"{headline} {summary}".lower()

        # Fast keyword scan
        kw_result = self._keyword_scan(combined)
        if kw_result is not None:
            return kw_result

        # Cybersecurity headlines default to priority 3 (normal), others to 4
        if category == "cyber":
            return 3

        return 4

    def _keyword_scan(self, text: str) -> Optional[int]:
        """Fast keyword-based priority detection."""
        for kw in _PRIORITY_1_KEYWORDS:
            if kw in text:
                return 1
        for kw in _PRIORITY_2_KEYWORDS:
            if kw in text:
                return 2
        return None

    def _llm_classify(self, headline: str, summary: str) -> int:
        """Use Qwen to classify ambiguous headlines. Reserved for future use."""
        if not self.llm:
            return 3
        try:
            prompt = (
                "Classify this news headline urgency on a scale of 1-4:\n"
                "1=CRITICAL (active threat, disaster, major breach)\n"
                "2=HIGH (significant security/political event)\n"
                "3=NORMAL (noteworthy news)\n"
                "4=LOW (routine/minor)\n\n"
                f"Headline: {headline}\n"
                f"Summary: {summary[:200]}\n\n"
                "Respond with ONLY the number (1, 2, 3, or 4):"
            )
            result = self.llm.generate(prompt, max_tokens=5)
            for ch in result.strip():
                if ch in "1234":
                    return int(ch)
        except Exception as e:
            self.logger.warning(f"LLM classify failed: {e}")
        return 3

    # ------------------------------------------------------------------
    # Semantic Deduplication
    # ------------------------------------------------------------------

    def _load_embedding_model(self):
        """Lazy-load the sentence-transformer model."""
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                cache_dir = self.config.get("semantic_matching.cache_dir", None)
                self._embedding_model = SentenceTransformer(
                    "all-MiniLM-L6-v2", cache_folder=cache_dir
                )
                self.logger.info("News dedup embedding model loaded")
            except Exception as e:
                self.logger.warning(f"Failed to load embedding model for dedup: {e}")

    def _is_semantic_duplicate(self, headline: str) -> bool:
        """Check if headline is semantically similar to a recent one."""
        self._load_embedding_model()
        if self._embedding_model is None:
            return False

        new_emb = self._embedding_model.encode([headline], convert_to_numpy=True)[0]

        for _, existing_emb in self._recent_embeddings:
            sim = self._cosine_similarity(new_emb, existing_emb)
            if sim >= self.dedup_threshold:
                return True

        # Add to cache
        self._recent_embeddings.append((headline, new_emb))
        return False

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        if norm < 1e-8:
            return 0.0
        return float(dot / norm)

    # ------------------------------------------------------------------
    # Polling Thread
    # ------------------------------------------------------------------

    def start(self):
        """Start background polling."""
        if not self.config.get("news.enabled", True):
            self.logger.info("News system disabled in config")
            return
        if not self.feeds:
            self.logger.warning("No RSS feeds configured — news polling skipped")
            return

        self.logger.info(f"Starting news monitor ({len(self.feeds)} feeds, "
                         f"interval={self.poll_interval}s)")
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="news-poll"
        )
        self._poll_thread.start()

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        self.logger.info("News monitor stopped")

    def _poll_loop(self):
        """Main polling loop: fetch feeds, classify, dedup, store."""
        # Initial delay to let other systems initialize
        for _ in range(6):  # 30 seconds
            if not self._running:
                return
            time.sleep(5)

        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                self.logger.error(f"News poll error: {e}", exc_info=True)

            # Sleep in small increments for responsive shutdown
            for _ in range(self.poll_interval // 5):
                if not self._running:
                    return
                time.sleep(5)

    def _poll_once(self):
        """Single poll cycle: fetch, classify, dedup, store, queue critical."""
        self.logger.info("Polling RSS feeds...")
        entries = self._fetch_all_feeds()
        self.logger.info(f"Fetched {len(entries)} entries from {len(self.feeds)} feeds")

        new_count = 0
        critical_count = 0

        for entry in entries:
            headline = entry["headline"]

            # Semantic dedup
            if self._is_semantic_duplicate(headline):
                continue

            # Classify priority
            priority = self._classify_priority(
                headline, entry.get("summary", ""), entry["category"]
            )

            # Store
            row_id = self._store_headline(
                source=entry["source"],
                category=entry["category"],
                headline=headline,
                summary=entry.get("summary", ""),
                url=entry.get("url", ""),
                published=entry.get("published_date"),
                priority=priority,
            )

            if row_id is not None:
                new_count += 1

                # Queue critical/high for announcement
                if priority <= 2:
                    critical_count += 1
                    with self._pending_lock:
                        self._pending_announcements.append({
                            "id": row_id,
                            "headline": headline,
                            "source": entry["source"],
                            "category": entry["category"],
                            "url": entry.get("url", ""),
                            "priority": priority,
                        })

        if new_count:
            self.logger.info(f"Stored {new_count} new headlines "
                             f"({critical_count} critical/high)")

        # Periodic cleanup
        self._cleanup_old()

    # ------------------------------------------------------------------
    # Announcements (critical/high priority)
    # ------------------------------------------------------------------

    def has_pending_announcement(self) -> bool:
        """Check if there are critical headlines waiting to be announced."""
        with self._pending_lock:
            return len(self._pending_announcements) > 0

    def announce_pending(self, max_items: int = 3):
        """Announce pending critical/high headlines.

        Args:
            max_items: Maximum headlines to announce per call (defense-in-depth).
                       Remaining items stay queued for the next call.
        """
        with self._pending_lock:
            if not self._pending_announcements:
                return
            announcements = list(self._pending_announcements[:max_items])
            self._pending_announcements = self._pending_announcements[max_items:]

        if self._pause_listener_callback:
            self._pause_listener_callback()

        import random

        ids_announced = []
        for item in announcements:
            source = item["source"]
            headline = item["headline"]
            label = _CATEGORY_LABELS.get(item["category"], item["category"])

            h = get_honorific()
            if item["priority"] == 1:
                text = random.choice([
                    f"{h.capitalize()}, urgent {label} news. {source} is reporting {headline}.",
                    f"{h.capitalize()}, I have a critical {label} alert. According to {source}, {headline}.",
                    f"Pardon the interruption, {h}. {source} reports {headline}.",
                ])
            else:
                text = random.choice([
                    f"{h.capitalize()}, a {label} alert from {source}: {headline}.",
                    f"{h.capitalize()}, {source} is reporting {headline}.",
                    f"Worth noting, {h}. {source} reports {headline}.",
                ])
            self.tts.speak(text)
            ids_announced.append(item["id"])
            self._last_read_url = item.get("url")
            self._last_read_headline = item["headline"]
            time.sleep(0.3)

        self.mark_announced(ids_announced)
        self.mark_read(ids_announced)

        if self._resume_listener_callback:
            self._resume_listener_callback()

        # Open follow-up window so user can say "pull that up"
        if self._open_window_callback:
            self._open_window_callback(15.0)

    # ------------------------------------------------------------------
    # Rundown Integration
    # ------------------------------------------------------------------

    def get_news_summary_for_rundown(self) -> str:
        """Get a brief, natural news summary sentence for the daily rundown."""
        counts = self.get_unread_count()
        if not counts:
            return ""

        total = sum(counts.values())
        if total == 0:
            return ""

        # Sort categories by count (highest first)
        sorted_cats = sorted(counts.items(), key=lambda x: x[1], reverse=True)

        # Natural summary: mention total + top 2 categories only
        top_cats = sorted_cats[:2]
        top_labels = [_CATEGORY_LABELS.get(cat, cat) for cat, _ in top_cats]

        if len(top_labels) == 1:
            emphasis = f"mostly {top_labels[0]}"
        else:
            emphasis = f"mostly {top_labels[0]} and {top_labels[1]}"

        if total < 20:
            count_phrase = f"{total} new headlines"
        elif total < 100:
            count_phrase = f"around {(total // 10) * 10} headlines"
        else:
            count_phrase = f"over {(total // 50) * 50} headlines"

        return f"On the news front, there are {count_phrase} waiting, {emphasis}. Just say the word if you'd like to hear them."

    # ------------------------------------------------------------------
    # Reading Headlines Aloud
    # ------------------------------------------------------------------

    def read_headlines(self, category: str = None, limit: int = 5) -> str:
        """Read top headlines with natural, varied cadence."""
        import random

        headlines = self.get_unread_by_category(category=category, limit=limit)

        if not headlines:
            cat_label = _CATEGORY_LABELS.get(category, category) if category else "news"
            return f"No new {cat_label} headlines at the moment, {get_honorific()}."

        ids_read = []
        lines = []
        count = len(headlines)

        for i, h in enumerate(headlines):
            source = h["source"]
            headline_text = h["headline"]
            ids_read.append(h["id"])

            # Track last URL for "pull that up"
            if h.get("url"):
                self._last_read_url = h["url"]
                self._last_read_headline = headline_text

            lines.append(self._format_headline_for_speech(
                source, headline_text, i, count
            ))

        self.mark_read(ids_read)

        # Build response
        cat_label = _CATEGORY_LABELS.get(category, "") if category else ""
        if cat_label:
            intro = f"Here are the top {cat_label} headlines, {get_honorific()}. "
        else:
            intro = f"Here are the top headlines, {get_honorific()}. "

        response = intro + " ".join(lines)

        # Check if there are more
        remaining = self.get_unread_count()
        total_remaining = sum(remaining.values())
        if total_remaining > 0:
            response += f" There are {total_remaining} more if you'd like to continue, {get_honorific()}."

        return response

    @staticmethod
    def _format_headline_for_speech(source: str, headline: str,
                                     index: int, total: int) -> str:
        """Format a single headline with natural, varied source attribution.

        Produces output like:
          "Ars Technica reports that NASA has a new problem..."
          "Over at BleepingComputer, one threat actor is responsible for..."
          "And finally from BBC News, Russia killed opposition leader..."
        """
        import random

        # Source introduction patterns — varied for natural cadence
        _LEAD_INS = [
            "{source} reports that {headline}.",
            "{source} is reporting {headline}.",
            "Over at {source}, {headline}.",
            "According to {source}, {headline}.",
            "From {source}, {headline}.",
        ]

        _FOLLOW_INS = [
            "Meanwhile, {source} reports {headline}.",
            "{source} is also covering {headline}.",
            "Over at {source}, {headline}.",
            "{source} reports {headline}.",
            "In other news, {source} says {headline}.",
            "Separately, {source} reports {headline}.",
        ]

        _FINAL_INS = [
            "Finally, {source} reports {headline}.",
            "Lastly, from {source}, {headline}.",
            "Rounding things out, {source} has {headline}.",
        ]

        if index == 0:
            template = random.choice(_LEAD_INS)
        elif index == total - 1 and total > 1:
            template = random.choice(_FINAL_INS)
        else:
            template = random.choice(_FOLLOW_INS)

        headline = NewsManager._clean_headline_for_speech(headline)
        return template.format(source=source, headline=headline)

    @staticmethod
    def _clean_headline_for_speech(text: str) -> str:
        """Normalize headline text for natural TTS output."""
        import re

        # Strip trailing source attribution (e.g. "- AP News", "| Reuters")
        # The source is already mentioned via the speech template
        text = re.sub(r'\s*[-|]\s*(?:AP\s*News|Reuters|BBC\s*News|The\s+\w+|NPR|CNN|'
                       r'Ars\s*Technica|BleepingComputer|The\s+Hacker\s+News|Wired|'
                       r'TechCrunch|Krebs\s+on\s+Security|Dark\s+Reading|SecurityWeek|'
                       r'The\s+Record|Threatpost)\s*$', '', text)

        # Strip leading "And " — sounds unnatural when template already adds connectors
        if text.startswith("And "):
            text = text[4:]

        # Em dashes / en dashes → comma pause
        text = text.replace("—", ", ").replace("–", ", ")

        # Pipes and colons used as separators in RSS → comma
        text = re.sub(r'\s*\|\s*', ', ', text)
        text = re.sub(r'(?<=[A-Za-z])\s*:\s+', '. ', text, count=1)

        # Ellipsis → period
        text = text.replace("...", ".")
        text = text.replace("…", ".")

        # Strip bracketed prefixes like [Update], [Breaking], [Exclusive]
        text = re.sub(r'^\[.*?\]\s*', '', text)

        # CVE IDs: CVE-2024-1234 → "C V E twenty twenty-four, twelve thirty-four"
        def _expand_cve(m):
            year = m.group(1)
            num = m.group(2)
            return f"C V E {year}, {num}"
        text = re.sub(r'CVE-(\d{4})-(\d+)', _expand_cve, text)

        # Spell out all-caps acronyms (3-6 letters) as spaced letters
        # e.g. "EPMM" → "E P M M", "FBI" → "F B I"
        # EXCEPT pronounceable acronyms that TTS handles well as words
        _PRONOUNCEABLE = {
            "ISIS", "NASA", "NATO", "FEMA", "POTUS", "SCOTUS",
            "OPEC", "BRICS", "ASEAN", "DARPA", "CISA", "NIST",
        }
        def _space_acronym(m):
            word = m.group(0)
            if word in _PRONOUNCEABLE:
                return word.capitalize()  # "ISIS" → "Isis" (TTS reads as word)
            return " ".join(word)
        text = re.sub(r'\b[A-Z]{3,6}\b', _space_acronym, text)

        # Numbers with commas: 6,200 → six thousand two hundred (leave as-is for TTS)
        # but strip dollar signs for cleaner speech
        text = text.replace("$", "")

        # Collapse multiple spaces
        text = re.sub(r'\s{2,}', ' ', text)

        return text.strip()

    def get_headline_count_response(self) -> str:
        """Generate a spoken count of unread headlines by category."""
        counts = self.get_unread_count()
        if not counts:
            return f"No new headlines at the moment, {get_honorific()}."

        total = sum(counts.values())
        parts = []
        for cat, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            label = _CATEGORY_LABELS.get(cat, cat)
            parts.append(f"{cnt} {label}")

        if len(parts) == 1:
            breakdown = parts[0]
        elif len(parts) == 2:
            breakdown = f"{parts[0]} and {parts[1]}"
        else:
            breakdown = ", ".join(parts[:-1]) + f", and {parts[-1]}"

        return (f"You have {total} new headlines, {get_honorific()}: {breakdown}. "
                "Would you like to hear them?")

    # ------------------------------------------------------------------
    # "Pull That Up" Support
    # ------------------------------------------------------------------

    def get_last_read_url(self) -> Optional[str]:
        """Return the URL of the most recently read headline."""
        return self._last_read_url

    def clear_last_read(self):
        """Clear the last-read tracking (e.g., after opening in browser)."""
        self._last_read_url = None
        self._last_read_headline = None

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def set_listener_callbacks(self, pause: Callable, resume: Callable):
        """Set callbacks for pausing/resuming the listener during announcements."""
        self._pause_listener_callback = pause
        self._resume_listener_callback = resume

    def set_window_callback(self, callback: Callable):
        """Set callback for opening a conversation window (callback(duration))."""
        self._open_window_callback = callback
