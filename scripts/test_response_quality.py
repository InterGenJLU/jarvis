#!/usr/bin/env python3
"""
Interactive Response Quality Test Harness for JARVIS

Pipes test commands through the full ConversationRouter pipeline (same path
as jarvis_console.py), speaks each response through TTS (Kokoro/Piper), and
presents results for human review. You hear exactly what a voice user would
hear, then mark pass/fail/note.

Tests cover:
  - Response structure (formatting, length, sentence boundaries)
  - Coherence (answers make sense for the question)
  - Cadence (how it sounds when spoken — pauses, segment length, pitch)
  - Pronunciation (TTS output — letters, model names, acronyms, odd pitches)

Usage:
    python3 scripts/test_response_quality.py              # Interactive + TTS playback
    python3 scripts/test_response_quality.py --no-tts      # Interactive, text only
    python3 scripts/test_response_quality.py --batch       # Non-interactive (auto checks only)
    python3 scripts/test_response_quality.py --phase 5A    # Single phase
    python3 scripts/test_response_quality.py --id 5A-01    # Single test
    python3 scripts/test_response_quality.py --replay       # Replay last response TTS
    python3 scripts/test_response_quality.py --category pronunciation  # By category
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

import sys
import re
import time
import json
import argparse
import warnings
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# ===========================================================================
# ANSI colors
# ===========================================================================

class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"


# ===========================================================================
# Test case structure
# ===========================================================================

@dataclass
class QualityTest:
    id: str                                  # "5A-01"
    input: str                               # Test command or raw text
    phase: str                               # "5A", "5B", etc.
    category: str                            # Human-readable group name

    # What to test
    mode: str = "full"                       # "full" = route+LLM, "normalize" = TTS only

    # Auto-checks (run before interactive review)
    expect_contains: Optional[list] = None           # raw response must contain ALL
    expect_not_contains: Optional[list] = None       # raw response must NOT contain any
    expect_tts_contains: Optional[list] = None       # normalized text must contain
    expect_tts_not_contains: Optional[list] = None   # normalized text must NOT contain
    expect_max_sentences: Optional[int] = None        # brevity check
    expect_min_length: Optional[int] = None           # minimum response length
    expect_max_length: Optional[int] = None           # maximum response length
    expect_handled: Optional[bool] = None             # expect skill routing (not LLM)
    expect_no_raw_markdown: bool = False              # no ** ## ` after normalization
    expect_no_raw_urls: bool = False                  # no http:// after normalization
    expect_no_orphan_letters: bool = False            # no standalone A/O/U unmapped

    notes: str = ""                                   # Hint for reviewer


# ===========================================================================
# Test definitions
# ===========================================================================

TESTS = []

# ---------------------------------------------------------------------------
# Phase 5A: Self-Knowledge (hardware identity + model awareness)
# ---------------------------------------------------------------------------

TESTS.extend([
    QualityTest("5A-01", "what model are you running", "5A", "Self-Knowledge",
        expect_contains=["qwen"],
        expect_not_contains=["searching", "let me search", "web_search"],
        expect_tts_contains=["qwen three point five"],
        expect_max_sentences=4,
        notes="Should answer from State line. TTS should normalize model name."),

    QualityTest("5A-02", "how many facts do you remember about me", "5A", "Self-Knowledge",
        expect_not_contains=["searching", "search the web", "web_search"],
        expect_max_sentences=3,
        notes="Must answer from State line fact count, not web search."),

    QualityTest("5A-03", "what CPU are you running", "5A", "Self-Knowledge",
        expect_contains=["ryzen"],
        expect_not_contains=["searching"],
        expect_max_sentences=3,
        notes="Hardware identity — should answer in first person from State line."),

    QualityTest("5A-04", "how much RAM do you have", "5A", "Self-Knowledge",
        expect_not_contains=["searching"],
        expect_max_sentences=3,
        notes="Should say ~63-64GB, first person."),

    QualityTest("5A-05", "what GPU are you using", "5A", "Self-Knowledge",
        expect_contains=["7900"],
        expect_not_contains=["searching"],
        expect_max_sentences=3,
        notes="Should mention RX 7900 XT, first person."),

    QualityTest("5A-06", "what can you do", "5A", "Self-Knowledge",
        expect_min_length=50,
        expect_max_length=800,
        expect_no_raw_markdown=True,
        notes="Should list capabilities from manifest. No markdown bullets in voice."),

    QualityTest("5A-07", "what quantization are you using", "5A", "Self-Knowledge",
        expect_contains=["Q3"],
        expect_not_contains=["searching"],
        expect_tts_contains=["Q three"],
        expect_max_sentences=3,
        notes="Quant info from State line."),
])

# ---------------------------------------------------------------------------
# Phase 5B: Skill Response Quality (skill-handled responses)
# ---------------------------------------------------------------------------

TESTS.extend([
    QualityTest("5B-01", "what time is it", "5B", "Skill Responses",
        expect_handled=True,
        expect_no_raw_urls=True,
        notes="Time skill — should be clean spoken text."),

    QualityTest("5B-02", "what CPU do I have", "5B", "Skill Responses",
        expect_handled=True,
        expect_tts_contains=["ryzen"],
        notes="System info skill — second person ('You have a...')."),

    QualityTest("5B-03", "what's the weather", "5B", "Skill Responses",
        expect_handled=True,
        expect_no_raw_urls=True,
        notes="Weather skill — clean spoken text, no JSON artifacts."),
])

# ---------------------------------------------------------------------------
# Phase 5C: LLM Response Structure (LLM-fallback quality)
# ---------------------------------------------------------------------------

TESTS.extend([
    QualityTest("5C-01", "what is the difference between TCP and UDP", "5C", "LLM Structure",
        expect_no_raw_markdown=True,
        expect_no_raw_urls=True,
        expect_tts_contains=["T C P"],
        expect_min_length=50,
        expect_max_sentences=6,
        notes="Technical explanation — acronyms normalized, no markdown."),

    QualityTest("5C-02", "what is the capital of Japan", "5C", "LLM Structure",
        expect_contains=["tokyo"],
        expect_max_sentences=2,
        expect_max_length=200,
        notes="Brief factual answer."),

    QualityTest("5C-03", "do you have a favorite programming language", "5C", "LLM Structure",
        expect_not_contains=["as an ai", "i don't have preferences"],
        expect_min_length=20,
        notes="Should express personality, not disclaim."),

    QualityTest("5C-04", "how much RAM do I have", "5C", "LLM Structure",
        expect_handled=True,
        expect_tts_contains=["gigabytes"],
        notes="System info skill — RAM should TTS-normalize (GB→gigabytes)."),
])

# ---------------------------------------------------------------------------
# Phase 5D: Pronunciation Safety (TTS normalizer validation)
# ---------------------------------------------------------------------------

TESTS.extend([
    QualityTest("5D-01",
        "I'm running the Qwen3.5-35B-A3B model.",
        "5D", "Pronunciation",
        mode="normalize",
        expect_tts_contains=["qwen three point five", "eh three"],
        expect_tts_not_contains=["A3B"],
        expect_no_orphan_letters=True,
        notes="Model name: A→'eh' phonetic mapping."),

    QualityTest("5D-02",
        "The server is at 192.168.1.100 on port 8080.",
        "5D", "Pronunciation",
        mode="normalize",
        expect_tts_contains=["dot", "eighty eighty"],
        notes="IP and port spoken out."),

    QualityTest("5D-03",
        "Check the file at /home/user/jarvis/config.yaml for settings.",
        "5D", "Pronunciation",
        mode="normalize",
        expect_tts_contains=["slash home", "config dot yaml"],
        notes="File path with 'slash' separators."),

    QualityTest("5D-04",
        "The API uses HTTPS on the GPU server with 64GB RAM.",
        "5D", "Pronunciation",
        mode="normalize",
        expect_tts_contains=["gigabytes"],
        notes="Technical acronyms + file sizes normalized."),
])


# ===========================================================================
# Component initialization (reuses Tier 2 pattern)
# ===========================================================================

def init_components():
    """Load JARVIS components for full-pipeline testing."""
    from core.config import load_config
    from core.conversation import ConversationManager
    from core.llm_router import LLMRouter
    from core.skill_manager import SkillManager
    from core.responses import get_response_library
    from core.conversation_state import ConversationState
    from core.conversation_router import ConversationRouter
    from core.self_awareness import SelfAwareness
    from core.task_planner import TaskPlanner

    class TTSStub:
        def speak(self, *a, **kw): pass
        def stop(self): pass

    print(f"{C.CYAN}Loading components...{C.RESET}")
    t0 = time.perf_counter()

    config = load_config()
    tts = TTSStub()
    conversation = ConversationManager(config)
    conversation.current_user = "user"
    # Clear stale history so tests start clean (avoids chat_history.jsonl poisoning)
    conversation.session_history = []
    responses = get_response_library()
    llm = LLMRouter(config)
    sm = SkillManager(config, conversation, tts, responses, llm)
    sm.load_all_skills()

    # Reminder manager
    reminder_manager = None
    if config.get("reminders.enabled", True):
        from core.reminder_manager import get_reminder_manager
        reminder_manager = get_reminder_manager(config, tts, conversation)
        reminder_manager.set_ack_window_callback(lambda rid: None)
        reminder_manager.set_window_callback(lambda d: None)
        reminder_manager.set_listener_callbacks(pause=lambda: None, resume=lambda: None)

    # Memory manager
    memory_manager = None
    if config.get("conversational_memory.enabled", False):
        from core.memory_manager import get_memory_manager
        memory_manager = get_memory_manager(
            config=config, conversation=conversation,
            embedding_model=sm._embedding_model,
        )
        conversation.set_memory_manager(memory_manager)

    # Context window
    context_window = None
    if config.get("context_window.enabled", False):
        from core.context_window import get_context_window
        context_window = get_context_window(
            config=config, embedding_model=sm._embedding_model, llm=llm,
        )
        conversation.set_context_window(context_window)

    # Web researcher
    web_researcher = None
    if config.get("llm.local.tool_calling", False):
        from core.web_research import WebResearcher
        web_researcher = WebResearcher(config)

    # Self-awareness + task planner
    sa = SelfAwareness(skill_manager=sm, memory_manager=memory_manager, config=config)
    tp = TaskPlanner(llm=None, skill_manager=sm, self_awareness=sa, event_queue=None)

    cs = ConversationState()
    router = ConversationRouter(
        skill_manager=sm, conversation=conversation, llm=llm,
        reminder_manager=reminder_manager, memory_manager=memory_manager,
        context_window=context_window, conv_state=cs, config=config,
        web_researcher=web_researcher, task_planner=tp,
        self_awareness=sa,
    )

    elapsed = time.perf_counter() - t0
    print(f"{C.GREEN}Ready — {len(sm.skills)} skills in {elapsed:.1f}s{C.RESET}")
    print(f"{C.DIM}State: {sa.get_compact_state()}{C.RESET}\n")

    return {
        "router": router,
        "llm": llm,
        "conv_state": cs,
        "skill_manager": sm,
        "memory_manager": memory_manager,
        "reminder_manager": reminder_manager,
        "task_planner": tp,
        "self_awareness": sa,
        "config": config,
    }


def reset_state(components):
    """Reset conversational state between tests."""
    if not components:
        return
    cs = components.get("conv_state")
    if cs:
        cs.jarvis_asked_question = False
        cs.turn_count = 0
        cs.research_results = None
        cs.research_exchange = None

    # Clear conversation history so prior test answers don't contaminate
    router = components.get("router")
    if router and hasattr(router, 'conversation'):
        router.conversation.session_history = []

    mm = components.get("memory_manager")
    if mm and hasattr(mm, '_pending_forget'):
        mm._pending_forget = None

    rm = components.get("reminder_manager")
    if rm and hasattr(rm, '_rundown_state'):
        rm._rundown_state = None

    tp = components.get("task_planner")
    if tp:
        tp._active_plan = None

    sm = components.get("skill_manager")
    if sm:
        for skill_obj in sm.skills.values():
            if hasattr(skill_obj, '_pending_confirmation'):
                skill_obj._pending_confirmation = None


# ===========================================================================
# TTS normalizer + engine
# ===========================================================================

_normalizer = None
_tts_engine = None

def get_normalizer():
    global _normalizer
    if _normalizer is None:
        from core.tts_normalizer import TTSNormalizer
        _normalizer = TTSNormalizer()
    return _normalizer


def init_tts(config):
    """Initialize TTS engine for playback."""
    global _tts_engine
    if _tts_engine is not None:
        return _tts_engine
    try:
        from core.tts import TextToSpeech
        _tts_engine = TextToSpeech(config)
        print(f"{C.GREEN}TTS ready ({_tts_engine.engine}){C.RESET}")
        return _tts_engine
    except Exception as e:
        print(f"{C.YELLOW}TTS init failed: {e} — running text-only{C.RESET}")
        return None


def speak_response(text):
    """Speak text through TTS engine. Returns True if spoken."""
    if not _tts_engine or not text:
        return False
    try:
        _tts_engine.speak(text)
        return True
    except Exception as e:
        print(f"  {C.YELLOW}TTS playback error: {e}{C.RESET}")
        return False


# ===========================================================================
# Run a single test
# ===========================================================================

def run_test(test, components):
    """Execute a test and return structured result."""
    reset_state(components)
    normalizer = get_normalizer()

    result = {
        "id": test.id,
        "input": test.input,
        "raw_response": "",
        "normalized": "",
        "source": "",
        "timing_ms": 0,
        "auto_failures": [],
        "auto_warnings": [],
    }

    t0 = time.perf_counter()

    # --- Pronunciation-only tests: skip routing, normalize input directly ---
    if test.mode == "normalize":
        result["raw_response"] = test.input
        result["source"] = "direct (pronunciation test)"
        result["normalized"] = normalizer.normalize(test.input)
        result["timing_ms"] = (time.perf_counter() - t0) * 1000
        _run_auto_checks(test, result)
        return result

    # --- Full pipeline: route through ConversationRouter ---
    router = components["router"]
    llm = components["llm"]

    r = router.route(test.input)

    if r.skip:
        result["raw_response"] = "(skipped)"
        result["source"] = "skip"
        result["timing_ms"] = (time.perf_counter() - t0) * 1000
        return result

    if r.handled:
        result["raw_response"] = r.text or ""
        info = r.match_info or {}
        result["source"] = f"skill:{info.get('skill_name', '?')} ({r.intent})"
    else:
        # LLM fallback — call chat() with router's assembled context
        try:
            response = llm.chat(
                user_message=r.llm_command,
                conversation_history=r.llm_history,
                memory_context=r.memory_context,
                conversation_messages=r.context_messages,
                max_tokens=r.llm_max_tokens or 300,
            )
            response = llm.strip_filler(response) if hasattr(llm, 'strip_filler') else response
            result["raw_response"] = response or ""
            result["source"] = "llm"
        except Exception as e:
            result["raw_response"] = f"(LLM error: {e})"
            result["source"] = "error"
            result["auto_failures"].append(f"LLM call failed: {e}")

    result["timing_ms"] = (time.perf_counter() - t0) * 1000

    # Apply TTS normalization
    if result["raw_response"] and not result["raw_response"].startswith("("):
        result["normalized"] = normalizer.normalize(result["raw_response"])

    # Run auto-checks
    _run_auto_checks(test, result)

    return result


def _run_auto_checks(test, result):
    """Run automated checks and populate failures/warnings."""
    raw = result["raw_response"]
    normalized = result["normalized"]
    failures = result["auto_failures"]
    warnings_ = result["auto_warnings"]

    raw_lower = raw.lower()

    # --- Content checks on raw response ---
    if test.expect_contains:
        for needle in test.expect_contains:
            if needle.lower() not in raw_lower:
                failures.append(f"missing '{needle}'")

    if test.expect_not_contains:
        for needle in test.expect_not_contains:
            if needle.lower() in raw_lower:
                failures.append(f"contains forbidden '{needle}'")

    # --- Structural checks ---
    if test.expect_handled is not None:
        is_skill = result["source"].startswith("skill:")
        if test.expect_handled and not is_skill:
            failures.append(f"expected skill routing, got {result['source']}")
        elif not test.expect_handled and is_skill:
            failures.append(f"expected LLM, got {result['source']}")

    if test.expect_max_sentences is not None:
        count = _count_sentences(raw)
        if count > test.expect_max_sentences:
            warnings_.append(f"verbose: {count} sentences (max {test.expect_max_sentences})")

    if test.expect_min_length is not None and len(raw) < test.expect_min_length:
        failures.append(f"too short: {len(raw)} chars (min {test.expect_min_length})")

    if test.expect_max_length is not None and len(raw) > test.expect_max_length:
        warnings_.append(f"long: {len(raw)} chars (max {test.expect_max_length})")

    # --- TTS-normalized checks ---
    if normalized:
        norm_lower = normalized.lower()

        if test.expect_tts_contains:
            for needle in test.expect_tts_contains:
                if needle.lower() not in norm_lower:
                    failures.append(f"TTS missing '{needle}'")

        if test.expect_tts_not_contains:
            for needle in test.expect_tts_not_contains:
                if needle.lower() in norm_lower:
                    failures.append(f"TTS contains forbidden '{needle}'")

        if test.expect_no_raw_markdown:
            if re.search(r'\*\*|^#{1,6}\s|`[^`]+`', normalized, re.MULTILINE):
                failures.append("raw markdown survived normalization")

        if test.expect_no_raw_urls:
            if re.search(r'https?://', normalized):
                failures.append("raw URL survived normalization")

        if test.expect_no_orphan_letters:
            # Find standalone A, O, U that weren't remapped (I is a valid pronoun)
            orphans = re.findall(r'(?<!\w)([AOU])(?!\w)', normalized)
            if orphans:
                warnings_.append(f"orphan letter(s) {orphans} may mispronounce")


def _count_sentences(text):
    """Rough sentence count."""
    cleaned = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'`[^`]+`', '', cleaned)
    sentences = re.split(r'[.!?]+', cleaned)
    return len([s for s in sentences if s.strip()])


# ===========================================================================
# Interactive display
# ===========================================================================

def display_result(test, result, verbose=False):
    """Display a test result for interactive review."""
    raw = result["raw_response"]
    normalized = result["normalized"]
    source = result["source"]
    ms = result["timing_ms"]
    failures = result["auto_failures"]
    warnings_ = result["auto_warnings"]

    # Header
    has_issues = bool(failures or warnings_)
    if failures:
        status = f"{C.RED}FAIL{C.RESET}"
    elif warnings_:
        status = f"{C.YELLOW}WARN{C.RESET}"
    else:
        status = f"{C.GREEN}OK{C.RESET}"

    print(f"\n{C.BOLD}[{test.id}]{C.RESET} [{status}] {C.CYAN}{test.input}{C.RESET}")
    print(f"  {C.DIM}Source: {source} | {ms:.0f}ms{C.RESET}")

    if test.notes:
        print(f"  {C.DIM}Note: {test.notes}{C.RESET}")

    # Raw response
    print(f"\n  {C.BOLD}Response:{C.RESET}")
    for line in raw.split('\n'):
        print(f"    {line}")

    # TTS normalized (always show — this is what they'd hear)
    if normalized and normalized != raw:
        print(f"\n  {C.BOLD}TTS (what you'd hear):{C.RESET}")
        # Wrap long lines for readability
        words = normalized.split()
        line = "    "
        for w in words:
            if len(line) + len(w) + 1 > 80:
                print(line)
                line = "    " + w
            else:
                line += (" " if len(line) > 4 else "") + w
        if line.strip():
            print(line)

    # Auto-check results
    if failures:
        print(f"\n  {C.RED}Auto-check failures:{C.RESET}")
        for f in failures:
            print(f"    {C.RED}✗ {f}{C.RESET}")

    if warnings_:
        print(f"\n  {C.YELLOW}Warnings:{C.RESET}")
        for w in warnings_:
            print(f"    {C.YELLOW}⚠ {w}{C.RESET}")

    return has_issues


def interactive_prompt(last_response=""):
    """Ask the reviewer for a verdict."""
    replay_hint = " / [r]eplay" if _tts_engine and last_response else ""
    print(f"\n  {C.MAGENTA}Verdict: [p]ass / [f]ail / [n]ote / [s]kip{replay_hint} / [q]uit{C.RESET}", end=" ")
    try:
        choice = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "quit", ""

    if choice.startswith("r") and last_response:
        speak_response(last_response)
        return interactive_prompt(last_response)  # ask again after replay
    elif choice.startswith("n"):
        print(f"  {C.MAGENTA}Note:{C.RESET} ", end="")
        try:
            note = input().strip()
        except (EOFError, KeyboardInterrupt):
            return "quit", ""
        return "note", note
    elif choice.startswith("f"):
        print(f"  {C.MAGENTA}Reason:{C.RESET} ", end="")
        try:
            reason = input().strip()
        except (EOFError, KeyboardInterrupt):
            return "quit", ""
        return "fail", reason
    elif choice.startswith("q"):
        return "quit", ""
    elif choice.startswith("s"):
        return "skip", ""
    else:
        return "pass", ""


# ===========================================================================
# Filter tests
# ===========================================================================

def filter_tests(tests, args):
    """Filter tests by CLI args."""
    selected = tests

    if args.id:
        selected = [t for t in selected if t.id == args.id]
    elif args.phase:
        selected = [t for t in selected if t.phase == args.phase]
    elif args.category:
        cat_lower = args.category.lower()
        selected = [t for t in selected if cat_lower in t.category.lower()]

    return selected


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Interactive Response Quality Tester")
    parser.add_argument("--batch", action="store_true", help="Non-interactive (auto checks only)")
    parser.add_argument("--no-tts", action="store_true", help="Skip TTS playback (text review only)")
    parser.add_argument("--phase", type=str, help="Run specific phase (e.g., 5A)")
    parser.add_argument("--id", type=str, help="Run specific test (e.g., 5A-01)")
    parser.add_argument("--category", type=str, help="Filter by category name")
    parser.add_argument("--json", action="store_true", help="JSON output (batch mode)")
    args = parser.parse_args()

    selected = filter_tests(TESTS, args)
    if not selected:
        print("No tests match the given filters.")
        return 1

    print("=" * 65)
    print("  JARVIS Response Quality Tester")
    if not args.batch:
        print("  Interactive mode — review each response")
    print("=" * 65)

    # Check llama-server for LLM tests
    needs_llm = any(t.mode == "full" for t in selected)
    llm_ready = False
    if needs_llm:
        try:
            req = urllib.request.Request("http://127.0.0.1:8080/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                llm_ready = data.get("status") == "ok"
        except Exception:
            pass

        if not llm_ready:
            # Filter to normalize-only tests
            llm_tests = [t for t in selected if t.mode == "full"]
            print(f"\n{C.YELLOW}llama-server not reachable — "
                  f"skipping {len(llm_tests)} full-pipeline tests{C.RESET}")
            selected = [t for t in selected if t.mode != "full"]
            if not selected:
                print("No tests remaining.")
                return 1

    # Load components
    components = None
    if any(t.mode == "full" for t in selected):
        try:
            components = init_components()
        except Exception as e:
            print(f"\n{C.RED}Failed to load components: {e}{C.RESET}")
            selected = [t for t in selected if t.mode == "normalize"]
            if not selected:
                return 1

    # Initialize TTS for playback (unless --no-tts or --batch)
    use_tts = not args.no_tts and not args.batch
    if use_tts:
        config = components["config"] if components else None
        if not config:
            from core.config import load_config
            config = load_config()
        init_tts(config)

    # Run tests
    results_log = []
    current_phase = None
    passed = failed = skipped = noted = 0

    for test in selected:
        # Phase header
        if test.phase != current_phase:
            current_phase = test.phase
            phase_tests = [t for t in selected if t.phase == test.phase]
            print(f"\n{'─' * 65}")
            print(f"  Phase {test.phase}: {test.category} ({len(phase_tests)} tests)")
            print(f"{'─' * 65}")

        # Run
        result = run_test(test, components or {})

        # Display
        has_issues = display_result(test, result)

        # TTS playback — speak the response so reviewer can hear it
        raw_resp = result["raw_response"]
        if use_tts and raw_resp and not raw_resp.startswith("("):
            print(f"\n  {C.DIM}🔊 Speaking...{C.RESET}")
            speak_response(raw_resp)

        # Interactive verdict
        if not args.batch:
            verdict, reason = interactive_prompt(raw_resp)
            if verdict == "quit":
                print(f"\n{C.YELLOW}Stopped early.{C.RESET}")
                break
            elif verdict == "fail":
                failed += 1
                result["verdict"] = "fail"
                result["verdict_reason"] = reason
            elif verdict == "note":
                noted += 1
                passed += 1
                result["verdict"] = "note"
                result["verdict_note"] = reason
            elif verdict == "skip":
                skipped += 1
                result["verdict"] = "skip"
            else:
                passed += 1
                result["verdict"] = "pass"
        else:
            # Batch mode: auto-check determines pass/fail
            if result["auto_failures"]:
                failed += 1
                result["verdict"] = "fail"
            else:
                passed += 1
                result["verdict"] = "pass"

        results_log.append(result)

    # Summary
    total = passed + failed + skipped
    print(f"\n{'=' * 65}")
    print(f"  Results: {C.GREEN}{passed} pass{C.RESET} / "
          f"{C.RED}{failed} fail{C.RESET} / "
          f"{C.YELLOW}{skipped} skip{C.RESET} / "
          f"{total} total")
    if noted:
        print(f"  ({noted} passed with notes)")
    print(f"{'=' * 65}")

    # JSON output
    if args.json:
        clean = []
        for r in results_log:
            clean.append({
                "id": r["id"],
                "input": r["input"],
                "source": r["source"],
                "response": r["raw_response"][:200],
                "normalized": r["normalized"][:200],
                "timing_ms": round(r["timing_ms"]),
                "auto_failures": r["auto_failures"],
                "auto_warnings": r["auto_warnings"],
                "verdict": r.get("verdict", ""),
            })
        print(json.dumps(clean, indent=2))

    # Show failures for review
    fail_results = [r for r in results_log if r.get("verdict") == "fail"]
    if fail_results:
        print(f"\n{C.RED}Failed tests:{C.RESET}")
        for r in fail_results:
            reason = r.get("verdict_reason", "") or "; ".join(r["auto_failures"])
            print(f"  {r['id']}: {reason}")

    # Show notes
    note_results = [r for r in results_log if r.get("verdict") == "note"]
    if note_results:
        print(f"\n{C.YELLOW}Notes:{C.RESET}")
        for r in note_results:
            print(f"  {r['id']}: {r.get('verdict_note', '')}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
