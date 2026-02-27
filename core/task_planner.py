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

logger = logging.getLogger("jarvis.task_planner")


# ---------------------------------------------------------------------------
# Compound detection whitelist
# ---------------------------------------------------------------------------
# English conjunctive structures that signal multi-step intent.
# Word-boundary matching avoids false positives from substrings.

COMPOUND_SIGNALS = [
    "and then",
    "and also",
    "and what",
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

Respond with ONLY a JSON array (no markdown, no explanation) or the word SINGLE.

JSON format:
[
  {{"step": 1, "skill": "skill_name", "input": "what to tell the skill", "description": "Searching for X"}},
  {{"step": 2, "skill": "skill_name", "input": "what to tell the skill", "description": "Creating Y"}}
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
                 context_window=None):
        self._llm = llm
        self._skill_manager = skill_manager
        self._self_awareness = self_awareness
        self._conversation = conversation
        self._config = config
        self._event_queue = event_queue  # For voice interrupt detection
        self._context_window = context_window

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

    def needs_planning(self, command: str) -> bool:
        """Check if command contains conjunctive phrases suggesting multi-step.

        Uses word-boundary whitelist — fast, no false positives from substrings.
        """
        for pattern in _COMPOUND_PATTERNS:
            if pattern.search(command):
                logger.info(f"Compound signal detected: {pattern.pattern}")
                return True
        return False

    # ------------------------------------------------------------------
    # Plan generation (single LLM call)
    # ------------------------------------------------------------------

    def generate_plan(self, command: str) -> Optional[TaskPlan]:
        """Ask the LLM to decompose a compound command into steps.

        Returns TaskPlan if multi-step, None if LLM decides single-step.
        """
        manifest = self._self_awareness.get_capability_manifest()
        if not manifest:
            logger.warning("No capability manifest available — skipping plan generation")
            return None

        prompt = _PLAN_PROMPT.format(manifest=manifest, command=command)

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

        steps = []
        for i, raw in enumerate(steps_raw[:4]):  # Max 4 steps
            skill = raw.get("skill", "").strip()
            if skill not in valid_skills:
                logger.warning(f"Plan step {i+1} references unknown skill '{skill}' — skipping")
                continue

            steps.append(PlanStep(
                step_id=i + 1,
                description=raw.get("description", f"Step {i+1}"),
                skill_name=skill,
                input_text=raw.get("input", command),
            ))

        if len(steps) < 2:
            logger.info(f"Plan has {len(steps)} valid steps — treating as single-step")
            return None

        plan = TaskPlan(original_request=command, steps=steps)
        logger.info(f"Generated plan: {len(steps)} steps for '{command[:60]}'")
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

            try:
                result = self._execute_step(step, prior_context)
                step.result = result or ""

                if result:
                    step.status = StepStatus.COMPLETED
                    results.append(f"[{step.description}]: {result}")
                    prior_context = result

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
                    self._mark_remaining_skipped(plan, step.step_id + 1)
                    break
            except Exception as e:
                step.status = StepStatus.FAILED
                step.result = f"Error: {e}"
                logger.error(f"Step {step.step_id} failed: {e} — breaking plan")
                self._mark_remaining_skipped(plan, step.step_id + 1)
                break

        # Set final plan status
        if plan.status != PlanStatus.CANCELLED:
            completed = sum(1 for s in plan.steps if s.status == StepStatus.COMPLETED)
            plan.status = PlanStatus.COMPLETED if completed > 0 else PlanStatus.FAILED

        # Synthesize final response
        final = self._synthesize_results(plan, results)
        self.active_plan = None
        return final

    def _mark_remaining_skipped(self, plan: TaskPlan, from_step_id: int):
        """Mark all steps from from_step_id onward as SKIPPED."""
        for step in plan.steps:
            if step.step_id >= from_step_id and step.status == StepStatus.PENDING:
                step.status = StepStatus.SKIPPED

    def _execute_step(self, step: PlanStep, prior_context: str) -> Optional[str]:
        """Execute a single plan step.

        Routes through skill_manager for real skills, LLM for synthesis.
        """
        input_text = step.input_text

        # Inject prior step context if available
        if prior_context:
            input_text = f"{input_text}\n\nContext from previous step: {prior_context}"

        # Handle pseudo-skills
        if step.skill_name == "llm_synthesis":
            return self._llm_synthesis(input_text)

        if step.skill_name == "web_research":
            return self._web_research(input_text, step)

        # Real skill — route through skill_manager
        response = self._skill_manager.execute_intent(input_text)
        if response:
            return response

        # Skill didn't match — try LLM as fallback for this step
        logger.info(f"Skill '{step.skill_name}' didn't match input — using LLM fallback")
        return self._llm_synthesis(input_text)

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
        """Execute web research step.

        Uses LLM with tool calling if available, falls back to plain LLM.
        """
        # Try to collect streamed response with tool calling
        try:
            tokens = []
            for chunk in self._llm.stream_with_tools(input_text, max_tokens=400):
                if isinstance(chunk, str):
                    tokens.append(chunk)
                else:
                    # ToolCallRequest — we can't handle tool execution here
                    # (would need the web_researcher). Fall back to plain LLM.
                    logger.info("Web research tool call requested — using LLM synthesis")
                    return self._llm_synthesis(f"Search the web and answer: {input_text}")
            return "".join(tokens)
        except Exception as e:
            logger.warning(f"Web research streaming failed: {e}")
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
