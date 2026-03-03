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
from dataclasses import dataclass, field
from typing import Optional

from core import persona
from core.conversation_state import ConversationState

logger = logging.getLogger("jarvis.router")


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

    def route(self, command: str, *,
              in_conversation: bool = False,
              doc_buffer=None) -> RouteResult:
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

        Returns:
            RouteResult with response text, metadata, and side-effect signals.
        """
        # --- Priority 1: Rundown acceptance ---
        result = self._handle_rundown(command)
        if result:
            return result

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

        # --- P4-LLM: Tool-calling path (LLM-centric migration Phase 1) ---
        # If the command appears relevant to a tool-enabled skill, route
        # through the LLM with dynamically pruned tools.  The LLM decides
        # whether to call a tool, ask for clarification, or answer directly.
        if not (doc_buffer and doc_buffer.active):
            result = self._handle_tool_calling(command, in_conversation)
            if result:
                return result

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
        return self._prepare_llm_context(
            command,
            in_conversation=in_conversation,
            doc_buffer=doc_buffer,
        )

    # -------------------------------------------------------------------
    # Priority handlers
    # -------------------------------------------------------------------

    def _route_greeting(self) -> RouteResult:
        """Handle wake-word-only or empty commands."""
        if self.reminder_manager and self.reminder_manager.has_rundown_mention():
            self.reminder_manager.clear_rundown_mention()
            text = persona.rundown_mention()
        else:
            text = persona.pick("greeting")
        return RouteResult(
            text=text, intent="greeting", source="canned",
            handled=True, open_window=EXTENDED_WINDOW,
        )

    def _handle_rundown(self, command: str) -> RouteResult | None:
        """P1: Rundown acceptance or deferral."""
        rm = self.reminder_manager
        if not rm or not rm.is_rundown_pending():
            return None

        text_lower = command.strip().lower()
        words = set(re.findall(r'\b\w+\b', text_lower))
        negative = bool(
            words & {"no", "later", "hold", "skip"}
            or "not now" in text_lower
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
        if not rm or not rm.is_awaiting_ack():
            return None
        logger.info("Treating response as reminder acknowledgment")
        rm.acknowledge_last()
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
        affirm = ("yes", "yeah", "yep", "go ahead", "do it",
                   "proceed", "confirm", "sure", "remove", "delete")
        deny = ("no", "nope", "nah", "cancel", "nevermind",
                "never mind", "keep", "don't")

        if any(w in cmd_lower for w in affirm):
            text = mm.confirm_forget()
            logger.info("Handled by memory forget confirmation")
            return RouteResult(
                text=text, intent="forget_confirm",
                source="memory", handled=True,
            )
        if any(w in cmd_lower for w in deny):
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
        if not session or not session.is_active():
            return None

        cmd = command.strip().lower().rstrip(".,!?")

        # Continue / affirm
        if cmd in self._READBACK_CONTINUE:
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
                return RouteResult(
                    handled=True, intent="readback_recall",
                    text=answer, open_window=120.0,
                )

        # Section recall: "go back to ingredients", "repeat the ingredients"
        for section_name in ("ingredients", "equipment", "instructions", "notes"):
            if section_name in cmd and ("back" in cmd or "repeat" in cmd or "again" in cmd):
                return RouteResult(
                    handled=True, intent="readback_section",
                    text=f"__READBACK_SECTION__{section_name}",
                    open_window=120.0,
                )

        # "repeat that" / "say that again"
        if any(p in cmd for p in ("repeat that", "say that again", "one more time", "repeat the last")):
            chunk = session.get_last_delivered()
            if chunk:
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

        user_id = getattr(self.conversation, 'current_user', None) or "primary_user"

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
                if len(content) > 3:
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
                    include_system_prompt=False
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

    # Recency references → return the latest synthesis
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

        content = self.web_researcher.fetch_page(url, max_chars=4000)
        if not content:
            return RouteResult(
                text=persona.research_page_fail(),
                intent="artifact_reference", source="cache",
                handled=True, open_window=EXTENDED_WINDOW,
            )

        history = self.conversation.format_history_for_llm(
            include_system_prompt=False,
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
        """Resolve 'repeat that', 'what did you say', etc."""
        if not self._RECENCY_PATTERNS.search(cmd):
            return None

        from core.interaction_cache import get_interaction_cache

        wid = self.conv_state.window_id
        cache = get_interaction_cache()

        # Try cache for latest synthesis
        text = None
        if cache and wid:
            art = cache.get_latest(wid, artifact_type="synthesis")
            if art:
                text = art.content
                logger.info("Recency ref: returning cached synthesis %s",
                            art.artifact_id)

        # Fallback: conv_state.last_response_text
        if not text:
            text = self.conv_state.last_response_text

        if not text:
            return None

        return RouteResult(
            text=text, intent="artifact_reference",
            source="cache", handled=True,
            open_window=EXTENDED_WINDOW,
        )

    def _resolve_type_reference(self, command: str,
                                cmd: str) -> RouteResult | None:
        """Resolve 'those results', 'that recipe', 'the weather', etc."""
        from core.interaction_cache import get_interaction_cache

        wid = self.conv_state.window_id
        cache = get_interaction_cache()
        if not cache or not wid:
            return None

        matched_art = None
        for pattern, art_type, keyword in self._TYPE_REFERENCE_PATTERNS:
            if not pattern.search(cmd):
                continue

            if art_type:
                matched_art = cache.get_latest(wid, artifact_type=art_type)
            elif keyword:
                matched_art = cache.find_by_keyword(wid, keyword)

            if matched_art:
                break

        if not matched_art:
            return None

        logger.info("Type ref: resolved '%s' to artifact %s [%s]",
                     cmd[:40], matched_art.artifact_id,
                     matched_art.artifact_type)

        # For search_result_set, re-present with LLM context
        # For synthesis, just return the content
        if matched_art.artifact_type == "synthesis":
            return RouteResult(
                text=matched_art.content, intent="artifact_reference",
                source="cache", handled=True,
                open_window=EXTENDED_WINDOW,
            )

        # For other types, ask LLM to answer in context of the artifact
        history = self.conversation.format_history_for_llm(
            include_system_prompt=False,
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

        if not tp.needs_planning(command):
            return None

        logger.info(f"Compound request detected — generating plan for: {command[:80]}")
        plan = tp.generate_plan(command)
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
                text=f"This plan includes running a command on your system: {desc}. Shall I proceed?",
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

    # Threshold for tool pruning.  Tuned via sweep across 56 queries at
    # thresholds 0.30-0.60 (scripts/test_intent_overlap.py).  0.40 is the
    # only value with zero cliff-risk AND zero false negatives.
    _TOOL_PRUNE_THRESHOLD = 0.40

    # Hard cap on domain tools per request (web_search is added on top).
    # Prevents exceeding the 5-6 tool cliff even if threshold is too loose.
    _MAX_DOMAIN_TOOLS = 4

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
        tools = self._select_tools_for_command(command)
        if not tools:
            logger.debug(f"P4-LLM: no tools selected for: {command[:80]}")
            return None
        logger.debug(f"P4-LLM: selected {len(tools)} tools, routing to LLM")

        # Prepare the same LLM context as _prepare_llm_context()
        result = self._prepare_llm_context(
            command,
            in_conversation=in_conversation,
        )
        # Augment with tool-calling fields
        result.use_tools = tools
        result.tool_temperature = 0.0    # Deterministic — sweep showed 0.0 is fastest, same accuracy
        result.tool_presence_penalty = 0.0  # Sweep: pp=1.5 doubled latency with zero accuracy gain
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
        tool schema dicts (always includes web_search) or None if no
        skill tools are relevant.

        Critical guard: also scores non-migrated skills.  If a non-migrated
        skill has a higher semantic score than the best migrated skill, we
        return None to let P4 (legacy skill routing) handle it.  This
        prevents over-capture of queries meant for non-tool skills.

        Hard cap: even if many skills pass the threshold, only the top
        _MAX_DOMAIN_TOOLS (4) are kept, preventing the 5-6 tool cliff.
        """
        sm = self.skill_manager
        if not hasattr(sm, '_embedding_model') or not sm._embedding_model:
            return None

        # Lazy import to avoid circular dependency at module load
        from core.llm_router import WEB_SEARCH_TOOL, SKILL_TOOLS

        try:
            from sentence_transformers import util as st_util
        except ImportError:
            return None

        user_embedding = sm._embedding_model.encode(
            command, convert_to_tensor=True, show_progress_bar=False
        )

        # Score ALL skills (migrated and non-migrated) to find the best match
        best_migrated_score = 0.0
        best_non_migrated_score = 0.0
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

            if skill_name in self._TOOL_SKILL_MAP:
                # Migrated skill — track for tool selection
                if skill_best > best_migrated_score:
                    best_migrated_score = skill_best
                if skill_best >= self._TOOL_PRUNE_THRESHOLD:
                    tool_name = self._TOOL_SKILL_MAP[skill_name]
                    tool_schema = SKILL_TOOLS.get(tool_name)
                    if tool_schema:
                        matched_tools.append((skill_best, tool_schema))
            else:
                # Non-migrated skill — track best score for guard check.
                if skill_name == 'web_navigation':
                    web_nav_score = skill_best
                elif skill_best > best_non_migrated_score:
                    best_non_migrated_score = skill_best

        if not matched_tools:
            # No domain tools matched, but if web_navigation scored well,
            # route through LLM with web_search instead of letting P4 open
            # a browser (which makes no sense in web UI, and web_search gives
            # better in-chat results for informational queries).
            if web_nav_score >= self._TOOL_PRUNE_THRESHOLD:
                logger.info(
                    f"Tool pruning: no domain tools, but web_navigation scored "
                    f"{web_nav_score:.2f} — routing to LLM with web_search"
                )
                return [WEB_SEARCH_TOOL]
            return None

        # Guard: if a non-migrated skill scores higher, defer to P4
        if best_non_migrated_score > best_migrated_score:
            logger.debug(
                f"Tool pruning: non-migrated skill scored higher "
                f"({best_non_migrated_score:.2f} > {best_migrated_score:.2f}), "
                f"deferring to P4"
            )
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

        # Always include web_search as a core tool
        return [WEB_SEARCH_TOOL] + [t[1] for t in matched_tools]

    def _handle_skill_routing(self, command: str) -> RouteResult | None:
        """P4: Skill routing (semantic + keyword matching)."""
        response = self.skill_manager.execute_intent(command)
        match_info = self.skill_manager._last_match_info
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
    # LLM context preparation
    # -------------------------------------------------------------------

    def _prepare_llm_context(self, command: str, *,
                              in_conversation: bool = False,
                              doc_buffer=None) -> RouteResult:
        """Prepare context for LLM fallback (streaming done by frontend)."""
        history = self.conversation.format_history_for_llm(
            include_system_prompt=False
        )

        # Context window assembly
        context_messages = None
        if self.context_window and self.context_window.enabled:
            context_messages = self.context_window.assemble_context(command)

        # Unified awareness context assembly
        user_id = getattr(self.conversation, 'current_user', None) or "primary_user"
        memory_context = None

        if self.awareness:
            # New unified path: single assembler replaces 5 scattered blocks
            memory_context = self.awareness.assemble(command, user_id=user_id)
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

        # Document-aware LLM hint (request-specific, not awareness)
        if doc_buffer and doc_buffer.active:
            doc_hint = ("The user has loaded a document into the context buffer. "
                        "Refer to the <document> tags in their message. "
                        "Be analytical and specific in your response.")
            memory_context = f"{doc_hint}\n\n{memory_context}" if memory_context else doc_hint

        # Fact-extraction acknowledgment
        llm_command = command
        if self.memory_manager and self.memory_manager.last_extracted:
            subjects = ", ".join(
                f.get("subject", "") for f in self.memory_manager.last_extracted
            )
            llm_command = (
                f"{command}\n\n[System: you just stored these facts from the user's "
                f"message: {subjects}. Briefly acknowledge you'll remember this.]"
            )

        # Conversation-window context preservation: when we're in a
        # conversation window, ALWAYS inject the prior exchange so the LLM
        # has context for implicit follow-ups ("What about in London?" after
        # a date query).  The conversation window IS the relatedness signal —
        # false positive cost is zero (LLM ignores irrelevant context).
        # _is_followup_request() is kept for skip-search in web research only.
        if in_conversation:
            # Build compact prior context from last 3 exchanges
            prior_lines = []
            history = self.conversation.get_recent_history(max_turns=3)
            # history is [{"role":"user","content":"..."}, {"role":"assistant","content":"..."}, ...]
            exchange_num = 0
            i = 0
            while i < len(history) - 1:
                if history[i].get("role") == "user" and history[i+1].get("role") == "assistant":
                    exchange_num += 1
                    q = history[i]["content"][:200]
                    a = history[i+1]["content"][:400]
                    prior_lines.append(f"[{exchange_num}] User: \"{q}\" → You: \"{a}\"")
                    i += 2
                else:
                    i += 1

            # Fall back to conv_state if session_history is empty
            if not prior_lines:
                if self.conv_state.research_exchange:
                    prev_q = self.conv_state.research_exchange['query']
                    prev_a = self.conv_state.research_exchange['answer'][:400]
                elif self.conv_state.last_response_text:
                    prev_q = self.conv_state.last_command
                    prev_a = self.conv_state.last_response_text[:400]
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
