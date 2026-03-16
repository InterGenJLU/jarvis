"""
Centralized command router — one router, three frontends.

Extracts the priority chain from pipeline.py into a shared class.
Each frontend (voice, console, web) creates a router with the same
components and calls route() to process commands.

Phase 3 of the Conversational Flow Refactor.
Phase 1 of LLM-centric migration: adds tool-calling path (P4-LLM).

Design principles:
    - Router handles decision logic and command execution (skill calls,
      memory ops, etc.) but NOT delivery (TTS, WebSocket, terminal printing).
    - Frontends call route() and handle RouteResult for their delivery.
    - One router, three frontends: voice/console/web all use the same code.
    - Semantic matcher PRUNES tools; LLM DECIDES which tool to call.
"""

import re
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from core import persona
from core.conversation_state import ConversationState
from core.honorific import set_honorific

logger = logging.getLogger("jarvis.router")

# Thread-local storage for per-request RouteContext.
# Set at the start of route(), cleared in finally.
# Allows properties (_user_id, _is_mobile, _is_guest) to resolve
# per-connection values without changing 30+ internal method signatures.
_router_thread_ctx = threading.local()


# ---------------------------------------------------------------------------
# Route context (per-request identity for session isolation)
# ---------------------------------------------------------------------------

@dataclass
class RouteContext:
    """Per-request identity passed from the frontend to the router.

    When provided, the router resolves user_id / client_type from this
    context instead of reading shared ConversationManager state.
    Voice and console paths pass None (backward compat — reads from
    self.conversation as before).
    """
    user_id: str | None = None
    client_type: str | None = None  # 'desktop' or 'mobile'
    client_id: str | None = None    # UUID from browser localStorage
    location: str | None = None
    away_geo: tuple[float, float] | None = None  # (lat, lon) if away from home


# ---------------------------------------------------------------------------
# Route result
# ---------------------------------------------------------------------------

@dataclass
class RouteResult:
    """Result of routing a command through the priority chain.

    Frontends use this to decide how to deliver the response (TTS, print,
    WebSocket) and what side effects to apply (window management, stats).
    """
    text: str = ""
    source: str = ""            # "canned", "skill", "memory"
    intent: str = ""            # Priority identifier (see route() docstring)
    handled: bool = False       # Command was fully handled by a priority
    open_window: float | None = None   # Open conversation window (seconds)
    close_window: bool = False  # Close conversation window
    skip: bool = False          # Drop silently (bare ack noise)
    match_info: dict | None = None     # Skill routing metadata
    used_llm: bool = False      # Whether the LLM was called (for stats)

    # LLM fallback context (populated when handled=False)
    llm_command: str = ""
    llm_history: str = ""
    memory_context: str | None = None
    context_messages: list | None = None
    llm_max_tokens: int | None = None

    # Tool-calling context (Phase 1 LLM-centric migration)
    # When set, frontends should pass these to stream_with_tools().
    use_tools: list | None = None           # List of tool schema dicts
    tool_temperature: float | None = None   # Override temp for tool selection
    tool_presence_penalty: float | None = None  # Qwen3.5 recommends 1.5
    synthesis_temperature: float | None = None  # Override temp for post-tool synthesis
    synthesis_category: str | None = None        # Domain category for prompt selection
    force_web_search: bool = False               # Force web_search (skip LLM tool decision)

    # Vision (multimodal image input)
    image_data: str | None = None           # Base64-encoded image for LLM


