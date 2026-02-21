"""
Conversational Memory Manager

Long-term memory system for JARVIS with SQLite-backed fact store and
real-time pattern-based extraction. Facts are extracted from user messages
via regex patterns (explicit extraction) and stored with category, confidence,
and provenance tracking.

Phase 1: Core module + fact store + pattern extraction + CRUD.
Phase 2: FAISS vector index for semantic search over conversation history.
Phase 3: Semantic search + recall detection — user-facing memory queries
         intercepted at Priority 3.5 in pipeline routing, results fed to LLM.
Phase 4: LLM batch extraction — every N messages, Qwen extracts implicit
         facts in a background thread. No perceptible latency.
Phase 5: Proactive memory surfacing — relevant facts injected into LLM
         system prompt so Qwen can weave them naturally into responses.
         Admin-only, confidence-gated, max 1 fact per conversation window.
Phase 6: Forgetting + transparency — user can review stored memories,
         request targeted deletion with confirmation, and see fact summaries.
         All 6 phases operational.

Uses a singleton pattern (matches reminder_manager.py, news_manager.py).
"""

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from core.logger import get_logger


# Singleton instance
_instance: Optional["MemoryManager"] = None


def get_memory_manager(config=None, conversation=None, embedding_model=None) -> Optional["MemoryManager"]:
    """Get or create the singleton MemoryManager.

    Call with all args on first invocation (from jarvis_continuous.py).
    Call with no args from skills to retrieve the existing instance.
    """
    global _instance
    if _instance is None and config is not None:
        _instance = MemoryManager(config, conversation, embedding_model)
    return _instance


