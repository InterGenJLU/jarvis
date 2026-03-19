"""
Task Planner — decompose compound requests into multi-step skill chains.

Phase 2-3 of the Autonomous Task Planner plan.

Design:
    - Pre-P4 whitelist gate detects compound requests (~microseconds, no LLM call)
    - LLM generates a plan as structured JSON using the capability manifest
    - Planner owns the execution loop; frontends provide progress_callback only
    - Steps execute sequentially via skill_manager.execute_intent() (direct P4)
    - Prior step results are injected as context for subsequent steps
    - Phase 3: Destructive step confirmation, failure-breaks, voice interrupts
"""

import json
import logging
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from core.logger import get_logger
logger = get_logger("jarvis.task_planner")


# ---------------------------------------------------------------------------
# Compound detection whitelist
# ---------------------------------------------------------------------------
# English conjunctive structures that signal multi-step intent.
# Word-boundary matching avoids false positives from substrings.

COMPOUND_SIGNALS = [
    "and then",
    "and also",
    "and remind",
    "and create",
    "and show",
    "and send",
    "and save",
    "and open",
    "and set",
    "and tell",
    "then create",
    "then send",
    "then show",
    "then open",
    "then set",
    "after that",
    ", then ",
    "research and",
    "check and",
    "find and",
    "search and",
    "look up and",
]

# Pre-compile patterns for performance
_COMPOUND_PATTERNS = [
    re.compile(r'\b' + re.escape(signal) + r'\b', re.IGNORECASE)
    if not signal.startswith(",")
    else re.compile(re.escape(signal), re.IGNORECASE)
    for signal in COMPOUND_SIGNALS
]


# Skills that require user confirmation before plan execution (arbitrary shell)
CONFIRMATION_REQUIRED_SKILLS = {"developer_tools"}

# Stop/cancel/skip/pause keywords for voice interrupt detection
_INTERRUPT_CANCEL = {"stop", "cancel", "abort", "halt", "nevermind", "never mind"}
_INTERRUPT_SKIP = {"skip", "next"}
_INTERRUPT_PAUSE = {"wait", "hold", "pause"}
_INTERRUPT_RESUME = {"continue", "resume", "proceed"}

# Pause timeout: auto-cancel after 2 minutes of inactivity
_PAUSE_TIMEOUT_SECONDS = 120

# LLM evaluation timeout: max wait for step evaluation call (seconds).
# The HTTP request has its own 30s timeout, but evaluation should be fast
# (100 tokens ≈ 1-2s). This catches edge cases where the server accepts
# the request but generates slowly.
_EVALUATE_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class PlanStep:
    """One step in a multi-step plan."""
    step_id: int
    description: str        # Human-readable: "Searching the web for AMD GPU drivers"
    skill_name: str         # "web_navigation", "weather", etc.
    input_text: str         # Text to pass to skill handler
    status: StepStatus = StepStatus.PENDING
    result: str = ""        # Step output (passed to next step as context)


@dataclass
class TaskPlan:
    """A multi-step execution plan."""
    original_request: str
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.PENDING
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


# ---------------------------------------------------------------------------
# Plan generation prompt
# ---------------------------------------------------------------------------

_PLAN_PROMPT = """You have these capabilities:
{manifest}

The user asked: "{command}"

RULES — follow EXACTLY:
1. If this needs multiple skills, respond with a JSON array of steps.
2. Each step MUST use one skill from the list above. Use the exact skill name.
3. Maximum 4 steps. Simpler is better.
4. If this is really a simple single-skill request, respond with exactly: SINGLE
5. Steps execute in order. Later steps receive earlier results as context.
6. Include a human-readable description for each step (spoken to the user).
7. For general knowledge synthesis that no specific skill handles, use skill "llm_synthesis".
8. For creating documents (DOCX, reports, lists, comparisons), use skill "create_document". Do NOT use file_editor for new document creation.
9. The "input" field MUST be natural language (a phrase the user would say). NEVER use function names, tool names, or code-like strings. Good: "create a packing list document". Bad: "write_file", "create_document".

Respond with ONLY a JSON array (no markdown, no explanation) or the word SINGLE.

JSON format:
[
  {{"step": 1, "skill": "skill_name", "input": "create a document with the packing list", "description": "Creating the document"}},
  {{"step": 2, "skill": "skill_name", "input": "search the web for flight prices", "description": "Looking up flights"}}
]"""


# ---------------------------------------------------------------------------
# Step evaluation prompt (Phase 4D)
# ---------------------------------------------------------------------------

