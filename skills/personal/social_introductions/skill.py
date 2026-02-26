"""
Social Introductions Skill

Multi-turn introduction flow and ad-hoc person fact management.
Handles "meet my niece Arya", "who is Arya", "Arya loves horses",
and pronunciation coaching via iterative TTS feedback.
"""

import re
import time
from enum import Enum
from typing import Optional

from core.base_skill import BaseSkill
from core import persona


class IntroState(Enum):
    """States for the multi-turn introduction flow."""
    IDLE = "idle"
    AWAITING_NAME_CONFIRM = "awaiting_name_confirm"
    AWAITING_PRONUNCIATION_CHECK = "awaiting_pronunciation_check"
    AWAITING_PRONUNCIATION_CORRECTION = "awaiting_pronunciation_correction"
    AWAITING_FACTS = "awaiting_facts"


class SocialIntroductionsSkill(BaseSkill):
    """Introduce people to JARVIS and manage contacts."""

    # Relationship words for extraction
    RELATIONSHIPS = frozenset({
        "niece", "nephew", "brother", "sister", "mom", "dad", "mother", "father",
        "friend", "colleague", "coworker", "boss", "neighbor", "wife", "husband",
        "son", "daughter", "uncle", "aunt", "cousin", "grandma", "grandpa",
        "grandmother", "grandfather", "partner", "fiancee",
        "boyfriend", "girlfriend", "roommate", "mentor", "student", "teacher",
        "stepbrother", "stepsister", "stepdad", "stepmom",
    })

    # Affirmative / negative word sets for pronunciation check
    _AFFIRM = frozenset({
        "yes", "yeah", "yep", "correct", "right", "perfect",
        "exactly", "good", "great", "spot on", "nailed it",
    })
    _DENY = frozenset({
        "no", "nope", "nah", "wrong", "close", "not quite",
    })
    # Done signals for facts phase
    _DONE = frozenset({
        "no", "nope", "nothing", "nah", "done",
    })
    _DONE_PHRASES = frozenset({
        "that's all", "that's it", "nothing else", "no thanks",
        "no thank you", "i'm good", "all good",
    })

    def initialize(self) -> bool:
        """Register semantic intents for introduction commands."""
        # State machine
        self._state = IntroState.IDLE
        self._pending_name: Optional[str] = None
        self._pending_rel: Optional[str] = None
        self._pending_person_id: Optional[str] = None
        self._state_expiry: float = 0

        # --- Introduce someone ---
        self.register_semantic_intent(
            examples=[
                "meet my niece Arya",
                "this is my brother Jake",
                "I'd like you to meet my friend Sarah",
                "my coworker's name is Dave",
                "let me introduce my wife Lisa",
                "I want to introduce you to my cousin Marcus",
                "meet my neighbor Tom",
                "my son's name is Ethan",
            ],
            handler=self.introduce_person,
            threshold=0.55,
        )

        # --- Who is someone ---
        self.register_semantic_intent(
            examples=[
                "who is Arya",
                "what do you know about Jake",
                "tell me about Sarah",
                "do you know who Dave is",
                "what do you remember about my niece",
            ],
            handler=self.who_is,
            threshold=0.62,
        )

        # --- Fix pronunciation ---
        self.register_semantic_intent(
            examples=[
                "that's not how you say Arya",
                "you're mispronouncing her name",
                "say her name differently",
                "pronounce Arya like Areea",
                "you're saying the name wrong",
            ],
            handler=self.fix_pronunciation,
            threshold=0.60,
        )

        # --- List known people ---
        self.register_semantic_intent(
            examples=[
                "who do you know",
                "list the people you know",
                "who have I introduced you to",
                "show me your contacts",
            ],
            handler=self.list_people,
            threshold=0.65,
        )

        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Route pattern-based intents (not used — we use semantic intents)."""
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]["handler"]
            return handler()
        return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def manager(self):
        """Lazy access to the PeopleManager singleton."""
        if not hasattr(self, "_manager_ref") or self._manager_ref is None:
            from core.people_manager import get_people_manager
            self._manager_ref = get_people_manager()
        return self._manager_ref

    @property
    def is_intro_active(self) -> bool:
        """Whether the introduction state machine is active."""
        if self._state == IntroState.IDLE:
            return False
        if time.time() > self._state_expiry:
            self._reset_state()
            return False
        return True

    # ------------------------------------------------------------------
    # State machine: called from router at P2.6
    # ------------------------------------------------------------------

    def handle_intro_turn(self, command: str) -> Optional[str]:
        """Process the next turn in the introduction flow.

        Called by ConversationRouter at Priority 2.6 when intro is active.
        Returns response text, or None to fall through.
        """
        if not self.is_intro_active:
            return None

        cmd_lower = command.strip().lower()
        words = set(re.findall(r'\b\w+\b', cmd_lower))

        if self._state == IntroState.AWAITING_NAME_CONFIRM:
            return self._handle_name_confirm(command)

        if self._state == IntroState.AWAITING_PRONUNCIATION_CHECK:
            return self._handle_pronunciation_check(command, cmd_lower, words)

        if self._state == IntroState.AWAITING_PRONUNCIATION_CORRECTION:
            return self._handle_pronunciation_correction(command)

        if self._state == IntroState.AWAITING_FACTS:
            return self._handle_facts_input(command, cmd_lower, words)

        return None

    # ------------------------------------------------------------------
    # State handlers (multi-turn flow)
    # ------------------------------------------------------------------

    def _handle_name_confirm(self, command: str) -> str:
        """User repeats the name for confirmation."""
        # Take the first real word as the name (strip filler)
        cleaned = command.strip()
        # Remove leading filler: "it's", "her name is", "his name is"
        cleaned = re.sub(
            r'^(?:it\'?s|her name is|his name is|their name is|the name is)\s+',
            '', cleaned, flags=re.IGNORECASE,
        ).strip()
        name = cleaned.split()[0] if cleaned else self._pending_name
        if name:
            self._pending_name = name.capitalize()

        # Create the person record now (pronunciation can be updated later)
        self._pending_person_id = self.manager.add_person(
            name=self._pending_name,
            relationship=self._pending_rel,
        )

        # Speak the name back and ask if pronunciation is correct
        self._state = IntroState.AWAITING_PRONUNCIATION_CHECK
        self._state_expiry = time.time() + 60
        return persona.intro_pron_check(self._pending_name)

    def _handle_pronunciation_check(self, command: str,
                                     cmd_lower: str, words: set) -> str:
        """User confirms or denies pronunciation correctness."""
        if words & self._AFFIRM or cmd_lower in self._AFFIRM:
            # Pronunciation accepted — move to facts
            self._state = IntroState.AWAITING_FACTS
            self._state_expiry = time.time() + 60
            return persona.intro_ask_facts(self._pending_name)

        if words & self._DENY or cmd_lower in self._DENY:
            # Ask for phonetic correction
            self._state = IntroState.AWAITING_PRONUNCIATION_CORRECTION
            self._state_expiry = time.time() + 60
            return (f"How should I say it, {self.honorific}? "
                    f"Say it slowly and I'll try to match.")

        # Ambiguous — treat as a pronunciation correction attempt
        return self._handle_pronunciation_correction(command)

    def _handle_pronunciation_correction(self, command: str) -> str:
        """User provides phonetic correction."""
        hint = command.strip()
        # Strip leading filler
        hint = re.sub(
            r'^(?:like|say it like|more like|it\'?s|it\'?s more like|say)\s+',
            '', hint, flags=re.IGNORECASE,
        ).strip()

        if not hint:
            hint = self._pending_name

        # Store pronunciation and update TTS
        if self._pending_person_id:
            self.manager.update_pronunciation(self._pending_person_id, hint)

        # Speak the corrected name and ask again
        self._state = IntroState.AWAITING_PRONUNCIATION_CHECK
        self._state_expiry = time.time() + 60
        return persona.intro_pron_corrected(hint)

    def _handle_facts_input(self, command: str,
                             cmd_lower: str, words: set) -> str:
        """User provides additional facts or signals done."""
        # Check for done signals
        if words & self._DONE or cmd_lower in self._DONE_PHRASES:
            rel = self._pending_rel or "contact"
            response = persona.intro_complete(self._pending_name, rel)
            self._reset_state()
            return response

        # Store the fact
        if self._pending_person_id:
            self.manager.add_person_fact(self._pending_person_id, command.strip())

        # Ask for more
        self._state_expiry = time.time() + 60
        return f"Got it. Anything else about {self._pending_name}, {self.honorific}?"

    # ------------------------------------------------------------------
    # Intent handlers (entry points from semantic matching)
    # ------------------------------------------------------------------

    def introduce_person(self, entities: dict = None) -> str:
        """Handle 'meet my niece Arya' commands."""
        text = getattr(self, "_last_user_text", "")
        name, rel = self._extract_name_and_relationship(text)

        if not name:
            return self.respond(
                f"I didn't quite catch the name, {self.honorific}. "
                f"Who would you like me to meet?",
            )

        # Check if person already exists
        existing = self.manager.get_person_by_name(name)
        if existing:
            existing_rel = existing.get("relationship") or "contact"
            return self.respond(
                f"I already know {name}, {self.honorific}. "
                f"Your {existing_rel}. Would you like to update anything?",
            )

        # Start the multi-turn introduction flow
        self._pending_name = name
        self._pending_rel = rel
        self._state = IntroState.AWAITING_NAME_CONFIRM
        self._state_expiry = time.time() + 60

        return self.respond(persona.intro_name_confirm(rel or "friend"))

    def who_is(self, entities: dict = None) -> str:
        """Handle 'who is Arya' queries."""
        text = getattr(self, "_last_user_text", "")
        name = self._extract_name_from_query(text)
        if not name:
            return self.respond(
                f"Who are you asking about, {self.honorific}?",
            )

        person = self.manager.get_person_with_facts(name)
        if not person:
            return self.respond(persona.intro_unknown(name))

        # Build natural response
        rel = person.get("relationship") or "someone you know"
        response = f"{person['name']} is your {rel}, {self.honorific}."
        facts = person.get("facts", [])
        if facts:
            fact_strs = [f["content"] for f in facts[:5]]
            response += " " + ". ".join(fact_strs) + "."
        return self.respond(response)

    def fix_pronunciation(self, entities: dict = None) -> str:
        """Handle explicit pronunciation fix requests."""
        text = getattr(self, "_last_user_text", "")
        people = self.manager.get_all_people()
        for person in people:
            if re.search(r'\b' + re.escape(person["name"]) + r'\b',
                         text, re.IGNORECASE):
                self._pending_name = person["name"]
                self._pending_person_id = person["person_id"]
                self._pending_rel = person.get("relationship")
                self._state = IntroState.AWAITING_PRONUNCIATION_CORRECTION
                self._state_expiry = time.time() + 60
                return self.respond(
                    f"Alright, {self.honorific}. "
                    f"How should I say {person['name']}?",
                )
        return self.respond(
            f"Which name are you referring to, {self.honorific}?",
        )

    def list_people(self, entities: dict = None) -> str:
        """List all known people."""
        people = self.manager.get_all_people()
        if not people:
            return self.respond(
                f"I don't know anyone yet, {self.honorific}. "
                f"Introduce someone by saying 'meet my friend Sarah'.",
            )

        if len(people) == 1:
            p = people[0]
            rel = p.get("relationship") or "contact"
            return self.respond(
                f"I know one person: {p['name']}, your {rel}.",
            )

        names = []
        for p in people:
            rel = p.get("relationship") or "contact"
            names.append(f"{p['name']}, your {rel}")
        joined = ", ".join(names[:-1]) + f", and {names[-1]}"
        return self.respond(
            f"I know {len(people)} people, {self.honorific}: {joined}.",
        )

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_name_and_relationship(self, text: str) -> tuple:
        """Extract name and relationship from introduction phrases.

        Returns (name: str | None, relationship: str | None).
        """
        text_clean = text.strip()

        # Pattern 1: "meet/introduce my [relationship] [Name]"
        for rel in self.RELATIONSHIPS:
            pattern = (
                rf"(?:meet|introduce|this is)"
                rf"\s+(?:you to\s+)?my\s+{re.escape(rel)}\s+(\w+)"
            )
            m = re.search(pattern, text_clean, re.IGNORECASE)
            if m:
                return m.group(1).capitalize(), rel

        # Pattern 2: "my [relationship]'s name is [Name]"
        for rel in self.RELATIONSHIPS:
            pattern = (
                rf"my\s+{re.escape(rel)}(?:'s)?\s+"
                rf"(?:name\s+is|is\s+(?:called|named))\s+(\w+)"
            )
            m = re.search(pattern, text_clean, re.IGNORECASE)
            if m:
                return m.group(1).capitalize(), rel

        # Pattern 3: "meet [Name], my [relationship]"
        m = re.search(r"meet\s+(\w+)\s*,?\s+my\s+(\w+)", text_clean, re.IGNORECASE)
        if m:
            name = m.group(1).capitalize()
            potential_rel = m.group(2).lower()
            if potential_rel in self.RELATIONSHIPS:
                return name, potential_rel

        # Pattern 4: "let me introduce [Name]" or "meet [Name]" (no relationship)
        m = re.search(
            r"(?:meet|introduce)\s+(?:you to\s+)?(\w+)",
            text_clean, re.IGNORECASE,
        )
        if m:
            name = m.group(1).capitalize()
            # Filter out articles/pronouns
            if name.lower() not in {"my", "a", "the", "our", "you", "him", "her"}:
                return name, None

        return None, None

    def _extract_name_from_query(self, text: str) -> Optional[str]:
        """Extract name from 'who is X' or 'tell me about X' queries."""
        # First check if any known person name appears
        people = self.manager.get_all_people()
        for person in people:
            if re.search(r'\b' + re.escape(person["name"]) + r'\b',
                         text, re.IGNORECASE):
                return person["name"]

        # Fallback: extract from query pattern
        m = re.search(
            r"(?:who is|who's|about|know about|know)\s+(\w+)",
            text, re.IGNORECASE,
        )
        if m:
            name = m.group(1).capitalize()
            if name.lower() not in {"my", "a", "the", "that", "this"}:
                return name
        return None

    def _reset_state(self):
        """Reset the state machine to idle."""
        self._state = IntroState.IDLE
        self._pending_name = None
        self._pending_rel = None
        self._pending_person_id = None
        self._state_expiry = 0
