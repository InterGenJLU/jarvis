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
                 people_manager=None):
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

        # --- Pre-P4b: Self-referential hardware queries ---
        # Answer directly from SelfAwareness data — avoids LLM hallucination
        # of hardware specs (Qwen Q3_K_M overrides context with training priors)
        is_hw_query = self._is_self_hardware_query(command)
        if is_hw_query:
            result = self._handle_hw_self_query(command)
            if result:
                return result
            logger.info("Self-referential hardware query — falling through to LLM")

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
    # Only skills that have been migrated to tool schemas appear here.
    _TOOL_SKILL_MAP = {
        "time_info": "get_time",
        "system_info": "get_system_info",
        "filesystem": "find_files",
    }

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
            return None

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
        logger.info(
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

            if skill_name in self._TOOL_SKILL_MAP:
                # Migrated skill — track for tool selection
                if skill_best > best_migrated_score:
                    best_migrated_score = skill_best
                if skill_best >= self._TOOL_PRUNE_THRESHOLD:
                    tool_name = self._TOOL_SKILL_MAP[skill_name]
                    tool_schema = SKILL_TOOLS.get(tool_name)
                    if tool_schema:
                        matched_tools.append((skill_best, tool_schema))
                        logger.debug(
                            f"Tool pruning: {tool_name} matched "
                            f"(score={skill_best:.2f})"
                        )
            else:
                # Non-migrated skill — track best score for guard check
                if skill_best > best_non_migrated_score:
                    best_non_migrated_score = skill_best

        if not matched_tools:
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

        # Proactive memory surfacing
        memory_context = None
        if self.memory_manager:
            memory_context = self.memory_manager.get_proactive_context(
                command,
                user_id=getattr(self.conversation, 'current_user', None) or "primary_user",
            )

        # People context injection (known contacts mentioned in utterance)
        if self.people_manager:
            people_ctx = self.people_manager.get_people_context(
                command,
                user_id=getattr(self.conversation, 'current_user', None) or "primary_user",
            )
            if people_ctx:
                memory_context = f"{people_ctx}\n\n{memory_context}" if memory_context else people_ctx

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

        # Conversation-window context preservation: when we're in a
        # conversation window, ALWAYS inject the prior exchange so the LLM
        # has context for implicit follow-ups ("What about in London?" after
        # a date query).  The conversation window IS the relatedness signal —
        # false positive cost is zero (LLM ignores irrelevant context).
        # _is_followup_request() is kept for skip-search in web research only.
        if in_conversation:
            # Prefer research exchange if available, else use conv_state
            if self.conv_state.research_exchange:
                prev_q = self.conv_state.research_exchange['query']
                prev_a = self.conv_state.research_exchange['answer']
            elif self.conv_state.last_response_text:
                prev_q = self.conv_state.last_command
                prev_a = self.conv_state.last_response_text
            else:
                prev_q = prev_a = None

            if prev_q and prev_a:
                # Truncate long answers to avoid blowing up context
                if len(prev_a) > 800:
                    prev_a = prev_a[:800] + "..."
                llm_command = (
                    f"Context — the user just asked '{prev_q}' and you answered: "
                    f"'{prev_a}'\n\n"
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
