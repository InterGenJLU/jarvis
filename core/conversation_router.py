"""
Centralized command router — one router, three frontends.

Extracts the priority chain from pipeline.py into a shared class.
Each frontend (voice, console, web) creates a router with the same
components and calls route() to process commands.

Phase 3 of the Conversational Flow Refactor.

Design principles:
    - Router handles decision logic and command execution (skill calls,
      memory ops, etc.) but NOT delivery (TTS, WebSocket, terminal printing).
    - Frontends call route() and handle RouteResult for their delivery.
    - One router, three frontends: voice/console/web all use the same code.
"""

import re
import logging
from dataclasses import dataclass
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
                 task_planner=None):
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
            P2.7      — Dismissal detection (conversation window only)
            P2.8      — Bare acknowledgment filter (conversation window only)
            P3        — Memory operations (forget, transparency, fact, recall)
            P3.5      — Research follow-up (conversation window only)
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

        # --- Priority 3: Memory operations ---
        result = self._handle_memory_ops(command)
        if result:
            return result

        # --- Priority 3.5: Research follow-up ---
        if in_conversation:
            result = self._handle_research_followup(command)
            if result:
                return result

        # --- Priority 3.7: News article pull-up ---
        result = self._handle_news_pullup(command)
        if result:
            return result

        # --- Pre-P4: Multi-step task planning ---
        if not (doc_buffer and doc_buffer.active):
            result = self._handle_task_planning(command)
            if result:
                return result

        # --- Priority 4: Skill routing (skip when doc_buffer active) ---
        if not (doc_buffer and doc_buffer.active):
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
            logger.info("Handled by memory fact request")
            return RouteResult(
                text=persona.pick("fact_stored"), intent="fact_stored",
                source="canned", handled=True,
            )

        if mm.is_recall_query(command):
            recall_context = mm.handle_recall(command, user_id)
            if recall_context:
                history = self.conversation.format_history_for_llm(
                    include_system_prompt=False
                )
                response = self.llm.chat(
                    user_message=(
                        f"The user is asking you to recall something. Here is what you found "
                        f"in your memory:\n\n{recall_context}\n\n"
                        f"Now answer their question naturally based on this context. "
                        f"Be specific about dates and details."
                    ),
                    conversation_history=history,
                    max_tokens=200,
                )
                logger.info("Handled by memory recall")
                return RouteResult(
                    text=response, intent="memory_recall",
                    source="memory", handled=True, used_llm=True,
                )
            # Nothing found — fall through to LLM
        return None

    def _handle_research_followup(self, command: str) -> RouteResult | None:
        """P3.5: Research follow-up ('tell me more about result 2').

        Only triggers when conv_state has cached search results from a
        previous web research query in the same conversation window.
        """
        results = self.conv_state.research_results
        if not results or not self.web_researcher:
            return None

        cmd = command.strip().lower()

        # Match "result N", "number N", "option N", "#N"
        num_match = re.search(r'(?:result|number|option|#)\s*(\d+)', cmd)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(results):
                url = results[idx]["url"]
                title = results[idx]["title"]
                logger.info(f"Research follow-up: fetching result {idx+1}: {url}")

                content = self.web_researcher.fetch_page(url, max_chars=4000)
                if not content:
                    return RouteResult(
                        text=persona.research_page_fail(),
                        intent="research_followup", source="memory",
                        handled=True, open_window=EXTENDED_WINDOW,
                    )

                history = self.conversation.format_history_for_llm(
                    include_system_prompt=False
                )
                response = self.llm.chat(
                    user_message=(
                        f"The user asked about a search result. Here is the full article "
                        f"content from \"{title}\":\n\n{content}\n\n"
                        f"Summarize the key information from this article, focusing on "
                        f"what the user was originally asking about. Be thorough but concise."
                        f"\n\nUser's request: {command}"
                    ),
                    conversation_history=history,
                    max_tokens=400,
                )
                return RouteResult(
                    text=response, intent="research_followup",
                    source="memory", handled=True, used_llm=True,
                    open_window=15.0,
                )

        # Generic follow-up ("tell me more", "elaborate")
        more_phrases = ["tell me more", "more about that", "what does it say",
                        "elaborate", "go into detail", "expand on that"]
        if any(p in cmd for p in more_phrases) and len(results) > 0:
            url = results[0]["url"]
            title = results[0]["title"]
            logger.info(f"Research follow-up (generic): fetching {url}")

            content = self.web_researcher.fetch_page(url, max_chars=4000)
            if not content:
                return RouteResult(
                    text=persona.research_page_fail(),
                    intent="research_followup", source="memory",
                    handled=True, open_window=EXTENDED_WINDOW,
                )

            history = self.conversation.format_history_for_llm(
                include_system_prompt=False
            )
            response = self.llm.chat(
                user_message=(
                    f"The user wants more detail about this article: \"{title}\"\n\n"
                    f"Full content:\n{content}\n\n"
                    f"Provide a thorough but spoken-word-friendly summary."
                    f"\n\nUser's request: {command}"
                ),
                conversation_history=history,
                max_tokens=400,
            )
            return RouteResult(
                text=response, intent="research_followup",
                source="memory", handled=True, used_llm=True,
                open_window=15.0,
            )

        return None

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
            # Pause/resume handled by event queue in voice mode;
            # router path covers console/web where event_queue=None
            if words & _INTERRUPT_PAUSE:
                logger.info("Pause request via router")
                return RouteResult(
                    text=persona.task_paused(),
                    intent="task_plan_pause",
                    source="planner",
                    handled=True,
                )
            if words & _INTERRUPT_RESUME:
                logger.info("Resume request via router")
                return RouteResult(
                    text=persona.task_resumed(),
                    intent="task_plan_resume",
                    source="planner",
                    handled=True,
                )

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

        # Proactive memory surfacing
        memory_context = None
        if self.memory_manager:
            memory_context = self.memory_manager.get_proactive_context(
                command,
                user_id=getattr(self.conversation, 'current_user', None) or "primary_user",
            )

        # Self-awareness: inject capability manifest + compact state
        if self.self_awareness:
            manifest = self.self_awareness.get_capability_manifest()
            compact = self.self_awareness.get_compact_state()
            awareness_block = "\n".join(filter(None, [manifest, compact]))
            if awareness_block:
                memory_context = f"{awareness_block}\n\n{memory_context}" if memory_context else awareness_block

        # Document-aware LLM hint
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

        # Research exchange augmentation (follow-ups like "try again")
        if in_conversation and self.conv_state.research_exchange:
            prev = self.conv_state.research_exchange
            llm_command = (
                f"Context: The user just asked '{prev['query']}' and I answered: "
                f"'{prev['answer']}'\n\n"
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
