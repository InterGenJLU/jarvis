"""
Context Window (Working Memory) System

Replaces the flat FIFO conversation history with intelligent working memory:
- Topic tracking via embedding-based segmentation
- Relevance-scored context assembly (semantic sim + recency + continuation)
- Background summarization of closed topics via Qwen (Phase 3)
- Cross-session persistence to SQLite (Phase 4)

Phase 1+2: Topic detection + relevance-based assembly.
Phase 3: Background Qwen summarization of closed segments.
Phase 4: SQLite persistence of closed segments across restarts.
"""

import json
import math
import sqlite3
import time
import threading
import uuid
import numpy as np
import requests
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional

from core.logger import get_logger


# Tokens ~ words * 1.3 (no external tokenizer needed)
TOKEN_RATIO = 1.3

# Common stop words (excluded from topic labeling)
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "out",
    "up", "down", "about", "that", "this", "these", "those", "it", "its",
    "i", "me", "my", "we", "our", "you", "your", "he", "him", "his",
    "she", "her", "they", "them", "their", "what", "which", "who",
    "when", "where", "how", "why", "not", "no", "so", "if", "or",
    "and", "but", "just", "also", "very", "much", "really", "too",
    "all", "any", "some", "many", "more", "most", "other", "such",
    "than", "then", "now", "here", "there", "jarvis", "please", "sir",
    "hey", "ok", "okay", "yeah", "yes", "no", "thanks", "thank",
})


def estimate_tokens(text: str) -> int:
    """Estimate token count from text (words * 1.3)."""
    return int(len(text.split()) * TOKEN_RATIO)


@dataclass
class TopicSegment:
    """A contiguous block of conversation about one topic."""

    segment_id: str = ""  # Phase 4: UUID for SQLite persistence
    messages: List[Dict] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    embedding: Optional[np.ndarray] = None  # centroid of message embeddings
    label: str = ""
    is_open: bool = True
    summary: str = ""  # Phase 3: background summarization
    token_count: int = 0
    from_prior_session: bool = False

    def add_message(self, msg: Dict, msg_embedding: np.ndarray):
        """Add a message to this segment and update centroid."""
        self.messages.append(msg)
        ts = msg.get("timestamp", time.time())
        if not self.start_time:
            self.start_time = ts
        self.end_time = ts
        self.token_count += estimate_tokens(msg.get("content", ""))

        # Running centroid: average of all message embeddings
        if self.embedding is None:
            self.embedding = msg_embedding.copy()
        else:
            n = len(self.messages)
            self.embedding = self.embedding * ((n - 1) / n) + msg_embedding / n