_EVALUATE_PROMPT = """A task plan just executed step {step_id}/{total_steps}.

Step: {description}
Result (first 500 chars): {result_excerpt}

Original user request: {original_request}

RULES:
1. If the step produced a useful result, respond: CONTINUE
2. If the step produced a partial or unexpected result that should modify the next step, respond: ADJUST <brief instruction for next step>
3. If the step completely failed or the result makes continuing pointless, respond: STOP <brief reason>
4. Respond with ONLY one of: CONTINUE, ADJUST <text>, or STOP <text>"""


# ---------------------------------------------------------------------------
# TaskPlanner
# ---------------------------------------------------------------------------

class TaskPlanner:
    """Decomposes compound requests into sequential skill chains."""

    def __init__(self, *,
                 llm,
                 skill_manager,
                 self_awareness,
                 conversation=None,
                 config=None,
                 event_queue=None,
                 context_window=None,
                 web_researcher=None):
        self._llm = llm
        self._skill_manager = skill_manager
        self._self_awareness = self_awareness
        self._conversation = conversation
        self._config = config
        self._event_queue = event_queue  # For voice interrupt detection
        self._context_window = context_window
        self._web_researcher = web_researcher

        self.active_plan: Optional[TaskPlan] = None
        self._cancel_requested = False
        self._skip_requested = False
        self._paused = False
        self._pending_plan_confirmation: Optional[TaskPlan] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """True if a plan is currently executing."""
        return (self.active_plan is not None
                and self.active_plan.status == PlanStatus.RUNNING)

    @property
    def has_pending_confirmation(self) -> bool:
        """True if a plan is waiting for user yes/no confirmation."""
        return self._pending_plan_confirmation is not None

    @property
    def is_paused(self) -> bool:
        """True if the plan is currently paused."""
        return self._paused

    @property
    def can_pause(self) -> bool:
        """True if pause/resume is supported (requires event queue for async input)."""
        return self._event_queue is not None

    # ------------------------------------------------------------------
    # Destructive step detection + confirmation
    # ------------------------------------------------------------------

    def has_destructive_steps(self, plan: TaskPlan) -> bool:
        """Check if any step targets a skill requiring confirmation."""
        return any(
            step.skill_name in CONFIRMATION_REQUIRED_SKILLS
            for step in plan.steps
        )

    def set_pending_confirmation(self, plan: TaskPlan):
        """Store a plan awaiting user yes/no."""
        self._pending_plan_confirmation = plan
        logger.info(f"Plan pending confirmation: {len(plan.steps)} steps")

    def resolve_confirmation(self, confirmed: bool) -> Optional[TaskPlan]:
        """Resolve pending confirmation. Returns plan if confirmed, None if denied."""
        plan = self._pending_plan_confirmation
        self._pending_plan_confirmation = None

        if not plan:
            return None

        if confirmed:
            logger.info("Plan confirmed by user")
            return plan
        else:
            plan.status = PlanStatus.CANCELLED
            for step in plan.steps:
                if step.status == StepStatus.PENDING:
                    step.status = StepStatus.SKIPPED
            logger.info("Plan denied by user — cancelled")
            return None

    # ------------------------------------------------------------------
    # Compound detection (microseconds, no LLM call)
    # ------------------------------------------------------------------

    def needs_planning(self, command: str) -> str | None:
        """Check if command contains conjunctive phrases suggesting multi-step.

        Uses word-boundary whitelist — fast, no false positives from substrings.
        Returns the matched signal phrase, or None if no compound detected.
        """
        for pattern, signal in zip(_COMPOUND_PATTERNS, COMPOUND_SIGNALS):
            if pattern.search(command):
                logger.info(f"Compound signal detected: {pattern.pattern}")
                return signal
        return None

    # ------------------------------------------------------------------
    # Plan generation (single LLM call)
    # ------------------------------------------------------------------

    def generate_plan(self, command: str, *,
                      signal: str | None = None) -> Optional[TaskPlan]:
        """Ask the LLM to decompose a compound command into steps.

        Returns TaskPlan if multi-step, None if LLM decides single-step.
        signal: the conjunctive phrase that triggered compound detection
                (e.g. "and then"). Passed as a hint to bias the LLM toward
                multi-step decomposition.
        """
        manifest = self._self_awareness.get_capability_manifest()
        if not manifest:
            logger.warning("No capability manifest available — skipping plan generation")
            return None

        prompt = _PLAN_PROMPT.format(manifest=manifest, command=command)

        # Bias toward multi-step when a strong conjunctive signal was detected
        if signal:
            prompt += (
                f'\n\nNote: The user explicitly said "{signal}", '
                "indicating they expect separate sequential steps. "
                "Do NOT respond SINGLE for this request."
            )

        # Error-aware planning: warn about unreliable skills
        if self._self_awareness:
            unreliable = self._self_awareness.get_unreliable_skills()
            if unreliable:
                warning = ("WARNING: These skills have been unreliable recently: "
                           + ", ".join(unreliable)
                           + ". Consider alternatives or warn the user.")
                prompt += f"\n\n{warning}"

        # Context-budget-aware planning
        if self._context_window and self._context_window.enabled:
            usage = self._context_window.get_usage_percentage()
            if usage > 80.0:
                prompt += (f"\n\nNOTE: Context memory is {usage:.0f}% full. "
                           "Prioritize concise responses in each step.")

        try:
            response = self._llm.chat(
                user_message=prompt,
                max_tokens=400,
            )
        except Exception as e:
            logger.error(f"Plan generation LLM call failed: {e}")
            return None

        if not response:
            return None

        response = response.strip()

        # LLM says single-step — fall through to normal routing
        if response.upper().startswith("SINGLE"):
            logger.info("LLM determined single-step — no plan needed")
            return None

        # Parse JSON (strip markdown code fences if present)
        json_str = response
        if json_str.startswith("```"):
            json_str = re.sub(r'^```(?:json)?\s*', '', json_str)
            json_str = re.sub(r'\s*```$', '', json_str)

        try:
            steps_raw = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Plan JSON parse failed: {e} — response: {response[:200]}")
            return None

        if not isinstance(steps_raw, list) or len(steps_raw) == 0:
            logger.warning(f"Plan response not a list or empty: {type(steps_raw)}")
            return None

        # Validate and build plan
        valid_skills = set(self._skill_manager.skills.keys())
        # Add pseudo-skills that we handle internally
        valid_skills.add("llm_synthesis")
        valid_skills.add("web_research")
        valid_skills.add("create_document")

        steps = []
        for i, raw in enumerate(steps_raw[:4]):  # Max 4 steps
            skill = raw.get("skill", "").strip()
            if skill not in valid_skills:
                logger.warning(f"Plan step {i+1} references unknown skill '{skill}' — skipping")
                continue

            input_text = raw.get("input", command)
            # Normalize terse/code-like input to natural language
            # If input has no spaces or looks like a function name, use description
            if input_text and (' ' not in input_text or '_' in input_text.split()[0]):
                description = raw.get("description", "")
                if description and ' ' in description:
                    logger.debug("Plan step %d: normalizing terse input '%s' → '%s'",
                                 i + 1, input_text, description)
                    input_text = description

            steps.append(PlanStep(
                step_id=i + 1,
                description=raw.get("description", f"Step {i+1}"),
                skill_name=skill,
                input_text=input_text,
            ))

        if len(steps) < 2:
            logger.info(f"Plan has {len(steps)} valid steps — treating as single-step")
            return None

        plan = TaskPlan(original_request=command, steps=steps)
        logger.info(f"Generated plan: {len(steps)} steps for '{command[:60]}'")

        # Debug logging — capture full plan details
        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_plan_generated(
            command=command,
            plan_json=json_str,
            step_count=len(steps),
            step_details=[
                {"step_id": s.step_id, "skill": s.skill_name,
                 "input": s.input_text, "description": s.description}
                for s in steps
            ],
            signal=signal,
            llm_response=response,
        )

        return plan

    # ------------------------------------------------------------------
    # Voice interrupt detection
    # ------------------------------------------------------------------

    def _check_for_interrupt(self) -> Optional[str]:
        """Non-blocking drain of event_queue between steps.

        Looks for TRANSCRIPTION_READY/COMMAND_DETECTED events matching
        stop/cancel/skip/pause keywords. Re-queues non-interrupt events.

        Returns: "cancel", "skip", "pause", or None.
        """
        if not self._event_queue:
            return None

        requeue = []
        result = None

        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break

            # Extract text from event
            text = None
            if hasattr(event, 'type'):
                from core.events import EventType
                if event.type in (EventType.TRANSCRIPTION_READY, EventType.COMMAND_DETECTED):
                    data = event.data
                    if isinstance(data, dict):
                        text = data.get("text", "").lower().strip()
                    elif isinstance(data, str):
                        text = data.lower().strip()

            if text:
                words = set(re.findall(r'\b\w+\b', text))
                if words & _INTERRUPT_CANCEL:
                    result = "cancel"
                    logger.info(f"Voice interrupt detected: cancel ('{text}')")
                    break
                elif words & _INTERRUPT_SKIP:
                    result = "skip"
                    logger.info(f"Voice interrupt detected: skip ('{text}')")
                    break
                elif words & _INTERRUPT_PAUSE:
                    result = "pause"
                    logger.info(f"Voice interrupt detected: pause ('{text}')")
                    break
                else:
                    # Not an interrupt — re-queue for later processing
                    requeue.append(event)
            else:
                requeue.append(event)

        # Re-queue non-interrupt events
        for event in requeue:
            self._event_queue.put(event)

        return result

    def _wait_for_resume(self) -> str:
        """Block until resume keyword, cancel keyword, or timeout.

        Called when a "pause" interrupt is detected during plan execution.
        Polls the event_queue with 1s intervals. Accumulates non-matching
        events and re-queues them on exit.

        Returns: "resume", "cancel", or "timeout"
        """
        if not self._event_queue:
            return "resume"  # Console mode: can't block on events

        self._paused = True
        deadline = time.time() + _PAUSE_TIMEOUT_SECONDS
        accumulated = []

        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                timeout = min(remaining, 1.0)

                try:
                    event = self._event_queue.get(timeout=timeout)
                except queue.Empty:
                    # Also check for programmatic cancel during pause
                    if self._cancel_requested:
                        return "cancel"
                    continue

                # Extract text from event
                text = None
                if hasattr(event, 'type'):
                    from core.events import EventType
                    if event.type in (EventType.TRANSCRIPTION_READY,
                                      EventType.COMMAND_DETECTED):
                        data = event.data
                        if isinstance(data, dict):
                            text = data.get("text", "").lower().strip()
                        elif isinstance(data, str):
                            text = data.lower().strip()

                if text:
                    words = set(re.findall(r'\b\w+\b', text))
                    if words & _INTERRUPT_CANCEL:
                        return "cancel"
                    if words & _INTERRUPT_RESUME or "go ahead" in text:
                        return "resume"
                    # Non-matching text — accumulate for re-queue
                    accumulated.append(event)
                else:
                    accumulated.append(event)

            # Timeout reached
            return "timeout"
        finally:
            self._paused = False
            # Re-queue accumulated events
            for event in accumulated:
                self._event_queue.put(event)

    # ------------------------------------------------------------------
    # Step evaluation (Phase 4D)
    # ------------------------------------------------------------------

    def _evaluate_step_result(self, step: PlanStep, plan: TaskPlan) -> tuple[str, str]:
        """LLM-based evaluation of step result quality.

        Returns:
            (decision, reason) where decision is "continue"|"adjust"|"stop"
            and reason is the LLM's explanation (empty for "continue").

        Fast-paths skip the LLM call for clearly empty/error results
        and for the last step (no continuation decision needed).
        Falls back to "continue" on LLM failure.
        """
        result_text = (step.result or "").strip()

        # Fast path: clearly empty or error result — no LLM call needed
        if not result_text or result_text.lower().startswith("error:"):
            return ("stop", "empty or error result")

        # Fast path: last step — no continuation decision needed
        if step.step_id >= len(plan.steps):
            return ("continue", "")

        if not self._llm:
            return ("continue", "")

        prompt = _EVALUATE_PROMPT.format(
            step_id=step.step_id,
            total_steps=len(plan.steps),
            description=step.description,
            result_excerpt=result_text[:500],
            original_request=plan.original_request,
        )

        try:
            # Use a tight timeout — evaluation is a simple classification task
            # (100 tokens ≈ 1-2s). Prevents stalling the plan if LLM is slow.
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self._llm.chat,
                    user_message=prompt,
                    max_tokens=100,
                )
                response = future.result(timeout=_EVALUATE_TIMEOUT_SECONDS)

            if not response:
                return ("continue", "")

            response = response.strip()
            upper = response.upper()

            if upper.startswith("STOP"):
                reason = response[4:].strip().lstrip(":").strip()
                return ("stop", reason or "LLM decided to stop")
            elif upper.startswith("ADJUST"):
                instruction = response[6:].strip().lstrip(":").strip()
                return ("adjust", instruction)
            else:
                # CONTINUE or unrecognized → continue
                return ("continue", "")
        except FuturesTimeoutError:
            logger.warning(f"Step evaluation timed out after {_EVALUATE_TIMEOUT_SECONDS}s — defaulting to continue")
            return ("continue", "")
        except Exception as e:
            logger.warning(f"Step evaluation LLM call failed: {e} — defaulting to continue")
            return ("continue", "")

    # ------------------------------------------------------------------
    # Plan execution
    # ------------------------------------------------------------------

    def execute_plan(self, plan: TaskPlan, *,
                     progress_callback: Optional[Callable[[str], None]] = None) -> str:
        """Execute a plan step-by-step via direct skill handler calls.

        Phase 3+4 behavior:
            - On step failure (empty result or exception): break loop,
              mark remaining steps SKIPPED (all sequential steps are dependent).
            - Between steps: check for voice interrupts (cancel/skip/pause).
            - On cancel: mark remaining SKIPPED, set plan CANCELLED.
            - On skip: mark current step SKIPPED, continue to next.
            - After each successful step: LLM evaluates continue/adjust/stop.
            - On LLM "stop": break loop. On "adjust": modify next step input.

        Args:
            plan: The plan to execute.
            progress_callback: Called with status text between steps (for TTS/UI).

        Returns:
            Final synthesized result combining all step outputs.
        """
        self.active_plan = plan
        self._cancel_requested = False
        self._skip_requested = False
        plan.status = PlanStatus.RUNNING

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _plan_start = time.time()

        results = []
        prior_context = ""

        for step in plan.steps:
            # Check for programmatic cancellation (from cancel() method)
            if self._cancel_requested:
                self._mark_remaining_skipped(plan, step.step_id)
                plan.status = PlanStatus.CANCELLED
                logger.info(f"Plan cancelled at step {step.step_id}")
                break

            # Check for voice interrupt between steps
            interrupt = self._check_for_interrupt()
            if interrupt == "cancel":
                self._mark_remaining_skipped(plan, step.step_id)
                plan.status = PlanStatus.CANCELLED
                logger.info(f"Plan cancelled by voice at step {step.step_id}")
                break
            elif interrupt == "skip":
                step.status = StepStatus.SKIPPED
                logger.info(f"Step {step.step_id} skipped by voice")
                continue
            elif interrupt == "pause":
                logger.info(f"Plan paused before step {step.step_id}")
                if progress_callback:
                    progress_callback("Paused. Say 'continue' to resume or 'cancel' to stop.")
                resume_result = self._wait_for_resume()
                if resume_result in ("cancel", "timeout"):
                    self._mark_remaining_skipped(plan, step.step_id)
                    plan.status = PlanStatus.CANCELLED
                    if resume_result == "timeout":
                        logger.info("Plan auto-cancelled after pause timeout")
                    else:
                        logger.info("Plan cancelled during pause")
                    break
                # resume_result == "resume" → continue with this step
                logger.info("Plan resumed")

            step.status = StepStatus.RUNNING
            logger.info(f"Executing step {step.step_id}/{len(plan.steps)}: {step.description}")

            # Report progress
            if progress_callback and step.step_id > 1:
                progress_callback(step.description)

            _step_start = time.time()
            try:
                result = self._execute_step(step, prior_context)
                step.result = result or ""
                _step_ms = (time.time() - _step_start) * 1000

                if result:
                    step.status = StepStatus.COMPLETED
                    results.append(f"[{step.description}]: {result}")
                    prior_context = result

                    _dbg.log_plan_step_result(
                        step_id=step.step_id, skill_name=step.skill_name,
                        status="completed", result_text=result,
                        elapsed_ms=_step_ms,
                        routing_method=step._routing_method if hasattr(step, '_routing_method') else None,
                    )

                    # LLM decision evaluation (Phase 4D)
                    decision, reason = self._evaluate_step_result(step, plan)
                    if decision == "stop":
                        logger.info(f"LLM evaluation: stop after step {step.step_id} — {reason}")
                        self._mark_remaining_skipped(plan, step.step_id + 1)
                        break
                    elif decision == "adjust" and reason:
                        # Modify next step's input with adjustment instruction
                        next_steps = [s for s in plan.steps
                                      if s.step_id == step.step_id + 1]
                        if next_steps:
                            next_steps[0].input_text += f"\n\nAdjustment: {reason}"
                            logger.info(f"LLM evaluation: adjust next step — {reason}")
                else:
                    # Failure: break loop, remaining steps depend on this one
                    step.status = StepStatus.FAILED
                    logger.warning(f"Step {step.step_id} returned empty result — breaking plan")
                    _dbg.log_plan_step_result(
                        step_id=step.step_id, skill_name=step.skill_name,
                        status="failed_empty", result_text=None,
                        elapsed_ms=_step_ms,
                        routing_method=step._routing_method if hasattr(step, '_routing_method') else None,
                    )
                    self._mark_remaining_skipped(plan, step.step_id + 1)
                    break
            except Exception as e:
                step.status = StepStatus.FAILED
                step.result = f"Error: {e}"
                _step_ms = (time.time() - _step_start) * 1000
                logger.error(f"Step {step.step_id} failed: {e} — breaking plan")
                _dbg.log_plan_step_result(
                    step_id=step.step_id, skill_name=step.skill_name,
                    status="exception", result_text=str(e),
                    elapsed_ms=_step_ms,
                )
                self._mark_remaining_skipped(plan, step.step_id + 1)
                break

        # Set final plan status
        if plan.status != PlanStatus.CANCELLED:
            completed = sum(1 for s in plan.steps if s.status == StepStatus.COMPLETED)
            plan.status = PlanStatus.COMPLETED if completed > 0 else PlanStatus.FAILED

        # Synthesize final response
        final = self._synthesize_results(plan, results)
        self.active_plan = None

        _plan_ms = (time.time() - _plan_start) * 1000
        _completed = sum(1 for s in plan.steps if s.status == StepStatus.COMPLETED)
        _failed = sum(1 for s in plan.steps if s.status == StepStatus.FAILED)
        _skipped = sum(1 for s in plan.steps if s.status == StepStatus.SKIPPED)
        _dbg.log_plan_complete(
            step_count=len(plan.steps), completed=_completed,
            failed=_failed, skipped=_skipped,
            status=plan.status.value if hasattr(plan.status, 'value') else str(plan.status),
            total_ms=_plan_ms, final_response=final,
        )

        return final

    def _mark_remaining_skipped(self, plan: TaskPlan, from_step_id: int):
        """Mark all steps from from_step_id onward as SKIPPED."""
        for step in plan.steps:
            if step.step_id >= from_step_id and step.status == StepStatus.PENDING:
                step.status = StepStatus.SKIPPED

    def _execute_step(self, step: PlanStep, prior_context: str) -> Optional[str]:
        """Execute a single plan step.

        Routes through skill_manager for real skills, LLM for synthesis.
        Prior context from earlier steps is passed to the handler but kept
        out of skill matching to avoid polluting semantic similarity scores.
        """
        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()

        input_text = step.input_text
        # Build enriched input for handlers (includes prior step context)
        enriched_text = input_text
        if prior_context:
            enriched_text = f"{input_text}\n\nContext from previous step: {prior_context}"

        _dbg.log_plan_step_start(
            step_id=step.step_id, total_steps=0,  # filled by caller
            skill_name=step.skill_name, description=step.description,
            input_text=input_text, enriched_text=enriched_text,
        )

        # Handle pseudo-skills
        if step.skill_name == "llm_synthesis":
            step._routing_method = "pseudo_llm_synthesis"
            _dbg.log_plan_step_routing(step.step_id, step.skill_name, "pseudo_llm_synthesis")
            return self._llm_synthesis(enriched_text)

        if step.skill_name == "web_research":
            step._routing_method = "pseudo_web_research"
            _dbg.log_plan_step_routing(step.step_id, step.skill_name, "pseudo_web_research")
            return self._web_research(enriched_text, step)

        if step.skill_name == "create_document":
            # Route to file_editor's create_document handler directly
            step._routing_method = "pseudo_create_document"
            _dbg.log_plan_step_routing(step.step_id, step.skill_name, "pseudo_create_document")
            fe = self._skill_manager.get_skill("file_editor")
            if fe and hasattr(fe, 'create_document'):
                try:
                    response = fe.create_document(entities={'original_text': enriched_text})
                    if response:
                        from core.honorific import resolve_honorific
                        return resolve_honorific(response)
                except Exception as e:
                    logger.warning(f"create_document pseudo-skill failed: {e}")
            return self._llm_synthesis(enriched_text)

        # --- Direct skill routing by step.skill_name ---
        # The plan LLM already identified the correct skill. Get it by name
        # and find the best handler via within-skill semantic matching.
        # This avoids global match_intent() which often misroutes LLM-generated
        # plan step descriptions.
        skill = self._skill_manager.get_skill(step.skill_name)
        if skill and hasattr(skill, 'semantic_intents') and skill.semantic_intents:
            result = self._direct_skill_route(
                step, skill, input_text, enriched_text, _dbg,
            )
            if result is not None:
                return result

        # Fallback 1: global match_intent (for skills without semantic intents
        # or if direct routing found no handler)
        match = self._skill_manager.match_intent(input_text)
        if match:
            skill_name, intent_id, entities = match
            match_score = entities.get('similarity')
            match_layer = entities.get('layer', 'unknown')
            _dbg.log_plan_step_routing(
                step.step_id, step.skill_name, "match_intent",
                matched_skill=skill_name, matched_intent=intent_id,
                match_score=match_score, match_layer=match_layer,
            )
            fallback_skill = self._skill_manager.get_skill(skill_name)
            if fallback_skill:
                entities['original_text'] = enriched_text
                logger.info(f"Plan step routing: {skill_name}.{intent_id}")
                try:
                    response = fallback_skill.handle_intent(intent_id, entities)
                    if response:
                        step._routing_method = f"match_intent:{skill_name}.{intent_id}"
                        return response
                except Exception as e:
                    logger.warning(f"Plan step handler failed: {e}")

        # Fallback 2: full execute_intent on clean input
        response = self._skill_manager.execute_intent(input_text)
        if response:
            step._routing_method = "execute_intent"
            _dbg.log_plan_step_routing(step.step_id, step.skill_name, "execute_intent")
            return response

        # Last resort: LLM synthesis with full context
        logger.info(f"Skill '{step.skill_name}' didn't match input — using LLM fallback")
        step._routing_method = "llm_fallback"
        _dbg.log_plan_step_routing(step.step_id, step.skill_name, "llm_fallback")
        return self._llm_synthesis(enriched_text)

    def _direct_skill_route(self, step: 'PlanStep', skill, input_text: str,
                             enriched_text: str, _dbg) -> Optional[str]:
        """Route a plan step directly to the named skill's best handler.

        Uses within-skill semantic matching with a relaxed threshold (0.20)
        since the plan LLM already confirmed the skill — we just need to
        pick the best handler within it.

        Returns the handler response, or None if no handler matched.
        """
        import inspect
        try:
            from sentence_transformers import util as st_util
        except ImportError:
            return None

        sm = self._skill_manager
        if not hasattr(sm, '_embedding_model') or not sm._embedding_model:
            return None

        # Embed both input_text and step.description — the plan LLM generates
        # a technical command (input_text) and a natural description.  One of
        # the two will match the skill's example phrases better depending on
        # how the examples are written.
        candidates = [input_text]
        if step.description and step.description != input_text:
            candidates.append(step.description)

        try:
            candidate_embs = sm._embedding_model.encode(
                candidates, convert_to_tensor=True, show_progress_bar=False,
            )
        except Exception as e:
            logger.warning(f"Direct skill route embedding failed: {e}")
            return None

        best_handler = None
        best_score = 0.0
        best_intent = None

        for intent_id, intent_data in skill.semantic_intents.items():
            cache_key = (step.skill_name, intent_id)
            ex_embs = sm._semantic_embedding_cache.get(cache_key)
            if ex_embs is None:
                continue
            sims = st_util.cos_sim(candidate_embs, ex_embs)
            max_sim = float(sims.max())
            if max_sim > best_score:
                best_score = max_sim
                best_handler = intent_data.get('handler')
                best_intent = intent_id

        if not best_handler or best_score < 0.20:
            logger.info(
                f"Direct skill route: no handler in '{step.skill_name}' "
                f"(best={best_score:.2f})"
            )
            return None

        logger.info(
            f"Direct skill route: {step.skill_name}.{best_intent} "
            f"(score={best_score:.2f})"
        )
        _dbg.log_plan_step_routing(
            step.step_id, step.skill_name, "direct_skill",
            matched_skill=step.skill_name, matched_intent=best_intent,
            match_score=best_score, match_layer="plan_direct",
        )

        entities = {'original_text': enriched_text}
        try:
            sig = inspect.signature(best_handler)
            if 'entities' in sig.parameters:
                response = best_handler(entities=entities)
            else:
                response = best_handler()
        except Exception as e:
            logger.warning(f"Direct skill handler {best_intent} failed: {e}")
            return None

        if response:
            step._routing_method = f"direct_skill:{step.skill_name}.{best_intent}"
            if isinstance(response, str):
                from core.honorific import resolve_honorific
                response = resolve_honorific(response)
            return response

        return None

    def _llm_synthesis(self, input_text: str) -> str:
        """Use LLM to synthesize/summarize content."""
        try:
            return self._llm.chat(
                user_message=input_text,
                max_tokens=300,
            )
        except Exception as e:
            logger.error(f"LLM synthesis failed: {e}")
            return ""

    def _web_research(self, input_text: str, step: PlanStep) -> str:
        """Execute web research step using WebResearcher.

        Searches DuckDuckGo, fetches top pages, then synthesizes via LLM.
        Falls back to LLM parametric knowledge if web_researcher unavailable.
        """
        if not self._web_researcher:
            logger.warning("Web research requested but no web_researcher available — LLM fallback")
            return self._llm_synthesis(f"Based on your knowledge, answer: {input_text}")

        # Extract a clean search query — input_text may contain adjustment
        # instructions and prior step context that would poison the search.
        # Use only the first paragraph (the step's core query).
        search_query = input_text.split('\n\n')[0].strip()
        if len(search_query) > 200:
            search_query = search_query[:200]
        logger.info(f"Web research query: {search_query[:80]}")

        try:
            # Search the web with the clean query
            results = self._web_researcher.search(search_query, max_results=5)
            if not results:
                logger.warning(f"Web research returned no results for: {search_query[:80]}")
                return self._llm_synthesis(f"Based on your knowledge, answer: {input_text}")

            # Fetch top pages in parallel
            pages = self._web_researcher.fetch_pages_parallel(
                results, max_results=3, max_chars=4000, timeout=5.0,
            )

            # Build research context from pages or fall back to snippets
            if pages:
                research_text = "\n\n".join(pages)
            else:
                snippets = []
                for r in results[:5]:
                    title = r.get('title', '')
                    snippet = r.get('snippet', '')
                    if snippet:
                        snippets.append(f"[{title}]: {snippet}")
                research_text = "\n\n".join(snippets) if snippets else ""

            if not research_text:
                return self._llm_synthesis(f"Based on your knowledge, answer: {input_text}")

            # Synthesize research — pass full context so LLM has prior step info
            logger.info(f"Web research complete: {len(results)} results, {len(pages)} pages fetched")
            synthesis_prompt = (
                f"Based on the following web research results, answer this question: {search_query}\n\n"
                f"RESEARCH DATA:\n{research_text[:8000]}\n\n"
                f"Provide a factual, concise answer using specific data from the research."
            )
            return self._llm.chat(user_message=synthesis_prompt, max_tokens=400)

        except Exception as e:
            logger.warning(f"Web research failed: {e}")
            return self._llm_synthesis(f"Based on your knowledge, answer: {input_text}")

    def _synthesize_results(self, plan: TaskPlan, results: list[str]) -> str:
        """Combine step results into a final response.

        Handles: full completion, partial completion, cancellation, and failure.
        """
        completed = [s for s in plan.steps if s.status == StepStatus.COMPLETED]
        failed = [s for s in plan.steps if s.status == StepStatus.FAILED]
        skipped = [s for s in plan.steps if s.status == StepStatus.SKIPPED]

        # Cancelled with nothing completed — no synthesis needed
        if plan.status == PlanStatus.CANCELLED and not completed:
            return ""  # Caller will use persona.task_cancelled()

        # Nothing completed at all (failure, not cancellation)
        if not results:
            return "I wasn't able to complete any of the steps for that request."

        # Single completed step — return its result directly
        if len(completed) == 1 and not failed and not skipped:
            return completed[0].result

        # Multiple steps or partial — ask LLM to synthesize
        combined = "\n\n".join(results)
        synthesis_prompt = (
            f"The user asked: \"{plan.original_request}\"\n\n"
            f"Here are the results from multiple steps:\n{combined}\n\n"
            f"Synthesize these into a single, natural spoken response. "
            f"Be concise and conversational."
        )

        if plan.status == PlanStatus.CANCELLED and completed:
            synthesis_prompt += (
                f"\nNote: the plan was cancelled after {len(completed)} of "
                f"{len(plan.steps)} steps. Report what was completed."
            )
        elif failed:
            synthesis_prompt += (
                f"\nNote: {len(failed)} step(s) failed and {len(skipped)} "
                f"subsequent step(s) were skipped. Report what succeeded."
            )

        try:
            return self._llm.chat(user_message=synthesis_prompt, max_tokens=400)
        except Exception:
            # Fallback: just return the last successful result
            return completed[-1].result if completed else "I completed the task but had trouble summarizing the results."

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self):
        """Request cancellation of the active plan."""
        if self.active_plan and self.active_plan.status == PlanStatus.RUNNING:
            self._cancel_requested = True
            logger.info("Plan cancellation requested")
        # Also cancel pending confirmation
        if self._pending_plan_confirmation:
            self.resolve_confirmation(False)

    def skip_current(self):
        """Skip the currently running step."""
        self._skip_requested = True
        if not self.active_plan:
            return
        for step in self.active_plan.steps:
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.SKIPPED
                logger.info(f"Step {step.step_id} skipped")
                break
