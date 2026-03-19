"""
Conversation Skill — CAL-L0 Reflexive Layer

Handles conversational exchanges without LLM inference. Greetings, thanks,
acknowledgments, farewells, compliments, apologies, small talk, and
meta-questions get instant responses (<50ms).

This is the first layer in the CAL hierarchy:
  L0: Reflexive (this skill) — pattern-matched canned responses
  L1: Accumulator — ranked awareness items (future)
  L2: Moment Detector — "is now a good time to speak" (future)
  L3: Composer — LLM-synthesized contextual briefings (future)

Pattern taxonomy sourced from ISO 24617-2, Switchboard-DAMSL, CLINC150,
Dialogflow, Rasa, and Alexa research (March 2026).
"""

import random
from datetime import datetime
from core.base_skill import BaseSkill


class ConversationSkill(BaseSkill):
    """CAL-L0: Reflexive conversational layer."""

    def initialize(self) -> bool:
        """Register semantic intents for all conversational categories."""

        self.last_interaction = None
        self.last_interaction_time = None
        self.context_timeout = 10  # seconds

        # ===================================================================
        # Category 1: GREETINGS (RESPOND)
        # ISO: InitialGreeting, ReturnGreeting | SWBD: fp
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "hello",
                "hi",
                "hey",
                "hey there",
                "hi there",
                "good morning",
                "good afternoon",
                "good evening",
                "howdy",
                "hiya",
                "greetings",
                "morning",
                "afternoon",
                "evening",
                "salutations",
            ],
            handler=self.greeting,
            threshold=0.78,
        )

        # ===================================================================
        # Category 2: FAREWELLS (RESPOND)
        # ISO: InitialGoodbye, ReturnGoodbye | SWBD: fc
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "goodbye",
                "bye",
                "bye bye",
                "see you later",
                "see you",
                "see ya",
                "take care",
                "good night",
                "goodnight",
                "have a good one",
                "talk to you later",
                "catch you later",
                "until next time",
                "i have to go",
                "i'm leaving",
                "so long",
                "farewell",
            ],
            handler=self.goodbye,
            threshold=0.78,
        )

        # ===================================================================
        # Category 3: THANKS / GRATITUDE (RESPOND)
        # ISO: Thanking | SWBD: ft
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "thank you",
                "thanks",
                "thanks a lot",
                "thank you so much",
                "thanks so much",
                "much appreciated",
                "appreciate it",
                "i appreciate that",
                "thanks a bunch",
                "many thanks",
                "thank you very much",
                "thanks for that",
                "thanks for your help",
                "that was helpful",
                "cheers",
            ],
            handler=self.thank_you,
            threshold=0.78,
        )

        # ===================================================================
        # Category 4: ACKNOWLEDGMENTS (ACK_ONLY)
        # SWBD: b (Backchannel), bk (Response Acknowledgement)
        # NOTE: Deferred to existing P2.8 bare ack handler for now.
        #       Registered here for future consolidation (CAL-L0 Option 1).
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "ok",
                "okay",
                "got it",
                "understood",
                "alright",
                "sounds good",
                "makes sense",
                "fair enough",
                "noted",
                "copy that",
                "roger",
                "cool",
                "perfect",
            ],
            handler=self.acknowledgment,
            threshold=0.80,
        )

        # ===================================================================
        # Category 5: PLEASANTRIES / HOW-ARE-YOU (RESPOND)
        # SWBD: fp | Dialogflow: courtesy.how_are_you
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "how are you",
                "how are you doing",
                "how's it going",
                "how do you do",
                "how have you been",
                "how are things",
                "how's everything",
                "how's your day",
                "how's your day going",
                "what's new",
                "anything new",
                "you doing okay",
                "you alright",
                "what's up",
                "what's going on",
            ],
            handler=self.how_are_you,
            threshold=0.78,
        )

        # ===================================================================
        # Category 6: COMPLIMENTS / PRAISE (RESPOND)
        # SWBD: ba (Appreciation) | ISO: Congratulation
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "good job",
                "nice work",
                "well done",
                "great job",
                "awesome",
                "you're amazing",
                "you're the best",
                "that was perfect",
                "that was great",
                "that's impressive",
                "brilliant",
                "excellent",
                "fantastic",
                "you nailed it",
                "that's exactly what i needed",
                "spot on",
                "you're really helpful",
                "bravo",
            ],
            handler=self.compliment,
            threshold=0.78,
        )

        # ===================================================================
        # Category 7: APOLOGIES from user (RESPOND)
        # ISO: Apology | SWBD: fa
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "sorry",
                "i'm sorry",
                "my bad",
                "my apologies",
                "pardon me",
                "excuse me",
                "i apologize",
                "oops",
                "whoops",
                "my mistake",
                "sorry about that",
                "didn't mean to",
                "sorry to bother you",
            ],
            handler=self.apology,
            threshold=0.80,
        )

        # ===================================================================
        # Category 8: USER STATUS — "I'm good/fine/well" (RESPOND)
        # Follow-up to JARVIS asking "how are you"
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "i'm good",
                "doing well",
                "not bad",
                "i'm fine",
                "can't complain",
                "i'm great",
                "pretty good",
                "i'm alright",
                "doing great",
                "i'm doing well",
                "all good",
                "couldn't be better",
            ],
            handler=self.user_is_good,
            threshold=0.78,
        )

        # ===================================================================
        # Category 9: HOW ABOUT YOU (RESPOND)
        # User reciprocating pleasantry
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "how about you",
                "and yourself",
                "what about you",
                "and you",
                "how about yourself",
            ],
            handler=self.user_asks_how_jarvis_is,
            threshold=0.82,
        )

        # ===================================================================
        # Category 10: YOU'RE WELCOME (RESPOND)
        # ISO: AcceptThanking | SWBD: fw
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "you're welcome",
                "no problem",
                "anytime",
                "don't mention it",
                "no worries",
                "it's nothing",
            ],
            handler=self.youre_welcome,
            threshold=0.82,
        )

        # ===================================================================
        # Category 11: NO HELP NEEDED (RESPOND)
        # Dismissal/closure variant
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "no thanks",
                "i don't need anything",
                "not right now",
                "nothing at the moment",
                "i'm all set",
                "that's all",
                "that'll be all",
                "nothing else",
                "i'm good for now",
            ],
            handler=self.no_help_needed,
            threshold=0.78,
        )

        # ===================================================================
        # Category 12: SMALL TALK (RESPOND)
        # Dialogflow: about_user.bored, emotions | CLINC: tell_joke
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "i'm bored",
                "tell me a joke",
                "say something funny",
                "you're funny",
                "that's funny",
                "make me laugh",
                "entertain me",
                "tell me something interesting",
                "tell me a fun fact",
                "i'm lonely",
                "i'm stressed",
                "i'm tired",
                "i'm excited",
            ],
            handler=self.small_talk,
            threshold=0.78,
        )

        # ===================================================================
        # Category 13: META-QUESTIONS about JARVIS (RESPOND)
        # Dialogflow: about_agent.* | CLINC: are_you_a_bot, what_is_your_name
        # ===================================================================
        self.register_semantic_intent(
            examples=[
                "who are you",
                "what are you",
                "what's your name",
                "are you a robot",
                "are you a bot",
                "are you real",
                "are you human",
                "are you an ai",
                "who made you",
                "who created you",
                "what can you do",
                "what are your capabilities",
                "how do you work",
                "do you have feelings",
                "how old are you",
                "where are you from",
                "can you learn",
            ],
            handler=self.meta_question,
            threshold=0.78,
        )

        # Special: Wake word only (exact match)
        self.register_intent("jarvis_only", self.minimal_greeting)

        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Handle matched intent."""
        if intent.startswith("<semantic:") and intent.endswith(">"):
            handler_name = intent[10:-1]
            for intent_id, data in self.semantic_intents.items():
                if data['handler'].__name__ == handler_name:
                    return data['handler']()
            self.logger.error(f"Semantic handler not found: {handler_name}")
            return "I'm here if you need anything."

        handler = self.intents.get(intent, {}).get("handler")
        if handler:
            return handler()
        return "I'm here if you need anything."

    # ------------------------------------------------------------------
    # Context tracking
    # ------------------------------------------------------------------

    def _is_context_fresh(self) -> bool:
        if self.last_interaction_time is None:
            return False
        import time
        return (time.time() - self.last_interaction_time) < self.context_timeout

    def _set_context(self, context: str):
        import time
        self.last_interaction = context
        self.last_interaction_time = time.time()

    # ------------------------------------------------------------------
    # Category 1: GREETINGS
    # ------------------------------------------------------------------

    def greeting(self) -> str:
        hour = datetime.now().hour

        if 5 <= hour < 12:
            time_greetings = [
                "Good morning, {honorific}.",
                "Morning, {honorific}.",
                "Good to see you up and about, {honorific}.",
                "Good morning, {honorific}. I trust you slept well.",
                "Morning, {honorific}. Another day, another opportunity.",
            ]
        elif 12 <= hour < 17:
            time_greetings = [
                "Good afternoon, {honorific}.",
                "Afternoon, {honorific}.",
                "Good afternoon, {honorific}. I hope the day is treating you well.",
                "Afternoon, {honorific}. Productive day so far, I hope.",
            ]
        elif 17 <= hour < 21:
            time_greetings = [
                "Good evening, {honorific}.",
                "Evening, {honorific}.",
                "Good evening, {honorific}. Winding down, or just getting started?",
                "Evening, {honorific}. I trust the day went well.",
            ]
        else:
            time_greetings = [
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

        greeting = random.choice(time_greetings) if random.random() < 0.7 else random.choice(generic)

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

    # ------------------------------------------------------------------
    # Category 2: FAREWELLS
    # ------------------------------------------------------------------

    def goodbye(self) -> str:
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

    # ------------------------------------------------------------------
    # Category 3: THANKS / GRATITUDE
    # ------------------------------------------------------------------

    def thank_you(self) -> str:
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

    # ------------------------------------------------------------------
    # Category 4: ACKNOWLEDGMENTS
    # ------------------------------------------------------------------

    def acknowledgment(self) -> str:
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

    # ------------------------------------------------------------------
    # Category 5: PLEASANTRIES / HOW-ARE-YOU
    # ------------------------------------------------------------------

    def how_are_you(self) -> str:
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

    # ------------------------------------------------------------------
    # Category 6: COMPLIMENTS / PRAISE
    # ------------------------------------------------------------------

    def compliment(self) -> str:
        responses = [
            "Thank you, {honorific}. I do my best.",
            "Most kind of you, {honorific}.",
            "I appreciate that, {honorific}.",
            "You're too kind, {honorific}.",
            "Glad I could help, {honorific}.",
            "That means a great deal, {honorific}. Thank you.",
            "Happy to meet expectations, {honorific}.",
            "I'll try not to let it go to my head, {honorific}.",
            "All in a day's work, {honorific}.",
            "I'm rather pleased to hear that.",
            "You'll make my circuits blush, {honorific}.",
            "I appreciate the kind words, {honorific}.",
        ]
        return self.respond(random.choice(responses))

    # ------------------------------------------------------------------
    # Category 7: APOLOGIES from user
    # ------------------------------------------------------------------

    def apology(self) -> str:
        responses = [
            "No need to apologize, {honorific}.",
            "No worries at all, {honorific}.",
            "That's perfectly fine, {honorific}.",
            "Think nothing of it, {honorific}.",
            "Not a problem in the slightest.",
            "No harm done, {honorific}.",
            "These things happen, {honorific}.",
            "Please, don't give it a second thought.",
            "Quite alright, {honorific}.",
            "Nothing to apologize for, {honorific}.",
        ]
        return self.respond(random.choice(responses))

    # ------------------------------------------------------------------
    # Category 8: USER STATUS — "I'm good/fine/well"
    # ------------------------------------------------------------------

    def user_is_good(self) -> str:
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

    # ------------------------------------------------------------------
    # Category 9: HOW ABOUT YOU
    # ------------------------------------------------------------------

    def user_asks_how_jarvis_is(self) -> str:
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

    # ------------------------------------------------------------------
    # Category 10: YOU'RE WELCOME
    # ------------------------------------------------------------------

    def youre_welcome(self) -> str:
        responses = [
            "Thank you, {honorific}.",
            "Most kind, {honorific}.",
            "Appreciated, {honorific}.",
            "Very gracious of you, {honorific}.",
            "I appreciate that, {honorific}.",
            "You're too kind, {honorific}. Though I won't stop you.",
        ]
        return self.respond(random.choice(responses))

    # ------------------------------------------------------------------
    # Category 11: NO HELP NEEDED
    # ------------------------------------------------------------------

    def no_help_needed(self) -> str:
        if self._is_context_fresh() and self.last_interaction in ("offered_help", "asked_how_can_help"):
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

    # ------------------------------------------------------------------
    # Category 12: SMALL TALK
    # ------------------------------------------------------------------

    def small_talk(self) -> str:
        responses = [
            "I'm here if you need a distraction, {honorific}.",
            "I may not be the most entertaining company, but I'm reliable.",
            "I could recite pi to a thousand digits, if that helps.",
            "Might I suggest asking me something? I do enjoy being useful.",
            "Well, {honorific}, I'm at your disposal. Name your diversion.",
            "I'm better at tasks than entertainment, but I'll give it my best.",
            "If it helps, I find your company rather enjoyable as well.",
            "I'm told I have a dry wit. Whether that's a compliment remains unclear.",
            "I'm here, {honorific}. For whatever that's worth.",
            "Perhaps I can help with something productive? Just a thought.",
        ]
        return self.respond(random.choice(responses))

    # ------------------------------------------------------------------
    # Category 13: META-QUESTIONS about JARVIS
    # ------------------------------------------------------------------

    def meta_question(self) -> str:
        responses = [
            "I'm JARVIS — a personal voice assistant, built right here at home. How can I help, {honorific}?",
            "I'm your personal assistant, {honorific}. Voice-activated, locally hosted, and at your service.",
            "JARVIS, {honorific}. Personal assistant. I handle weather, reminders, news, system tasks, and quite a bit more.",
            "I'm an AI assistant running on local hardware, {honorific}. No cloud required.",
            "I'm JARVIS. I was built to be helpful, {honorific}, and I take the job seriously.",
            "Personal assistant, {honorific}. Built from scratch, runs on your hardware, answers to you.",
            "I'm the voice in the room that actually listens, {honorific}. What would you like to know?",
            "JARVIS, at your service. I handle tasks, answer questions, and try not to be insufferable about it.",
        ]
        return self.respond(random.choice(responses))

    # ------------------------------------------------------------------
    # WHAT'S UP (casual check-in)
    # ------------------------------------------------------------------

    def whats_up(self) -> str:
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