class ContextWindow:
    """Intelligent working memory for JARVIS conversations.

    Tracks topic segments and assembles optimally-scored context
    for each LLM call, replacing the flat FIFO history.
    """

    def __init__(self, config, embedding_model, llm=None):
        self.config = config
        self.logger = get_logger("context_window", config)
        self.embedding_model = embedding_model
        self.llm = llm  # for Phase 3 summarization

        # Configuration
        self.enabled = config.get("context_window.enabled", False)
        self.topic_shift_threshold = config.get(
            "context_window.topic_shift_threshold", 0.45
        )
        self.max_segments = config.get("context_window.max_segments", 20)
        self.verbatim_recent = config.get(
            "context_window.verbatim_recent_messages", 6
        )
        self.token_budget = config.get("context_window.token_budget", 6500)
        self.summarize_enabled = config.get(
            "context_window.summarize_closed_segments", True
        )

        # Phase 4: SQLite persistence
        self._db_path = Path(config.get(
            "context_window.db_path",
            config.get("conversational_memory.db_path",
                       "/mnt/storage/jarvis/data/memory.db")
        ))
        self._db_lock = threading.Lock()
        self._session_id = uuid.uuid4().hex[:12]
        self._retention_days = config.get("context_window.segment_retention_days", 7)

        # Scoring weights
        self.w_semantic = 0.6
        self.w_recency = 0.3
        self.w_continuation = 0.1
        self.recency_half_life = 600  # 10 minutes
        self.prior_session_penalty = 0.8

        # State
        self.segments: List[TopicSegment] = []
        self._current_segment: Optional[TopicSegment] = None
        self._last_embedding: Optional[np.ndarray] = None

        if self.enabled:
            self._init_db()
            self.logger.info(
                f"Context window initialized (threshold={self.topic_shift_threshold}, "
                f"budget={self.token_budget} tokens, verbatim={self.verbatim_recent}, "
                f"summarize={'on' if self.summarize_enabled else 'off'}, "
                f"session={self._session_id})"
            )

    # ----- public API -----

    def on_message(self, message: Dict):
        """Hook called from conversation.add_message() on every new message."""
        if not self.enabled or not self.embedding_model:
            return

        content = message.get("content", "").strip()
        if not content:
            return

        try:
            embedding = self.embedding_model.encode(
                content, normalize_embeddings=True
            )
            embedding = np.array(embedding, dtype=np.float32)

            if self._current_segment is None:
                self._open_segment(message, embedding)
            elif self._detect_topic_shift(embedding):
                self._close_segment()
                self._open_segment(message, embedding)
            else:
                self._current_segment.add_message(message, embedding)

            self._last_embedding = embedding

            # Enforce max segments (evict oldest closed)
            while len(self.segments) > self.max_segments:
                for i, seg in enumerate(self.segments):
                    if not seg.is_open:
                        self.segments.pop(i)
                        break
                else:
                    break  # all segments are open (shouldn't happen)

        except Exception as e:
            self.logger.warning(f"Context window on_message error (non-fatal): {e}")

    def assemble_context(self, current_query: str) -> List[Dict]:
        """Build an optimally-scored message list for the LLM.

        Returns a list of {"role": ..., "content": ...} dicts ready
        for the llama.cpp /v1/chat/completions messages array.

        Strategy:
        1. Always include the last N verbatim messages (freshest context)
        2. Score remaining segments by relevance to current query
        3. Fill token budget with highest-scored segments
        """
        if not self.enabled or not self.segments:
            return []

        try:
            # 1. Gather all messages across all segments
            all_messages = []
            for seg in self.segments:
                for msg in seg.messages:
                    all_messages.append(msg)

            if not all_messages:
                return []

            # Sort by timestamp
            all_messages.sort(key=lambda m: m.get("timestamp", 0))

            # 2. Reserve the most recent N messages (verbatim, always included)
            verbatim = all_messages[-self.verbatim_recent:]
            verbatim_tokens = sum(
                estimate_tokens(m.get("content", "")) for m in verbatim
            )
            remaining_budget = self.token_budget - verbatim_tokens

            if remaining_budget <= 0:
                return self._format_messages(verbatim)

            # 3. Score closed segments for relevance
            query_embedding = None
            if self.embedding_model:
                query_embedding = self.embedding_model.encode(
                    current_query, normalize_embeddings=True
                )
                query_embedding = np.array(query_embedding, dtype=np.float32)

            # Messages in verbatim set (avoid duplicates)
            verbatim_timestamps = {m.get("timestamp", 0) for m in verbatim}

            scored_segments = []
            for seg in self.segments:
                non_verbatim = [
                    m for m in seg.messages
                    if m.get("timestamp", 0) not in verbatim_timestamps
                ]
                if not non_verbatim:
                    continue

                score = self._score_segment_relevance(seg, query_embedding)

                # Phase 3: use summary instead of raw messages when available
                if seg.summary and not seg.is_open:
                    summary_msg = {
                        "role": "assistant",
                        "content": f"[Earlier: {seg.summary}]",
                        "timestamp": seg.start_time,
                    }
                    summary_tokens = estimate_tokens(seg.summary) + 3  # +3 for "[Earlier: ]"
                    scored_segments.append((score, summary_tokens, [summary_msg]))
                else:
                    non_verbatim_tokens = sum(
                        estimate_tokens(m.get("content", "")) for m in non_verbatim
                    )
                    scored_segments.append((score, non_verbatim_tokens, non_verbatim))

            # Sort by score descending
            scored_segments.sort(key=lambda x: x[0], reverse=True)

            # 4. Fill budget
            extra_messages = self._fill_budget(scored_segments, remaining_budget)

            # 5. Combine: extra context + verbatim (chronological order)
            combined = extra_messages + verbatim
            combined.sort(key=lambda m: m.get("timestamp", 0))

            return self._format_messages(combined)

        except Exception as e:
            self.logger.warning(f"Context assembly error (non-fatal): {e}")
            return []

    def replay_prior_session(self, messages: List[Dict]):
        """Replay cross-session messages so they become segments.

        Called once at startup after set_context_window(). Feeds prior
        session messages through on_message() so they become topic
        segments, then marks all resulting segments as from_prior_session
        and closes them (so they get the 0.8x relevance penalty).

        Summarization is suppressed during replay — these are old messages
        and we don't want to fire off Qwen requests for stale history.
        """
        if not self.enabled or not messages:
            return

        count_before = len(self.segments)

        # Suppress summarization during replay (old messages, not worth summarizing)
        saved_summarize = self.summarize_enabled
        self.summarize_enabled = False

        try:
            for msg in messages:
                self.on_message(msg)

            # Close the current open segment (prior session is over)
            if self._current_segment:
                self._close_segment()
        finally:
            self.summarize_enabled = saved_summarize

        # Mark all newly-created segments as prior session
        for seg in self.segments[count_before:]:
            seg.from_prior_session = True

        replayed_segs = len(self.segments) - count_before
        total_msgs = sum(len(s.messages) for s in self.segments[count_before:])
        self.logger.info(
            f"Replayed {len(messages)} prior-session messages → "
            f"{replayed_segs} segment(s), {total_msgs} messages ingested"
        )

    def load_prior_segments(self, fallback_messages: Optional[List[Dict]] = None):
        """Load prior session segments from SQLite (Phase 4).

        If segments exist in the DB, loads them directly — no re-embedding
        or re-segmenting needed. If no segments in DB (first run after
        upgrade), falls back to replay_prior_session() from JSONL history.

        Args:
            fallback_messages: Prior session messages for JSONL fallback.
        """
        if not self.enabled:
            return

        loaded = self._load_segments_from_db()
        if loaded > 0:
            self.logger.info(
                f"Loaded {loaded} prior segment(s) from SQLite "
                f"({sum(len(s.messages) for s in self.segments)} messages)"
            )
        elif fallback_messages:
            self.logger.info(
                "No segments in SQLite — falling back to JSONL replay"
            )
            self.replay_prior_session(fallback_messages)
        else:
            self.logger.info("No prior segments available")

    def reset(self):
        """Reset all state (on conversation window close)."""
        if self._current_segment:
            self._close_segment()
        self.segments.clear()
        self._current_segment = None
        self._last_embedding = None
        self.logger.info("Context window reset")

    def flush(self):
        """Persist the current open segment on shutdown (no summarization)."""
        if self._current_segment and self._current_segment.messages:
            self._current_segment.is_open = False
            self._current_segment.label = self._label_topic(self._current_segment)
            if not self._current_segment.from_prior_session:
                self._persist_segment(self._current_segment)  # synchronous
            self.logger.info(f"Flushed open segment on shutdown: '{self._current_segment.label}'")

    def get_stats(self) -> Dict:
        """Return stats for health/debug reporting."""
        total_messages = sum(len(s.messages) for s in self.segments)
        total_tokens = sum(s.token_count for s in self.segments)
        summarized = sum(1 for s in self.segments if s.summary)
        return {
            "enabled": self.enabled,
            "segments": len(self.segments),
            "open_segment": self._current_segment is not None,
            "total_messages": total_messages,
            "estimated_tokens": total_tokens,
            "summarized": summarized,
        }

    # ----- Phase 4: SQLite persistence -----

    def _init_db(self):
        """Create the topic_segments table if it doesn't exist."""
        with self._db_lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS topic_segments (
                        segment_id    TEXT PRIMARY KEY,
                        session_id    TEXT NOT NULL,
                        label         TEXT NOT NULL DEFAULT '',
                        start_time    REAL NOT NULL,
                        end_time      REAL NOT NULL,
                        messages_json TEXT NOT NULL,
                        embedding     BLOB,
                        token_count   INTEGER NOT NULL DEFAULT 0,
                        summary       TEXT DEFAULT '',
                        created_at    REAL NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_segments_session "
                    "ON topic_segments(session_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_segments_time "
                    "ON topic_segments(start_time DESC)"
                )
                conn.commit()
            finally:
                conn.close()

        # Prune old segments on startup
        self._cleanup_old_segments()

    def _persist_segment(self, segment: TopicSegment):
        """Persist a closed segment to SQLite. Called in a background thread."""
        try:
            embedding_bytes = (
                segment.embedding.tobytes()
                if segment.embedding is not None else None
            )
            with self._db_lock:
                conn = sqlite3.connect(str(self._db_path))
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO topic_segments
                            (segment_id, session_id, label, start_time, end_time,
                             messages_json, embedding, token_count, summary, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        segment.segment_id,
                        self._session_id,
                        segment.label,
                        segment.start_time,
                        segment.end_time,
                        json.dumps(segment.messages),
                        embedding_bytes,
                        segment.token_count,
                        segment.summary,
                        time.time(),
                    ))
                    conn.commit()
                finally:
                    conn.close()
            self.logger.debug(
                f"Persisted segment '{segment.label}' ({segment.segment_id})"
            )
        except Exception as e:
            self.logger.warning(
                f"Failed to persist segment '{segment.label}' (non-fatal): {e}"
            )

    def _update_segment_summary(self, segment_id: str, summary: str):
        """Update the summary column for a persisted segment."""
        try:
            with self._db_lock:
                conn = sqlite3.connect(str(self._db_path))
                try:
                    conn.execute(
                        "UPDATE topic_segments SET summary = ? WHERE segment_id = ?",
                        (summary, segment_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            self.logger.warning(
                f"Failed to update segment summary (non-fatal): {e}"
            )

    def _load_segments_from_db(self) -> int:
        """Load prior segments from SQLite into self.segments.

        Returns the number of segments loaded.
        """
        try:
            with self._db_lock:
                conn = sqlite3.connect(str(self._db_path))
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        "SELECT * FROM topic_segments "
                        "ORDER BY start_time DESC LIMIT ?",
                        (self.max_segments,),
                    ).fetchall()
                finally:
                    conn.close()

            if not rows:
                return 0

            # Reverse so oldest is first (chronological order)
            for row in reversed(rows):
                seg = TopicSegment(
                    segment_id=row["segment_id"],
                    messages=json.loads(row["messages_json"]),
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    label=row["label"],
                    is_open=False,
                    summary=row["summary"] or "",
                    token_count=row["token_count"],
                    from_prior_session=True,
                )
                if row["embedding"]:
                    seg.embedding = np.frombuffer(
                        row["embedding"], dtype=np.float32
                    ).copy()
                self.segments.append(seg)

            return len(rows)

        except Exception as e:
            self.logger.warning(
                f"Failed to load segments from SQLite (non-fatal): {e}"
            )
            return 0

    def _cleanup_old_segments(self):
        """Remove segments older than retention_days from SQLite."""
        cutoff = time.time() - (self._retention_days * 86400)
        try:
            with self._db_lock:
                conn = sqlite3.connect(str(self._db_path))
                try:
                    cursor = conn.execute(
                        "DELETE FROM topic_segments WHERE end_time < ?",
                        (cutoff,),
                    )
                    conn.commit()
                    deleted = cursor.rowcount
                finally:
                    conn.close()
            if deleted > 0:
                self.logger.info(
                    f"Pruned {deleted} segment(s) older than "
                    f"{self._retention_days} days"
                )
        except Exception as e:
            self.logger.warning(
                f"Segment cleanup failed (non-fatal): {e}"
            )

    # ----- internal -----

    def _detect_topic_shift(self, new_embedding: np.ndarray) -> bool:
        """Return True if the new message represents a topic shift."""
        if self._current_segment is None or self._current_segment.embedding is None:
            return True

        # Cosine similarity (both embeddings are normalized, so dot = cosine)
        sim = float(np.dot(new_embedding, self._current_segment.embedding))
        shifted = sim < self.topic_shift_threshold

        if shifted:
            self.logger.info(
                f"Topic shift detected (sim={sim:.3f} < {self.topic_shift_threshold})"
            )
        return shifted

    def _open_segment(self, message: Dict, embedding: np.ndarray):
        """Open a new topic segment."""
        seg = TopicSegment(segment_id=uuid.uuid4().hex[:16])
        seg.add_message(message, embedding)
        self._current_segment = seg
        self.segments.append(seg)
        self.logger.info(f"Opened topic segment #{len(self.segments)}")

    def _close_segment(self):
        """Close the current topic segment, persist, and spawn summarization."""
        if self._current_segment:
            self._current_segment.is_open = False
            self._current_segment.label = self._label_topic(self._current_segment)
            self.logger.info(
                f"Closed topic segment: '{self._current_segment.label}' "
                f"({len(self._current_segment.messages)} msgs, "
                f"~{self._current_segment.token_count} tokens)"
            )

            seg = self._current_segment

            # Phase 4: persist to SQLite (non-blocking)
            if not seg.from_prior_session:
                threading.Thread(
                    target=self._persist_segment,
                    args=(seg,),
                    daemon=True,
                ).start()

            # Phase 3: background Qwen summarization
            if self.summarize_enabled and len(seg.messages) >= 2:
                self.logger.info(
                    f"Summarizing closed segment: '{seg.label}' "
                    f"({len(seg.messages)} msgs, ~{seg.token_count} tokens)"
                )
                threading.Thread(
                    target=self._summarize_segment,
                    args=(seg,),
                    daemon=True,
                ).start()

            self._current_segment = None

    def _summarize_segment(self, segment: TopicSegment):
        """Background-summarize a closed segment via Qwen (llama-server).

        Called in a daemon thread after a topic segment closes.
        Writes segment.summary on success; leaves empty on failure
        (graceful degradation — raw messages used as fallback).
        """
        try:
            # Format messages for the summarization prompt
            lines = []
            for msg in segment.messages:
                role_tag = "USER" if msg.get("role") == "user" else "ASSISTANT"
                lines.append(f"{role_tag}: {msg.get('content', '')}")
            transcript = "\n".join(lines)

            response = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Summarize this conversation excerpt in 1-2 concise sentences. "
                                "State the topic and key points only."
                            ),
                        },
                        {"role": "user", "content": transcript},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 100,
                },
                timeout=15,
            )
            response.raise_for_status()
            summary = response.json()["choices"][0]["message"]["content"].strip()

            if summary:
                segment.summary = summary
                self.logger.info(
                    f"Segment summarized: '{segment.label}' → "
                    f"'{summary[:80]}{'...' if len(summary) > 80 else ''}'"
                )
                # Phase 4: persist summary to SQLite
                if segment.segment_id:
                    self._update_segment_summary(segment.segment_id, summary)
            else:
                self.logger.warning(
                    f"Empty summary for segment '{segment.label}'"
                )

        except Exception as e:
            self.logger.warning(
                f"Segment summarization failed for '{segment.label}' (non-fatal): {e}"
            )

    def _score_segment_relevance(
        self, segment: TopicSegment, query_embedding: Optional[np.ndarray]
    ) -> float:
        """Score a segment's relevance to the current query.

        Score = w_semantic * cosine_sim + w_recency * decay + w_continuation * bonus
        """
        score = 0.0

        # Semantic similarity
        if query_embedding is not None and segment.embedding is not None:
            semantic_sim = float(np.dot(query_embedding, segment.embedding))
            semantic_sim = max(0.0, semantic_sim)  # clamp negative
            score += self.w_semantic * semantic_sim

        # Recency decay (exponential, half-life = 10 minutes)
        age = time.time() - segment.end_time
        recency = math.exp(-age * math.log(2) / self.recency_half_life)
        score += self.w_recency * recency

        # Continuation bonus: if segment is the current open one
        if segment.is_open:
            score += self.w_continuation

        # Prior session penalty
        if segment.from_prior_session:
            score *= self.prior_session_penalty

        return score

    def _fill_budget(self, scored_segments: list, budget: int) -> List[Dict]:
        """Greedily fill the token budget with highest-scored segment messages."""
        selected = []
        remaining = budget

        for _score, tokens, messages in scored_segments:
            if tokens <= remaining:
                selected.extend(messages)
                remaining -= tokens
            elif remaining > 0:
                # Partial: add messages until budget exhausted
                for msg in messages:
                    msg_tokens = estimate_tokens(msg.get("content", ""))
                    if msg_tokens <= remaining:
                        selected.append(msg)
                        remaining -= msg_tokens
                    else:
                        break

            if remaining <= 0:
                break

        return selected

    def _label_topic(self, segment: TopicSegment) -> str:
        """Generate a short topic label from the most frequent non-stop-words."""
        word_freq: Dict[str, int] = {}
        for msg in segment.messages:
            content = msg.get("content", "").lower()
            for word in content.split():
                word = word.strip(".,!?;:'\"()-[]{}")
                if len(word) > 2 and word not in _STOP_WORDS:
                    word_freq[word] = word_freq.get(word, 0) + 1

        if not word_freq:
            return "general"

        top_words = sorted(word_freq, key=word_freq.get, reverse=True)[:3]
        return " ".join(top_words)

    @staticmethod
    def _format_messages(messages: List[Dict]) -> List[Dict]:
        """Convert internal message dicts to LLM-ready format."""
        formatted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content:
                formatted.append({"role": role, "content": content})
        return formatted


def get_context_window(config, embedding_model, llm=None) -> ContextWindow:
    """Factory function to create a ContextWindow instance."""
    return ContextWindow(config, embedding_model, llm)
