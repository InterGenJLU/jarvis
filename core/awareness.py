"""
Unified Awareness Layer — contextual presence across sessions.

Single assembly point for all awareness signals:
  - User facts (memory_manager)
  - Interaction history (memory_manager.recall_interactions)
  - Calendar context (google_calendar.get_upcoming_context)
  - People mentions (people_manager)
  - Self-awareness (self_awareness)
  - Proactive surfacing (semantic-scored, budget-constrained, adaptive)

Replaces the scattered injection chain in ConversationRouter._prepare_llm_context().
"""

import math
import time
import threading
from typing import Optional

from core.logger import get_logger


class AwarenessItem:
    """A single proactive awareness signal to potentially surface."""

    __slots__ = ("item_id", "source", "text", "relevance", "recency",
                 "novelty", "score")

    def __init__(self, item_id: str, source: str, text: str,
                 relevance: float = 0.0, recency: float = 1.0,
                 novelty: float = 1.0):
        self.item_id = item_id
        self.source = source
        self.text = text
        self.relevance = relevance
        self.recency = recency
        self.novelty = novelty
        self.score = 0.0

    def compute_score(self, w_rel=0.6, w_rec=0.3, w_nov=0.1):
        self.score = (w_rel * self.relevance
                      + w_rec * self.recency
                      + w_nov * self.novelty)
        return self.score