# Conversation window duration defaults (match ContinuousListener config)
EXTENDED_WINDOW = 8.0
DEFAULT_WINDOW = 5.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class ConversationRouter:
    """Shared command router — one router, three frontends.

    Encapsulates the priority chain that was previously duplicated across
    pipeline.py, jarvis_console.py, and jarvis_web.py.
    """

    # Dismissal phrases (moved from pipeline.Coordinator)
    _DISMISSAL_PHRASES = frozenset({
        "no", "no thanks", "no thank you", "nah", "nope",
        "not right now", "not at the moment", "not now",
        "that's all", "that's it", "that'll be all", "that will be all",
        "i'm good", "i'm fine", "all good", "all set",
        "nothing", "nothing else", "nothing for now",
        "never mind", "nevermind", "maybe later",
    })

    # Bare acknowledgments — noise during conversation windows unless
    # JARVIS just asked a question.
    _BARE_ACKS = frozenset({
        "yeah", "yep", "yes", "yup", "uh huh", "uh-huh", "uhuh",
        "ok", "okay", "sure", "right", "mm hmm", "mmhmm", "hmm",
        "no", "nah", "nope",
    })

    def __init__(self, *,
                 skill_manager,
                 conversation,
                 llm,
                 reminder_manager=None,
                 memory_manager=None,
                 news_manager=None,
                 context_window=None,
                 conv_state=None,
                 config=None,
                 web_researcher=None,
                 self_awareness=None,
                 task_planner=None,
                 people_manager=None,
                 awareness=None):
        self.skill_manager = skill_manager
        self.conversation = conversation
        self.llm = llm
        self.reminder_manager = reminder_manager
        self.memory_manager = memory_manager
        self.news_manager = news_manager
        self.context_window = context_window
        self.conv_state = conv_state or ConversationState()
        self.config = config
        self.web_researcher = web_researcher
        self.self_awareness = self_awareness
        self.task_planner = task_planner
        self.people_manager = people_manager
        self.awareness = awareness
        self._target_history = None  # Set per-request by jarvis_web.py

    @property
    def _user_id(self) -> str:
        """Current user ID, defaulting to 'christopher'.

        When a RouteContext is active (set by route()), resolves from
        the per-connection context instead of shared conversation state.
        """
        ctx = getattr(_router_thread_ctx, 'ctx', None)
        if ctx and ctx.user_id:
            return ctx.user_id
        return getattr(self.conversation, 'current_user', None) or "primary_user"

    @property
    def _is_guest(self) -> bool:
        """True when the current speaker is unrecognized (guest mode)."""
        return self._user_id == "__guest__"

    @property
    def _is_mobile(self) -> bool:
        """True when the session is from a mobile client.

        When a RouteContext is active, resolves from per-connection context.
        """
        ctx = getattr(_router_thread_ctx, 'ctx', None)
        if ctx and ctx.client_type:
            return ctx.client_type == "mobile"
        return getattr(self.conversation, 'client_type', 'desktop') == "mobile"

    @property
    def _away_geo(self) -> tuple[float, float] | None:
        """Raw (lat, lon) if current user is away from home, else None."""
        ctx = getattr(_router_thread_ctx, 'ctx', None)
        if ctx and ctx.away_geo:
            return ctx.away_geo
        return None

    def route(self, command: str, *,
              in_conversation: bool = False,
              doc_buffer=None,
              image_data: str = None,
              route_ctx: 'RouteContext | None' = None) -> RouteResult:
        """Route a command through the priority chain.

        Priority order:
            greeting  — wake word only / empty command
            P1        — Rundown acceptance/deferral
            P1.5      — Plan control (confirmation or active plan interrupt)
            P2        — Reminder acknowledgment
            P2.5      — Memory forget confirmation/cancellation
            P2.6      — Introduction state machine (social introductions)
            P2.7      — Dismissal detection (conversation window only)
            P2.8      — Bare acknowledgment filter (conversation window only)
            P3.1      — Active readback session (conversation window only)
            P3        — Memory operations (forget, transparency, fact, recall)
            P3.5      — Artifact reference resolution (conversation window only)
            P3.7      — News article pull-up
            Pre-P4    — Multi-step task planning (compound requests)
            P4        — Skill routing (skipped when doc_buffer active)
            P5        — News continuation
            LLM       — Prepare context for streaming (frontend handles delivery)

        Args:
            command: User's command text (wake word already stripped).
            in_conversation: Whether a conversation window is active.
            doc_buffer: DocumentBuffer instance (or None). When active,
                        skill routing is skipped and LLM gets document context.
            image_data: Base64-encoded image for multimodal queries.
            route_ctx: Per-connection identity (web). None for voice/console.

        Returns:
            RouteResult with response text, metadata, and side-effect signals.
        """
        # Install per-request context for thread-safe property + honorific resolution
        from core.honorific import set_thread_user, clear_thread_user
        _router_thread_ctx.ctx = route_ctx
        if route_ctx and route_ctx.user_id:
            set_thread_user(route_ctx.user_id)
        try:
            return self._route_inner(command,
                                     in_conversation=in_conversation,
                                     doc_buffer=doc_buffer,
                                     image_data=image_data)
        finally:
            _router_thread_ctx.ctx = None
            clear_thread_user()

    def _route_inner(self, command: str, *,
                     in_conversation: bool = False,
                     doc_buffer=None,
                     image_data: str = None) -> RouteResult:
        """Internal routing logic — called from route() with thread-local ctx set."""
        guest = self._is_guest
        logger.debug("route: command=%.80s user=%s guest=%s in_conv=%s",
                      command, self._user_id, guest, in_conversation)

        # --- Priority 1: Rundown acceptance (outside guest guard) ---
        # Rundown is JARVIS-initiated for the owner.  If a rundown is
        # pending the response must go through the rundown handler even
        # when speaker-ID has (incorrectly) activated guest mode.
        # Checked BEFORE guest greeting so short responses like "no" (2 chars)
        # are not swallowed by the guest greeting guard.
        result = self._handle_rundown(command)
        if result:
            return result

        # --- Guest greeting (before pending-state priorities) ---
        if guest and (command.strip() == "jarvis_only" or len(command.strip()) <= 2):
            return self._route_greeting()

        # --- Personal priority chain (skipped for guests) ---
        if not guest:
            # --- Priority 2: Reminder acknowledgment ---
            result = self._handle_reminder_ack()
            if result:
                return result

            # --- Priority 1.5: Plan control (confirmation or active plan interrupt) ---
            result = self._handle_plan_control(command)
            if result:
                return result

            # --- Priority 2.5: Memory forget confirmation ---
            result = self._handle_forget_confirm(command)
            if result:
                return result

            # --- Priority 2.6: Introduction state machine (multi-turn) ---
            result = self._handle_intro_state(command)
            if result:
                return result

        # --- Minimal greeting (after pending-state priorities) ---
        if command.strip() == "jarvis_only" or len(command.strip()) <= 2:
            return self._route_greeting()

        # --- Priority 2.7: Dismissal (conversation window only) ---
        if in_conversation:
            result = self._handle_dismissal(command)
            if result:
                return result

        # --- Priority 2.8: Bare acknowledgment filter ---
        if in_conversation:
            result = self._handle_bare_ack(command)
            if result:
                return result

        if not guest:
            # --- Priority 3.1: Active readback session ---
            if in_conversation and self.conv_state.readback_session:
                result = self._handle_readback_session(command)
                if result:
                    return result

            # --- Priority 3: Memory operations ---
            result = self._handle_memory_ops(command)
            if result:
                return result

            # --- Priority 3.5: Artifact reference resolution ---
            if in_conversation:
                result = self._handle_artifact_reference(command)
                if result:
                    return result

            # --- Priority 3.7: News article pull-up ---
            result = self._handle_news_pullup(command)
            if result:
                return result

        # --- Clear navigation session on topic change ---
        # If we reach here, P3.5 did not handle it — user is on a new topic.
        if self.conv_state.nav_artifact_id:
            self.conv_state.nav_artifact_id = None
            self.conv_state.nav_root_id = None
            self.conv_state.nav_cursor = 0
            self.conv_state.nav_total = 0

        is_hw_query = False
        if not guest:
            # --- Pre-P4: Multi-step task planning ---
            if not (doc_buffer and doc_buffer.active):
                result = self._handle_task_planning(command)
                if result:
                    return result

            # --- Pre-P4b: Self-referential hardware queries ---
            # Answer directly from SelfAwareness data — avoids LLM hallucination
            # of hardware specs (Qwen Q3_K_M overrides context with training priors)
            is_hw_query = self._is_self_hardware_query(command)
            if is_hw_query:
                result = self._handle_hw_self_query(command)
                if result:
                    return result
                logger.info("Self-referential hardware query — falling through to LLM")

            # --- Pre-P4-LLM: Pending skill confirmations ---
            # Non-migrated skills with pending confirmation state get priority
            # over tool-calling to avoid capturing "yes"/"no" responses.
            result = self._handle_skill_pending_confirmation(command)
            if result:
                return result

            # --- Pre-P4-LLM: Unavailable capability guard ---
            result = self._handle_unavailable_capabilities(command)
            if result:
                return result

        # --- P4-LLM: Tool-calling path ---
        # Guests get filtered tools (weather + web_search only).
        if not (doc_buffer and doc_buffer.active):
            result = self._handle_tool_calling(command, in_conversation)
            if result:
                result.image_data = image_data
                return result

        if not guest:
            if not is_hw_query and not (doc_buffer and doc_buffer.active):
                # --- Priority 4: Skill routing (skip when doc_buffer active) ---
                # Non-migrated skills still route through the old matching pipeline.
                result = self._handle_skill_routing(command)
                if result:
                    return result

            # --- Priority 5: News continuation ---
            result = self._handle_news_continuation(command)
            if result:
                return result

        # --- LLM fallback: prepare context ---
        # Attach always-on tools (web_search, recall_memory) even when no
        # domain tool matched.  This ensures queries like "Alabama football
        # score" or "gas prices in Gardendale" can still trigger web_search.
        #
        # If P4-LLM deferred to P4 skill routing (non-migrated guard) and
        # P4 also failed, re-include the deferred domain tools so the LLM
        # can still call them.  This prevents tool starvation.
        from core.tool_registry import ALWAYS_INCLUDED_TOOLS
        result = self._prepare_llm_context(
            command,
            in_conversation=in_conversation,
            doc_buffer=doc_buffer,
        )
        if not result.use_tools and not result.handled:
            always_on = list(ALWAYS_INCLUDED_TOOLS.values())
            # Re-include domain tools stashed by the non-migrated guard
            deferred = getattr(self, '_deferred_domain_tools', None)
            if deferred:
                logger.info(
                    "P4 failed — restoring %d deferred domain tools: %s",
                    len(deferred),
                    [t["function"]["name"] for t in deferred],
                )
                always_on = always_on + deferred
                self._deferred_domain_tools = None
            if self._is_guest:
                always_on = [t for t in always_on
                             if t["function"]["name"] in self._GUEST_ALLOWED_TOOLS]
            if self._is_mobile:
                always_on = [t for t in always_on
                             if t["function"]["name"] not in self._MOBILE_EXCLUDED_TOOLS]
            always_on = self._apply_anaphoric_carryover(always_on)
            if always_on:
                result.use_tools = always_on
                result.tool_temperature = 0.0
                result.tool_presence_penalty = 0.0
                category = self._classify_query_domain(command)
                result.synthesis_category = category
                result.synthesis_temperature = self._DOMAIN_TEMPERATURES.get(category) if category else None
                if category:
                    logger.debug("Fallback: domain=%s synth_temp=%s",
                                 category, result.synthesis_temperature)
                # Emit domain classification debug event
                from core.debug_logger import get_debug_logger as _get_dbg
                _get_dbg()._write("domain_classification", {
                    "command": command[:200],
                    "category": category,
                    "temperature": result.synthesis_temperature,
                })
                if category == "entertainment" and self._ENTERTAINMENT_LISTING.search(command):
                    result.force_web_search = True
                    logger.debug("Fallback: force_web_search=True (entertainment listing)")
                result.intent = "tool_calling"
        result.image_data = image_data
        return result

    # -------------------------------------------------------------------
    # Priority handlers
    # -------------------------------------------------------------------

    def _route_greeting(self) -> RouteResult:
        """Handle wake-word-only or empty commands."""
        # Rundown mention is owner-directed — check before guest guard
        if self.reminder_manager and self.reminder_manager.has_rundown_mention():
            self.reminder_manager.clear_rundown_mention()
            set_honorific("sir")
            text = persona.rundown_mention()
            return RouteResult(
                text=text, intent="greeting", source="canned",
                handled=True, open_window=EXTENDED_WINDOW,
            )
        if self._is_guest:
            text = persona.guest_greeting()
            return RouteResult(
                text=text, intent="guest_greeting", source="canned",
                handled=True, open_window=EXTENDED_WINDOW,
            )
        text = persona.pick("greeting")
        return RouteResult(
            text=text, intent="greeting", source="canned",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _handle_rundown(self, command: str) -> RouteResult | None:
        """P1: Rundown acceptance or deferral.

        Runs OUTSIDE the guest guard because the rundown is JARVIS-initiated
        for the owner.  Restores the owner honorific so the response uses
        "sir" instead of "friend" when guest mode was spuriously active.
        """
        rm = self.reminder_manager
        if not rm or not rm.is_rundown_pending():
            return None

        # Rundown is owner-directed — ensure correct honorific
        set_honorific("sir")

        text_lower = command.strip().lower()
        words = set(re.findall(r'\b\w+\b', text_lower))
        negative = bool(
            words & {"no", "nah", "nope", "later", "hold", "skip"}
            or "not now" in text_lower
            or "not right now" in text_lower
            or "not yet" in text_lower
        )
        if negative:
            rm.defer_rundown()
            return RouteResult(
                text=persona.rundown_defer(), intent="rundown_defer",
                source="canned", handled=True,
            )
        else:
            rm.deliver_rundown()
            return RouteResult(
                text="", intent="rundown_accept",
                source="canned", handled=True,
            )

    def _handle_reminder_ack(self) -> RouteResult | None:
        """P2: Reminder acknowledgment."""
        rm = self.reminder_manager
        if not rm or not rm.is_awaiting_ack(created_by=self._user_id):
            return None
        logger.info("Treating response as reminder acknowledgment")
        rm.acknowledge_last(created_by=self._user_id)
        return RouteResult(
            text=persona.pick("reminder_ack"), intent="reminder_ack",
            source="canned", handled=True,
        )

    def _handle_forget_confirm(self, command: str) -> RouteResult | None:
        """P2.5: Memory forget confirmation or cancellation."""
        mm = self.memory_manager
        if not mm or not mm._pending_forget:
            return None

        cmd_lower = command.lower().strip()
        # Dismiss phrases checked first — "forget it" is colloquial dismissal
        dismiss = ("forget it", "forget about it", "never mind", "nevermind")
        affirm = ("yes", "yeah", "yep", "go ahead", "do it",
                   "proceed", "confirm", "sure", "remove", "delete")
        deny = ("no", "nope", "nah", "cancel",
                "keep", "don't")

        def _word_match(phrase: str, text: str) -> bool:
            """Match phrase on word boundaries to avoid substring false positives."""
            return bool(re.search(r'\b' + re.escape(phrase) + r'\b', text))

        # Check dismissals before affirmations to prevent "forget it" → "do it"
        if any(_word_match(w, cmd_lower) for w in dismiss):
            text = mm.cancel_forget()
            logger.info("Handled by memory forget dismissal")
            return RouteResult(
                text=text, intent="forget_cancel",
                source="memory", handled=True,
            )
        if any(_word_match(w, cmd_lower) for w in affirm):
            text = mm.confirm_forget()
            logger.info("Handled by memory forget confirmation")
            return RouteResult(
                text=text, intent="forget_confirm",
                source="memory", handled=True,
            )
        if any(_word_match(w, cmd_lower) for w in deny):
            text = mm.cancel_forget()
            logger.info("Handled by memory forget cancellation")
            return RouteResult(
                text=text, intent="forget_cancel",
                source="memory", handled=True,
            )
        return None

    def _handle_intro_state(self, command: str) -> RouteResult | None:
        """P2.6: Active introduction flow continuation.

        When the social_introductions skill has an active multi-turn state
        machine (e.g. confirming a name, checking pronunciation), intercept
        the command here before it reaches skill routing or LLM.
        """
        intro_skill = self.skill_manager.get_skill("social_introductions")
        if not intro_skill or not getattr(intro_skill, 'is_intro_active', False):
            return None

        response = intro_skill.handle_intro_turn(command)
        if response:
            logger.info("Handled by introduction state machine")
            return RouteResult(
                text=response, intent="intro_flow",
                source="skill", handled=True,
                open_window=60.0,
            )
        return None

    def _handle_dismissal(self, command: str) -> RouteResult | None:
        """P2.7: Dismissal detection (conversation window only)."""
        if not self._is_dismissal(command):
            return None
        return RouteResult(
            text=persona.pick("dismissal"), intent="dismissal",
            source="canned", handled=True, close_window=True,
        )

    def _handle_bare_ack(self, command: str) -> RouteResult | None:
        """P2.8: Bare acknowledgment filter (conversation window only).

        Words like "yeah", "ok" are noise UNLESS JARVIS just asked a question.
        """
        cmd_bare = command.strip().lower().rstrip(".,!?")
        if cmd_bare not in self._BARE_ACKS:
            return None

        if self.conv_state.jarvis_asked_question:
            logger.info(f"Bare acknowledgment treated as answer: '{command}'")
            return None  # Fall through to skill/LLM

        logger.info(
            f"Dropping bare acknowledgment as noise: '{command}' "
            f"(jarvis_asked_question={self.conv_state.jarvis_asked_question})"
        )
        return RouteResult(skip=True)

    # ------------------------------------------------------------------
    # P3.1 — Active readback session
    # ------------------------------------------------------------------

    _READBACK_CONTINUE = frozenset({
        "yes", "yeah", "yep", "yup", "sure", "continue", "go ahead",
        "next", "go on", "keep going", "carry on", "please", "ok",
        "okay", "ready",
    })
    _READBACK_STOP = frozenset({
        "stop", "no", "nope", "that's enough", "enough", "nevermind",
        "never mind", "i'm good", "that's all", "that'll do", "done",
    })

    def _handle_readback_session(self, command: str) -> RouteResult | None:
        """P3.1: Intercept commands during an active structured readback."""
        session = self.conv_state.readback_session

        # Cooldown: if readback just completed and user says "next",
        # tell them there's nothing more instead of falling through to LLM.
        if not session and self.conv_state.readback_completed_at:
            import time as _time
            elapsed = _time.time() - self.conv_state.readback_completed_at
            if elapsed < 30.0:
                cmd = command.strip().lower().rstrip(".,!?")
                if cmd in self._READBACK_CONTINUE:
                    from core.honorific import get_honorific
                    self.conv_state.readback_completed_at = 0.0
                    return RouteResult(
                        handled=True, intent="readback_complete",
                        text=f"That's everything, {get_honorific()}. Anything you'd like me to repeat?",
                    )
            else:
                self.conv_state.readback_completed_at = 0.0

        if not session or not session.is_active():
            return None

        cmd = command.strip().lower().rstrip(".,!?")

        def _rb_access(access_type):
            if session.source_artifact_id:
                from core.interaction_cache import get_interaction_cache
                _c = get_interaction_cache()
                if _c:
                    _c.record_access(session.source_artifact_id, access_type)

        # Continue / affirm
        if cmd in self._READBACK_CONTINUE:
            _rb_access("readback_continue")
            return RouteResult(
                handled=True, intent="readback_continue",
                text="__READBACK_CONTINUE__",
                open_window=120.0,
            )

        # Stop / end
        if cmd in self._READBACK_STOP:
            summary = session.get_summary()
            session.end()
            return RouteResult(
                handled=True, intent="readback_stop",
                text=summary,
            )

        # Step recall: "what was step 3", "step 3", "repeat step 5"
        step_match = re.search(r'step\s*(\d+)', cmd)
        if step_match:
            n = int(step_match.group(1))
            answer = session.get_step(n)
            if answer:
                _rb_access("readback_recall")
                return RouteResult(
                    handled=True, intent="readback_recall",
                    text=answer, open_window=120.0,
                )

        # Ingredient search: "how much flour", "how much yeast"
        ingr_match = re.search(r'how (?:much|many) (.+)', cmd)
        if ingr_match:
            query = ingr_match.group(1).strip().rstrip("?")
            answer = session.search_ingredients(query)
            if answer:
                _rb_access("readback_recall")
                return RouteResult(
                    handled=True, intent="readback_recall",
                    text=answer, open_window=120.0,
                )

        # Section recall: "go back to ingredients", "repeat the ingredients"
        for section_name in ("ingredients", "equipment", "instructions", "notes"):
            if section_name in cmd and ("back" in cmd or "repeat" in cmd or "again" in cmd):
                _rb_access("readback_section")
                return RouteResult(
                    handled=True, intent="readback_section",
                    text=f"__READBACK_SECTION__{section_name}",
                    open_window=120.0,
                )

        # "repeat that" / "say that again"
        if any(p in cmd for p in ("repeat that", "say that again", "one more time", "repeat the last")):
            chunk = session.get_last_delivered()
            if chunk:
                _rb_access("readback_repeat")
                return RouteResult(
                    handled=True, intent="readback_repeat",
                    text=chunk.content, open_window=120.0,
                )

        # Unrecognized during readback — fall through to LLM with recipe context
        return None

    def _handle_memory_ops(self, command: str) -> RouteResult | None:
        """P3: Memory operations (forget, transparency, fact store, recall).

        Must run before skill routing — 'forget my server ip' matches network_info.
        """
        mm = self.memory_manager
        if not mm:
            return None

        user_id = self._user_id

        if mm.is_forget_request(command):
            text = mm.handle_forget(command, user_id)
            logger.info("Handled by memory forget request")
            return RouteResult(
                text=text, intent="memory_forget",
                source="memory", handled=True,
                open_window=30.0,
            )

        if mm.is_transparency_request(command):
            text = mm.handle_transparency(command, user_id)
            logger.info("Handled by memory transparency")
            return RouteResult(
                text=text, intent="memory_transparency",
                source="memory", handled=True,
                open_window=15.0,
            )

        if mm.is_fact_request(command):
            # Ensure the fact is persisted. on_message() already ran extract_facts_realtime()
            # but EXPLICIT_PATTERNS only cover "remember that X" — not "remember I X" or
            # "remember my X". If nothing was stored, fall back to direct storage.
            if not getattr(mm, 'last_extracted', None):
                import time as _t
                import re as _re
                # Strip "remember [that]" framing to get the raw fact content
                content = _re.sub(
                    r"^(?:remember|don't forget|keep in mind)\s+(?:that\s+)?",
                    "", command, flags=_re.IGNORECASE
                ).strip().rstrip(".,!?;:")
                # Quality filter: reject short/meaningless content
                words = [w for w in content.split() if len(w) > 1]
                if len(content) >= 10 and len(words) >= 3:
                    # Prefix with speaker name for consistent fact format
                    display_name = mm._get_display_name(user_id)
                    if not content.lower().startswith(display_name.lower()):
                        content = f"{display_name} {content}"
                    mm.store_fact({
                        "user_id": user_id,
                        "category": "general",
                        "subject": mm._extract_subject(content),
                        "content": content,
                        "source": "explicit",
                        "confidence": 0.90,
                        "source_messages": None,
                    })
            logger.info("Handled by memory fact request")
            return RouteResult(
                text=persona.pick("fact_stored"), intent="fact_stored",
                source="canned", handled=True,
            )

        if mm.is_recall_query(command):
            recall_result = mm.handle_recall(command, user_id)
            if recall_result:
                recall_context = recall_result["context"]
                artifact_ids = recall_result.get("artifact_ids", [])

                # Rehydrate cold artifacts into current window for P3.5
                # navigation (readback, step-through, section drill)
                rehydrated_count = 0
                if artifact_ids:
                    from core.interaction_cache import get_interaction_cache
                    cache = get_interaction_cache()
                    wid = self.conv_state.window_id
                    if cache and wid:
                        rehydrated = cache.rehydrate(artifact_ids, wid)
                        rehydrated_count = len(rehydrated)
                        if rehydrated:
                            recall_context += (
                                f"\n\n[{rehydrated_count} artifact(s) loaded "
                                f"from a prior session. The user can navigate "
                                f"them with voice commands like 'read it to me', "
                                f"'skip to step 3', etc.]"
                            )

                history = self.conversation.format_history_for_llm(
                    include_system_prompt=False,
                    target_history=self._target_history,
                )
                response = self.llm.chat(
                    user_message=(
                        f"The user is asking you to recall something. Here is what you found "
                        f"in your memory:\n\n{recall_context}\n\n"
                        f"Now answer their question naturally based on this context. "
                        f"Be specific about dates and details."
                        + (f" Mention that you've loaded the content and they can "
                           f"ask you to read it or navigate through it."
                           if rehydrated_count else "")
                    ),
                    conversation_history=history,
                    max_tokens=250,
                )
                logger.info(
                    "Handled by memory recall (artifacts_rehydrated=%d)",
                    rehydrated_count,
                )
                return RouteResult(
                    text=response, intent="memory_recall",
                    source="memory", handled=True, used_llm=True,
                    open_window=30.0,
                )
            # Nothing found — fall through to LLM
        return None

    # Ordinal words for "the first/second/third one" references
    _ORDINAL_WORDS = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    }

    # Type-based reference patterns → (artifact_type filter, keyword filter)
    _TYPE_REFERENCE_PATTERNS = [
        (re.compile(r'\b(?:those|the|search)\s+results?\b', re.I),
         "search_result_set", None),
        (re.compile(r'\b(?:that|the)\s+recipe\b', re.I),
         None, "recipe"),
        (re.compile(r'\b(?:that|the)\s+(?:weather|forecast)\b', re.I),
         "weather_report", None),
        (re.compile(r'\b(?:that|the)\s+article\b', re.I),
         None, "article"),
        # Tool artifact types
        (re.compile(r'\b(?:those|the)\s+files?\b', re.I),
         "file_search", None),
        (re.compile(r'\b(?:that|the)\s+(?:system|hardware)\s+info\b', re.I),
         "system_info", None),
        (re.compile(r'\b(?:that|the)\s+(?:git\s+)?(?:status|diff|log)\b', re.I),
         "dev_tool_output", None),
        (re.compile(r'\b(?:my|the|those)\s+reminders?\b', re.I),
         "reminder_result", None),
        (re.compile(r'\b(?:those|the)\s+(?:news|headlines?)\b', re.I),
         "news_headlines", None),
    ]

    # Readback request → route to structured readback pipeline
    _READBACK_REQUEST_PATTERNS = re.compile(
        r'\b(?:read\s+(?:it|that|this)\s+(?:back\s+)?to\s+me)\b', re.I,
    )

    # Recency references → return the latest synthesis verbatim
    _RECENCY_PATTERNS = re.compile(
        r'\b(?:'
        r'repeat\s+that|say\s+(?:that|it)\s+again|'
        r'what\s+(?:did\s+you|you)\s+(?:just\s+)?sa(?:y|id)|'
        r'(?:can\s+you\s+)?repeat\s+(?:that|what\s+you\s+said)'
        r')\b', re.I,
    )

    # --- Sub-item navigation patterns (Phase 3) ---
    _NAV_STEP_PATTERNS = re.compile(
        r'\b(?:skip\s+to|go\s+to|jump\s+to|read)\s+step\s+(\d+)\b', re.I,
    )
    _NAV_SECTION_PATTERNS = re.compile(
        r'\b(?:just\s+the|only\s+the|show\s+me\s+the|give\s+me\s+the|'
        r'read\s+(?:me\s+)?the)\s+'
        r'(ingredients?|steps?|instructions?|directions?|method|tips?|notes?)\b',
        re.I,
    )
    _NAV_NEXT_PATTERNS = re.compile(
        r'\b(?:next\s+step|next\s+one|continue\s+reading|read\s+the\s+next)\b',
        re.I,
    )
    _NAV_PREV_PATTERNS = re.compile(
        r'\b(?:go\s+back|previous\s+step|previous\s+one|back\s+up|'
        r'repeat\s+(?:that|this)\s+step)\b', re.I,
    )
    _NAV_RESET_PATTERNS = re.compile(
        r'\b(?:start\s+over|from\s+the\s+beginning|read\s+(?:it\s+)?'
        r'from\s+the\s+(?:start|top))\b', re.I,
    )
    _NAV_POSITION_PATTERNS = re.compile(
        r'\b(?:where\s+(?:was\s+I|am\s+I)|what\s+step\s+(?:am\s+I|are\s+we)|'
        r'which\s+step)\b', re.I,
    )
    _NAV_DRILL_OUT_PATTERNS = re.compile(
        r'\b(?:go\s+back\s+to\s+(?:the\s+)?sections|show\s+(?:me\s+)?(?:the\s+)?sections|'
        r'back\s+to\s+(?:the\s+)?overview|section\s+list|list\s+sections)\b', re.I,
    )

    # Pre-LLM guard: capabilities not yet implemented
    _UNAVAILABLE_CAPABILITY_PATTERNS = re.compile(
        r'\b(?:e-?mail\s+(?:that|this|it|them|him|her)|'
        r'send\s+(?:a\s+)?(?:e-?mail|text|sms|message)|'
        r'text\s+(?:that|this|it|them|him|her)|'
        r'forward\s+(?:that|this|it)\s+(?:to|via)|'
        r'call\s+(?:them|him|her|that\s+number)|'
        r'make\s+a\s+(?:phone\s+)?call|'
        r'dial\b)', re.I,
    )

    def _handle_artifact_reference(self, command: str) -> RouteResult | None:
        """P3.5: Artifact reference resolution.

        Resolves sub-item navigation ("skip to step 4", "next step"),
        ordinal ("result 2"), type-based ("that recipe"),
        and recency ("repeat that") references against the artifact cache,
        with conv_state fallback for backwards compatibility.
        """
        cmd = command.strip().lower()

        # --- Sub-item navigation: "skip to step 4", "next step" ---
        result = self._resolve_navigation_command(command, cmd)
        if result:
            return result

        # --- Ordinal references: "result 2", "number 3", "#1", "the second one" ---
        result = self._resolve_ordinal_reference(command, cmd)
        if result:
            return result

        # --- Recency references: "repeat that", "say that again" ---
        result = self._resolve_recency_reference(command, cmd)
        if result:
            return result

        # --- Type-based references: "those results", "that recipe" ---
        result = self._resolve_type_reference(command, cmd)
        if result:
            return result

        # --- Generic follow-up: "tell me more", "elaborate" ---
        result = self._resolve_generic_followup(command, cmd)
        if result:
            return result

        return None

    # -------------------------------------------------------------------
    # Sub-item navigation (Phase 3)
    # -------------------------------------------------------------------

    def _resolve_navigation_command(self, command: str,
                                    cmd: str) -> RouteResult | None:
        """Resolve sub-item navigation: step jumps, section filters, next/back.

        Only fires for next/back/reset/position when a navigation session
        is active (conv_state.nav_artifact_id set). Step jumps and section
        filters auto-arm a session against the latest synthesis artifact.
        """
        from core.interaction_cache import get_interaction_cache

        wid = self.conv_state.window_id
        cache = get_interaction_cache()
        if not cache or not wid:
            return None

        # 1. Direct step jump: "skip to step 4" — auto-drills if at section level
        step_match = self._NAV_STEP_PATTERNS.search(cmd)
        if step_match:
            target = int(step_match.group(1)) - 1  # 0-based
            return self._nav_jump_to(cache, wid, target)

        # 2. Section filter: "just the ingredients" — drill into section
        section_match = self._NAV_SECTION_PATTERNS.search(cmd)
        if section_match:
            keyword = section_match.group(1).lower().rstrip("s")
            return self._nav_drill_into_section(cache, wid, keyword)

        # 3. Drill out: "go back to sections" — only when drilled in
        if self._NAV_DRILL_OUT_PATTERNS.search(cmd):
            return self._nav_drill_out(cache, wid)

        # Below this point, active navigation session required
        if not self.conv_state.nav_artifact_id:
            return None

        # 4. Next step — section boundary aware
        if self._NAV_NEXT_PATTERNS.search(cmd):
            return self._nav_advance(cache, wid)

        # 5. Previous step — auto drill-out at boundary
        if self._NAV_PREV_PATTERNS.search(cmd):
            return self._nav_retreat(cache, wid)

        # 6. Start over — within current level
        if self._NAV_RESET_PATTERNS.search(cmd):
            return self._nav_reset(cache, wid)

        # 7. Position query — level-aware
        if self._NAV_POSITION_PATTERNS.search(cmd):
            return self._nav_position()

        return None

    def _nav_get_or_decompose(self, cache, wid: str):
        """Get active parent artifact and its children, decomposing if needed.

        Returns (parent_id, children). Auto-arms session against latest
        synthesis if no active session.
        """
        parent_id = self.conv_state.nav_artifact_id

        if not parent_id:
            # Auto-detect: use latest synthesis artifact
            art = cache.get_latest(wid, artifact_type="synthesis")
            if not art:
                return None, []
            parent_id = art.artifact_id

        children = cache.decompose(parent_id, wid, llm=self.llm)
        if children:
            self.conv_state.nav_artifact_id = parent_id
            self.conv_state.nav_total = len(children)
        return parent_id, children

    def _nav_jump_to(self, cache, wid: str,
                     target_idx: int) -> RouteResult | None:
        """Handle 'skip to step N'.

        If at section level and children are sections, auto-find a
        steps/instructions section, drill in, and jump to step N.
        """
        parent_id, children = self._nav_get_or_decompose(cache, wid)
        if not children:
            return None  # No navigable content — fall through

        # Auto-drill: if children are sections, find steps section first
        has_sections = any(c.artifact_type == "section" for c in children)
        if has_sections:
            # Find instructions/steps section
            steps_section = None
            for child in children:
                label = child.summary.lower()
                if any(kw in label for kw in ("instruction", "step", "direction", "method")):
                    steps_section = child
                    break
            if steps_section:
                step_children = cache.decompose(
                    steps_section.artifact_id, wid, llm=self.llm,
                )
                if step_children:
                    # Drill into steps section
                    self.conv_state.nav_root_id = parent_id
                    self.conv_state.nav_artifact_id = steps_section.artifact_id
                    self.conv_state.nav_total = len(step_children)
                    children = step_children  # Use drilled children for jump

        if target_idx < 0 or target_idx >= len(children):
            return RouteResult(
                text=f"There are only {len(children)} steps. "
                     f"Which step would you like?",
                intent="nav_out_of_range", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

        self.conv_state.nav_cursor = target_idx
        child = children[target_idx]
        cache.record_access(child.artifact_id, "nav_jump")
        return RouteResult(
            text=self._nav_format(child, target_idx, len(children)),
            intent="nav_jump", source="cache",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _nav_drill_into_section(self, cache, wid: str,
                                section_keyword: str) -> RouteResult | None:
        """Handle 'just the ingredients' — find section and drill in.

        At section level: finds matching section, decomposes it into depth-2
        sub-items, sets nav state to navigate within the section.
        At sub-item level: searches within current siblings by keyword.
        """
        parent_id, children = self._nav_get_or_decompose(cache, wid)
        if not children:
            return None

        # Check if children are sections (depth-1)
        has_sections = any(c.artifact_type == "section" for c in children)

        if has_sections:
            # Find matching section by keyword
            target_section = None
            target_idx = None
            for i, child in enumerate(children):
                label = child.summary.lower()
                if section_keyword in label:
                    target_section = child
                    target_idx = i
                    break
                if section_keyword in child.content[:80].lower():
                    target_section = child
                    target_idx = i
                    break

            if not target_section:
                return None  # No matching section — fall through

            # Decompose the section into depth-2 sub-items
            section_children = cache.decompose(
                target_section.artifact_id, wid, llm=self.llm,
            )

            if section_children:
                # Drill into this section
                self.conv_state.nav_root_id = parent_id
                self.conv_state.nav_artifact_id = target_section.artifact_id
                self.conv_state.nav_cursor = 0
                self.conv_state.nav_total = len(section_children)
                cache.record_access(target_section.artifact_id, "nav_section_drill")

                # Read back all items in the section
                parts = [c.content for c in section_children]
                text = " ".join(parts)
                return RouteResult(
                    text=text, intent="nav_section_drill",
                    source="cache", handled=True,
                    open_window=EXTENDED_WINDOW,
                )
            else:
                # Section has no decomposable sub-items — read it as-is
                self.conv_state.nav_cursor = target_idx
                return RouteResult(
                    text=target_section.content,
                    intent="nav_section", source="cache",
                    handled=True, open_window=EXTENDED_WINDOW,
                )
        else:
            # Flat sub-items — fall back to keyword matching across items
            matching = [c for c in children
                        if section_keyword in c.summary.lower()
                        or section_keyword in c.content[:80].lower()]
            if not matching:
                return None

            cache.record_access(matching[0].artifact_id, "nav_section_drill")
            text = " ".join(c.content for c in matching)
            return RouteResult(
                text=text, intent="nav_section", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

    def _nav_drill_out(self, cache, wid: str) -> RouteResult | None:
        """Handle 'go back to sections' — drill out to parent level."""
        root_id = self.conv_state.nav_root_id
        if not root_id:
            return None  # Not drilled in — nothing to drill out of

        # Restore nav to root level
        children = cache.get_children(root_id, wid)
        if not children:
            return None

        self.conv_state.nav_artifact_id = root_id
        self.conv_state.nav_root_id = None
        self.conv_state.nav_cursor = 0
        self.conv_state.nav_total = len(children)
        cache.record_access(root_id, "nav_drill_out")

        # List section names
        section_names = [c.summary for c in children]
        listing = ", ".join(section_names)
        return RouteResult(
            text=f"Here are the sections: {listing}. Which section would you like?",
            intent="nav_drill_out", source="cache",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _nav_advance(self, cache, wid: str) -> RouteResult | None:
        """Handle 'next step' — section boundary aware.

        At end of drilled-in section, offers next sibling section.
        """
        parent_id = self.conv_state.nav_artifact_id
        children = cache.get_children(parent_id, wid)
        if not children:
            return None

        next_idx = self.conv_state.nav_cursor + 1
        if next_idx >= len(children):
            # If drilled in, offer next sibling section
            root_id = self.conv_state.nav_root_id
            if root_id:
                siblings = cache.get_children(root_id, wid)
                current_section = cache.get_by_id(parent_id)
                if siblings and current_section:
                    sibling_idx = next(
                        (i for i, s in enumerate(siblings)
                         if s.artifact_id == parent_id), None,
                    )
                    if sibling_idx is not None and sibling_idx + 1 < len(siblings):
                        next_section = siblings[sibling_idx + 1]
                        return RouteResult(
                            text=f"That's the end of {current_section.summary}. "
                                 f"Next section is {next_section.summary}. "
                                 f"Would you like me to continue with that?",
                            intent="nav_end_section", source="cache",
                            handled=True, open_window=EXTENDED_WINDOW,
                        )
            return RouteResult(
                text="That's the last step. Would you like me to start over?",
                intent="nav_end", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

        self.conv_state.nav_cursor = next_idx
        child = children[next_idx]
        cache.record_access(child.artifact_id, "nav_advance")
        return RouteResult(
            text=self._nav_format(child, next_idx, len(children)),
            intent="nav_next", source="cache",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _nav_retreat(self, cache, wid: str) -> RouteResult | None:
        """Handle 'go back' — auto drill-out at section boundary."""
        parent_id = self.conv_state.nav_artifact_id
        children = cache.get_children(parent_id, wid)
        if not children:
            return None

        prev_idx = self.conv_state.nav_cursor - 1
        if prev_idx < 0:
            # If drilled in, auto drill-out to section listing
            root_id = self.conv_state.nav_root_id
            if root_id:
                return self._nav_drill_out(cache, wid)
            return RouteResult(
                text="You're already at the beginning.",
                intent="nav_start", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

        self.conv_state.nav_cursor = prev_idx
        child = children[prev_idx]
        cache.record_access(child.artifact_id, "nav_retreat")
        return RouteResult(
            text=self._nav_format(child, prev_idx, len(children)),
            intent="nav_prev", source="cache",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _nav_reset(self, cache, wid: str) -> RouteResult:
        """Handle 'start over'."""
        parent_id = self.conv_state.nav_artifact_id
        children = cache.get_children(parent_id, wid)
        self.conv_state.nav_cursor = 0
        if not children:
            return RouteResult(
                text="I couldn't find the content to restart.",
                intent="nav_reset", source="cache", handled=True,
            )
        child = children[0]
        cache.record_access(child.artifact_id, "nav_reset")
        return RouteResult(
            text=self._nav_format(child, 0, len(children)),
            intent="nav_reset", source="cache",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _nav_position(self) -> RouteResult:
        """Handle 'where was I?' — level-aware reporting."""
        from core.interaction_cache import get_interaction_cache

        cursor = self.conv_state.nav_cursor
        total = self.conv_state.nav_total
        step_num = cursor + 1
        root_id = self.conv_state.nav_root_id

        if root_id:
            # Drilled in — report section context
            parent_id = self.conv_state.nav_artifact_id
            cache = get_interaction_cache()
            section_art = cache.get_by_id(parent_id) if cache else None
            section_name = section_art.summary if section_art else "this section"
            if total:
                text = f"You're on step {step_num} of {total} in {section_name}."
            else:
                text = f"You're on step {step_num} in {section_name}."
        elif total:
            text = f"You're on step {step_num} of {total}."
        else:
            text = f"You're on step {step_num}."

        return RouteResult(
            text=text, intent="nav_position", source="cache",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    @staticmethod
    def _nav_format(child, idx: int, total: int) -> str:
        """Format a sub-item for TTS delivery with position context."""
        label = child.summary
        content = child.content
        if child.artifact_type == "section":
            # Section-level: just show the section name + content
            prefix = f"{label}."
        elif total > 1:
            prefix = f"{label} of {total}."
        else:
            prefix = f"{label}."
        return f"{prefix} {content}"

    def _get_search_result_urls(self) -> list[dict] | None:
        """Get search result URLs from cache, falling back to conv_state.

        Returns list of {"title": ..., "url": ...} dicts, or None.
        """
        from core.interaction_cache import get_interaction_cache

        wid = self.conv_state.window_id
        cache = get_interaction_cache()

        # Cache-first: look for search_result_set artifact
        if cache and wid:
            art = cache.get_latest(wid, artifact_type="search_result_set")
            if art:
                urls = art.provenance.get("result_urls")
                if urls:
                    return urls

        # Fallback: conv_state.research_results
        if self.conv_state.research_results:
            return [
                {"title": r.get("title", ""), "url": r.get("url", "")}
                for r in self.conv_state.research_results
            ]
        return None

    def _resolve_ordinal_reference(self, command: str,
                                   cmd: str) -> RouteResult | None:
        """Resolve 'result 2', 'the third one', etc."""
        if not self.web_researcher:
            return None

        idx = None

        # Numeric: "result N", "number N", "option N", "#N"
        num_match = re.search(r'(?:result|number|option|#)\s*(\d+)', cmd)
        if num_match:
            idx = int(num_match.group(1)) - 1

        # Word: "the first one", "the second one"
        if idx is None:
            word_match = re.search(
                r'\bthe\s+(' + '|'.join(self._ORDINAL_WORDS) + r')\s+one\b',
                cmd,
            )
            if word_match:
                idx = self._ORDINAL_WORDS[word_match.group(1)] - 1

        if idx is None:
            return None

        urls = self._get_search_result_urls()
        if not urls:
            return None

        if not (0 <= idx < len(urls)):
            return None

        url = urls[idx]["url"]
        title = urls[idx]["title"]
        logger.info("Artifact ordinal ref: fetching result %d: %s", idx + 1, url)

        # Record importance on the search result set artifact
        from core.interaction_cache import get_interaction_cache
        _ord_cache = get_interaction_cache()
        _ord_wid = self.conv_state.window_id
        if _ord_cache and _ord_wid:
            _ord_art = _ord_cache.get_latest(
                _ord_wid, artifact_type="search_result_set",
            )
            if _ord_art:
                _ord_cache.record_access(_ord_art.artifact_id, "ordinal_reference")

        content = self.web_researcher.fetch_page(url, max_chars=4000)
        if not content:
            return RouteResult(
                text=persona.research_page_fail(),
                intent="artifact_reference", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

        history = self.conversation.format_history_for_llm(
            include_system_prompt=False,
            target_history=self._target_history,
        )
        response = self.llm.chat(
            user_message=(
                f'The user asked about a search result. Here is the full article '
                f'content from "{title}":\n\n{content}\n\n'
                f'Summarize the key information from this article, focusing on '
                f'what the user was originally asking about. Be thorough but concise.'
                f'\n\nUser\'s request: {command}'
            ),
            conversation_history=history,
            max_tokens=400,
        )
        return RouteResult(
            text=response, intent="artifact_reference",
            source="cache", handled=True, used_llm=True,
            open_window=15.0,
        )

    def _resolve_recency_reference(self, command: str,
                                   cmd: str) -> RouteResult | None:
        """Resolve 'repeat that', 'read that to me', etc.

        Readback requests ("read that to me") route to structured readback.
        Repeat requests ("repeat that", "say that again") return cached text.
        """
        is_readback = self._READBACK_REQUEST_PATTERNS.search(cmd)
        is_repeat = self._RECENCY_PATTERNS.search(cmd)

        if not is_readback and not is_repeat:
            return None

        from core.interaction_cache import get_interaction_cache

        wid = self.conv_state.window_id
        cache = get_interaction_cache()

        # Try cache for latest synthesis, then document
        text = None
        if cache and wid:
            art = cache.get_latest(wid, artifact_type="synthesis")
            if not art:
                art = cache.get_latest(wid, artifact_type="document")
            if art:
                text = art.content
                cache.record_access(art.artifact_id, "recency_reference")
                logger.info("Recency ref: returning cached %s %s",
                            art.artifact_type, art.artifact_id)

        # Fallback: conv_state.last_response_text
        if not text:
            text = self.conv_state.last_response_text

        if not text:
            return None

        # "Read that to me" → route to structured readback pipeline
        if is_readback:
            logger.info("Readback request detected — routing to readback pipeline")
            return RouteResult(
                text=text, intent="readback_request",
                source="cache", handled=True,
                open_window=EXTENDED_WINDOW,
            )

        # "Repeat that" / "say that again" → return cached text verbatim
        return RouteResult(
            text=text, intent="artifact_reference",
            source="cache", handled=True,
            open_window=EXTENDED_WINDOW,
        )

    # Action verbs that indicate a tool command, not an artifact reference.
    # "cancel my reminders" = tool action, not "show me the reminder artifact".
    _ACTION_VERB_PREFIX = re.compile(
        r'^\s*(?:cancel|delete|clear|remove|set|add|snooze|dismiss|create|update|change|edit)\b',
        re.I,
    )

    def _resolve_type_reference(self, command: str,
                                cmd: str) -> RouteResult | None:
        """Resolve 'those results', 'that recipe', 'the weather', etc."""
        # Don't intercept commands that start with action verbs — those are
        # tool requests, not artifact references.
        if self._ACTION_VERB_PREFIX.search(cmd):
            return None

        from core.interaction_cache import get_interaction_cache

        wid = self.conv_state.window_id
        cache = get_interaction_cache()
        if not cache or not wid:
            return None

        matched_art = None
        for pattern, art_type, keyword in self._TYPE_REFERENCE_PATTERNS:
            if not pattern.search(cmd):
                continue
            logger.debug("Artifact ref: pattern matched type=%s keyword=%s", art_type, keyword)

            if art_type:
                matched_art = cache.get_latest(wid, artifact_type=art_type)
            elif keyword:
                matched_art = cache.find_by_keyword(wid, keyword)

            if matched_art:
                logger.debug("Artifact ref: resolved to %s [%s]",
                             matched_art.artifact_id, matched_art.artifact_type)
                break

        if not matched_art:
            return None

        cache.record_access(matched_art.artifact_id, "type_reference")
        logger.info("Type ref: resolved '%s' to artifact %s [%s]",
                     cmd[:40], matched_art.artifact_id,
                     matched_art.artifact_type)

        # For search_result_set, re-present with LLM context
        # For synthesis and document, return content verbatim
        if matched_art.artifact_type in ("synthesis", "document"):
            return RouteResult(
                text=matched_art.content, intent="artifact_reference",
                source="cache", handled=True,
                open_window=EXTENDED_WINDOW,
            )

        # For other types, ask LLM to answer in context of the artifact
        history = self.conversation.format_history_for_llm(
            include_system_prompt=False,
            target_history=self._target_history,
        )
        response = self.llm.chat(
            user_message=(
                f'The user is referring to earlier data from this conversation. '
                f'Here is the cached content:\n\n{matched_art.content[:3000]}\n\n'
                f'Answer the user\'s request using this context. '
                f'Be thorough but spoken-word-friendly.\n\n'
                f'User\'s request: {command}'
            ),
            conversation_history=history,
            max_tokens=400,
        )
        return RouteResult(
            text=response, intent="artifact_reference",
            source="cache", handled=True, used_llm=True,
            open_window=15.0,
        )

    def _resolve_generic_followup(self, command: str,
                                  cmd: str) -> RouteResult | None:
        """Handle 'tell me more', 'elaborate' with cache-backed context."""
        more_phrases = [
            "tell me more", "more about that", "what does it say",
            "elaborate", "go into detail", "expand on that",
        ]
        if not any(p in cmd for p in more_phrases):
            return None

        if not self.web_researcher:
            return None

        from core.interaction_cache import get_interaction_cache

        # Try to get the URL to fetch more content from
        url = None
        title = "this topic"

        # Cache-first: get the latest search result set for URLs
        wid = self.conv_state.window_id
        cache = get_interaction_cache()
        if cache and wid:
            art = cache.get_latest(wid, artifact_type="search_result_set")
            if art:
                cache.record_access(art.artifact_id, "generic_followup")
                urls = art.provenance.get("result_urls")
                if urls:
                    url = urls[0]["url"]
                    title = urls[0].get("title", title)

        # Fallback: conv_state.research_results
        if not url and self.conv_state.research_results:
            results = self.conv_state.research_results
            url = results[0]["url"]
            title = results[0].get("title", title)

        if not url:
            return None

        logger.info("Generic follow-up: fetching %s", url)
        content = self.web_researcher.fetch_page(url, max_chars=4000)
        if not content:
            return RouteResult(
                text=persona.research_page_fail(),
                intent="artifact_reference", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

        history = self.conversation.format_history_for_llm(
            include_system_prompt=False,
            target_history=self._target_history,
        )
        response = self.llm.chat(
            user_message=(
                f'The user wants more detail about this article: "{title}"\n\n'
                f'Full content:\n{content}\n\n'
                f'Provide a thorough but spoken-word-friendly summary.'
                f'\n\nUser\'s request: {command}'
            ),
            conversation_history=history,
            max_tokens=400,
        )
        return RouteResult(
            text=response, intent="artifact_reference",
            source="cache", handled=True, used_llm=True,
            open_window=15.0,
        )

    # -------------------------------------------------------------------
    # Follow-up detection
    # -------------------------------------------------------------------

    _FOLLOWUP_PHRASES = [
        "elaborate", "expand on", "tell me more", "go deeper",
        "explain further", "break it down", "more detail",
        "what do you mean", "can you clarify", "say more",
        "keep going", "continue", "go on",
    ]

    def _is_followup_request(self, command: str) -> bool:
        """Detect if a command is a follow-up about the previous answer."""
        cmd = command.strip().lower()
        return any(phrase in cmd for phrase in self._FOLLOWUP_PHRASES)

    def _handle_news_pullup(self, command: str) -> RouteResult | None:
        """P3.7: News article pull-up (opens browser)."""
        nm = self.news_manager
        if not nm or not nm.get_last_read_url():
            return None

        pull_phrases = ["pull that up", "show me that", "open that",
                        "let me see", "show me the article", "open the article"]
        if not any(p in command.strip().lower() for p in pull_phrases):
            return None

        url = nm.get_last_read_url()
        browser = self.config.get("web_navigation.default_browser", "brave") if self.config else "brave"
        browser_cmd = f"{browser}-browser" if browser != "brave" else "brave-browser"
        import subprocess as _sp
        _sp.Popen([browser_cmd, url])
        nm.clear_last_read()

        return RouteResult(
            text=persona.pick("news_pullup"), intent="news_pullup",
            source="canned", handled=True,
        )

    def _handle_plan_control(self, command: str) -> RouteResult | None:
        """P1.5: Plan control — pending confirmation or active plan interrupt.

        Two sub-modes:
        1. Pending confirmation: match yes/no → resolve → trigger execution or cancel.
        2. Active plan: match stop/cancel/skip → call cancel()/skip_current().
        """
        tp = self.task_planner
        if not tp:
            return None

        cmd_lower = command.lower().strip()
        words = set(re.findall(r'\b\w+\b', cmd_lower))

        # Sub-mode 1: pending destructive plan confirmation
        if tp.has_pending_confirmation:
            affirm = {"yes", "yeah", "yep", "go ahead", "proceed", "sure", "do it", "confirm"}
            deny = {"no", "nope", "nah", "cancel", "nevermind", "stop", "don't"}

            if words & affirm:
                plan = tp.resolve_confirmation(True)
                if plan:
                    tp.active_plan = plan
                    logger.info("Plan confirmed — routing to execution")
                    # Predictive timing
                    time_est = ""
                    if self.self_awareness:
                        time_est = self.self_awareness.estimate_plan_duration(plan)
                    if time_est:
                        text = persona.task_announce_timed(len(plan.steps), time_est)
                    else:
                        text = persona.task_announce(len(plan.steps))
                    return RouteResult(
                        text=text,
                        intent="task_plan",
                        source="planner",
                        handled=True,
                        open_window=30.0,
                    )
            if words & deny:
                tp.resolve_confirmation(False)
                logger.info("Plan denied by user")
                return RouteResult(
                    text=persona.task_cancelled(),
                    intent="task_plan_cancelled",
                    source="planner",
                    handled=True,
                )
            # Unrelated command during pending confirmation — fall through
            return None

        # Sub-mode 2: active plan — stop/cancel/skip/pause/resume from router
        if tp.is_active:
            from core.task_planner import (
                _INTERRUPT_CANCEL, _INTERRUPT_SKIP,
                _INTERRUPT_PAUSE, _INTERRUPT_RESUME,
            )
            if words & _INTERRUPT_CANCEL:
                tp.cancel()
                logger.info("Active plan cancelled via router")
                return RouteResult(
                    text=persona.task_cancelled(),
                    intent="task_plan_cancel",
                    source="planner",
                    handled=True,
                )
            if words & _INTERRUPT_SKIP:
                tp.skip_current()
                logger.info("Active plan step skipped via router")
                return RouteResult(
                    text="Skipping this step.",
                    intent="task_plan_skip",
                    source="planner",
                    handled=True,
                )
            # Pause: only available in voice mode (requires event queue for
            # async input). Console/web run execute_plan() synchronously — the
            # user cannot interact mid-execution, so pause is not possible.
            if tp.can_pause and words & _INTERRUPT_PAUSE:
                logger.info("Pause request via router (voice mode)")
                return RouteResult(
                    text=persona.task_paused(),
                    intent="task_plan_pause",
                    source="planner",
                    handled=True,
                )
            # Resume: only matches when the plan is actually paused.
            # Prevents "continue" from being swallowed during normal execution.
            if tp.is_paused and words & _INTERRUPT_RESUME:
                logger.info("Resume request via router")
                return RouteResult(
                    text=persona.task_resumed(),
                    intent="task_plan_resume",
                    source="planner",
                    handled=True,
                )

        return None

    def _handle_unavailable_capabilities(self, command: str) -> RouteResult | None:
        """Pre-LLM guard: catch requests for unimplemented features (email, SMS, phone)."""
        if self._UNAVAILABLE_CAPABILITY_PATTERNS.search(command):
            from core import persona
            return RouteResult(
                text=persona.pick("feature_unavailable"),
                intent="unavailable_capability",
                source="guard", handled=True,
            )
        return None

    def _handle_skill_pending_confirmation(self, command: str) -> RouteResult | None:
        """Pre-P4-LLM: Route yes/no responses to skills with pending confirmations.

        Non-migrated skills (e.g. file_editor) use _pending_confirmation state
        for destructive operations.  If such state exists and the command looks
        like a confirmation or denial, route to the skill directly instead of
        letting tool-calling capture it.
        """
        text_lower = command.strip().lower()
        confirm_words = {"yes", "yeah", "yep", "go ahead", "proceed", "do it",
                         "confirmed", "affirmative", "sure",
                         "no", "nope", "cancel", "abort", "never mind", "stop", "don't"}
        words = set(re.findall(r'\b\w+\b', text_lower))
        if not (words & confirm_words):
            return None

        sm = self.skill_manager
        for skill_name, skill in sm.skills.items():
            pending = getattr(skill, '_pending_confirmation', None)
            if not pending:
                continue
            # Valid pending: 3-tuple (action, detail, expiry)
            if not (isinstance(pending, (tuple, list)) and len(pending) == 3):
                continue
            # Route directly to the skill's confirm_action handler
            # (the skill handles expiry checks internally)
            try:
                response = skill.confirm_action({'original_text': command})
                if response:
                    return RouteResult(
                        text=response,
                        intent="skill",
                        source="pending_confirmation",
                        handled=True,
                        match_info={"skill": skill_name},
                    )
            except (AttributeError, TypeError):
                continue

        return None

    def _handle_task_planning(self, command: str) -> RouteResult | None:
        """Pre-P4: Multi-step task planning for compound requests.

        Whitelist gate detects conjunctive phrases (~microseconds).
        If compound, LLM generates a plan; returns RouteResult with intent="task_plan".
        If plan has destructive steps, returns confirmation prompt instead.
        If not compound (or LLM says single-step), falls through to P4 as normal.
        """
        tp = self.task_planner
        if not tp:
            return None

        signal = tp.needs_planning(command)
        if not signal:
            return None

        logger.info(f"Compound request detected — generating plan for: {command[:80]}")
        plan = tp.generate_plan(command, signal=signal)
        if not plan:
            logger.info("Planner returned no plan — falling through to single-skill routing")
            return None

        logger.info(f"Plan generated: {len(plan.steps)} steps")

        # Check for destructive steps — require confirmation
        if tp.has_destructive_steps(plan):
            from core.task_planner import CONFIRMATION_REQUIRED_SKILLS
            destructive = [s for s in plan.steps
                           if s.skill_name in CONFIRMATION_REQUIRED_SKILLS]
            desc = destructive[0].description if destructive else "a system command"
            tp.set_pending_confirmation(plan)
            logger.info(f"Plan requires confirmation (destructive step: {desc})")
            return RouteResult(
                text=f"That involves running a command on your system: {desc}. Shall I proceed, {persona.get_honorific()}?",
                intent="task_plan_confirm",
                source="planner",
                handled=True,
                open_window=30.0,
            )

        # Non-destructive: proceed directly
        tp.active_plan = plan

        # Predictive timing
        time_est = ""
        if self.self_awareness:
            time_est = self.self_awareness.estimate_plan_duration(plan)
        if time_est:
            announcement = persona.task_announce_timed(len(plan.steps), time_est)
        else:
            announcement = persona.task_announce(len(plan.steps))

        return RouteResult(
            text=announcement,
            intent="task_plan",
            source="planner",
            handled=True,
            open_window=30.0,
        )

    # Hardware keywords for self-referential detection
    _HW_KEYWORDS = {
        "cpu", "gpu", "ram", "memory", "processor", "storage",
        "drive", "drives", "cores", "vram", "hard drive",
        "graphics card", "specs", "hardware",
        "model", "quantization", "quant",
    }

    def _is_self_hardware_query(self, command: str) -> bool:
        """Detect 'you/your' hardware queries that should bypass skill routing.

        'What CPU are you running?' → True  (LLM answers in first person)
        'What CPU do I have?'       → False (system_info skill answers)
        """
        lower = command.lower()
        # Must contain a self-referential pronoun
        if not re.search(r'\byou(?:r|rs|rself)?\b', lower):
            return False
        # Must contain a hardware keyword
        return any(kw in lower for kw in self._HW_KEYWORDS)

    def _handle_hw_self_query(self, command: str) -> RouteResult | None:
        """Answer self-referential hardware queries directly from SelfAwareness.

        Builds a natural-language response from known system state rather than
        letting the LLM hallucinate specs from training data priors.
        Returns None for unrecognized hardware questions (falls through to LLM).
        """
        if not self.self_awareness:
            return None

        state = self.self_awareness.get_system_state()
        h = persona.get_honorific()
        lower = command.lower()
        words = set(re.findall(r'\b\w+\b', lower))

        # Determine which hardware aspect they're asking about
        if words & {"model", "llm"} and not words & {"cpu", "gpu", "ram"}:
            if state.llm_provider and state.llm_provider != "unknown":
                text = f"I'm running the {state.llm_provider}"
                if state.llm_quant:
                    text += f" with {state.llm_quant} quantization"
                text += f", {h}."
            else:
                return None

        elif words & {"quantization", "quant"}:
            if state.llm_quant:
                text = f"I'm using {state.llm_quant} quantization"
                if state.llm_provider and state.llm_provider != "unknown":
                    text += f" for the {state.llm_provider} model"
                text += f", {h}."
            else:
                return None

        elif words & {"cpu", "processor"}:
            if state.cpu_model:
                text = f"I'm running on an {state.cpu_model} with {state.cpu_cores} cores, {h}."
            else:
                return None

        elif words & {"gpu", "graphics"}:
            if state.gpu_model:
                text = f"I'm running on a {state.gpu_model}"
                if state.gpu_vram_gb:
                    text += f" with {state.gpu_vram_gb:.0f}GB of VRAM"
                text += f", {h}."
            else:
                return None

        elif words & {"ram"} and not words & {"cpu", "gpu"}:
            if state.ram_total_gb:
                text = f"I have {state.ram_total_gb:.0f}GB of RAM, {h}."
            else:
                return None

        elif words & {"vram"}:
            if state.gpu_vram_gb:
                text = f"I have {state.gpu_vram_gb:.0f}GB of VRAM"
                if state.gpu_model:
                    text += f" on my {state.gpu_model}"
                text += f", {h}."
            else:
                return None

        elif words & {"specs", "hardware"}:
            # Broad specs question — list everything
            parts = []
            if state.cpu_model:
                parts.append(f"an {state.cpu_model} with {state.cpu_cores} cores")
            if state.ram_total_gb:
                parts.append(f"{state.ram_total_gb:.0f}GB of RAM")
            if state.gpu_model:
                gpu = state.gpu_model
                if state.gpu_vram_gb:
                    gpu += f" with {state.gpu_vram_gb:.0f}GB of VRAM"
                parts.append(gpu)
            if state.llm_provider and state.llm_provider != "unknown":
                llm = state.llm_provider
                if state.llm_quant:
                    llm += f" at {state.llm_quant}"
                parts.append(f"running the {llm} model")
            if parts:
                text = f"I'm running on {', '.join(parts)}, {h}."
            else:
                return None

        else:
            # Unrecognized hardware aspect — let LLM handle it
            return None

        logger.info(f"Hardware self-query answered directly: {text[:60]}...")
        return RouteResult(
            text=text, intent="hw_self_query", source="self_awareness",
            handled=True, open_window=DEFAULT_WINDOW,
        )

    # -------------------------------------------------------------------
    # P4-LLM: Tool-calling path (LLM-centric migration Phase 1)
    # -------------------------------------------------------------------

    # Map skill names → tool names for semantic matching.
    # Auto-built from core/tools/*.py definitions via tool_registry.
    from core.tool_registry import TOOL_SKILL_MAP as _TOOL_SKILL_MAP

    # Skills that should route through P4 (native skill handlers) instead of
    # P4-LLM tool-calling, even though they have tool-migrated versions.
    _PREFER_SKILL_ROUTING = {"weather"}

    # Threshold for tool pruning.  Tuned via sweep across 56 queries at
    # thresholds 0.30-0.60 (scripts/test_intent_overlap.py).  0.40 is the
    # only value with zero cliff-risk AND zero false negatives.
    _TOOL_PRUNE_THRESHOLD = 0.40

    # Hard cap on domain tools per request (web_search is added on top).
    # Prevents exceeding the 5-6 tool cliff even if threshold is too loose.
    _MAX_DOMAIN_TOOLS = 4

    # Tools allowed for guest (unrecognized speaker) sessions.
    _GUEST_ALLOWED_TOOLS = {"get_weather", "web_search"}

    # Skills/tools excluded on mobile (desktop-only: open browser, launch apps, etc.)
    _MOBILE_EXCLUDED_SKILLS = {"web_navigation", "app_launcher", "file_editor"}
    _MOBILE_EXCLUDED_TOOLS = {"developer_tools", "take_screenshot"}

    # ── Domain classification for synthesis grounding ──────────────

    _DOMAIN_MATH = re.compile(
        r'\b(calculat|convert|'
        r'how many(?! (calorie|protein|carb|fat|sodium|sugar))|'
        r'how much(?! (calorie|protein|carb|fat|sodium|sugar|notice|time|longer|experience|notice|warning))|'
        r'square feet|square meter|'
        r'gallons?|liters?|grams?|ounces?|pounds?|kilograms?|miles?|'
        r'kilometers?|fahrenheit|celsius|cost estimate|total|subtract|'
        r'multiply|divide\b|divid(ed|ing)|percentage|ratio|mph|km/h)', re.IGNORECASE)

    _DOMAIN_ENTERTAINMENT = re.compile(
        r'\b(movies?|films?|shows?|TV\b|television|series|sitcoms?|'
        r'streaming|netflix|hulu|disney\+?|HBO|prime video|'
        r'oscars?|emmy|golden globe|box office|'
        r'actor|actress|director|screenwriter|'
        r'franchise|sequel|prequel|trilogy|'
        r'season \d|episode|pilot|finale|'
        r'cinema|theater|theatrical|'
        r'rated R|rated PG|rotten tomatoes|imdb|'
        r'starring|cast|cameo|'
        r'horror|comedy|thriller|documentary|anime|'
        r'watch.{1,15}(tonight|next|good)|'
        r'binge|spoiler|trailer|'
        r'grossed|blockbuster|flop|'
        # factual entertainment queries (from old _SYNTH_TEMP_FACTUAL)
        r'who directed|who wrote|who starred|filmography|'
        r'cast of|directed by|'
        r'rank.{1,40}(movies?|films?|shows?|albums?|songs?|books?)|'
        r'best.{1,30}(movies?|films?|shows?)|worst.{1,30}(movies?|films?|shows?)|'
        r'release date|came out|opening weekend|'
        r'still (putting out|making|releasing))\b', re.IGNORECASE)

    # Entertainment listing/ranking pattern — forces web_search when
    # combined with entertainment domain to prevent LLM from skipping
    # search on follow-up ranking queries it thinks it can answer from context.
    _ENTERTAINMENT_LISTING = re.compile(
        r'\b(rank|top \d+|best .{1,30}(movies?|films?|shows?)|'
        r'worst .{1,30}(movies?|films?|shows?)|'
        r'all the .{1,30}(movies?|films?)|'
        r'list .{1,20}(movies?|films?|shows?)|'
        r'every .{0,20}(movies?|films?|shows?|episodes?|seasons?))\b', re.IGNORECASE)

    # Gaming opinion/review queries — force web search so the LLM doesn't
    # punt on "which has the best reviews" style follow-ups.
    _GAMING_OPINION = re.compile(
        r'\b(best|top|highest.{0,10}rated|reviews?|scores?|ratings?|'
        r'rank|recommend|worth.{0,10}(buying|playing|getting))\b', re.IGNORECASE)

    # NOTE: New domain regexes omit trailing \b so stem-matches work
    # (e.g. "vaccin" matches "vaccination", "nanotechnol" matches "nanotechnology").
    # Leading \b still ensures matches start at a word boundary.

    _DOMAIN_VETERINARY = re.compile(
        r'\b('
        # Clinical / facility terms
        r'veterinar|vet (clinic|visit|appointment|bill|checkup|emergency)|'
        r'pet (health|medication|insurance|surgery|emergency|food|diet|poison)|'
        r'animal (hospital|clinic|health|welfare)|Banfield|'
        r'spay(ed|ing)?|neuter(ed|ing)?|microchip(ped)?|'
        r'heartworm|flea (treat|medic|prevent)|tick (treat|prevent)|'
        r'parvo(virus)?|distemper|bordetella|leptospirosis|kennel cough|'
        r'(toxic|poison).{1,15}(dog|cat|pet|animal)|'
        # Canines — .{1,16} gap allows "dog is vomiting", "puppy won't eat his food"
        r'canine|dog.{1,12}(breed|food|health|sick|allerg|weight|vaccin|ate |vomit|diarrhea)|'
        r'puppy.{1,16}(food|vaccin|train|health|worm)|'
        # Felines
        r'feline|cat.{1,12}(breed|food|health|sick|allerg|weight|vaccin|vomit|diarrhea)|'
        r'kitten.{1,8}(food|vaccin|health|worm)|'
        # Equine
        r'equine|horse.{1,10}(health|vet|colic|lame|lamin|hoof|feed|vaccin|deworm|breed)|'
        r'foal|mare|stallion|gelding|'
        # Bovidae / Bovine
        r'bovid(ae|e)?|bovine|cattle.{1,10}(health|vet|vaccin|disease|feed)|'
        r'calf.{1,8}(health|vaccin|scour)|heifer|'
        # Avian
        r'avian|bird.{1,10}(health|vet|sick|feather|beak)|'
        r'parrot.{1,8}(health|diet|feather)|parakeet|cockatiel|cockatoo|'
        r'chicken.{1,10}(health|vet|disease|egg)|poultry.{1,8}(health|disease)|'
        # Rodentia
        r'rodent(ia)?|hamster.{1,8}(health|vet|sick|diet)|'
        r'guinea pig.{1,8}(health|vet|sick|diet)|gerbil|chinchilla|'
        r'rabbit.{1,8}(health|vet|sick|diet|vaccin)|bunny.{1,8}(health|sick)|'
        # Piscine / Ichthyic
        r'piscine|ichthy(ic|olog)|fish.{1,10}(health|disease|tank|sick|parasite|fungus)|'
        r'aquarium.{1,10}(health|disease|medic)|'
        # Serpentes
        r'serpentes|snake.{1,10}(health|vet|sick|shed|feed|habitat)|'
        r'reptile.{1,10}(health|vet|sick|habitat)|lizard.{1,8}(health|vet)|gecko.{1,8}(health|vet)|'
        r'turtle.{1,8}(health|vet|shell)|tortoise.{1,8}(health|vet)|'
        # Primates
        r'primate.{1,10}(health|vet|diet|behavior|enrich)|'
        r'monkey.{1,8}(health|vet|diet)|ape.{1,8}(health|vet)|'
        # Cross-cutting pet health terms
        r'pet.{1,6}(vaccin|deworm|dental|groom|nutrition|obesity|anxiety|behavior)|'
        r'animal.{1,6}(vaccin|welfare|rescue|shelter)'
        r')', re.IGNORECASE)

    _DOMAIN_MEDICAL = re.compile(
        r'\b(symptom|diagnosis|medication|prescription|dosage|'
        r'side effects?|treatment|therapy|disease|disorder|'
        r'surgery|blood pressure|cholesterol|diabetes|'
        r'antibiotic|vaccine|infection|allerg(y|ic|ies)|'
        r'FDA|CDC|NIH|'
        r'cancer|tumor|oncolog|'
        r'vitamin|supplement|deficiency|'
        r'(drug|medicine).{1,15}(interact|effect|safe)|'
        r'medical|prognosis|'
        r'pregnant|pregnancy|prenatal|'
        r'CPR|first aid|emergency room|urgent care)', re.IGNORECASE)

    _DOMAIN_FINANCE = re.compile(
        r'\b(stock|stocks|S&P|nasdaq|dow jones|NYSE|'
        r'invest(ing|ment|or)|portfolio|dividend|'
        r'crypto(currency)?|bitcoin|ethereum|'
        r'interest rate|mortgage rate|APR|APY|'
        r'401k|IRA|Roth|mutual fund|ETF|index fund|'
        r'market (cap|crash|correction|rally|bear|bull)|bull market|bear market|'
        r'earnings (report|call|per share)|EPS|P/E ratio|'
        r'inflation|recession|GDP|'
        r'forex|treasury|bond yield|'
        r'financial (advi|plan)|'
        r'tax (bracket|deduction|credit|return))', re.IGNORECASE)

    _DOMAIN_GAMING = re.compile(
        r'\b(video game|game (release|review|score|trailer|DLC|expansion|patch|update)|'
        r'PlayStation|PS[45]|Xbox|Nintendo|Switch|Steam|Epic Games|'
        r'PC gaming|gaming PC|'
        r'Metacritic|IGN|GameSpot|'
        r'RPG|FPS|MMO|MOBA|battle royale|'
        r'DLC|downloadable content|season pass|microtransaction|'
        r'E3|Game Awards|Gamescom|'
        r'esports?|competitive gaming|'
        r'(game|gam(er|ing)).{1,20}(recommend|suggest|similar|like)|'
        r'speedrun|achievement|trophy|platinum|'
        r'gameplay|multiplayer|co.op|single.player|'
        r'rank.{1,40}(games?|video games?)|'
        r'best.{1,30}(games?|video games?)|worst.{1,30}(games?|video games?)|'
        r'(indie|AAA) (game|title|studio))', re.IGNORECASE)

    _DOMAIN_SPORTS = re.compile(
        r'\b(NFL|NBA|MLB|NHL|MLS|NCAA|FIFA|UEFA|'
        r'Super Bowl|World Series|World Cup|Stanley Cup|'
        r'playoffs?|championship|tournament|'
        r'quarterback|touchdown|home run|three.pointer|'
        r'batting average|ERA\b|yards|assists|rebounds|'
        r'(team|player) (stats?|record|roster|draft)|'
        r'free agent|trade deadline|'
        r'(season|game|match) (score|result|recap|highlight)|'
        r'standings|rankings|seedings?|bracket|'
        r'Olympics|medal count|'
        r'soccer|football|basketball|baseball|hockey|tennis|golf)', re.IGNORECASE)

    _DOMAIN_AUTOMOTIVE = re.compile(
        r'\b(car (review|price|spec|recall|safety|insurance|loan|lease|comparison)|'
        r'truck (review|price|spec|recall|towing)|'
        r'SUV|sedan|coupe|hatchback|minivan|'
        r'MSRP|invoice price|sticker price|'
        r'NHTSA|IIHS|crash test|(vehicle|car|auto|truck) safety rating|'
        r'oil change|tire (rotat|replac|pressure)|'
        r'check engine|transmission|brake (pad|rotor)|'
        r'miles per gallon|MPG|fuel economy|'
        r'car (buy|shop|deal|financ)|'
        r'horsepower|torque|cylind|turbo(charg)?|'
        r'electric vehicle|EV (range|charg|battery)|hybrid|'
        r'(vehicle|auto) (recall|warranty|maintenance)|'
        r'test drive|trade.in value|KBB|Kelley Blue Book|'
        r'CarFax|car history|VIN)', re.IGNORECASE)

    _DOMAIN_PROGRAMMING = re.compile(
        r'\b(API (endpoint|key|rate limit|documentation|version)|'
        r'(Python|JavaScript|TypeScript|Rust|Go|Java|C\+\+|Ruby|Swift|Kotlin)'
        r'.{1,20}(library|package|framework|version|syntax|error)|'
        r'npm|pip install|cargo|gem install|maven|gradle|'
        r'stack overflow|github|gitlab|'
        r'debug(ging)?|error (message|code|handling)|'
        r'framework (comparison|recommend|vs)|'
        r'database (schema|query|migration|index)|'
        r'REST(ful)?\b|GraphQL|WebSocket|gRPC|'
        r'docker|kubernetes|CI/CD|deployment|'
        r'git (command|branch|merge|rebase|cherry))', re.IGNORECASE)

    _DOMAIN_SCIENCE_TECH = re.compile(
        r'\b(research (paper|study|finding|journal)|peer.review|'
        r'clinical trial|scientific (consensus|evidence|method|study)|'
        r'quantum (comput|mechanic|physic)|nanotechnol|biotechnol|'
        r'gene (edit|therap)|CRISPR|genome|genomic|'
        r'artificial intelligence|machine learning|deep learning|neural network|'
        r'SpaceX|NASA|ESA|rocket launch|'
        r'renewable energy|solar (panel|energy)|wind (turbine|energy)|'
        r'semiconductor|microchip|'
        r'climate (change|science|model)|global warming|'
        r'fusion (reactor|energy)|particle (accelerat|collid))', re.IGNORECASE)

    _DOMAIN_NUTRITION = re.compile(
        r'\b(calorie|caloric|macro(nutrient)?s?\b|'
        r'protein (content|per|in|daily|intake)|carb(ohydrate)?s? (content|per|in|daily)|'
        r'fat (content|per|in|daily|saturated|trans|unsaturated)|'
        r'fiber (content|per|in|daily|intake)|'
        r'(daily|recommended) (intake|allowance|value)|RDA\b|'
        r'nutrition (fact|label|info|data|value)|'
        r'(food|meal) (calorie|nutrition|macro)|'
        r'(keto|paleo|vegan|vegetarian|carnivore|mediterranean|DASH|whole30) diet|'
        r'intermittent fasting|'
        r'glycemic (index|load)|'
        r'sodium (content|per|in|daily|intake)|cholesterol (content|per|in)|'
        r'(how many|how much) (calorie|protein|carb|fat|sodium|sugar).{1,20}(in|per|does)|'
        r'weight (loss|gain) (diet|food|plan|meal)|'
        r'body mass index|BMI\b|'
        r'nutritionist|dietitian|dietician)', re.IGNORECASE)

    _DOMAIN_LEGAL = re.compile(
        r'\b(lawsuit|litigation|class action|'
        r'statute|ordinance|regulation.{1,10}(law|legal|comply|violat)|'
        r'felony|misdemeanor|indictment|arraign|'
        r'legal (advi|right|counsel|precedent|represent|obligat|liabilit|remedy|standard|limit)|'
        r'attorney|lawyer|paralegal|law firm|'
        r'plaintiff|defendant|prosecutor|'
        r'court (ruling|order|case|hearing|filing)|'
        r'supreme court|circuit court|appeals court|'
        r'contract (law|breach|clause|term|negotiat)|'
        r'tort\b|negligence|malpractice|'
        r'intellectual property|trademark|patent (law|filing|infring)|copyright (law|infring)|'
        r'custody|divorce (law|filing|proceed)|child support|alimony|'
        r'estate (plan|law|probate)|will and testament|trust (fund|law)|'
        r'criminal (charge|record|defense|law)|'
        r'immigration (law|visa|status|petition)|deportat|asylum|'
        r'(is it|am I) (legal|illegal|allowed|liable)|'
        r'(can I|can they) (sue|be sued|file)|'
        r'eviction (notice|process|law)|tenant (right|law)|'
        r'(landlord|tenant).{1,30}(is trying|wants to|right|required|can|may|will need|won.t need|notice|eviction|provide|exempt|allowed|obligat))', re.IGNORECASE)

    _DOMAIN_HISTORY = re.compile(
        r'\b(ancient (rome|greece|egypt|civilization|world)|'
        r'medieval|renaissance|enlightenment era|'
        r'(world war|civil war|revolutionary war|cold war)|'
        r'(roman|ottoman|british|persian|byzantine|mongol) empire|'
        r'industrial revolution|french revolution|american revolution|'
        r'(19th|18th|17th|16th|15th|14th|13th|20th) century|'
        r'historical (event|figure|period|significance|context)|'
        r'founding father|declaration of independence|constitution.{1,10}(amendment|ratif|sign)|'
        r'(king|queen|emperor|pharaoh|czar|tsar).{1,15}(of|ruled|reign)|'
        r'colonialis|imperialis|abolitio|emancipat|'
        r'(battle of|siege of|treaty of|fall of)\b)', re.IGNORECASE)

    _DOMAIN_REAL_ESTATE = re.compile(
        r'\b(home (value|price|apprais|inspect|buy|sell|list|worth)|'
        r'house (value|price|apprais|inspect|buy|sell|list|worth|hunt)|'
        r'property (value|tax|assess|zoning|line|boundar)|'
        r'real estate (agent|market|invest|price|trend|listing)|'
        r'realtor|MLS\b|Zillow|Redfin|Trulia|'
        r'(median|average) home price|'
        r'(buy|sell|flip)(ing)? (a |)(house|home|property|condo|townhouse)|'
        r'mortgage (pre.approv|lender|broker|payment|calculat|qualify)|'
        r'down payment|closing cost|escrow|title (insurance|company|search)|'
        r'home (equity|loan|refinanc|warranty)|HELOC|'
        r'(HOA|homeowner.{1,4}association) (fee|rule|dues)|'
        r'condo (fee|association|board)|'
        r'(residential|commercial) (property|zoning|real estate)|'
        r'property management|rental (property|income|market|rate)|'
        r'(housing|real estate) market|housing bubble|'
        r'square (foot|feet) (cost|price|value)|cost per square (foot|feet)|'
        r'(neighborhood|area) (safe|school|rating|walkab))', re.IGNORECASE)

    _DOMAIN_TRAVEL = re.compile(
        r'\b(hotel|motel|hostel|airbnb|vrbo|booking\.com|'
        r'(flights?|airline|airfare) (to|from|price|cost|deal|book|cancel)|'
        r'airport (code|terminal|lounge|shuttle)|TSA|'
        r'(tourist|travel) (attract|destin|guide|adviso|visa|insurance)|'
        r'(best|top|popular|cheap) (restaurant|hotel|bar|cafe|attraction|thing).{1,15}(in|near|around)|'
        r'(restaurant|hotel|bar|cafe|attraction) (recommend|suggest|review|rating).{1,15}(in|near|for)|'
        r'itinerary|travel (plan|budget|tip|hack|safe)|'
        r'(cruise|resort|all.inclusive|vacation (package|rental|spot))|'
        r'passport (renew|expir|applicat)|visa (requir|applicat|process)|'
        r'(things to do|places to visit|where to eat|where to stay|must.see).{0,15}(in|near|around)|'
        r'best time to (visit|travel to|go to)|'
        r'travel (to|from|between).{1,30}(cost|time|best|cheap|fast)|'
        r'layover|connecting flight|stopover|'
        r'jet lag|travel (vaccin|immuniz)|'
        r'(carry.on|checked bag|luggage) (size|weight|allow|restrict)|'
        r'currency exchange|local (currency|money)|exchange rate)', re.IGNORECASE)

    _DOMAIN_FACTUAL = re.compile(
        r'\b(when did|what year|who won|who invented|capital of|'
        r'population of|founded in|born in|died in|height of|'
        r'who is the|who was the)\b', re.IGNORECASE)

    _DOMAIN_GEO = re.compile(
        r'\b(drive from|driving|route to|road trip|directions to|'
        r'border crossing|gas stops?|how far is|distance from|'
        r'navigate to|get to .+ from)\b', re.IGNORECASE)

    # Category → synthesis temperature mapping
    _DOMAIN_TEMPERATURES = {
        "math": 0.2,
        "veterinary": 0.2,
        "medical": 0.2,
        "nutrition": 0.2,
        "finance": 0.2,
        "legal": 0.2,
        "entertainment": 0.3,
        "gaming": 0.3,
        "sports": 0.3,
        "automotive": 0.3,
        "real_estate": 0.3,
        "programming": 0.3,
        "science_tech": 0.3,
        "history": 0.3,
        "factual": 0.3,
        "travel": 0.4,
        "geo": 0.4,
    }

    def _classify_query_domain(self, command: str) -> str | None:
        """Classify query into a domain category for synthesis grounding.

        Returns a category string that maps to both a synthesis temperature
        and a domain-specific synthesis prompt in llm_router.py.

        Priority order (17 domains):
        math → vet → medical → nutrition → finance → legal → sports →
        gaming → entertainment → automotive → real_estate → programming →
        science_tech → history → factual → travel → geo.

        Rationale: math highest precision; vet before medical (pet-specific);
        nutrition after medical (vitamin/supplement stay medical); legal after
        finance (mortgage rate stays finance); gaming/sports before entertainment
        (broad terms); history before factual (specific eras vs generic "when did");
        travel before geo (planning vs navigation).

        Returns None for general/conversational queries.
        """
        if self._DOMAIN_MATH.search(command):
            return "math"
        if self._DOMAIN_VETERINARY.search(command):
            return "veterinary"
        if self._DOMAIN_MEDICAL.search(command):
            return "medical"
        if self._DOMAIN_NUTRITION.search(command):
            return "nutrition"
        if self._DOMAIN_FINANCE.search(command):
            return "finance"
        if self._DOMAIN_LEGAL.search(command):
            return "legal"
        if self._DOMAIN_SPORTS.search(command):
            return "sports"
        if self._DOMAIN_GAMING.search(command):
            return "gaming"
        if self._DOMAIN_ENTERTAINMENT.search(command):
            return "entertainment"
        if self._DOMAIN_AUTOMOTIVE.search(command):
            return "automotive"
        if self._DOMAIN_REAL_ESTATE.search(command):
            return "real_estate"
        if self._DOMAIN_PROGRAMMING.search(command):
            return "programming"
        if self._DOMAIN_SCIENCE_TECH.search(command):
            return "science_tech"
        if self._DOMAIN_HISTORY.search(command):
            return "history"
        if self._DOMAIN_FACTUAL.search(command):
            return "factual"
        if self._DOMAIN_TRAVEL.search(command):
            return "travel"
        if self._DOMAIN_GEO.search(command):
            return "geo"
        return None

    def _handle_tool_calling(self, command: str,
                             in_conversation: bool = False) -> RouteResult | None:
        """P4-LLM: Route through LLM with dynamically selected tools.

        If the command appears relevant to any tool-enabled skill, prepare
        LLM context with pruned tools.  Returns a RouteResult with
        handled=False and use_tools set, signaling the frontend to call
        stream_with_tools() with the specified tools.

        Returns None if no tool-enabled skills are relevant (falls through
        to P4 legacy skill routing).
        """
        logger.debug("_handle_tool_calling: command=%.80s guest=%s mobile=%s",
                     command, self._is_guest, self._is_mobile)
        tools = self._select_tools_for_command(command) or []

        # Inject prior-turn tool families for anaphoric follow-ups.
        # This runs BEFORE the empty-tools bail-out so that vague
        # follow-ups like "list them" / "which is biggest" still get
        # the tools from the prior turn even when semantic matching
        # finds nothing.
        tools = self._apply_anaphoric_carryover(tools)

        if not tools:
            logger.debug(f"P4-LLM: no tools selected for: {command[:80]}")
            return None

        # Guest mode: restrict to weather + web_search, strip personal tools
        if self._is_guest:
            tools = [t for t in tools
                     if t["function"]["name"] in self._GUEST_ALLOWED_TOOLS]
            if not tools:
                logger.debug("P4-LLM: guest — no allowed tools for command")
                return None

        # Mobile mode: strip desktop-only tools (browser, app launcher, etc.)
        if self._is_mobile:
            tools = [t for t in tools
                     if t["function"]["name"] not in self._MOBILE_EXCLUDED_TOOLS]

        logger.debug(f"P4-LLM: selected {len(tools)} tools, routing to LLM")

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg._write("tool_selection", {
            "command": command[:200],
            "tool_count": len(tools),
            "tool_names": [t["function"]["name"] for t in tools],
            "guest": self._is_guest,
            "mobile": self._is_mobile,
        })

        # Prepare the same LLM context as _prepare_llm_context()
        result = self._prepare_llm_context(
            command,
            in_conversation=in_conversation,
        )
        # Augment with tool-calling fields
        result.use_tools = tools
        result.tool_temperature = 0.0    # Deterministic — sweep showed 0.0 is fastest, same accuracy
        result.tool_presence_penalty = 0.0  # Sweep: pp=1.5 doubled latency with zero accuracy gain
        category = self._classify_query_domain(command)
        result.synthesis_category = category
        result.synthesis_temperature = self._DOMAIN_TEMPERATURES.get(category) if category else None
        if category:
            logger.debug("P4-LLM: domain=%s synth_temp=%s",
                         category, result.synthesis_temperature)
        # Emit domain classification debug event
        _dbg._write("domain_classification", {
            "command": command[:200],
            "category": category,
            "temperature": result.synthesis_temperature,
        })
        if category == "entertainment" and self._ENTERTAINMENT_LISTING.search(command):
            result.force_web_search = True
            logger.debug("P4-LLM: force_web_search=True (entertainment listing)")
        if category == "gaming" and self._GAMING_OPINION.search(command):
            result.force_web_search = True
            logger.debug("P4-LLM: force_web_search=True (gaming opinion/review)")
        result.intent = "tool_calling"

        tool_names = [t["function"]["name"] for t in tools]
        logger.debug(
            f"P4-LLM: routing via tool-calling with {len(tools)} tools: "
            f"{', '.join(tool_names)}"
        )
        return result

    def _select_tools_for_command(self, command: str) -> list | None:
        """Select relevant tool schemas for a command via semantic matching.

        Uses the skill_manager's pre-computed embedding cache to score the
        command against tool-enabled skills' intents.  Returns a list of
        tool schema dicts (always includes always-on tools like web_search
        and recall_memory) or None if no skill tools are relevant.

        Critical guard: also scores non-migrated skills.  If a non-migrated
        skill has a higher semantic score than the best migrated skill, we
        return None to let P4 (legacy skill routing) handle it.  This
        prevents over-capture of queries meant for non-tool skills.

        Hard cap: even if many skills pass the threshold, only the top
        _MAX_DOMAIN_TOOLS (4) are kept, preventing the 5-6 tool cliff.
        """
        # NOTE: do NOT reset self._deferred_domain_tools here — the
        # stash may have been set by a previous deferral and is consumed
        # by the LLM fallback path in route().
        sm = self.skill_manager
        if not hasattr(sm, '_embedding_model') or not sm._embedding_model:
            return None

        # Lazy import to avoid circular dependency at module load
        from core.tool_registry import (ALWAYS_INCLUDED_TOOLS, SKILL_TOOLS,
                                         _tool_modules)

        try:
            from sentence_transformers import util as st_util
        except ImportError:
            return None

        try:
            user_embedding = sm._embedding_model.encode(
                command, convert_to_tensor=True, show_progress_bar=False
            )
        except (RuntimeError, Exception) as e:
            if 'out of memory' in str(e).lower():
                logger.warning("VRAM OOM in tool pruner — including all tools")
                return None
            raise

        # Score always-included tools that declare INTENT_EXAMPLES.
        # These tools have no corresponding skill, so the skill loop below
        # can't see them.  Treating a high-scoring always-included tool as
        # a "migrated" match prevents the pruner from incorrectly deferring
        # to P4 skill routing (e.g. file_editor for "take a screenshot").
        best_always_included_score = 0.0
        for mod in _tool_modules:
            examples = getattr(mod, 'INTENT_EXAMPLES', None)
            if not examples or not getattr(mod, 'ALWAYS_INCLUDED', False):
                continue
            if not hasattr(self, '_always_included_embeddings'):
                self._always_included_embeddings = {}
            cache_key = mod.TOOL_NAME
            if cache_key not in self._always_included_embeddings:
                self._always_included_embeddings[cache_key] = (
                    sm._embedding_model.encode(
                        examples, convert_to_tensor=True,
                        show_progress_bar=False,
                    )
                )
            embs = self._always_included_embeddings[cache_key]
            sims = st_util.cos_sim(user_embedding, embs)
            score = float(sims.max())
            logger.debug("  Tool pruning: always_included %s = %.2f",
                         mod.TOOL_NAME, score)
            if score > best_always_included_score:
                best_always_included_score = score

        # Score ALL skills (migrated and non-migrated) to find the best match
        best_migrated_score = 0.0
        best_non_migrated_score = 0.0
        best_non_migrated_name = ""
        web_nav_score = 0.0
        matched_tools = []  # [(score, tool_schema), ...]

        for skill_name, skill in sm.skills.items():
            if not hasattr(skill, 'semantic_intents'):
                continue

            # Best score across all intents for this skill
            skill_best = 0.0
            for intent_id, data in skill.semantic_intents.items():
                cache_key = (skill_name, intent_id)
                example_embeddings = sm._semantic_embedding_cache.get(cache_key)
                if example_embeddings is None:
                    continue
                similarities = st_util.cos_sim(user_embedding, example_embeddings)
                max_sim = float(similarities.max())
                if max_sim > skill_best:
                    skill_best = max_sim

            logger.debug(f"  Tool pruning: {skill_name} = {skill_best:.2f}")

            if skill_name in self._TOOL_SKILL_MAP and skill_name not in self._PREFER_SKILL_ROUTING:
                # Migrated skill — track for tool selection
                if skill_best > best_migrated_score:
                    best_migrated_score = skill_best
                if skill_best >= self._TOOL_PRUNE_THRESHOLD:
                    tool_names = self._TOOL_SKILL_MAP[skill_name]
                    for tn in tool_names:
                        schema = SKILL_TOOLS.get(tn)
                        if schema:
                            matched_tools.append((skill_best, schema))
            else:
                # Non-migrated skill — track best score for guard check.
                if skill_name == 'web_navigation':
                    web_nav_score = skill_best
                    # Keyword override: if query contains a web_navigation
                    # keyword, ensure deferral to P4 so native handlers run.
                    kw_meta = sm.skill_metadata.get('web_navigation')
                    if kw_meta:
                        for kw in getattr(kw_meta, 'keywords', []):
                            if re.search(r'\b' + re.escape(kw.lower()) + r'\b', command.lower()):
                                web_nav_score = max(web_nav_score, self._TOOL_PRUNE_THRESHOLD)
                                logger.debug("web_nav keyword '%s' found — forcing deferral", kw)
                                break
                elif skill_best > best_non_migrated_score:
                    best_non_migrated_score = skill_best
                    best_non_migrated_name = skill_name

        # Fold always-included tool scores into migrated score so they
        # participate in the non-migrated guard.  An always-included tool
        # that scores higher than any non-migrated skill should prevent
        # deferral to P4 (e.g. take_screenshot > file_editor).
        effective_migrated = max(best_migrated_score, best_always_included_score)

        logger.debug(
            "Tool pruning summary: migrated=%.2f, always_incl=%.2f, "
            "non_migrated=%.2f (%s), web_nav=%.2f, domain_tools=%d",
            best_migrated_score, best_always_included_score,
            best_non_migrated_score,
            best_non_migrated_name or "none", web_nav_score, len(matched_tools),
        )

        if not matched_tools:
            # If an always-included tool scored well, route through tool
            # calling with always-on tools — don't defer to P4.
            if (best_always_included_score >= self._TOOL_PRUNE_THRESHOLD
                    and best_always_included_score >= best_non_migrated_score
                    and best_always_included_score >= web_nav_score):
                logger.info(
                    "Tool pruning: always-included tool scored %.2f — "
                    "routing with always-on tools",
                    best_always_included_score,
                )
                return self._apply_anaphoric_carryover(list(ALWAYS_INCLUDED_TOOLS.values()))

            # No domain tools matched.  If web_navigation scored well,
            # defer to P4 so the native handlers (YouTube, Amazon, etc.)
            # can run instead of the generic web_search tool.
            if web_nav_score >= self._TOOL_PRUNE_THRESHOLD:
                if best_non_migrated_score > web_nav_score:
                    logger.info(
                        "Tool pruning: web_nav scored %.2f but %s scored "
                        "%.2f — deferring to P4 skill routing",
                        web_nav_score, best_non_migrated_name,
                        best_non_migrated_score,
                    )
                    return None
                logger.info(
                    "Tool pruning: web_nav scored %.2f — deferring to P4 "
                    "for native web_navigation handlers",
                    web_nav_score,
                )
                return None
            # If a non-migrated skill (not web_nav) scored well, defer to P4.
            # Stash its domain tools so LLM fallback can use them if P4 fails.
            if best_non_migrated_score >= self._TOOL_PRUNE_THRESHOLD:
                logger.info(
                    "Tool pruning: non-migrated '%s' scored %.2f — "
                    "deferring to P4 skill routing",
                    best_non_migrated_name, best_non_migrated_score,
                )
                domain_tools = []
                if best_non_migrated_name in self._TOOL_SKILL_MAP:
                    for tn in self._TOOL_SKILL_MAP[best_non_migrated_name]:
                        schema = SKILL_TOOLS.get(tn)
                        if schema and schema not in domain_tools:
                            domain_tools.append(schema)
                for prefer_name in self._PREFER_SKILL_ROUTING:
                    if prefer_name in self._TOOL_SKILL_MAP:
                        for tn in self._TOOL_SKILL_MAP[prefer_name]:
                            schema = SKILL_TOOLS.get(tn)
                            if schema and schema not in domain_tools:
                                domain_tools.append(schema)
                self._deferred_domain_tools = domain_tools if domain_tools else None
                return None
            # No domain tools AND no non-migrated skill matched.
            # Still give the LLM always-on tools (web_search, recall_memory)
            # so queries like "Alabama football score" can trigger web_search.
            logger.debug("P4-LLM: no domain tools, falling through with always-on tools")
            return self._apply_anaphoric_carryover(list(ALWAYS_INCLUDED_TOOLS.values()))

        # Guard: if a non-migrated skill (including web_navigation) scores
        # meaningfully higher than the best migrated/always-included tool,
        # defer to P4 so native skill handlers run instead of LLM tool-calling.
        # Stash the domain tools so the main route() can use them if P4
        # also fails (prevents tool starvation).
        effective_non_migrated = max(best_non_migrated_score, web_nav_score)
        if effective_non_migrated > effective_migrated:
            winner = (best_non_migrated_name if best_non_migrated_score >= web_nav_score
                      else "web_navigation")
            logger.info(
                "Tool pruning: non-migrated skill '%s' scored higher "
                "(%.2f > migrated %.2f) — deferring to P4",
                winner, effective_non_migrated, effective_migrated,
            )
            # Stash domain tools for fallback if P4 also fails
            domain_tools = [t[1] for t in matched_tools]
            # Ensure prefer-skill tools are included in deferred set (guest fallback)
            for prefer_name in self._PREFER_SKILL_ROUTING:
                if prefer_name in self._TOOL_SKILL_MAP:
                    for tn in self._TOOL_SKILL_MAP[prefer_name]:
                        schema = SKILL_TOOLS.get(tn)
                        if schema:
                            domain_tools = domain_tools or []
                            if schema not in domain_tools:
                                domain_tools.append(schema)
            self._deferred_domain_tools = domain_tools if domain_tools else None
            return None

        # Hard cap: keep only the top-scoring domain tools
        matched_tools.sort(key=lambda x: x[0], reverse=True)
        if len(matched_tools) > self._MAX_DOMAIN_TOOLS:
            dropped = matched_tools[self._MAX_DOMAIN_TOOLS:]
            dropped_names = [t[1]["function"]["name"] for t in dropped]
            logger.info(
                f"Tool pruning: hard cap applied, dropped {dropped_names}"
            )
            matched_tools = matched_tools[:self._MAX_DOMAIN_TOOLS]

        # Always include always-on tools (web_search, recall_memory)
        result_tools = list(ALWAYS_INCLUDED_TOOLS.values()) + [t[1] for t in matched_tools]

        # Anaphoric carryover: if the prior turn called domain tools
        # (filesystem, developer), keep them available for follow-up
        # references like "list them", "what's in there", "delete the largest"
        result_tools = self._apply_anaphoric_carryover(result_tools)

        return result_tools

    # Tool families for anaphoric carryover.  If any member of a family
    # was used in the prior turn, ALL members are injected so the LLM
    # can chain related operations (e.g. find → delete, list → modify).
    _ANAPHORIC_TOOL_FAMILIES = [
        {"find_files", "developer_tools", "get_system_info"},
        {"manage_reminders"},
    ]

    def _apply_anaphoric_carryover(self, tools: list) -> list:
        """Add prior-turn domain tools for anaphoric follow-ups.

        When the prior turn called a tool in a defined family, inject the
        entire family so the LLM can chain related operations like
        'list them', 'what's in there now', 'delete the largest one'.
        """
        # Never inject extra tools for guest users — respect the guest gate
        if self._is_guest:
            return tools
        prior = getattr(self.conv_state, 'last_tools_called', None)
        if not prior:
            return tools

        # Collect all tools to inject: for each prior tool that belongs
        # to a family, add every member of that family.
        inject = set()
        for tool_name in prior:
            for family in self._ANAPHORIC_TOOL_FAMILIES:
                if tool_name in family:
                    inject |= family

        if not inject:
            return tools

        current_names = {t["function"]["name"] for t in tools}
        from core.tool_registry import SKILL_TOOLS
        added = []
        for tool_name in inject:
            if tool_name not in current_names:
                schema = SKILL_TOOLS.get(tool_name)
                if schema:
                    tools.append(schema)
                    added.append(tool_name)
        if added:
            logger.info("Anaphoric carryover: added %s (family) from prior turn", added)
        return tools

    # Global confidence floor for skill routing.  Matches below this
    # threshold fall through to LLM instead of executing the skill.
    # Prevents short ambiguous utterances ("delete it", "open it") from
    # triggering low-confidence skill matches (0.52-0.54).
    _SKILL_CONFIDENCE_FLOOR = 0.60

    def _handle_skill_routing(self, command: str) -> RouteResult | None:
        """P4: Skill routing (semantic + keyword matching)."""
        # Mobile mode: check match BEFORE executing to prevent side effects
        # (e.g. opening a browser on the server desktop)
        if self._is_mobile:
            match = self.skill_manager.match_intent(command)
            if match and match[0] in self._MOBILE_EXCLUDED_SKILLS:
                logger.debug(f"P4: blocked desktop-only skill '{match[0]}' on mobile (pre-exec)")
                return None

        # Pre-check confidence before executing (avoids side effects from
        # low-confidence matches).  Layers without confidence scores
        # (exact, fuzzy, keyword-direct with single match) are trusted.
        match = self.skill_manager.match_intent(command)
        match_info = self.skill_manager._last_match_info
        if match and match_info:
            conf = match_info.get("confidence")
            layer = match_info.get("layer", "")
            if conf is not None and conf < self._SKILL_CONFIDENCE_FLOOR:
                logger.info(
                    "P4: skill '%s' confidence %.2f < floor %.2f "
                    "(layer=%s, intent=%s) — falling through to LLM",
                    match_info.get("skill_name"), conf,
                    self._SKILL_CONFIDENCE_FLOOR, layer,
                    match_info.get("intent_id"),
                )
                return None

        if not match:
            return None

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_skill_match(
            skill_name=match_info.get("skill_name", "") if match_info else "",
            layer=match_info.get("layer") if match_info else None,
            confidence=match_info.get("confidence") if match_info else None,
            intent_id=match_info.get("intent_id") if match_info else None,
        )

        # Confidence OK per pre-check — execute the skill
        response = self.skill_manager.execute_intent(command)
        match_info = self.skill_manager._last_match_info

        # Post-execution floor check for keyword-direct disambiguation.
        # Pre-check (above) can't catch these because confidence is None
        # at match time — disambiguation only scores during execute_intent.
        # Scoped to keyword_direct to avoid breaking keyword-semantic
        # relaxed tiers (0.20-0.40) used by multi-keyword compounds.
        if response and match_info:
            post_conf = match_info.get("confidence")
            post_layer = match_info.get("layer", "")
            if (post_conf is not None
                    and post_layer == "keyword_direct"
                    and post_conf < self._SKILL_CONFIDENCE_FLOOR):
                logger.info(
                    "P4: post-exec skill '%s' confidence %.2f < floor %.2f "
                    "(layer=%s) — discarding, falling through to LLM",
                    match_info.get("skill_name"), post_conf,
                    self._SKILL_CONFIDENCE_FLOOR, post_layer,
                )
                return None

        if response:
            logger.info("Handled by skill")
            return RouteResult(
                text=response, intent="skill", source="skill",
                handled=True, match_info=match_info,
            )
        return None

    def _handle_news_continuation(self, command: str) -> RouteResult | None:
        """P5: News continuation ('continue', 'more headlines')."""
        nm = self.news_manager
        if not nm:
            return None

        continue_words = ["continue", "keep going", "more headlines",
                          "go on", "read more"]
        if not any(w in command.strip().lower() for w in continue_words):
            return None

        remaining = nm.get_unread_count()
        if sum(remaining.values()) <= 0:
            return None

        text = nm.read_headlines(limit=5)
        return RouteResult(
            text=text, intent="news_continue", source="skill",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    # -------------------------------------------------------------------
    # Progressive context compression
    # -------------------------------------------------------------------

    def _extract_topic(self, command: str, response: str) -> None:
        """Extract a one-line topic anchor from the first exchange.

        Called lazily at the start of turn 2 when the topic is still empty.
        Stores the result in conv_state.conversation_topic.
        """
        prompt = (
            "Summarize the topic of this conversation in one short phrase "
            "(under 15 words). Include key specifics (names, places, "
            "destinations, numbers).\n"
            f'User: "{command[:200]}"\n'
            f'Assistant: "{response[:400]}"\n'
            "Topic:"
        )
        try:
            topic = self.llm.generate(
                prompt, max_tokens=30, temperature=0.0
            ).strip().strip('"').strip()
            if topic:
                self.conv_state.conversation_topic = topic
                logger.debug("Topic anchor extracted: %s", topic)
        except Exception as e:
            logger.warning("Topic extraction failed: %s", e)

    def _summarize_exchange(self, question: str, answer: str) -> str:
        """Compress a Q&A exchange into 1-2 sentences for context retention."""
        prompt = (
            "Compress this Q&A exchange into 1-2 sentences. "
            "Preserve exact numerical values, multipliers, formulas, "
            "and units — these are critical for follow-up calculations. "
            "Keep all key facts and specifics.\n"
            f'Q: "{question[:200]}"\n'
            f'A: "{answer[:800]}"\n'
            "Summary:"
        )
        try:
            summary = self.llm.generate(
                prompt, max_tokens=60, temperature=0.0
            ).strip()
            return summary or f"{question[:100]} → {answer[:100]}"
        except Exception as e:
            logger.warning("Exchange summarization failed: %s", e)
            return f"{question[:100]} → {answer[:100]}"

    def _get_or_create_summary(self, turn_num: int, question: str,
                                answer: str) -> str:
        """Return cached summary for a turn, or generate and cache one."""
        for entry in self.conv_state.exchange_summaries:
            if entry["turn"] == turn_num:
                return entry["summary"]
        summary = self._summarize_exchange(question, answer)
        self.conv_state.exchange_summaries.append(
            {"turn": turn_num, "summary": summary}
        )
        # Cap at 20 summaries to prevent unbounded growth in long conversations
        if len(self.conv_state.exchange_summaries) > 20:
            self.conv_state.exchange_summaries = self.conv_state.exchange_summaries[-20:]
        return summary

    # -------------------------------------------------------------------
    # LLM context preparation
    # -------------------------------------------------------------------

    def _prepare_llm_context(self, command: str, *,
                              in_conversation: bool = False,
                              doc_buffer=None) -> RouteResult:
        """Prepare context for LLM fallback (streaming done by frontend)."""
        guest = self._is_guest

        history = self.conversation.format_history_for_llm(
            include_system_prompt=False,
            target_history=self._target_history,
        )
        logger.debug("_prepare_llm_context: history_len=%d", len(history) if history else 0)

        # Context window assembly (skip for guests — no personal history)
        context_messages = None
        if not guest and self.context_window and self.context_window.enabled:
            speaker_labels = None
            if self.conversation.is_multi_speaker:
                speaker_labels = {
                    uid: self.conversation._get_speaker_label(uid)
                    for uid in self.conversation.session_participants
                }
            context_messages = self.context_window.assemble_context(
                command, speaker_labels=speaker_labels
            )

        memory_context = None

        if guest:
            # Guest mode: inject restriction instructions, skip personal context
            memory_context = (
                "GUEST MODE — the current speaker is unrecognized. "
                "You may help with general knowledge, weather, and time queries. "
                "If they ask about personal features (reminders, calendar, files, "
                "email, memory, people, news, or system administration), "
                "politely explain that voice authorization is required."
            )
        else:
            # Unified awareness context assembly
            user_id = self._user_id

            if self.awareness:
                # New unified path: single assembler replaces 5 scattered blocks
                memory_context = self.awareness.assemble(command, user_id=user_id)
                logger.debug("_prepare_llm_context: awareness ctx_len=%d",
                             len(memory_context) if memory_context else 0)
            else:
                # Legacy fallback (when awareness assembler not wired)
                if self.memory_manager:
                    memory_context = self.memory_manager.get_proactive_context(
                        command, user_id=user_id)
                if self.memory_manager:
                    user_ctx = self.memory_manager.get_full_user_context(user_id=user_id)
                    if user_ctx:
                        memory_context = f"{memory_context}\n\n{user_ctx}" if memory_context else user_ctx
                if self.people_manager:
                    people_ctx = self.people_manager.get_people_context(command, user_id=user_id)
                    if people_ctx:
                        memory_context = f"{people_ctx}\n\n{memory_context}" if memory_context else people_ctx
                if self.self_awareness:
                    manifest = self.self_awareness.get_capability_manifest()
                    compact = self.self_awareness.get_compact_state()
                    awareness_block = "\n".join(filter(None, [manifest, compact]))
                    if awareness_block:
                        memory_context = f"{awareness_block}\n\n{memory_context}" if memory_context else awareness_block

            # Multi-speaker session context
            if self.conversation.is_multi_speaker:
                participants = [
                    self.conversation._get_speaker_label(uid)
                    for uid in sorted(self.conversation.session_participants)
                ]
                current = self.conversation._get_speaker_label(self._user_id)
                speaker_note = (
                    f"MULTI-SPEAKER SESSION: {', '.join(participants)} are present. "
                    f"The current speaker is {current}. "
                    f"Messages in conversation history are labeled with [Name] prefixes."
                )
                memory_context = (
                    f"{speaker_note}\n\n{memory_context}" if memory_context
                    else speaker_note
                )

            # Mobile context: tell LLM not to suggest desktop actions
            if self._is_mobile:
                current_loc = getattr(self.conversation, 'current_location', None)
                loc_line = f"The user's current location is {current_loc}.\n" if current_loc else ""
                mobile_note = (
                    f"{loc_line}"
                    "MOBILE SESSION — the user is on their phone. Do NOT suggest opening "
                    "browsers, launching apps, editing files on the server, or any "
                    "desktop-only actions. Prefer concise answers. "
                    "The user's PHONE CAMERA is available. When they ask you to look at "
                    "something, see something, or capture an image, ALWAYS use "
                    "capture_webcam with source='mobile' — NEVER use source='auto' or "
                    "source='desktop'."
                )
                memory_context = (
                    f"{mobile_note}\n\n{memory_context}" if memory_context
                    else mobile_note
                )

            # Document-aware LLM hint (request-specific, not awareness)
            if doc_buffer and doc_buffer.active:
                doc_hint = ("The user has loaded a document into the context buffer. "
                            "Refer to the <document> tags in their message. "
                            "Be analytical and specific in your response.")
                memory_context = f"{doc_hint}\n\n{memory_context}" if memory_context else doc_hint

        # Fact-extraction acknowledgment (skip for guests)
        llm_command = command
        if not guest and self.memory_manager and self.memory_manager.last_extracted:
            subjects = ", ".join(
                f.get("subject", "") for f in self.memory_manager.last_extracted
            )
            llm_command = (
                f"{command}\n\n[System: you just stored these facts from the user's "
                f"message: {subjects}. Briefly acknowledge you'll remember this.]"
            )

        # Progressive context compression with topic anchoring.
        # Replaces fixed 3-exchange window with: topic anchor + compressed
        # older exchanges + last 2 exchanges in full.  Covers ~5 exchanges
        # at equal or lower token cost vs the old 3-exchange window.
        if in_conversation:
            prior_lines = []
            multi_speaker = self.conversation.is_multi_speaker

            # Fetch up to 5 exchanges worth of history
            history = self.conversation.get_recent_history(max_turns=5, target_history=self._target_history)

            # Parse history into exchange tuples: (turn_num, question, answer, user_id)
            exchanges = []
            i = 0
            turn_num = 0
            while i < len(history) - 1:
                if (history[i].get("role") == "user"
                        and history[i + 1].get("role") == "assistant"):
                    turn_num += 1
                    exchanges.append((
                        turn_num,
                        history[i]["content"],
                        history[i + 1]["content"],
                        history[i].get("user_id"),
                    ))
                    i += 2
                else:
                    i += 1

            # Lazy topic extraction: at turn 2+, if no topic yet, extract
            # from the first exchange in history (the conversation opener).
            if (not self.conv_state.conversation_topic
                    and self.conv_state.turn_count >= 1
                    and exchanges):
                first_q, first_a = exchanges[0][1], exchanges[0][2]
                self._extract_topic(first_q, first_a)

            # Topic anchor line
            if self.conv_state.conversation_topic:
                prior_lines.append(
                    f"[topic] {self.conv_state.conversation_topic}")

            if len(exchanges) > 3:
                # Older exchanges → compressed summaries (cached)
                for ex_turn, ex_q, ex_a, ex_uid in exchanges[:-3]:
                    summary = self._get_or_create_summary(
                        ex_turn, ex_q, ex_a)
                    prior_lines.append(f"[{ex_turn}] {summary}")

                # Last 3 exchanges → full fidelity
                for ex_turn, ex_q, ex_a, ex_uid in exchanges[-3:]:
                    q = ex_q[:200]
                    a = ex_a[:800]
                    # Mark the most recent exchange so ambiguous follow-ups
                    # like "is that normal?" anchor to the right topic.
                    tag = "MOST RECENT" if (ex_turn, ex_q) == (exchanges[-1][0], exchanges[-1][1]) else str(ex_turn)
                    if multi_speaker:
                        speaker = self.conversation._get_speaker_label(ex_uid)
                        prior_lines.append(
                            f"[{tag}] {speaker}: \"{q}\" → You: \"{a}\"")
                    else:
                        prior_lines.append(
                            f"[{tag}] User: \"{q}\" → You: \"{a}\"")
            else:
                # Short conversation — all exchanges in full (same as before)
                for ex_turn, ex_q, ex_a, ex_uid in exchanges:
                    q = ex_q[:200]
                    a = ex_a[:800]
                    tag = "MOST RECENT" if (ex_turn, ex_q) == (exchanges[-1][0], exchanges[-1][1]) else str(ex_turn)
                    if multi_speaker:
                        speaker = self.conversation._get_speaker_label(ex_uid)
                        prior_lines.append(
                            f"[{tag}] {speaker}: \"{q}\" → You: \"{a}\"")
                    else:
                        prior_lines.append(
                            f"[{tag}] User: \"{q}\" → You: \"{a}\"")

            # Inject tool result data for anaphoric follow-ups.
            if self.conv_state.last_tool_result_text:
                prior_lines.append(
                    f"[tool_data] {self.conv_state.last_tool_result_text[:1200]}"
                )

            # Fall back to conv_state if session_history is empty
            if not prior_lines:
                if self.conv_state.research_exchange:
                    prev_q = self.conv_state.research_exchange['query']
                    prev_a = self.conv_state.research_exchange['answer'][:800]
                elif self.conv_state.last_response_text:
                    prev_q = self.conv_state.last_command
                    prev_a = self.conv_state.last_response_text[:800]
                else:
                    prev_q = prev_a = None
                if prev_q and prev_a:
                    prior_lines.append(f"[1] User: \"{prev_q}\" → You: \"{prev_a}\"")

            if prior_lines:
                context_block = "\n".join(prior_lines)
                llm_command = (
                    f"<prior_context>\n{context_block}\n</prior_context>\n\n"
                    f"Now the user asks: {llm_command}"
                )

        # Document buffer injection
        if doc_buffer and doc_buffer.active:
            llm_command = doc_buffer.build_augmented_message(llm_command)

        # Max tokens hint for document queries
        max_tokens = 600 if (doc_buffer and doc_buffer.active) else None

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_conversation_history(
            history_text=history,
            message_count=len(context_messages) if context_messages else 0,
        )
        _dbg.log_context_window(
            segments_count=len(context_messages) if context_messages else 0,
            query=command[:200],
        )

        return RouteResult(
            handled=False,
            llm_command=llm_command,
            llm_history=history,
            memory_context=memory_context,
            context_messages=context_messages,
            llm_max_tokens=max_tokens,
        )

    # -------------------------------------------------------------------
    # Detection helpers
    # -------------------------------------------------------------------

    def _is_dismissal(self, command: str) -> bool:
        """Detect short dismissal phrases during a conversation window."""
        text = command.strip().lower().rstrip(".!,")
        if len(text.split()) > 10:
            return False
        # Strip trailing courtesy phrases before matching
        text = re.sub(r',?\s*(?:thank you|thanks|thank you so much)$', '', text)
        if text in self._DISMISSAL_PHRASES:
            return True
        # "no, that's all" / "nah, I'm good" — check after the comma
        if text.startswith(("no,", "nah,", "nope,")):
            rest = text.split(",", 1)[1].strip()
            if not rest or rest in self._DISMISSAL_PHRASES:
                return True
        return False
