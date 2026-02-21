"""
Centralized conversation state tracker.

Single source of truth for what happened in the current conversation turn
and across the active conversation window.  Each priority handler updates
state explicitly via update().  Phase 3's ConversationRouter will use this
for context-aware routing decisions.

Replaces scattered booleans (_jarvis_asked_question, _last_research_results,
_last_research_exchange) that were spread across pipeline.py.
"""

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class ConversationState:
    """Tracks conversation flow across turns within a conversation window."""

    # --- Last turn ---
    last_intent: str = ""              # "weather", "dismissal", "memory_recall", "llm", "greeting"
    last_response_type: str = ""       # "skill", "llm", "canned", "memory"
    last_response_text: str = ""       # The actual response JARVIS gave
    last_command: str = ""             # What the user said

    # --- Conversation context ---
    jarvis_asked_question: bool = False    # Did JARVIS's last response end with "?"
    conversation_active: bool = False      # Is the conversation window open?

    # --- Research context (replaces pipeline._last_research_*) ---
    research_results: Optional[list] = None    # Cached web search results
    research_exchange: Optional[dict] = None   # {"query": ..., "answer": ...}

    # --- Timing ---
    last_interaction_time: float = 0.0   # time.time() of last command
    window_opened_at: float = 0.0        # When the conversation window opened

    def update(self, *,
               intent: str = "",
               response_type: str = "",
               response_text: str = "",
               command: str = ""):
        """Update state after processing a command.

        Call this at the end of each priority handler with the relevant fields.
        Only non-empty values overwrite the current state.
        """
        if intent:
            self.last_intent = intent
        if response_type:
            self.last_response_type = response_type
        if response_text:
            self.last_response_text = response_text
            self.jarvis_asked_question = response_text.rstrip().endswith("?")
        if command:
            self.last_command = command
            self.last_interaction_time = time.time()

    def open_window(self):
        """Mark conversation window as active."""
        self.conversation_active = True
        self.window_opened_at = time.time()

    def close_window(self):
        """Reset all turn state on conversation window close."""
        self.conversation_active = False
        self.jarvis_asked_question = False
        self.research_results = None
        self.research_exchange = None
        self.last_intent = ""
        self.last_response_type = ""
        self.last_response_text = ""
        self.last_command = ""

    def set_research_context(self, results: list, exchange: dict):
        """Store research results for follow-up queries."""
        self.research_results = results
        self.research_exchange = exchange

    def clear_research_context(self):
        """Clear research results (e.g., on window close)."""
        self.research_results = None
        self.research_exchange = None