class AwarenessAssembler:
    """Unified context assembler for LLM prompt injection.

    Orchestrates existing modules' output into a single, token-budgeted
    context block with clear section headers.
    """

    def __init__(self, *, memory_manager=None, people_manager=None,
                 self_awareness=None, calendar_manager=None,
                 news_manager=None, context_window=None, config=None):
        self.memory_manager = memory_manager
        self.people_manager = people_manager
        self.self_awareness = self_awareness
        self.calendar_manager = calendar_manager
        self.news_manager = news_manager
        self.context_window = context_window
        self.config = config or {}

        self.logger = get_logger(__name__, config)

        # Proactive surfacing config
        self._base_budget_tokens = self.config.get(
            "awareness.proactive_token_budget", 400)
        self._expanded_budget_tokens = self.config.get(
            "awareness.expanded_token_budget", 600)
        self._base_max_items = self.config.get(
            "awareness.base_max_items", 3)
        self._expanded_max_items = self.config.get(
            "awareness.expanded_max_items", 5)
        self._context_headroom_threshold = self.config.get(
            "awareness.context_headroom_threshold", 20000)

        # Per-source thresholds
        self._fact_threshold = self.config.get(
            "awareness.fact_threshold", 0.45)
        self._interaction_threshold = self.config.get(
            "awareness.interaction_threshold", 0.50)
        self._calendar_horizon_hours = self.config.get(
            "awareness.calendar_horizon_hours", 4)
        self._news_threshold = self.config.get(
            "awareness.news_threshold", 0.55)
        self._interaction_retention_days = self.config.get(
            "awareness.interaction_retention_days", 30)

        # Dedup within conversation window
        self._surfaced_this_window: set = set()
        self._lock = threading.Lock()

    def assemble(self, utterance: str, user_id: str = "primary_user") -> str:
        """Build the complete awareness context block for LLM injection.

        Returns a single string with labeled sections, ready for system
        prompt append. Token-budgeted to stay under the configured limit.
        """
        sections = []

        # 1. Self-awareness (capabilities + hardware) — always included
        if self.self_awareness:
            manifest = self.self_awareness.get_capability_manifest()
            compact = self.self_awareness.get_compact_state()
            awareness_block = "\n".join(filter(None, [manifest, compact]))
            if awareness_block:
                sections.append(awareness_block)

        # 2. User facts (passive background) — always included
        if self.memory_manager:
            user_ctx = self.memory_manager.get_full_user_context(user_id=user_id)
            if user_ctx:
                sections.append(user_ctx)

        # 3. People mentioned — only if names detected
        if self.people_manager:
            people_ctx = self.people_manager.get_people_context(
                utterance, user_id=user_id)
            if people_ctx:
                sections.append(people_ctx)

        # 4. AWARENESS section (proactive, budget-constrained)
        proactive_block = self._build_proactive_block(utterance, user_id)
        if proactive_block:
            sections.append(proactive_block)

        return "\n\n".join(sections) if sections else ""

    def _build_proactive_block(self, utterance: str,
                                user_id: str) -> Optional[str]:
        """Build the scored, ranked, budget-constrained AWARENESS section."""
        candidates: list[AwarenessItem] = []

        # Determine adaptive budget based on context window load
        ctx_tokens = 0
        if self.context_window:
            try:
                stats = self.context_window.get_stats()
                ctx_tokens = stats.get("estimated_tokens", 0)
            except Exception:
                pass

        if ctx_tokens < self._context_headroom_threshold:
            max_items = self._expanded_max_items
            budget_tokens = self._expanded_budget_tokens
        else:
            max_items = self._base_max_items
            budget_tokens = self._base_budget_tokens

        # --- Gather candidates from each source ---

        # Memory facts (proactive surfacing, expanded from 1 → max_items)
        self._gather_fact_candidates(utterance, user_id, candidates)

        # Interaction history (research, tool calls, etc.)
        self._gather_interaction_candidates(utterance, user_id, candidates)

        # Calendar (upcoming events)
        self._gather_calendar_candidates(candidates)

        # News (topic relevance) — future extension point
        # self._gather_news_candidates(utterance, candidates)

        if not candidates:
            return None

        # Score and rank all candidates
        for item in candidates:
            item.compute_score()

        candidates.sort(key=lambda x: x.score, reverse=True)

        # Dedup and budget enforcement
        selected = []
        est_tokens = 0

        with self._lock:
            for item in candidates:
                if item.item_id in self._surfaced_this_window:
                    continue
                # Estimate tokens for this item (~4 chars per token)
                item_tokens = len(item.text) // 4 + 10
                if est_tokens + item_tokens > budget_tokens:
                    continue
                if len(selected) >= max_items:
                    break

                selected.append(item)
                est_tokens += item_tokens
                self._surfaced_this_window.add(item.item_id)

        if not selected:
            return None

        lines = ["AWARENESS:"]
        for item in selected:
            lines.append(f"- {item.text}")
            self.logger.debug(
                f"Surfacing [{item.source}] score={item.score:.3f}: "
                f"{item.text[:60]}"
            )

        return "\n".join(lines)

    def _gather_fact_candidates(self, utterance: str, user_id: str,
                                 candidates: list):
        """Gather relevant memory facts as awareness candidates."""
        if not self.memory_manager or not self.memory_manager.proactive_enabled:
            return

        # Only for admin users (same check as original)
        try:
            from core.user_profile import get_profile_manager
            pm = get_profile_manager()
            profile = pm.get_profile(user_id) if pm else None
            if profile and profile.get("role") not in ("admin", None):
                return
        except Exception:
            pass

        facts = self.memory_manager._search_facts_semantic(
            utterance, user_id, top_k=5)

        for f in facts:
            if f["score"] < self._fact_threshold:
                continue

            phrase = self.memory_manager._fact_to_phrase(f) or f["content"]

            # Special handling for computed values (e.g., age)
            if "currently " in phrase and "years old" in phrase:
                text = (f"KNOWN FACT: {phrase}. Use this pre-computed "
                        f"value — do NOT calculate it yourself.")
            else:
                text = (f"You recall: {phrase}")

            # Novelty: inverse of times surfaced
            times_ref = f.get("times_referenced", 0)
            novelty = 1.0 / (1.0 + times_ref * 0.5)

            candidates.append(AwarenessItem(
                item_id=f"fact:{f['fact_id']}",
                source="fact",
                text=text,
                relevance=f["score"],
                recency=1.0,  # facts don't decay
                novelty=novelty,
            ))

    def _gather_interaction_candidates(self, utterance: str, user_id: str,
                                        candidates: list):
        """Gather relevant past interactions as awareness candidates."""
        if not self.memory_manager:
            return

        try:
            interactions = self.memory_manager.recall_interactions(
                utterance, top_k=5,
                days=self._interaction_retention_days,
                user_id=user_id,
            )
        except Exception as e:
            self.logger.debug(f"Interaction recall failed (non-fatal): {e}")
            return

        now = time.time()
        half_life = 7 * 86400  # 7 days

        for interaction in interactions:
            score = interaction.get("score", 0)
            if score < self._interaction_threshold:
                continue

            itype = interaction.get("type", "research")
            query = interaction.get("query", "")
            answer = interaction.get("answer_summary", "")
            created = interaction.get("created_at", now)

            # Recency: exponential decay with 7-day half-life
            age_seconds = now - created
            recency = math.exp(-0.693 * age_seconds / half_life)

            # Format based on type
            age_str = _format_age(age_seconds)
            if itype == "research":
                text = (f"You previously researched \"{query[:80]}\" "
                        f"{age_str} and found: {answer[:150]}")
            elif itype == "tool_call":
                detail = interaction.get("detail", "")
                text = (f"You ran {detail} for \"{query[:60]}\" "
                        f"{age_str}: {answer[:120]}")
            elif itype == "document":
                detail = interaction.get("detail", "")
                text = (f"You generated a {detail} about \"{query[:60]}\" "
                        f"{age_str}")
            else:
                text = (f"You helped with \"{query[:80]}\" {age_str}")

            candidates.append(AwarenessItem(
                item_id=f"interaction:{interaction.get('interaction_id', '')}",
                source="interaction",
                text=text,
                relevance=score,
                recency=recency,
                novelty=1.0,  # interactions are unique
            ))

    def _gather_calendar_candidates(self, candidates: list):
        """Gather upcoming calendar events as awareness candidates."""
        if not self.calendar_manager:
            return

        try:
            upcoming = self.calendar_manager.get_upcoming_context(
                hours=self._calendar_horizon_hours)
        except Exception as e:
            self.logger.debug(f"Calendar context failed (non-fatal): {e}")
            return

        for event in upcoming:
            title = event.get("title", "")
            minutes = event.get("minutes_until", 0)
            attendees = event.get("attendees", [])

            # Format naturally
            if minutes < 5:
                time_str = "starting now"
            elif minutes < 60:
                time_str = f"in {minutes} minutes"
            else:
                hours = minutes // 60
                mins = minutes % 60
                if mins:
                    time_str = f"in {hours}h {mins}m"
                else:
                    time_str = f"in {hours} hour{'s' if hours > 1 else ''}"

            text = f"The user has \"{title}\" {time_str}"
            if attendees:
                names = ", ".join(attendees[:3])
                text += f" (with {names})"

            # Relevance: time proximity (closer = higher)
            # 1.0 at 0 minutes, 0.5 at 120 minutes
            relevance = max(0.3, 1.0 - (minutes / 240.0))

            # Recency for calendar = time proximity (reuse as urgency)
            recency = relevance

            candidates.append(AwarenessItem(
                item_id=f"calendar:{title}:{event.get('start_time', '')}",
                source="calendar",
                text=text,
                relevance=relevance,
                recency=recency,
                novelty=1.0,
            ))

    def reset_window(self):
        """Called on conversation window close. Resets surfacing dedup."""
        with self._lock:
            if self._surfaced_this_window:
                self.logger.debug(
                    f"Awareness window reset "
                    f"({len(self._surfaced_this_window)} items cleared)"
                )
            self._surfaced_this_window.clear()


def _format_age(seconds: float) -> str:
    """Format age in seconds as a human-readable string."""
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hour{'s' if int(hours) != 1 else ''} ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)} day{'s' if int(days) != 1 else ''} ago"
    weeks = days / 7
    return f"{int(weeks)} week{'s' if int(weeks) != 1 else ''} ago"