class MemoryManager:
    """Long-term conversational memory with fact extraction and storage."""

    # ------------------------------------------------------------------
    # Pattern-based extraction
    # ------------------------------------------------------------------

    EXPLICIT_PATTERNS = [
        # Preferences (positive)
        (re.compile(r"\b(?:i|I) (?:really )?(?:prefer|like|love|enjoy|always use|always go with)\s+(.+)", re.IGNORECASE), "preference"),
        (re.compile(r"\bmy (?:favorite|favourite)\s+(\w+)\s+is\s+(.+)", re.IGNORECASE), "preference"),
        # Preferences (negative)
        (re.compile(r"\b(?:i|I) (?:don't|do not|hate|dislike|can't stand|never use)\s+(.+)", re.IGNORECASE), "preference"),
        # Explicit memory requests (imperative only — exclude "do you remember", "what do you remember")
        (re.compile(r"(?<!\byou )(?:remember|don't forget|keep in mind)\s+that\s+(.+)", re.IGNORECASE), "general"),
        # Relationships
        (re.compile(r"\bmy (\w+(?:'s)?)\s+name is\s+(\w+)", re.IGNORECASE), "relationship"),
        (re.compile(r"\bmy (\w+)\s+is\s+(?:called|named)\s+(\w+)", re.IGNORECASE), "relationship"),
        # Work
        (re.compile(r"\b(?:i|I) (?:work|am employed)\s+(?:at|for)\s+(.+)", re.IGNORECASE), "work"),
        (re.compile(r"\bmy (?:job|role|title|position) is\s+(.+)", re.IGNORECASE), "work"),
        # Location
        (re.compile(r"\b(?:i|I) live (?:in|at|on)\s+(.+)", re.IGNORECASE), "location"),
        # Health
        (re.compile(r"\b(?:i|I)(?:'m| am| have)\s+(?:allergic to|intolerant of)\s+(.+)", re.IGNORECASE), "health"),
        # Habits
        (re.compile(r"\b(?:i|I) (?:usually|typically|normally|always)\s+(.+?)(?:\s+(?:every|each|in the|at)\s+.+)?$", re.IGNORECASE), "habit"),
    ]

    def __init__(self, config, conversation, embedding_model=None):
        self.config = config
        self.conversation = conversation
        self.embedding_model = embedding_model  # all-MiniLM-L6-v2, shared from skill_manager

        self.logger = get_logger(__name__, config)

        # Paths
        self.db_path = Path(config.get("conversational_memory.db_path",
            "/mnt/storage/jarvis/data/memory.db"))
        self.faiss_index_path = Path(config.get("conversational_memory.faiss_index_path",
            "/mnt/storage/jarvis/data/memory_faiss"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Batch extraction config (Phase 4)
        self.batch_interval = config.get("conversational_memory.batch_extraction_interval", 25)

        # Proactive surfacing config (Phase 5)
        self.proactive_enabled = config.get("conversational_memory.proactive_surfacing", True)
        self.proactive_threshold = config.get("conversational_memory.proactive_confidence_threshold", 0.45)

        # Thread safety
        self._db_lock = threading.Lock()
        self._message_count_since_batch = 0
        self._surfaced_this_window = set()  # fact_ids surfaced in current conversation window
        self._pending_forget = None  # Phase 6: pending forget confirmation
        self.last_extracted = []  # Facts extracted from most recent user message

        # FAISS index state (Phase 2)
        self.faiss_index = None
        self.faiss_metadata = []
        self._faiss_dirty = 0  # messages since last persist

        self._init_db()
        self._init_faiss()
        self.logger.info(
            f"MemoryManager initialized (Phase 6 complete: facts + FAISS + recall + batch + proactive + forget/transparency "
            f"[{self.faiss_index.ntotal if self.faiss_index else 0} vectors, "
            f"batch every {self.batch_interval} msgs, "
            f"proactive={'on' if self.proactive_enabled else 'off'} "
            f"threshold={self.proactive_threshold}])"
        )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create the facts table and indexes if they don't exist."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS facts (
                        fact_id         TEXT PRIMARY KEY,
                        user_id         TEXT NOT NULL DEFAULT 'user',
                        category        TEXT NOT NULL,
                        subject         TEXT NOT NULL,
                        content         TEXT NOT NULL,
                        source          TEXT NOT NULL,
                        confidence      REAL NOT NULL DEFAULT 0.90,
                        source_messages TEXT,
                        created_at      REAL NOT NULL,
                        last_referenced REAL NOT NULL,
                        times_referenced INTEGER NOT NULL DEFAULT 0,
                        superseded_by   TEXT,
                        deleted         INTEGER NOT NULL DEFAULT 0
                    )
                """)

                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(user_id, category)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(user_id, subject)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_deleted ON facts(deleted)")

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS extraction_state (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
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
    # FAISS vector index (Phase 2)
    # ------------------------------------------------------------------

    def _init_faiss(self):
        """Load or create FAISS index for semantic search over history."""
        if not self.embedding_model:
            self.logger.info("No embedding model provided — FAISS indexing disabled")
            return

        try:
            import faiss
            import numpy as np
        except ImportError:
            self.logger.warning("faiss-cpu not installed — FAISS indexing disabled")
            return

        self.faiss_index_path.mkdir(parents=True, exist_ok=True)
        index_file = self.faiss_index_path / "default.index"
        meta_file = self.faiss_index_path / "default_meta.jsonl"

        if index_file.exists():
            self.faiss_index = faiss.read_index(str(index_file))
            self._load_faiss_metadata(meta_file)
            self.logger.info(
                f"Loaded FAISS index: {self.faiss_index.ntotal} vectors, "
                f"{len(self.faiss_metadata)} metadata entries"
            )
        else:
            # 384-dim for all-MiniLM-L6-v2; inner product (cosine after L2 normalization)
            self.faiss_index = faiss.IndexFlatIP(384)
            self.faiss_metadata = []
            self.logger.info("Created new FAISS index (384-dim, inner product)")

    def _load_faiss_metadata(self, meta_file: Path):
        """Load FAISS metadata from JSONL file."""
        self.faiss_metadata = []
        if not meta_file.exists():
            return
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.faiss_metadata.append(json.loads(line))
        except Exception as e:
            self.logger.error(f"Failed to load FAISS metadata: {e}")

    def _save_faiss_index(self):
        """Persist FAISS index + metadata to disk."""
        if self.faiss_index is None:
            return
        try:
            import faiss
            faiss.write_index(self.faiss_index, str(self.faiss_index_path / "default.index"))
            with open(self.faiss_index_path / "default_meta.jsonl", "w", encoding="utf-8") as f:
                for entry in self.faiss_metadata:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._faiss_dirty = 0
        except Exception as e:
            self.logger.error(f"Failed to save FAISS index: {e}")

    def index_message(self, message: dict):
        """Embed and add a single message to FAISS index. ~1-2ms."""
        if self.faiss_index is None or not self.embedding_model:
            return

        content = message.get("content", "")
        if len(content.strip()) < 10:
            return

        try:
            import numpy as np
            embedding = self.embedding_model.encode(content, normalize_embeddings=True)
            self.faiss_index.add(np.array([embedding], dtype=np.float32))
            self.faiss_metadata.append({
                "timestamp": message.get("timestamp") or time.time(),
                "role": message.get("role", "user"),
                "content": content[:500],
                "user_id": message.get("user_id") or "primary_user",
            })

            self._faiss_dirty += 1
            if self._faiss_dirty >= 50:
                self._save_faiss_index()
        except Exception as e:
            self.logger.warning(f"FAISS index_message failed (non-fatal): {e}")

    def backfill_history(self):
        """One-time: embed all existing chat_history.jsonl messages into FAISS."""
        if self.faiss_index is None or not self.embedding_model:
            self.logger.error("Cannot backfill: FAISS or embedding model not available")
            return 0

        if not self.conversation:
            self.logger.error("Cannot backfill: no conversation manager")
            return 0

        messages = self.conversation.load_full_history()
        if not messages:
            self.logger.info("No history to backfill")
            return 0

        # Filter to messages with enough content
        eligible = [m for m in messages if len(m.get("content", "").strip()) >= 10]
        self.logger.info(f"Backfilling {len(eligible)} messages into FAISS index...")

        try:
            import numpy as np
            texts = [m["content"][:500] for m in eligible]

            # Batch encode for efficiency
            embeddings = self.embedding_model.encode(
                texts, normalize_embeddings=True,
                batch_size=64, show_progress_bar=True
            )

            self.faiss_index.add(np.array(embeddings, dtype=np.float32))

            for m in eligible:
                self.faiss_metadata.append({
                    "timestamp": m.get("timestamp") or 0,
                    "role": m.get("role", "user"),
                    "content": m["content"][:500],
                    "user_id": m.get("user_id") or "primary_user",
                })

            self._save_faiss_index()
            self.logger.info(f"Backfill complete: {len(eligible)} messages indexed "
                             f"(FAISS total: {self.faiss_index.ntotal})")
            return len(eligible)

        except Exception as e:
            self.logger.error(f"Backfill failed: {e}")
            return 0

    def save(self):
        """Persist any dirty state (FAISS index) to disk. Call on shutdown."""
        if self._faiss_dirty > 0:
            self._save_faiss_index()
            self.logger.info(f"Saved FAISS index ({self.faiss_index.ntotal} vectors)")

    # ------------------------------------------------------------------
    # Semantic search (Phase 3)
    # ------------------------------------------------------------------

    def search_history(self, query: str, user_id: str = "primary_user", top_k: int = 8) -> list[dict]:
        """Semantic search over FAISS index. Returns top-K matching messages with scores."""
        if not self.embedding_model or not self.faiss_index or self.faiss_index.ntotal == 0:
            return []

        try:
            import numpy as np
            query_embedding = self.embedding_model.encode(query, normalize_embeddings=True)
            scores, indices = self.faiss_index.search(
                np.array([query_embedding], dtype=np.float32), top_k
            )

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self.faiss_metadata):
                    continue
                meta = self.faiss_metadata[idx]
                if user_id and meta.get("user_id") not in (user_id, None):
                    continue  # Per-user filtering
                results.append({**meta, "score": float(score)})

            return results
        except Exception as e:
            self.logger.error(f"search_history failed: {e}")
            return []

    def search_combined(self, query: str, user_id: str = "primary_user") -> dict:
        """Search both fact store and FAISS history. Returns unified results."""
        facts = self.search_facts_text(query, user_id)
        history = self.search_history(query, user_id)
        return {"facts": facts, "history": history}

    def format_recall_context(self, results: dict) -> str:
        """Format search results into natural context for LLM response generation."""
        from datetime import datetime
        lines = []
        if results["facts"]:
            lines.append("Facts about the user:")
            for f in results["facts"][:5]:
                phrase = self._fact_to_phrase(f) or f['content']
                lines.append(f"  - {phrase} (confidence: {f['confidence']:.0%})")
        if results["history"]:
            lines.append("Relevant past conversations:")
            for h in results["history"][:5]:
                ts = datetime.fromtimestamp(h["timestamp"]).strftime("%b %d, %I:%M %p")
                lines.append(f"  [{ts}] {h['role'].upper()}: {h['content'][:200]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Recall detection (Phase 3)
    # ------------------------------------------------------------------

    RECALL_PATTERNS = [
        r"(?:do you |can you )?remember (?:when|that time|the time)",
        r"what did (?:i|we) (?:say|talk|discuss|mention) about",
        r"have (?:we|i) (?:ever )?(?:talked|discussed|spoken)\b",
        r"did i (?:ever )?(?:mention|tell you|say)\b",
        r"what (?:was|were) (?:that|those) (?:thing|things?) about",
        r"when did (?:we|i) (?:last )?(?:discuss|talk about)",
        r"(?:do you |can you )?recall",
        r"last time i (?:asked|mentioned|said|talked) about",
        r"what do you (?:know|remember) about",
    ]

    FACT_REQUEST_PATTERNS = [
        r"^(?:remember|don't forget|keep in mind)\s+that\s+",
        r"^(?:remember|don't forget|keep in mind)\s+my\s+",
        r"^(?:remember|don't forget|keep in mind)\s+i\s+",
    ]

    def is_fact_request(self, text: str) -> bool:
        """Detect if user is telling JARVIS to remember a fact."""
        text_lower = text.lower().strip()
        return any(re.search(p, text_lower) for p in self.FACT_REQUEST_PATTERNS)

    def is_recall_query(self, text: str) -> bool:
        """Detect if user is asking a memory recall question."""
        text_lower = text.lower().strip()
        return any(re.search(p, text_lower) for p in self.RECALL_PATTERNS)

    def handle_recall(self, query: str, user_id: str = "primary_user") -> Optional[str]:
        """Handle a recall-type query. Returns formatted context for LLM, or None."""
        search_topic = self._extract_recall_topic(query)
        results = self.search_combined(search_topic, user_id)

        if not results["facts"] and not results["history"]:
            return None  # Let LLM handle with "I don't have any record of that"

        return self.format_recall_context(results)

    def _extract_recall_topic(self, query: str) -> str:
        """Strip recall framing to get the search topic.

        'what did I say about Docker?' → 'Docker'
        'remember when we talked about the network migration?' → 'network migration'
        'do you know anything about my coffee preferences?' → 'coffee preferences'
        """
        text = query.strip()

        # Strip common recall prefixes via ordered regexes
        strip_patterns = [
            r"^(?:do you |can you )?(?:remember|recall)\s+(?:when\s+)?(?:we\s+|I\s+)?(?:talked|discussed|spoke|said|mentioned)?\s*(?:about\s+)?",
            r"^what did (?:I|we) (?:say|talk|discuss|mention) about\s+",
            r"^have (?:we|I) (?:ever )?(?:talked|discussed|spoken) about\s+",
            r"^did I (?:ever )?(?:mention|tell you|say)\s+(?:anything\s+)?(?:about\s+)?",
            r"^what (?:was|were) (?:that|those) (?:thing|things?) about\s+",
            r"^when did (?:we|I) (?:last )?(?:discuss|talk about)\s+",
            r"^last time I (?:asked|mentioned|said|talked) about\s+",
            r"^what do you (?:know|remember) about\s+",
        ]
        for pattern in strip_patterns:
            result = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
            if result and result != text:
                text = result
                break

        # Strip trailing question mark and common suffixes
        text = re.sub(r"\?$", "", text).strip()
        text = re.sub(r"\s+(?:again|exactly|specifically)$", "", text, flags=re.IGNORECASE).strip()

        # If stripping failed and we still have the full query, use it as-is
        # (FAISS semantic search handles noisy queries well)
        return text if text else query.strip().rstrip("?")

    # ------------------------------------------------------------------
    # Forgetting + transparency (Phase 6)
    # ------------------------------------------------------------------

    FORGET_PATTERNS = [
        r"(?:forget|delete|remove|erase) (?:what (?:i|you) (?:said|know) about|everything about|the fact about)\s+(.+)",
        r"forget (?:that|the) (.+)",
        r"(?:don't|do not) remember (.+?) (?:anymore|any more|any longer)",
    ]

    TRANSPARENCY_PATTERNS = [
        r"what do you (?:know|remember) about me",
        r"what (?:facts|memories|information) do you have",
        r"show me (?:my|what you) (?:know|remember|stored)",
        r"what have you learned about me",
        r"tell me (?:everything |all (?:that )?)?you (?:know|remember) about me",
    ]

    def is_forget_request(self, text: str) -> bool:
        """Detect if user is requesting memory deletion."""
        return any(re.search(p, text.lower()) for p in self.FORGET_PATTERNS)

    def is_transparency_request(self, text: str) -> bool:
        """Detect if user is asking what JARVIS knows about them."""
        return any(re.search(p, text.lower()) for p in self.TRANSPARENCY_PATTERNS)

    def handle_forget(self, query: str, user_id: str = "primary_user") -> str:
        """Find matching facts and prepare deletion preview with confirmation."""
        from core.honorific import get_honorific

        topic = self._extract_forget_topic(query)
        matching_facts = self.search_facts_text(topic, user_id)

        # Also try semantic search if text search found nothing
        if not matching_facts and self.embedding_model:
            semantic = self._search_facts_semantic(topic, user_id, top_k=5)
            matching_facts = [f for f in semantic if f.get("score", 0) >= 0.5]

        if not matching_facts:
            return f"I don't have any stored memories about that, {get_honorific()}."

        # Store pending deletion for confirmation
        self._pending_forget = {
            "facts": matching_facts,
            "user_id": user_id,
            "expires": time.time() + 30,
        }

        count = len(matching_facts)
        h = get_honorific()
        # Use natural phrasing with proper perspective
        phrases = [f"\"{self._fact_to_phrase(f) or f['content']}\"" for f in matching_facts]
        if count == 1:
            return (
                f"I found one stored fact about that: {phrases[0]}. "
                f"Shall I remove it, {h}?"
            )
        listing = "; ".join(phrases)
        return (
            f"I found {count} stored facts about that, {h}: {listing}. "
            f"Shall I remove them all?"
        )

    def confirm_forget(self) -> str:
        """Execute pending forget after user confirmation."""
        from core.honorific import get_honorific
        h = get_honorific()

        if not self._pending_forget or time.time() > self._pending_forget["expires"]:
            self._pending_forget = None
            return f"The deletion request has expired, {h}."

        deleted = 0
        for fact in self._pending_forget["facts"]:
            if self.delete_fact(fact["fact_id"], soft=True):
                deleted += 1

        self._pending_forget = None
        self.logger.info(f"Forget confirmed: {deleted} facts soft-deleted")
        if deleted == 1:
            return f"Consider it forgotten, {h}."
        return f"Consider them forgotten, {h}. {deleted} items removed."

    def cancel_forget(self) -> str:
        """Cancel pending forget request."""
        from core.honorific import get_honorific
        self._pending_forget = None
        return f"Understood, {get_honorific()}. I'll keep those memories."

    def handle_transparency(self, query: str, user_id: str = "primary_user") -> str:
        """Return a natural summary of stored facts with examples."""
        from core.honorific import get_honorific
        h = get_honorific()
        all_facts = self.get_facts(user_id, limit=50)

        # Filter out action/plan extractions and low-quality third-person extractions
        facts = [f for f in all_facts
                 if f.get("category") != "plan"
                 and not f.get("content", "").startswith("The user ")]

        if not facts:
            return f"I haven't stored any specific facts about you yet, {h}."

        total = len(facts)

        # Qualitative descriptor
        if total <= 2:
            quantity = "a couple of things"
        elif total <= 5:
            quantity = "a few things"
        elif total <= 10:
            quantity = "quite a bit"
        else:
            quantity = "quite a lot"

        # Pick up to 3 representative examples, phrased naturally
        examples = []
        for f in facts[:3]:
            phrase = self._fact_to_phrase(f)
            if phrase:
                examples.append(phrase)

        if not examples:
            return f"I know {quantity} about you, {h}. Would you like me to go through it?"

        # Join examples with natural connectors
        if len(examples) == 1:
            example_str = f"for instance, {examples[0]}"
        elif len(examples) == 2:
            example_str = f"for instance, {examples[0]} and {examples[1]}"
        else:
            example_str = f"for instance, {examples[0]}, {examples[1]}, and {examples[2]}"

        # Uppercase first letter without lowercasing the rest (unlike str.capitalize())
        example_sentence = example_str[0].upper() + example_str[1:]
        return (
            f"I know {quantity} about you, {h}. {example_sentence}. "
            f"Is there something in particular you'd like me to recall?"
        )

    @staticmethod
    def _enrich_birthday(phrase: str) -> str:
        """If the phrase contains a birthday with a year, append the computed age."""
        import re as _re
        from datetime import datetime
        m = _re.search(
            r"birthday\b.*?\b(january|february|march|april|may|june|july|august|"
            r"september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})",
            phrase, _re.IGNORECASE,
        )
        if not m:
            return phrase
        try:
            month_str, day, year = m.group(1), int(m.group(2)), int(m.group(3))
            birth = datetime.strptime(f"{month_str} {day} {year}", "%B %d %Y")
            today = datetime.now()
            age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            return f"{phrase}, making you currently {age} years old"
        except (ValueError, TypeError):
            return phrase

    def _fact_to_phrase(self, fact: dict) -> Optional[str]:
        """Convert a stored fact into a natural spoken phrase."""
        category = fact.get("category", "general")
        content = fact.get("content", "")
        subject = fact.get("subject", "")

        if not content:
            return None

        # Two-part facts (from "my X is Y" patterns) have "subject: value" format
        if ": " in content:
            parts = content.split(": ", 1)
            key, value = parts[0].strip(), parts[1].strip()
            if category == "preference":
                return f"your favorite {key} is {value}"
            elif category == "relationship":
                return f"your {key}'s name is {value}"
            return f"your {key} is {value}"

        # Single-value facts
        if category == "location":
            return f"you live in {content}"
        elif category == "preference":
            return f"you prefer {content}"
        elif category == "work":
            return f"you work {content}"
        elif category == "habit":
            return f"you {content}"
        elif category == "health":
            return f"you're {content}"
        elif category == "general":
            # "Remember that..." facts — content is already a full phrase
            # Convert first-person to second-person for natural delivery
            import re as _re
            phrase = content
            phrase = _re.sub(r"\bmy\b", "your", phrase, flags=_re.IGNORECASE)
            phrase = _re.sub(r"\bI am\b", "you are", phrase, flags=_re.IGNORECASE)
            phrase = _re.sub(r"\bI'm\b", "you're", phrase, flags=_re.IGNORECASE)
            phrase = _re.sub(r"\bI\b", "you", phrase)  # case-sensitive: only uppercase I
            if phrase[0].isupper() and phrase != content:
                phrase = phrase[0].lower() + phrase[1:]
            elif phrase[0].isupper():
                phrase = phrase[0].lower() + phrase[1:]
            # Compute age for birthday facts so the LLM doesn't have to do date math
            phrase = self._enrich_birthday(phrase)
            return phrase
        return content

    def list_facts_by_category(self, user_id: str, category: str = None) -> str:
        """Detailed listing for voice or console delivery."""
        from core.honorific import get_honorific
        facts = self.get_facts(user_id, category=category)
        if not facts:
            return f"No facts in that category, {get_honorific()}."

        lines = []
        for f in facts:
            source_label = "you told me" if f["source"] == "explicit" else "I inferred"
            lines.append(f"  - {f['content']} ({source_label}, {f['confidence']:.0%} confidence)")

        return "\n".join(lines)

    def _extract_forget_topic(self, query: str) -> str:
        """Strip forget framing to get the topic to delete.

        'forget what I said about coffee' → 'coffee'
        'delete the fact about my job' → 'my job'
        """
        text = query.strip()
        for pattern in self.FORGET_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match and match.group(1):
                return match.group(1).strip().rstrip(".,!?;:")
        # Fallback: strip common prefixes
        for prefix in ["forget ", "delete ", "remove ", "erase "]:
            if text.lower().startswith(prefix):
                return text[len(prefix):].strip().rstrip(".,!?;:")
        return text

    # ------------------------------------------------------------------
    # Real-time fact extraction
    # ------------------------------------------------------------------

    def extract_facts_realtime(self, message: dict) -> list:
        """Extract facts from a single user message via regex patterns.

        Designed to be <5ms per message. Only processes user messages.
        Returns list of stored fact dicts.
        """
        if message.get("role") != "user":
            return []

        content = message.get("content", "")
        if not content or len(content.strip()) < 5:
            return []

        user_id = message.get("user_id") or "primary_user"
        timestamp = message.get("timestamp") or time.time()
        extracted = []

        for pattern, category in self.EXPLICIT_PATTERNS:
            match = pattern.search(content)
            if match:
                # Build the fact content from matched groups
                groups = match.groups()
                if len(groups) == 2:
                    # Two-group patterns (e.g. "my X is Y", "my X's name is Y")
                    fact_content = f"{groups[0]}: {groups[1]}"
                    subject = groups[0].strip().lower()
                elif len(groups) == 1:
                    fact_content = groups[0].strip()
                    # Extract a short subject (first 2-3 meaningful words)
                    subject = self._extract_subject(fact_content)
                else:
                    continue

                # Clean up trailing punctuation from fact content
                fact_content = fact_content.rstrip(".,!?;:")

                fact = {
                    "user_id": user_id,
                    "category": category,
                    "subject": subject,
                    "content": fact_content,
                    "source": "explicit",
                    "confidence": 0.90,
                    "source_messages": json.dumps([timestamp]),
                }

                fact_id = self.store_fact(fact)
                if fact_id:
                    fact["fact_id"] = fact_id
                    extracted.append(fact)
                    self.logger.info(f"Extracted fact [{category}]: {fact_content}")

                # "remember that..." is an explicit instruction — don't also
                # match implicit patterns (avoids duplicates like Tool→preference + Tool→general)
                if category == "general":
                    break

        return extracted

    def _extract_subject(self, text: str) -> str:
        """Extract a short subject (1-3 words) from fact content."""
        words = text.strip().split()
        # Skip common filler words at the start
        skip = {"a", "an", "the", "my", "that", "to", "it", "is"}
        meaningful = [w for w in words[:5] if w.lower() not in skip]
        return " ".join(meaningful[:3]).lower().rstrip(".,!?;:")

    # ------------------------------------------------------------------
    # on_message hook (called from conversation.add_message)
    # ------------------------------------------------------------------

    def on_message(self, message: dict):
        """Called on every message. Handles FAISS indexing + fact extraction + batch trigger."""
        # Index all messages (user + assistant) in FAISS
        self.index_message(message)

        if message.get("role") == "user":
            # Skip fact extraction on meta-commands (forget, recall, transparency)
            content = message.get("content", "").lower().strip()
            is_meta = (self.is_forget_request(content) or
                       self.is_recall_query(content) or
                       self.is_transparency_request(content))
            self.last_extracted = [] if is_meta else self.extract_facts_realtime(message)
            self._message_count_since_batch += 1
            if self._message_count_since_batch >= self.batch_interval:
                self._trigger_batch_extraction()

    # ------------------------------------------------------------------
    # LLM batch extraction (Phase 4)
    # ------------------------------------------------------------------

    BATCH_EXTRACTION_PROMPT = (
        "Analyze these recent conversation messages and extract any personal facts, "
        "preferences, relationships, habits, plans, or opinions mentioned by the user.\n\n"
        "For each fact found, output a JSON line with:\n"
        '- "category": one of [preference, relationship, habit, plan, opinion, location, work, health, general]\n'
        '- "subject": brief topic (1-3 words)\n'
        '- "content": the fact in a clear sentence\n\n'
        "Messages:\n{messages}\n\n"
        "Output only JSON lines, one per fact. If no facts found, output nothing."
    )

    def _trigger_batch_extraction(self):
        """Launch background thread for LLM batch extraction."""
        self._message_count_since_batch = 0
        thread = threading.Thread(target=self._run_batch_extraction, daemon=True)
        thread.start()

    def _run_batch_extraction(self):
        """Background: extract implicit facts from last N messages via Qwen."""
        try:
            from datetime import datetime

            recent = [m for m in self.conversation.session_history
                      if m.get("role") == "user"][-self.batch_interval:]
            if not recent:
                return

            formatted = "\n".join(
                f"[{datetime.fromtimestamp(m.get('timestamp', 0)).strftime('%b %d %I:%M %p')}] {m['content']}"
                for m in recent
            )

            from core.llm_router import get_llm_router
            llm = get_llm_router(self.config)
            response = llm.chat(
                user_message=self.BATCH_EXTRACTION_PROMPT.format(messages=formatted),
                max_tokens=300,
            )

            extracted_count = 0
            for line in response.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    fact_data = json.loads(line)
                    if "content" not in fact_data:
                        continue
                    user_id = recent[0].get("user_id") or "primary_user"
                    fact_id = self.store_fact({
                        "user_id": user_id,
                        "category": fact_data.get("category", "general"),
                        "subject": fact_data.get("subject", "unknown"),
                        "content": fact_data["content"],
                        "source": "inferred",
                        "confidence": 0.70,
                        "source_messages": json.dumps([m.get("timestamp", 0) for m in recent]),
                    })
                    if fact_id:
                        extracted_count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

            if extracted_count:
                self.logger.info(f"Batch extraction: found {extracted_count} facts")

        except Exception as e:
            self.logger.warning(f"Batch extraction failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def store_fact(self, fact: dict) -> Optional[str]:
        """Store a new fact. Returns fact_id, or None if duplicate."""
        user_id = fact.get("user_id") or "primary_user"
        subject = fact.get("subject", "")
        content = fact.get("content", "")

        # Check for duplicate/update
        existing = self._find_similar_fact(user_id, subject, content)
        if existing:
            if existing["content"].lower().strip() == content.lower().strip():
                return None  # Exact duplicate, skip
            # Supersede old fact
            new_id = str(uuid.uuid4())
            self.update_fact(existing["fact_id"], superseded_by=new_id)
            self.logger.info(f"Superseding fact {existing['fact_id'][:8]}... with {new_id[:8]}...")
        else:
            new_id = str(uuid.uuid4())

        now = time.time()
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    INSERT INTO facts
                        (fact_id, user_id, category, subject, content, source,
                         confidence, source_messages, created_at, last_referenced,
                         times_referenced, superseded_by, deleted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0)
                """, (
                    new_id,
                    user_id,
                    fact.get("category", "general"),
                    subject,
                    content,
                    fact.get("source", "explicit"),
                    fact.get("confidence", 0.90),
                    fact.get("source_messages"),
                    now,
                    now,
                ))
                conn.commit()
            finally:
                conn.close()

        return new_id

    def get_facts(self, user_id: str = "primary_user", category: str = None,
                  limit: int = 50) -> list:
        """Get active (non-deleted, non-superseded) facts for a user."""
        with self._db_lock:
            conn = self._get_conn()
            try:
                if category:
                    rows = conn.execute("""
                        SELECT * FROM facts
                        WHERE user_id = ? AND category = ?
                              AND deleted = 0 AND superseded_by IS NULL
                        ORDER BY last_referenced DESC
                        LIMIT ?
                    """, (user_id, category, limit)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT * FROM facts
                        WHERE user_id = ? AND deleted = 0 AND superseded_by IS NULL
                        ORDER BY last_referenced DESC
                        LIMIT ?
                    """, (user_id, limit)).fetchall()

                return [dict(row) for row in rows]
            finally:
                conn.close()

    def search_facts_text(self, query: str, user_id: str = "primary_user") -> list:
        """Search facts by text (LIKE search on subject and content)."""
        search_term = f"%{query}%"
        with self._db_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT * FROM facts
                    WHERE user_id = ? AND deleted = 0 AND superseded_by IS NULL
                          AND (subject LIKE ? OR content LIKE ?)
                    ORDER BY confidence DESC, last_referenced DESC
                    LIMIT 20
                """, (user_id, search_term, search_term)).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def update_fact(self, fact_id: str, **kwargs) -> bool:
        """Update specific fields on a fact."""
        if not kwargs:
            return False

        allowed_fields = {"category", "subject", "content", "confidence",
                          "last_referenced", "times_referenced", "superseded_by", "deleted"}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [fact_id]

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                cursor = conn.execute(
                    f"UPDATE facts SET {set_clause} WHERE fact_id = ?", values
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def delete_fact(self, fact_id: str, soft: bool = True) -> bool:
        """Delete a fact. Soft delete by default (sets deleted=1)."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                if soft:
                    cursor = conn.execute(
                        "UPDATE facts SET deleted = 1 WHERE fact_id = ?", (fact_id,)
                    )
                else:
                    cursor = conn.execute(
                        "DELETE FROM facts WHERE fact_id = ?", (fact_id,)
                    )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def get_fact_count(self, user_id: str = "primary_user") -> dict:
        """Get count of active facts by category."""
        with self._db_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT category, COUNT(*) as cnt FROM facts
                    WHERE user_id = ? AND deleted = 0 AND superseded_by IS NULL
                    GROUP BY category
                """, (user_id,)).fetchall()
                return {row["category"]: row["cnt"] for row in rows}
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Proactive memory surfacing (Phase 5)
    # ------------------------------------------------------------------

    def get_proactive_context(self, utterance: str, user_id: str = "primary_user") -> Optional[str]:
        """Check if any stored facts are relevant to the current utterance.

        Returns formatted context string for LLM system prompt injection,
        or None if nothing relevant found.  Max 1 fact per conversation
        window (dedup via _surfaced_this_window).
        """
        if not self.proactive_enabled:
            return None

        # Only for admin users
        from core.user_profile import get_profile_manager
        pm = get_profile_manager()
        profile = pm.get_profile(user_id) if pm else None
        if profile and profile.get("role") not in ("admin", None):
            return None

        # Search fact store by embedding similarity
        facts = self._search_facts_semantic(utterance, user_id, top_k=3)

        # Filter by confidence threshold and dedup within window
        relevant = [f for f in facts
                    if f["score"] >= self.proactive_threshold
                    and f["fact_id"] not in self._surfaced_this_window]

        if not relevant:
            return None

        # Take only the best match (max 1 per response)
        best = relevant[0]
        self._surfaced_this_window.add(best["fact_id"])

        # Update reference tracking
        self.update_fact(best["fact_id"],
                         last_referenced=time.time(),
                         times_referenced=best["times_referenced"] + 1)

        # Use natural phrase form for better LLM context
        phrase = self._fact_to_phrase(best) or best['content']

        self.logger.info(f"Proactive surfacing: '{phrase[:60]}' "
                         f"(score={best['score']:.3f})")

        # Use stronger injection for facts with pre-computed values (e.g. age)
        if "currently " in phrase and "years old" in phrase:
            return (
                f"KNOWN FACT: {phrase}. "
                f"Use this pre-computed value — do NOT calculate it yourself."
            )

        return (
            f"You recall a relevant fact about this user: {phrase}. "
            f"If naturally appropriate, you may briefly reference this in your response. "
            f"Do NOT force it — only mention if genuinely relevant to what they're asking."
        )

    def reset_surfacing_window(self):
        """Called when conversation window closes — allows facts to be
        re-surfaced in the next conversation."""
        if self._surfaced_this_window:
            self.logger.debug(f"Surfacing window reset ({len(self._surfaced_this_window)} facts cleared)")
        self._surfaced_this_window.clear()

    def _search_facts_semantic(self, query: str, user_id: str, top_k: int = 3) -> list[dict]:
        """Embed query and compare against stored fact content embeddings."""
        facts = self.get_facts(user_id, limit=100)  # All active facts for this user
        if not facts or not self.embedding_model:
            return []

        try:
            import numpy as np
            query_emb = self.embedding_model.encode(query, normalize_embeddings=True)
            # Enrich fact text for better semantic matching
            # e.g. birthday facts should also match "how old am I", "age"
            fact_texts = [self._enrich_fact_for_search(f["content"]) for f in facts]
            fact_embs = self.embedding_model.encode(fact_texts, normalize_embeddings=True)

            scores = np.dot(fact_embs, query_emb)
            ranked = sorted(zip(facts, scores), key=lambda x: x[1], reverse=True)

            return [{**f, "score": float(s)} for f, s in ranked[:top_k]]
        except Exception as e:
            self.logger.warning(f"Semantic fact search failed (non-fatal): {e}")
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _enrich_fact_for_search(content: str) -> str:
        """Add semantic aliases so related queries match stored facts.

        e.g. "my birthday is June 12th, 1979" also matches "how old am I", "age"
        """
        text = content.lower()
        if "birthday" in text or "born" in text:
            return f"{content} age how old years old"
        if "name is" in text:
            return f"{content} who am I what is my name called"
        return content

    def _find_similar_fact(self, user_id: str, subject: str, content: str) -> Optional[dict]:
        """Find an existing active fact with the same subject (for dedup/supersede)."""
        if not subject:
            return None
        with self._db_lock:
            conn = self._get_conn()
            try:
                row = conn.execute("""
                    SELECT * FROM facts
                    WHERE user_id = ? AND subject = ?
                          AND deleted = 0 AND superseded_by IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (user_id, subject)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
