"""
News Skill

Voice interface for JARVIS news headline system.
Handles reading headlines, filtering by category, and checking counts.
Delegates all fetching and storage to the core NewsManager.
"""

import random
from core.base_skill import BaseSkill
from core.news_manager import get_news_manager


# Urgency aliases — map spoken words to max_priority values
# Priority 1 = critical, 2 = high, 3 = normal, 4 = low
_URGENCY_MAP = {
    "critical": 1,
    "urgent": 1,
    "breaking": 1,
    "alert": 1,
    "emergency": 1,
    "important": 2,
    "high priority": 2,
    "significant": 2,
    "major": 2,
}

# Category aliases — map spoken words to config category keys
_CATEGORY_MAP = {
    "tech": "tech",
    "technology": "tech",
    "computing": "tech",
    "cyber": "cyber",
    "cybersecurity": "cyber",
    "security": "cyber",
    "infosec": "cyber",
    "hacking": "cyber",
    "politics": "politics",
    "political": "politics",
    "general": "general",
    "world": "general",
    "international": "general",
    "local": "local",
    "local_city": "local",
    "alabama": "local",
}


class NewsSkill(BaseSkill):
    """Voice interface for the news headline system."""

    def initialize(self) -> bool:
        """Register semantic intents for news commands."""

        # --- Read all news (with optional urgency filtering) ---
        self.register_semantic_intent(
            examples=[
                "what's the news",
                "any headlines",
                "read me the news",
                "give me the headlines",
                "news update",
                "read the news",
                "any breaking news",
                "catch me up on the news",
                "read critical headlines",
                "any urgent news",
                "are there any important headlines",
            ],
            handler=self.read_news,
            threshold=0.55,
        )

        # --- Read category-specific news (with optional urgency filtering) ---
        self.register_semantic_intent(
            examples=[
                "any tech news today",
                "read me the technology headlines",
                "what's happening in tech news",
                "cybersecurity news headlines",
                "any cyber security headlines",
                "read security news",
                "any political news today",
                "read the politics headlines",
                "local news headlines",
                "local news today",
                "any general news headlines",
                "world news update",
                "critical cybersecurity headlines",
                "any urgent tech news",
            ],
            handler=self.read_category,
            threshold=0.62,
        )

        # --- Continue reading ---
        self.register_semantic_intent(
            examples=[
                "continue",
                "keep going",
                "read more",
                "more headlines",
                "yes please continue",
                "next",
                "go on",
                "what else",
            ],
            handler=self.continue_reading,
            threshold=0.50,
        )

        # --- News count ---
        self.register_semantic_intent(
            examples=[
                "how many headlines do I have",
                "any new articles",
                "how many news stories",
                "do I have any news",
                "any new headlines",
            ],
            handler=self.news_count,
            threshold=0.58,
        )

        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Fallback handler (semantic intents handle routing)."""
        return self.read_news()

    def _get_manager(self):
        """Get the NewsManager singleton."""
        mgr = get_news_manager()
        if mgr is None:
            return None
        return mgr

    def _detect_category(self, text: str = None) -> str:
        """Detect which news category the user is asking about."""
        if not text:
            text = getattr(self, '_last_user_text', '')
        text_lower = text.lower()

        for keyword, category in _CATEGORY_MAP.items():
            if keyword in text_lower:
                return category

        return None

    def _detect_urgency(self, text: str = None) -> int:
        """Detect urgency level from user text. Returns max_priority or None."""
        if not text:
            text = getattr(self, '_last_user_text', '')
        text_lower = text.lower()

        for keyword, priority in _URGENCY_MAP.items():
            if keyword in text_lower:
                return priority

        return None

    def read_news(self) -> str:
        """Read top headlines across all categories."""
        mgr = self._get_manager()
        if not mgr:
            return self.respond(f"The news system isn't available at the moment, {self.honorific}.")

        text = getattr(self, '_last_user_text', '') if hasattr(self.conversation, '_last_user_message') else ""

        # Check if user mentioned a specific category
        category = self._detect_category(text)
        max_priority = self._detect_urgency(text)

        if category:
            return self._read_for_category(mgr, category, max_priority=max_priority)

        response = mgr.read_headlines(limit=5, max_priority=max_priority)

        # Request follow-up window for "pull that up" / "more headlines"
        self.conversation.request_follow_up = 15.0

        return self.respond(response)

    def read_category(self) -> str:
        """Read headlines for a specific category."""
        mgr = self._get_manager()
        if not mgr:
            return self.respond(f"The news system isn't available at the moment, {self.honorific}.")

        text = getattr(self, '_last_user_text', '') if hasattr(self.conversation, '_last_user_message') else ""
        category = self._detect_category(text)
        max_priority = self._detect_urgency(text)

        if not category:
            return self.respond(
                f"Which category would you like, {self.honorific}? "
                "I have tech, cybersecurity, politics, general, and local."
            )

        return self._read_for_category(mgr, category, max_priority=max_priority)

    def _read_for_category(self, mgr, category: str, max_priority: int = None) -> str:
        """Read headlines for a given category."""
        response = mgr.read_headlines(
            category=category, limit=5, max_priority=max_priority
        )
        self.conversation.request_follow_up = 15.0
        return self.respond(response)

    def continue_reading(self) -> str:
        """Continue reading the next batch of headlines."""
        mgr = self._get_manager()
        if not mgr:
            return self.respond(f"The news system isn't available at the moment, {self.honorific}.")

        remaining = mgr.get_unread_count()
        total_remaining = sum(remaining.values())

        if total_remaining == 0:
            return self.respond(f"That's all the headlines I have for now, {self.honorific}.")

        response = mgr.read_headlines(limit=5)
        self.conversation.request_follow_up = 15.0
        return self.respond(response)

    def news_count(self) -> str:
        """Report how many unread headlines are available."""
        mgr = self._get_manager()
        if not mgr:
            return self.respond(f"The news system isn't available at the moment, {self.honorific}.")

        response = mgr.get_headline_count_response()
        self.conversation.request_follow_up = 15.0
        return self.respond(response)
