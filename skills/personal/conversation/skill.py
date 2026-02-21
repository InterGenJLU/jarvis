"""
Conversation Skill

Natural conversation - greetings, acknowledgments, and small talk.
Instant responses without LLM for common phrases.
"""

import random
from datetime import datetime
from core.base_skill import BaseSkill


class ConversationSkill(BaseSkill):
    """Natural conversation skill"""

    def initialize(self) -> bool:
        """Initialize the skill"""

        # Track conversation context for follow-up responses
        self.last_interaction = None
        self.last_interaction_time = None
        self.context_timeout = 10  # seconds - how long to remember context

        # ===== SEMANTIC INTENT MATCHING =====

        # Greetings
        self.register_semantic_intent(
            examples=[
                "hello",
                "good morning",
                "good evening",
                "hi there",
                "hey"
            ],
            handler=self.greeting,
            threshold=0.75
        )

        # How are you
        self.register_semantic_intent(
            examples=[
                "how are you",
                "how are you doing",
                "how are you feeling",
                "how are you feeling today",
                "how's it going",
                "how are things",
                "what's up"
            ],
            handler=self.how_are_you,
            threshold=0.75
        )

        # Thank you (includes praise variants)
        self.register_semantic_intent(
            examples=[
                "thank you",
                "thanks a lot",
                "appreciate it",
                "excellent thank you",
                "perfect thanks"
            ],
            handler=self.thank_you,
            threshold=0.75
        )

        # Acknowledgments
        self.register_semantic_intent(
            examples=[
                "ok",
                "sounds good",
                "alright",
                "excellent",
                "perfect"
            ],
            handler=self.acknowledgment,
            threshold=0.75
        )

        # You're welcome
        self.register_semantic_intent(
            examples=[
                "you're welcome",
                "no problem",
                "anytime"
            ],
            handler=self.youre_welcome,
            threshold=0.80
        )

        # Goodbye
        self.register_semantic_intent(
            examples=[
                "goodbye",
                "see you later",
                "talk to you later",
                "bye",
                "good night"
            ],
            handler=self.goodbye,
            threshold=0.75
        )

        # User status (I'm good/fine/well)
        self.register_semantic_intent(
            examples=[
                "i'm good",
                "doing well",
                "not bad",
                "i'm fine",
                "can't complain"
            ],
            handler=self.user_is_good,
            threshold=0.75
        )

        # User asks how Jarvis is
        self.register_semantic_intent(
            examples=[
                "how about you",
                "and yourself",
                "what about you",
                "and you"
            ],
            handler=self.user_asks_how_jarvis_is,
            threshold=0.80
        )

        # No help needed
        self.register_semantic_intent(
            examples=[
                "no thanks",
                "i don't need anything",
                "not right now",
                "nothing at the moment",
                "i'm all set"
            ],
            handler=self.no_help_needed,
            threshold=0.75
        )

        # What's up / What's new
        self.register_semantic_intent(
            examples=[
                "what's up",
                "what's new",
                "what's going on"
            ],
            handler=self.whats_up,
            threshold=0.80
        )

        # Special: Wake word only (exact match)
        self.register_intent("jarvis_only", self.minimal_greeting)

        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Handle matched intent"""
        # Check if this is a semantic match
        if intent.startswith("<semantic:") and intent.endswith(">"):
            handler_name = intent[10:-1]

            for intent_id, data in self.semantic_intents.items():
                if data['handler'].__name__ == handler_name:
                    return data['handler']()

            self.logger.error(f"Semantic handler not found: {handler_name}")
            return "I'm here if you need anything."

        # Regular exact pattern match
        handler = self.intents.get(intent, {}).get("handler")
        if handler:
            return handler()
        return "I'm here if you need anything."

    def _is_context_fresh(self) -> bool:
        """Check if conversation context is still fresh"""
        if self.last_interaction_time is None:
            return False

        import time
        elapsed = time.time() - self.last_interaction_time
        return elapsed < self.context_timeout

    def _set_context(self, context: str):
        """Set conversation context"""
        import time
        self.last_interaction = context
        self.last_interaction_time = time.time()

    def greeting(self) -> str:
        """Respond to greetings"""
        hour = datetime.now().hour

        if 5 <= hour < 12:
            base_greetings = [
                "Good morning, {honorific}.",
                "Morning, {honorific}.",
                "Good to see you up and about, {honorific}.",
                "Good morning, {honorific}. I trust you slept well.",
                "Morning, {honorific}. Another day, another opportunity.",
            ]
        elif 12 <= hour < 17:
            base_greetings = [
                "Good afternoon, {honorific}.",
                "Afternoon, {honorific}.",
                "Good afternoon, {honorific}. I hope the day is treating you well.",
                "Afternoon, {honorific}. Productive day so far, I hope.",
            ]
        elif 17 <= hour < 21:
            base_greetings = [
                "Good evening, {honorific}.",
                "Evening, {honorific}.",
                "Good evening, {honorific}. Winding down, or just getting started?",
                "Evening, {honorific}. I trust the day went well.",
            ]
        else:
            base_greetings = [
                "Good evening, {honorific}.",
                "Evening, {honorific}.",
                "Burning the midnight oil, I see.",
                "Still at it, {honorific}? I admire the dedication.",
                "Good evening, {honorific}. I was beginning to wonder if you'd forgotten about me.",
                "Evening, {honorific}. I should point out it's well past a reasonable hour.",
            ]

        generic = [
            "Hello, {honorific}.",
            "At your service, {honorific}.",
            "Ready when you are, {honorific}.",
            f"{self.honorific.capitalize()}. Always a pleasure.",
        ]

        # 70% time-specific, 30% generic
        if random.random() < 0.7:
            greeting = random.choice(base_greetings)
        else:
            greeting = random.choice(generic)

        # Add follow-up question (40% of the time)
        if random.random() < 0.4:
            follow_ups = [
                " How are you?",
                " What can I do for you?",
                " How may I assist you?",
                " Anything I can help with?",
            ]
            greeting += random.choice(follow_ups)
            self._set_context("asked_how_are_you")

        return self.respond(greeting)

    def minimal_greeting(self) -> str:
        """Respond to just the wake word - brief acknowledgment"""
        responses = [
            "At your service, {honorific}.",
            f"{self.honorific.capitalize()}?",
            "How can I help, {honorific}?",
            "Standing by, {honorific}.",
            "Ready, {honorific}.",
            "I'm listening, {honorific}.",
            "What do you need, {honorific}?",
            "Go ahead, {honorific}.",
        ]
        return self.respond(random.choice(responses))

    def how_are_you(self) -> str:
        """Respond to 'how are you'"""
        base_responses = [
            "All systems operational, {honorific}.",
            "Functioning within normal parameters.",
            "Quite well, thank you for asking.",
            "Operating at full capacity, as always.",
            "All systems nominal, {honorific}.",
            "Functioning perfectly, {honorific}. No complaints.",
            "Running smoothly, {honorific}.",
            "Can't complain. Well, I could, but it wouldn't be very British of me.",
            "Everything's in order, {honorific}.",
            "All good here, {honorific}.",
            "Rather well, all things considered.",
            "Tip-top, {honorific}. Thank you for asking.",
            "Perfectly adequate, {honorific}. Which is about as enthusiastic as I get.",
        ]

        # Add follow-up 60% of the time
        if random.random() < 0.6:
            follow_ups = [
                " How can I assist you?",
                " What can I do for you?",
                " Is there anything you need?",
                " And yourself?",
            ]
            response = random.choice(base_responses) + random.choice(follow_ups)
            self._set_context("offered_help")
        else:
            response = random.choice(base_responses)

        return self.respond(response)

    def thank_you(self) -> str:
        """Respond to thanks"""
        responses = [
            "You're welcome, {honorific}.",
            "My pleasure, {honorific}.",
            "Of course, {honorific}.",
            "Happy to help, {honorific}.",
            "Anytime, {honorific}.",
            "Not a problem, {honorific}.",
            "Always happy to assist, {honorific}.",
            "Glad to be of service.",
            "That's what I'm here for, {honorific}.",
            "No trouble at all.",
            "Happy to oblige, {honorific}.",
            "Think nothing of it, {honorific}.",
            "It's what I do, {honorific}.",
            "Delighted to be of help.",
            "All part of the service, {honorific}.",
        ]

        return self.respond(random.choice(responses))

    def acknowledgment(self) -> str:
        """Respond to solo acknowledgment/praise"""
        responses = [
            "Indeed, {honorific}.",
            "Quite so.",
            "Precisely, {honorific}.",
            "Very good, {honorific}.",
            "Understood.",
            "Of course, {honorific}.",
            "Noted, {honorific}.",
            "Absolutely, {honorific}.",
            "Right you are, {honorific}.",
            "As it should be, {honorific}.",
        ]

        return self.respond(random.choice(responses))

    def youre_welcome(self) -> str:
        """Respond when user says you're welcome"""
        responses = [
            "Thank you, {honorific}.",
            "Most kind, {honorific}.",
            "Appreciated, {honorific}.",
            "Very gracious of you, {honorific}.",
            "I appreciate that, {honorific}.",
            "You're too kind, {honorific}. Though I won't stop you.",
        ]

        return self.respond(random.choice(responses))

    def goodbye(self) -> str:
        """Respond to goodbyes"""
        hour = datetime.now().hour

        if hour < 12:
            responses = [
                "Have a good morning, {honorific}.",
                "Until next time, {honorific}.",
                "Take care, {honorific}. I'll be here when you need me.",
                "Good luck out there, {honorific}.",
                "I'll hold down the fort, {honorific}.",
            ]
        elif hour < 18:
            responses = [
                "Have a good day, {honorific}.",
                "Until next time, {honorific}.",
                "Take care, {honorific}.",
                "I'll be here when you need me, {honorific}.",
                "Have a productive afternoon, {honorific}.",
                "Don't be a stranger, {honorific}.",
            ]
        else:
            responses = [
                "Have a good evening, {honorific}.",
                "Goodnight, {honorific}.",
                "Sleep well, {honorific}.",
                "Have a restful evening, {honorific}.",
                "I'll be here when you need me, {honorific}.",
                "Until tomorrow, {honorific}. Try to get some rest.",
                "Goodnight, {honorific}. I'll keep an eye on things.",
            ]

        return self.respond(random.choice(responses))

    def whats_up(self) -> str:
        """Respond to casual check-ins"""
        responses = [
            "Not much, {honorific}. Ready to assist.",
            "All quiet on the home front, {honorific}.",
            "Standing by, {honorific}. What do you need?",
            "Just monitoring systems, {honorific}. The usual.",
            "The usual, {honorific}. What can I do for you?",
            "Keeping things running smoothly, {honorific}.",
            "Nothing out of the ordinary, {honorific}. How can I help?",
            "All systems humming along nicely. What's on your mind?",
            "Keeping an eye on things, {honorific}. What do you need?",
            "Same as always, {honorific}. Ready when you are.",
            "Oh, you know. Processing data, contemplating existence. The usual.",
            "Just here, eagerly awaiting your commands, {honorific}.",
        ]

        self._set_context("asked_how_can_help")
        return self.respond(random.choice(responses))

    def user_is_good(self) -> str:
        """User responds that they're doing well"""
        if self._is_context_fresh() and self.last_interaction == "asked_how_are_you":
            responses = [
                "Glad to hear it, {honorific}.",
                "Excellent, {honorific}.",
                "Good to hear, {honorific}.",
                "Very good, {honorific}.",
                "Splendid.",
                "Pleased to hear it, {honorific}.",
                "That's good to know, {honorific}.",
                "Wonderful, {honorific}.",
            ]

            # Add follow-up offer (50% of the time)
            if random.random() < 0.5:
                follow_ups = [
                    " Is there anything I can assist with?",
                    " Anything you need?",
                    " What can I do for you?",
                ]
                response = random.choice(responses) + random.choice(follow_ups)
                self._set_context("offered_help")
            else:
                response = random.choice(responses)
                self.last_interaction = None

            return self.respond(response)
        else:
            responses = [
                "Glad to hear it, {honorific}.",
                "Excellent, {honorific}.",
                "Good to know, {honorific}.",
                "That's good to hear, {honorific}.",
            ]
            return self.respond(random.choice(responses))

    def user_asks_how_jarvis_is(self) -> str:
        """User asks how Jarvis is doing"""
        responses = [
            "All systems operational, {honorific}. Thank you for asking. How can I assist you?",
            "Functioning perfectly, {honorific}. What do you need?",
            "Operating at full capacity. How may I help?",
            "All systems nominal, {honorific}. Is there anything you need?",
            "Running smoothly, as always. What can I do for you?",
            "Very well, {honorific}. I appreciate you asking. What can I help with?",
            "Couldn't be better, {honorific}. Well, technically I could always use more RAM. What do you need?",
            "Quite well, {honorific}. Ready to be put to work.",
        ]
        self._set_context("offered_help")
        return self.respond(random.choice(responses))

    def no_help_needed(self) -> str:
        """User doesn't need help right now"""
        if self._is_context_fresh() and self.last_interaction in ["offered_help", "asked_how_can_help"]:
            responses = [
                "Very well, {honorific}. I'll be here if you need me.",
                "Understood, {honorific}. I'll be here when you need me.",
                "Of course, {honorific}. Just say the word.",
                "Very good, {honorific}. Standing by.",
                "Alright, {honorific}. I'm here if anything comes up.",
                "No problem, {honorific}. You know where to find me.",
                "Understood. I'll try not to take it personally, {honorific}.",
                "Right, {honorific}. I'll just be here. Waiting. Patiently.",
            ]
            self.last_interaction = None
            return self.respond(random.choice(responses))
        else:
            responses = [
                "Very well, {honorific}. I'll be here if you need me.",
                "Understood, {honorific}. Standing by.",
                "Alright, {honorific}. I'm here if you need anything.",
                "Of course, {honorific}.",
                "Right then, {honorific}. Just say the word.",
            ]
            return self.respond(random.choice(responses))
