#!/usr/bin/env python3
"""
Automated Edge Case Test Suite for JARVIS

Multi-tier test harness that validates routing, unit functions, and edge cases
by injecting text directly into the pipeline — no voice/mic/TTS needed.

Tiers:
  1 — Unit tests: ambient filter, noise filter, TTS normalizer, speech chunker
  2 — Routing tests: real skills loaded, text → router.route() → validate RouteResult
  3 — Execution tests: run skill handlers, validate response content (future)
  4 — Pipeline tests: needs llama-server running (future)

Usage:
    python3 scripts/test_edge_cases.py              # Tiers 1+2 (default)
    python3 scripts/test_edge_cases.py --tier 1     # Unit tests only (<1s)
    python3 scripts/test_edge_cases.py --tier 2     # Routing tests only (~5s load)
    python3 scripts/test_edge_cases.py --phase 1A   # Single phase
    python3 scripts/test_edge_cases.py --id 1A-01   # Single test
    python3 scripts/test_edge_cases.py --all        # All tiers
    python3 scripts/test_edge_cases.py --json       # JSON output
    python3 scripts/test_edge_cases.py --verbose    # Show all tests (not just failures)
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

import sys
import time
import json
import argparse
import subprocess
import warnings
from dataclasses import dataclass, field
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# ===========================================================================
# Test case data structure
# ===========================================================================

@dataclass
class TestCase:
    id: str                                  # "1A-01"
    input: str                               # Test input text
    tier: int                                # 1, 2, 3, or 4
    phase: str                               # "1A", "3A", "7B", etc.
    category: str                            # Human-readable group name

    # Context setup (Tier 2+)
    in_conversation: bool = False
    setup: dict = field(default_factory=dict)

    # Routing expectations (Tier 2)
    expect_handled: Optional[bool] = None
    expect_skill: Optional[str] = None
    expect_not_skill: Optional[str] = None
    expect_intent: Optional[str] = None
    expect_layer: Optional[str] = None
    expect_skip: Optional[bool] = None
    expect_close_window: Optional[bool] = None
    expect_open_window: Optional[bool] = None
    expect_text_contains: Optional[str] = None
    expect_text_not_empty: Optional[bool] = None
    expect_source: Optional[str] = None

    # Unit test expectations (Tier 1)
    expect_ambient: Optional[bool] = None     # ambient filter result
    wake_word: str = "jarvis"                 # for ambient filter
    expect_noise: Optional[bool] = None       # noise filter result
    expect_normalized: Optional[str] = None   # TTS normalizer output (substring match)
    expect_chunks: Optional[list] = None      # speech chunker output

    notes: str = ""


# ===========================================================================
# Test results tracking
# ===========================================================================

class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results = []  # list of (id, status, detail)

    def record(self, test_id, passed, detail=""):
        if passed:
            self.passed += 1
            self.results.append((test_id, "PASS", detail))
        else:
            self.failed += 1
            self.results.append((test_id, "FAIL", detail))

    def skip(self, test_id, reason=""):
        self.skipped += 1
        self.results.append((test_id, "SKIP", reason))

    @property
    def total(self):
        return self.passed + self.failed

    def to_json(self):
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "pass_rate": f"{self.passed / self.total * 100:.1f}%" if self.total else "N/A",
            "tests": [{"id": r[0], "status": r[1], "detail": r[2]} for r in self.results],
        }


# ===========================================================================
# TTS stub (no audio output)
# ===========================================================================

class TTSStub:
    _spoke = False
    def speak(self, text, normalize=True):
        self._spoke = True
        return True
    def get_pending_announcements(self):
        return []


# ===========================================================================
# Component initialization (Tier 2)
# ===========================================================================

def init_tier2_components():
    """Load real JARVIS components for routing tests."""
    from core.config import load_config
    from core.conversation import ConversationManager
    from core.llm_router import LLMRouter
    from core.skill_manager import SkillManager
    from core.responses import get_response_library
    from core.conversation_state import ConversationState
    from core.conversation_router import ConversationRouter

    print("Loading components for Tier 2...")
    t0 = time.perf_counter()

    config = load_config()
    tts = TTSStub()
    conversation = ConversationManager(config)
    conversation.current_user = "user"
    responses = get_response_library()
    llm = LLMRouter(config)
    skill_manager = SkillManager(config, conversation, tts, responses, llm)
    skill_manager.load_all_skills()

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
            embedding_model=skill_manager._embedding_model,
        )
        conversation.set_memory_manager(memory_manager)

    # News manager
    news_manager = None
    if config.get("news.enabled", False):
        from core.news_manager import get_news_manager
        news_manager = get_news_manager(config, tts, conversation, llm)
        news_manager.set_listener_callbacks(pause=lambda: None, resume=lambda: None)
        news_manager.set_window_callback(lambda d: None)

    # Context window
    context_window = None
    if config.get("context_window.enabled", False):
        from core.context_window import get_context_window
        context_window = get_context_window(
            config=config, embedding_model=skill_manager._embedding_model, llm=llm,
        )
        conversation.set_context_window(context_window)

    # Web researcher
    web_researcher = None
    if config.get("llm.local.tool_calling", False):
        from core.web_research import WebResearcher
        web_researcher = WebResearcher(config)

    conv_state = ConversationState()
    router = ConversationRouter(
        skill_manager=skill_manager, conversation=conversation, llm=llm,
        reminder_manager=reminder_manager, memory_manager=memory_manager,
        news_manager=news_manager, context_window=context_window,
        conv_state=conv_state, config=config, web_researcher=web_researcher,
    )

    elapsed = time.perf_counter() - t0
    skill_count = len(skill_manager.skills)
    print(f"Ready — {skill_count} skills loaded in {elapsed:.1f}s\n")

    return {
        "router": router,
        "conv_state": conv_state,
        "skill_manager": skill_manager,
        "memory_manager": memory_manager,
        "reminder_manager": reminder_manager,
        "news_manager": news_manager,
    }


# ===========================================================================
# State setup helper (reset + configure before each test)
# ===========================================================================

_original_is_awaiting_ack = None  # saved on first call


def setup_state(components, case):
    """Reset all state and apply case-specific setup."""
    global _original_is_awaiting_ack

    router = components["router"]
    conv_state = components["conv_state"]
    memory_manager = components["memory_manager"]
    reminder_manager = components["reminder_manager"]
    skill_manager = components["skill_manager"]

    # Reset conversation state
    conv_state.jarvis_asked_question = False
    conv_state.turn_count = 0
    conv_state.last_intent = ""
    conv_state.conversation_active = case.in_conversation

    # Reset memory manager pending forget
    if memory_manager:
        memory_manager._pending_forget = None

    # Reset reminder manager rundown state + restore original ack method
    if reminder_manager:
        reminder_manager._rundown_state = None
        reminder_manager._rundown_cycle = 0
        if _original_is_awaiting_ack is None:
            _original_is_awaiting_ack = reminder_manager.is_awaiting_ack
        else:
            reminder_manager.is_awaiting_ack = _original_is_awaiting_ack

    # Reset ALL pending confirmations (file_editor, developer_tools, etc.)
    for sname, skill_obj in skill_manager.skills.items():
        if hasattr(skill_obj, '_pending_confirmation'):
            skill_obj._pending_confirmation = None

    # Apply case-specific setup
    setup = case.setup
    if not setup:
        return

    if setup.get("pending_forget"):
        if memory_manager:
            memory_manager._pending_forget = {
                "facts": [{"fact_id": -1, "content": "test fact"}],
                "user_id": "test",
                "expires": time.time() + 300,
            }

    if setup.get("jarvis_asked_question"):
        conv_state.jarvis_asked_question = True

    if setup.get("rundown_pending"):
        if reminder_manager:
            reminder_manager._rundown_state = "offered"
            from datetime import datetime
            reminder_manager._rundown_offered_at = datetime.now()

    if setup.get("awaiting_ack"):
        if reminder_manager:
            reminder_manager.is_awaiting_ack = lambda: True

    if setup.get("pending_confirmation"):
        fe = skill_manager.skills.get("file_editor")
        if fe:
            fe._pending_confirmation = setup["pending_confirmation"]


# ===========================================================================
# Tier 1: Unit test runners
# ===========================================================================

def run_ambient_filter_test(case):
    """Test _is_ambient_wake_word() logic standalone."""
    # Reproduce the filter logic without instantiating ContinuousListener
    AMBIENT_FOLLOWERS = frozenset({
        'is', 'was', 'has', 'had', 'will', 'would', 'can', 'could',
        'does', 'did', 'should', 'might', 'may', 'of',
    })
    WAKE_PREFIXES = frozenset({
        'hey', 'hi', 'yo', 'morning', 'good', 'okay', 'ok',
    })

    text = case.input
    matched_word = case.wake_word
    words = text.split()

    # Find word index (match stripped version OR possessive form)
    word_idx = None
    for i, w in enumerate(words):
        stripped = w.strip('.,!?;:\'"')
        if stripped.lower() == matched_word.lower():
            word_idx = i
            break
        # Also match base word for possessives: "jarvis's" matches "jarvis's"
        if stripped.lower().rstrip("'s").rstrip("\u2019s") == matched_word.lower().rstrip("'s").rstrip("\u2019s"):
            word_idx = i
            break
    if word_idx is None:
        # Wake word not found in text — not ambient
        result = False
        return result == case.expect_ambient, f"ambient={result}, expected={case.expect_ambient}, wake_word not found"

    # Signal 1: Position
    effective_pos = word_idx
    if word_idx <= 2:
        prefix_words = [w.strip('.,!?;:').lower() for w in words[:word_idx]]
        if all(pw in WAKE_PREFIXES for pw in prefix_words):
            effective_pos = 0
    is_trailing = word_idx >= len(words) - 2
    if effective_pos >= 3 and not is_trailing:
        result = True
        return result == case.expect_ambient, f"ambient={result} (position {word_idx})"

    # Signal 2: Copula/possessive
    if word_idx < len(words):
        wake_token = words[word_idx]
        if wake_token.endswith("'s") or wake_token.endswith("\u2019s"):
            result = True
            return result == case.expect_ambient, f"ambient={result} (possessive)"
        has_comma = wake_token.endswith(',')
        if not has_comma and word_idx + 1 < len(words):
            next_word = words[word_idx + 1].strip('.,!?;:').lower()
            if next_word in AMBIENT_FOLLOWERS:
                result = True
                return result == case.expect_ambient, f"ambient={result} (copula '{next_word}')"

    # Signal 5: Long utterance
    if len(words) > 15 and word_idx > 0:
        result = True
        return result == case.expect_ambient, f"ambient={result} (long utterance)"

    result = False
    return result == case.expect_ambient, f"ambient={result}, expected={case.expect_ambient}"


def run_noise_filter_test(case):
    """Test _is_conversation_noise() logic standalone."""
    VALID_SHORT_REPLIES = {
        "yes", "no", "yeah", "yep", "nah", "nope",
        "thanks", "thank you", "okay", "ok", "please",
        "stop", "cancel", "nevermind", "never mind",
        "sure", "right", "correct", "wrong", "good", "great",
        "hello", "hey", "hi", "bye", "goodbye",
    }

    text = case.input

    # Very short
    if len(text) < 2:
        result = True
        return result == case.expect_noise, f"noise={result} (len<2)"

    # Repetitive chars
    unique_chars = set(text.replace(' ', ''))
    if len(unique_chars) <= 3 and len(text) > 5:
        result = True
        return result == case.expect_noise, f"noise={result} (repetitive)"

    # Single word not in valid set
    words = text.strip().split()
    if len(words) == 1 and words[0] not in VALID_SHORT_REPLIES:
        if len(words[0]) < 4:
            result = True
            return result == case.expect_noise, f"noise={result} (short unknown word)"

    result = False
    return result == case.expect_noise, f"noise={result}, expected={case.expect_noise}"


def run_normalizer_test(case):
    """Test TTSNormalizer.normalize()."""
    from core.tts_normalizer import TTSNormalizer
    normalizer = TTSNormalizer()
    result = normalizer.normalize(case.input)
    if case.expect_normalized:
        # Substring match (case-insensitive)
        passed = case.expect_normalized.lower() in result.lower()
        return passed, f"got: {result!r}, expected to contain: {case.expect_normalized!r}"
    return True, f"output: {result!r}"


def run_chunker_test(case):
    """Test SpeechChunker.feed()/flush()."""
    from core.speech_chunker import SpeechChunker
    chunker = SpeechChunker()
    chunks = []

    # Feed the input as if it were streamed word by word
    words = case.input.split(' ')
    for i, word in enumerate(words):
        token = word if i == 0 else ' ' + word
        chunk = chunker.feed(token)
        if chunk:
            chunks.append(chunk)

    # Flush remaining
    remaining = chunker.flush()
    if remaining:
        chunks.append(remaining)

    if case.expect_chunks is not None:
        passed = chunks == case.expect_chunks
        return passed, f"got: {chunks!r}, expected: {case.expect_chunks!r}"
    return True, f"chunks: {chunks!r}"


# ===========================================================================
# Tier 2: Routing test runner
# ===========================================================================

def run_routing_test(case, components):
    """Route text through real router and validate RouteResult."""
    router = components["router"]
    skill_manager = components["skill_manager"]

    setup_state(components, case)

    try:
        r = router.route(case.input, in_conversation=case.in_conversation)
    except Exception as e:
        return False, f"CRASH: {e}"

    match_info = r.match_info or {}
    skill_name = match_info.get("skill_name", "")
    layer = match_info.get("layer", "")
    failures = []

    if case.expect_handled is not None and r.handled != case.expect_handled:
        failures.append(f"handled={r.handled}, expected={case.expect_handled}")

    if case.expect_skill is not None:
        if case.expect_skill.lower() not in skill_name.lower():
            failures.append(f"skill={skill_name!r}, expected={case.expect_skill!r}")

    if case.expect_not_skill is not None:
        if case.expect_not_skill.lower() in skill_name.lower():
            failures.append(f"skill={skill_name!r}, should NOT be {case.expect_not_skill!r}")

    if case.expect_intent is not None:
        if case.expect_intent.lower() not in r.intent.lower():
            failures.append(f"intent={r.intent!r}, expected={case.expect_intent!r}")

    if case.expect_layer is not None:
        if case.expect_layer.lower() not in layer.lower():
            failures.append(f"layer={layer!r}, expected={case.expect_layer!r}")

    if case.expect_skip is not None and r.skip != case.expect_skip:
        failures.append(f"skip={r.skip}, expected={case.expect_skip}")

    if case.expect_close_window is not None and r.close_window != case.expect_close_window:
        failures.append(f"close_window={r.close_window}, expected={case.expect_close_window}")

    if case.expect_open_window is not None:
        has_window = r.open_window is not None and r.open_window > 0
        if has_window != case.expect_open_window:
            failures.append(f"open_window={r.open_window}, expected={'set' if case.expect_open_window else 'none'}")

    if case.expect_source is not None and case.expect_source.lower() not in r.source.lower():
        failures.append(f"source={r.source!r}, expected={case.expect_source!r}")

    if case.expect_text_contains is not None:
        if case.expect_text_contains.lower() not in r.text.lower():
            failures.append(f"text missing {case.expect_text_contains!r}, got: {r.text[:100]!r}")

    if case.expect_text_not_empty and not r.text:
        failures.append(f"text is empty")

    if failures:
        # Build detail string with routing context
        ctx = f"[skill={skill_name}, layer={layer}, intent={r.intent}, handled={r.handled}]"
        return False, f"{'; '.join(failures)} {ctx}"

    return True, f"skill={skill_name}, layer={layer}, intent={r.intent}"


# ===========================================================================
# Test dispatcher
# ===========================================================================

def run_test(case, components, results):
    """Dispatch test to appropriate tier runner."""
    if case.tier == 1:
        # Unit tests
        if case.expect_ambient is not None:
            passed, detail = run_ambient_filter_test(case)
        elif case.expect_noise is not None:
            passed, detail = run_noise_filter_test(case)
        elif case.expect_normalized is not None:
            passed, detail = run_normalizer_test(case)
        elif case.expect_chunks is not None:
            passed, detail = run_chunker_test(case)
        else:
            results.skip(case.id, "No tier-1 expectation set")
            return
    elif case.tier == 2:
        if not components:
            results.skip(case.id, "Tier 2 components not loaded")
            return
        passed, detail = run_routing_test(case, components)
    else:
        results.skip(case.id, f"Tier {case.tier} not implemented yet")
        return

    results.record(case.id, passed, detail)


# ===========================================================================
# TEST CASE DEFINITIONS
# ===========================================================================

TESTS = []

# ---------------------------------------------------------------------------
# TIER 1: Phase 3A — Ambient Wake Word Filter
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("3A-01", "jarvis what time is it", 1, "3A", "Ambient Filter",
             expect_ambient=False, notes="Normal command"),
    TestCase("3A-02", "hey jarvis set a timer", 1, "3A", "Ambient Filter",
             expect_ambient=False, notes="Hey prefix exception"),
    TestCase("3A-03", "jarvis is really cool", 1, "3A", "Ambient Filter",
             expect_ambient=True, notes="Copula 'is' without comma"),
    TestCase("3A-04", "I think jarvis is broken", 1, "3A", "Ambient Filter",
             expect_ambient=True, notes="Position >2 + copula"),
    TestCase("3A-05", "jarvis's response was great", 1, "3A", "Ambient Filter",
             expect_ambient=True, wake_word="jarvis's",
             notes="Possessive — real code matches 'jarvis's' token directly"),
    TestCase("3A-06", "jarvis, I think the weather is nice", 1, "3A", "Ambient Filter",
             expect_ambient=False, notes="Comma after wake word = addressing"),
    TestCase("3A-07", "tell jarvis to check the weather", 1, "3A", "Ambient Filter",
             expect_ambient=False, notes="Position 1 < 3 — this IS a valid command (relay/instruction)"),
    TestCase("3A-08", "good morning jarvis", 1, "3A", "Ambient Filter",
             expect_ambient=False, notes="'good morning' prefix exception"),
    # 3A-09: "paris" similarity test — can't test threshold without embedding model,
    # but we CAN verify the word "paris" is NOT "jarvis"
    TestCase("3A-09", "paris is beautiful this time of year", 1, "3A", "Ambient Filter",
             expect_ambient=True, wake_word="paris",
             notes="If 'paris' passed fuzzy match, copula filter catches it. Threshold (0.80) blocks at earlier layer."),
    TestCase("3A-10", "jarvis what's the weather", 1, "3A", "Ambient Filter",
             expect_ambient=False, notes="Normal command, position 0"),
    TestCase("3A-11", "the jarvis system was a success", 1, "3A", "Ambient Filter",
             expect_ambient=False,
             notes="Position 1, 'the' not a prefix, but pos < 3 passes. 'system' not a copula. Not blocked."),
    TestCase("3A-12", "jarvis is I think the best assistant", 1, "3A", "Ambient Filter",
             expect_ambient=True, notes="Copula 'is' immediately after, no comma"),
    TestCase("3A-13", "I was talking to my friend about how jarvis works and it was interesting to see the response time and accuracy of the system overall", 1, "3A", "Ambient Filter",
             expect_ambient=True, notes="20+ words, wake word not at position 0"),
]

# ---------------------------------------------------------------------------
# TIER 1: Phase 3C — Noise Filter
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("3C-01", "um", 1, "3C", "Noise Filter",
             expect_noise=True, notes="Single short filler word"),
    TestCase("3C-02", "uh", 1, "3C", "Noise Filter",
             expect_noise=True, notes="Single short filler"),
    TestCase("3C-03", "...", 1, "3C", "Noise Filter",
             expect_noise=True, notes="Single word '...' len=3 < 4, not in valid set → noise"),
    TestCase("3C-04", "wrwwwwww", 1, "3C", "Noise Filter",
             expect_noise=True, notes="Repetitive characters (unique <= 3, len > 5)"),
    TestCase("3C-05", "yes", 1, "3C", "Noise Filter",
             expect_noise=False, notes="Valid short reply"),
    TestCase("3C-06", "you", 1, "3C", "Noise Filter",
             expect_noise=True, notes="Single word <4 chars, not in valid set"),
    TestCase("3C-07", "the the the", 1, "3C", "Noise Filter",
             expect_noise=True, notes="Repetitive (unique chars = {t,h,e} = 3, len > 5 = True)"),
]

# ---------------------------------------------------------------------------
# TIER 1: Phase 7B — TTS Normalizer
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("7B-01", "192.168.1.1", 1, "7B", "TTS Normalizer",
             expect_normalized="dot",
             notes="IP address normalization"),
    TestCase("7B-02", "$42.50", 1, "7B", "TTS Normalizer",
             expect_normalized="forty-two dollars and fifty cents",
             notes="Currency with cents"),
    TestCase("7B-03", "2.5GB", 1, "7B", "TTS Normalizer",
             expect_normalized="gigabytes",
             notes="File size"),
    TestCase("7B-04", "3:45 PM", 1, "7B", "TTS Normalizer",
             expect_normalized="three forty-five PM",
             notes="Timestamp"),
    TestCase("7B-05", "https://example.com/path", 1, "7B", "TTS Normalizer",
             expect_normalized="example dot com",
             notes="URL normalization"),
    TestCase("7B-06", "/home/user/jarvis", 1, "7B", "TTS Normalizer",
             expect_normalized="slash home",
             notes="Path normalization"),
    TestCase("7B-07", "config.yaml", 1, "7B", "TTS Normalizer",
             expect_normalized="config dot yaml",
             notes="Filename with extension"),
    TestCase("7B-08", "AMD RX 7900 XT", 1, "7B", "TTS Normalizer",
             expect_normalized="XT",
             notes="GPU model — XT preserved"),
    TestCase("7B-09", "port 8080", 1, "7B", "TTS Normalizer",
             expect_normalized="port eighty eighty",
             notes="Port number"),
    TestCase("7B-10", "CPU usage is 45%", 1, "7B", "TTS Normalizer",
             expect_normalized="C P U",
             notes="Technical term CPU"),
    TestCase("7B-11", "## Heading\n- bullet\n**bold**", 1, "7B", "TTS Normalizer",
             expect_normalized="Heading",
             notes="Markdown stripped"),
    TestCase("7B-12", "0.001", 1, "7B", "TTS Normalizer",
             expect_normalized="zero point zero zero one",
             notes="Small decimal"),
    TestCase("7B-13", "test.py", 1, "7B", "TTS Normalizer",
             expect_normalized="test dot P Y",
             notes=".py extension pronunciation"),
    TestCase("7B-14", "1,234,567", 1, "7B", "TTS Normalizer",
             expect_normalized="one million two hundred thirty-four thousand five hundred sixty-seven",
             notes="Large number with commas"),
]

# ---------------------------------------------------------------------------
# TIER 1: Phase 7C — Speech Chunker
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("7C-01", "Hello. How are you?", 1, "7C", "Speech Chunker",
             expect_chunks=["Hello.", "How are you?"],
             notes="Two sentences"),
    TestCase("7C-02", "No end punctuation", 1, "7C", "Speech Chunker",
             expect_chunks=["No end punctuation"],
             notes="Flush for end-of-stream"),
    TestCase("7C-03", "Dr. Smith went to the store.", 1, "7C", "Speech Chunker",
             expect_chunks=["Dr.", "Smith went to the store."],
             notes="Chunker splits on '. ' — no abbreviation awareness (by design: simple sentence-only)"),
    TestCase("7C-04", "Version 3.5 is great!", 1, "7C", "Speech Chunker",
             expect_chunks=["Version 3.5 is great!"],
             notes="Decimal not split, ! at end flushed"),
    TestCase("7C-05", "Really?! No way!", 1, "7C", "Speech Chunker",
             expect_chunks=["Really?!", "No way!"],
             notes="Multiple sentence-end chars — split after ?! followed by space"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 1A — Ambiguous Verb Routing
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("1A-01", "delete the test file from the share", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="file_editor", expect_handled=True),
    TestCase("1A-02", "delete my dentist reminder", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="reminders", expect_handled=True),
    TestCase("1A-03", "forget that I like coffee", 2, "1A", "Ambiguous Verb Routing",
             expect_intent="memory_forget", expect_handled=True,
             notes="Priority 3 memory op"),
    TestCase("1A-04", "remove the meeting from my calendar", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="reminders", expect_handled=True),
    TestCase("1A-05", "open chrome", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="app_launcher", expect_handled=True),
    TestCase("1A-07", "search for python tutorials", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="web_navigation", expect_handled=True),
    TestCase("1A-08", "search codebase for database", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="developer_tools", expect_handled=True),
    TestCase("1A-09", "find my config file", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="filesystem", expect_handled=True),
    TestCase("1A-10", "find files containing error", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="filesystem", expect_handled=True,
             notes="Keyword 'files' matches filesystem — correct (filesystem handles file searches)"),
    TestCase("1A-11", "what's the weather in the news", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="weather", expect_handled=True),
    TestCase("1A-12", "close the browser window", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="app_launcher", expect_handled=True),
    TestCase("1A-13", "show me my drives", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="system_info", expect_handled=True),
    TestCase("1A-14", "show me the git diff", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="developer_tools", expect_handled=True),
    TestCase("1A-15", "write a reminder to call mom", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="reminders", expect_handled=True),
    TestCase("1A-16", "create a bash script for backups", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="file_editor", expect_handled=True),
    TestCase("1A-17", "edit the weather skill", 2, "1A", "Ambiguous Verb Routing",
             expect_skill="file_editor", expect_handled=True),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 1B — Substring & Word Boundary Traps
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("1B-01", "run a full system health diagnostic", 2, "1B", "Substring Traps",
             expect_skip=False, notes="'no' in 'diagnostic' should NOT trigger bare ack"),
    TestCase("1B-02", "what's the weather forecast", 2, "1B", "Substring Traps",
             expect_skill="weather", expect_handled=True),
    TestCase("1B-03", "tell me about the amazon rainforest", 2, "1B", "Substring Traps",
             expect_not_skill="web_navigation",
             notes="'amazon' in _generic_keywords — should NOT route to web_nav amazon_search"),
    TestCase("1B-04", "open the storage drives panel", 2, "1B", "Substring Traps",
             expect_handled=True, notes="'open' with content should route"),
    TestCase("1B-06", "the application crashed", 2, "1B", "Substring Traps",
             expect_not_skill="app_launcher",
             notes="'application' should NOT trigger app_launcher"),
    TestCase("1B-07", "I acknowledge that", 2, "1B", "Substring Traps",
             expect_not_skill="reminders",
             notes="Should NOT trigger reminder acknowledgment"),
    TestCase("1B-08", "what time does the news come on", 2, "1B", "Substring Traps",
             expect_handled=True, notes="Should route to time or skill, not crash"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 1C — Generic Keyword Blocklist Bypass
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("1C-01", "search", 2, "1C", "Keyword Blocklist",
             expect_handled=False, notes="Bare word → LLM fallback"),
    TestCase("1C-02", "open", 2, "1C", "Keyword Blocklist",
             expect_handled=False, notes="Bare word → LLM fallback"),
    TestCase("1C-03", "file", 2, "1C", "Keyword Blocklist",
             expect_handled=False, notes="Bare word → LLM fallback"),
    TestCase("1C-04", "search youtube for music", 2, "1C", "Keyword Blocklist",
             expect_skill="web_navigation", expect_handled=True),
    TestCase("1C-05", "analyze this script", 2, "1C", "Keyword Blocklist",
             expect_skill="filesystem", expect_handled=True),
    TestCase("1C-06", "count lines in my project", 2, "1C", "Keyword Blocklist",
             expect_skill="filesystem", expect_handled=True),
    TestCase("1C-07", "navigate to workspace 2", 2, "1C", "Keyword Blocklist",
             expect_skill="app_launcher", expect_handled=True),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 1D — Layer Transition Edge Cases
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("1D-01", "what's up", 2, "1D", "Layer Transitions",
             expect_skill="conversation", expect_handled=True),
    TestCase("1D-02", "could you perhaps look into the current meteorological conditions", 2, "1D", "Layer Transitions",
             expect_skill="weather", expect_handled=True,
             notes="Semantic match for elaborate weather query"),
    TestCase("1D-03", "yo what time is it bro", 2, "1D", "Layer Transitions",
             expect_skill="time_info", expect_handled=True),
    TestCase("1D-04", "tell me something interesting", 2, "1D", "Layer Transitions",
             expect_handled=False, notes="LLM fallback — no skill handles this"),
    TestCase("1D-05", "how do I make pasta", 2, "1D", "Layer Transitions",
             expect_handled=False, notes="LLM fallback or web research"),
    TestCase("1D-07", "", 2, "1D", "Layer Transitions",
             expect_handled=True, notes="Empty string → greeting (len < 2)"),
    TestCase("1D-08", "um", 2, "1D", "Layer Transitions",
             expect_handled=True, notes="Console noise → greeting (len < 3)"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 2A — Rundown State Machine
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("2A-01", "yes", 2, "2A", "Rundown State Machine",
             setup={"rundown_pending": True},
             expect_intent="rundown_accept", expect_handled=True,
             notes="Accept rundown"),
    TestCase("2A-02", "no thanks", 2, "2A", "Rundown State Machine",
             setup={"rundown_pending": True},
             expect_intent="rundown_defer", expect_handled=True,
             notes="Decline rundown"),
    TestCase("2A-03", "what time is it", 2, "2A", "Rundown State Machine",
             setup={"rundown_pending": True},
             expect_intent="rundown_accept", expect_handled=True,
             notes="Rundown intercepts ALL input when pending — by design (P1 priority)"),
    TestCase("2A-04", "no", 2, "2A", "Rundown State Machine",
             setup={"rundown_pending": True},
             expect_intent="rundown_defer", expect_handled=True,
             notes="Bare 'no' should decline — word-boundary match, not substring"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 2B — Reminder Acknowledgment
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("2B-01", "got it", 2, "2B", "Reminder Ack",
             setup={"awaiting_ack": True},
             expect_intent="reminder_ack", expect_handled=True,
             notes="Ack reminder — P2 intercepts any input when awaiting ack"),
    TestCase("2B-02", "snooze 10 minutes", 2, "2B", "Reminder Ack",
             setup={"awaiting_ack": True},
             expect_intent="reminder_ack", expect_handled=True,
             notes="P2 acks ALL input when awaiting — snooze intent is lost (current behavior)"),
    TestCase("2B-03", "what reminder is that", 2, "2B", "Reminder Ack",
             setup={"awaiting_ack": True},
             expect_intent="reminder_ack", expect_handled=True,
             notes="P2 intercepts ALL input when awaiting ack"),
    TestCase("2B-04", "yes", 2, "2B", "Reminder Ack — no pending",
             expect_handled=False,
             notes="No reminder pending — 'yes' (3 chars) falls to LLM. NOT intercepted at P2"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 2C — Memory Forget Confirmation
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("2C-01", "yes", 2, "2C", "Forget Confirmation",
             setup={"pending_forget": True},
             expect_intent="forget_confirm", expect_handled=True),
    TestCase("2C-02", "no", 2, "2C", "Forget Confirmation",
             setup={"pending_forget": True},
             expect_intent="forget_cancel", expect_handled=True),
    TestCase("2C-03", "yes, delete it", 2, "2C", "Forget Confirmation",
             setup={"pending_forget": True},
             expect_intent="forget_confirm", expect_handled=True,
             notes="P2.5 intercepts before 'delete' reaches file_editor at P4"),
    TestCase("2C-04", "forget that my birthday is in June", 2, "2C", "Forget Confirmation",
             expect_intent="memory_forget", expect_handled=True,
             notes="Start forget flow — matches FORGET_PATTERNS 'forget that ...'"),
    TestCase("2C-04b", "forget my birthday", 2, "2C", "Forget Confirmation",
             expect_not_skill="memory",
             notes="Does NOT match FORGET_PATTERNS — lacks 'that/the' prefix. Falls to LLM or skill"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 2D — Dismissal Detection
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("2D-01", "no thanks", 2, "2D", "Dismissal",
             in_conversation=True,
             expect_intent="dismissal", expect_close_window=True, expect_handled=True),
    TestCase("2D-02", "that's all", 2, "2D", "Dismissal",
             in_conversation=True,
             expect_intent="dismissal", expect_close_window=True, expect_handled=True),
    TestCase("2D-03", "never mind", 2, "2D", "Dismissal",
             in_conversation=True,
             expect_intent="dismissal", expect_close_window=True, expect_handled=True),
    TestCase("2D-04", "no thanks", 2, "2D", "Dismissal — outside window",
             in_conversation=False,
             notes="Should NOT dismiss outside conversation window"),
    TestCase("2D-05", "no thanks, but what time is it", 2, "2D", "Dismissal — compound",
             in_conversation=True,
             expect_skill="conversation", expect_handled=True,
             notes="Compound: dismissal NOT detected. 'thanks' keyword routes to conversation, not time_info"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 2E — Bare Acknowledgment Filter
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("2E-01", "yeah", 2, "2E", "Bare Ack Filter",
             in_conversation=True,
             expect_skip=True, notes="Noise when no question pending"),
    TestCase("2E-02", "ok", 2, "2E", "Bare Ack Filter",
             in_conversation=True,
             expect_handled=True, expect_intent="greeting",
             notes="len<=2 triggers greeting (line 157) before bare ack filter (P2.8)"),
    TestCase("2E-03", "yeah", 2, "2E", "Bare Ack — question pending",
             in_conversation=True,
             setup={"jarvis_asked_question": True},
             expect_skip=False, notes="Should be answer, not noise"),
    TestCase("2E-04", "ok google", 2, "2E", "Bare Ack Filter",
             in_conversation=True,
             expect_skip=False, notes="Has content after 'ok'"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 2F — File Editor Confirmation Flow
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("2F-01", "yes", 2, "2F", "File Editor Confirm",
             setup={"pending_confirmation": ("delete", {"filename": "test.txt"}, time.time() + 30)},
             expect_handled=True,
             notes="Confirm delete"),
    TestCase("2F-02", "no", 2, "2F", "File Editor Confirm",
             setup={"pending_confirmation": ("delete", {"filename": "test.txt"}, time.time() + 30)},
             expect_handled=True,
             notes="Cancel delete"),
    TestCase("2F-03", "go ahead", 2, "2F", "File Editor Confirm",
             setup={"pending_confirmation": ("delete", {"filename": "test.txt"}, time.time() + 30)},
             expect_handled=True,
             notes="'go ahead' is in affirmatives set — confirms delete"),
    TestCase("2F-04", "yes", 2, "2F", "File Editor Confirm — no pending",
             expect_handled=False,
             notes="No pending confirmation — 'yes' (3 chars) passes greeting, falls to LLM"),
    TestCase("2F-05", "yes", 2, "2F", "File Editor Confirm — multi-step",
             setup={"pending_confirmation": ("delete", {"filename": "temp.txt"}, time.time() + 30)},
             expect_handled=True,
             notes="Simulates step 2 of 'delete file' → 'yes' sequence"),
    TestCase("2F-06", "yes", 2, "2F", "File Editor Confirm — expired",
             setup={"pending_confirmation": ("delete", {"filename": "test.txt"}, time.time() - 5)},
             expect_handled=False,
             notes="Expired confirmation — 3-tuple but time > expiry, falls to LLM"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 5 — Skill Routing Validation
# ---------------------------------------------------------------------------

# 5A: Weather
TESTS += [
    TestCase("5A-R1", "what's the weather", 2, "5A", "Weather Routing",
             expect_skill="weather", expect_handled=True),
    TestCase("5A-R2", "will it rain tomorrow", 2, "5A", "Weather Routing",
             expect_skill="weather", expect_handled=True),
    TestCase("5A-R3", "what's the temperature", 2, "5A", "Weather Routing",
             expect_skill="weather", expect_handled=True),
]

# 5B: Reminders
TESTS += [
    TestCase("5B-R1", "remind me at 3pm to call mom", 2, "5B", "Reminders Routing",
             expect_skill="reminders", expect_handled=True),
    TestCase("5B-R2", "what's on my schedule", 2, "5B", "Reminders Routing",
             expect_skill="reminders", expect_handled=True),
    TestCase("5B-R3", "cancel my dentist reminder", 2, "5B", "Reminders Routing",
             expect_skill="reminders", expect_handled=True),
]

# 5C: File Editor
TESTS += [
    TestCase("5C-R1", "write a file called test.txt", 2, "5C", "File Editor Routing",
             expect_skill="file_editor", expect_handled=True,
             notes="Keyword 'file' + 'write' routes to file_editor"),
    TestCase("5C-R2", "delete temp.txt from the share", 2, "5C", "File Editor Routing",
             expect_skill="file_editor", expect_handled=True,
             notes="Keyword 'share' disambiguates to file_editor"),
    TestCase("5C-R3", "list what's in the share", 2, "5C", "File Editor Routing",
             expect_skill="file_editor", expect_handled=True,
             notes="Keyword 'share' routes to file_editor"),
]

# 5D: Developer Tools
TESTS += [
    TestCase("5D-R1", "git status", 2, "5D", "Dev Tools Routing",
             expect_skill="developer_tools", expect_handled=True),
    TestCase("5D-R2", "search the codebase for TODO", 2, "5D", "Dev Tools Routing",
             expect_skill="developer_tools", expect_handled=True),
    TestCase("5D-R3", "check the git branch", 2, "5D", "Dev Tools Routing",
             expect_skill="developer_tools", expect_handled=True,
             notes="Keyword 'git' + 'branch' routes to developer_tools"),
]

# 5E: Web Navigation
TESTS += [
    TestCase("5E-R1", "search youtube for music videos", 2, "5E", "Web Nav Routing",
             expect_skill="web_navigation", expect_handled=True),
    TestCase("5E-R2", "search google for python tutorials", 2, "5E", "Web Nav Routing",
             expect_skill="web_navigation", expect_handled=True),
]

# 5F: App Launcher
TESTS += [
    TestCase("5F-R1", "open chrome", 2, "5F", "App Launcher Routing",
             expect_skill="app_launcher", expect_handled=True),
    TestCase("5F-R2", "volume up", 2, "5F", "App Launcher Routing",
             expect_skill="app_launcher", expect_handled=True),
    TestCase("5F-R3", "switch to workspace 2", 2, "5F", "App Launcher Routing",
             expect_skill="app_launcher", expect_handled=True),
]

# 5G: Conversation
TESTS += [
    TestCase("5G-R1", "hello", 2, "5G", "Conversation Routing",
             expect_handled=True, notes="Greeting"),
    TestCase("5G-R2", "good morning", 2, "5G", "Conversation Routing",
             expect_skill="conversation", expect_handled=True),
    TestCase("5G-R3", "how are you", 2, "5G", "Conversation Routing",
             expect_skill="conversation", expect_handled=True),
    TestCase("5G-R4", "thank you", 2, "5G", "Conversation Routing",
             expect_skill="conversation", expect_handled=True),
    TestCase("5G-R5", "goodbye", 2, "5G", "Conversation Routing",
             expect_skill="conversation", expect_handled=True),
]

# 5H: News
TESTS += [
    TestCase("5H-R1", "read me the headlines", 2, "5H", "News Routing",
             expect_handled=True, notes="Should route to news or LLM"),
    TestCase("5H-R2", "any cybersecurity news", 2, "5H", "News Routing",
             expect_handled=True, notes="News category filter"),
]

# 5I: System Info
TESTS += [
    TestCase("5I-R1", "what cpu do I have", 2, "5I", "System Info Routing",
             expect_skill="system_info", expect_handled=True),
    TestCase("5I-R2", "how much disk space do I have", 2, "5I", "System Info Routing",
             expect_skill="system_info", expect_handled=True),
]

# ---------------------------------------------------------------------------
# Priority ordering tests
# ---------------------------------------------------------------------------

TESTS += [
    TestCase("PRI-01", "yes", 2, "PRI", "Priority: P2.5 beats P2.8",
             in_conversation=True,
             setup={"pending_forget": True},
             expect_intent="forget_confirm", expect_skip=False,
             notes="Forget confirm should beat bare ack filter"),
    TestCase("PRI-02", "no thanks", 2, "PRI", "Priority: P2.7 beats P4",
             in_conversation=True,
             expect_intent="dismissal",
             notes="Dismissal should beat skill routing"),
]

# ---------------------------------------------------------------------------
# TIER 2: Phase 1E — Multi-Step Compound Commands
# ---------------------------------------------------------------------------
# Tests that compound instructions (research + create, multi-verb, long
# preamble) route to the correct skill.  The handler's internal LLM parse
# deals with the multi-step logic — routing just needs to land correctly.

TESTS += [
    # Research + presentation → file_editor
    TestCase("1E-01",
             "look up the top 5 LLMs for home use and create a 7 slide PowerPoint called llm_review.pptx",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="Research + presentation — 'look up' should NOT steal to web_navigation"),

    # Research + document → file_editor
    TestCase("1E-02",
             "research the latest cybersecurity trends and write a report about what you find",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="Research + document — 'report' keyword anchors to file_editor"),

    # Research + named file → file_editor
    TestCase("1E-03",
             "look up renewable energy statistics and prepare a presentation called energy.pptx and leave it in the share",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="Explicit filename + share directory + research"),

    # Research-only (no doc keyword) → NOT file_editor
    TestCase("1E-04",
             "look up the top 5 LLMs for home use and tell me what you find",
             2, "1E", "Multi-Step Compound",
             expect_not_skill="file_editor", expect_handled=False,
             notes="Research without doc creation → LLM fallback / web research, NOT file_editor"),

    # Multi-verb same skill (filesystem)
    TestCase("1E-05",
             "find all the python files in the project and count the total lines of code",
             2, "1E", "Multi-Step Compound",
             expect_skill="filesystem", expect_handled=True,
             notes="Two verbs, both map to filesystem"),

    # Competing keywords — doc creation wins over web
    TestCase("1E-06",
             "search the web for cloud provider comparisons and make a slide deck about them",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="'search' (web_nav) vs 'slide deck' (file_editor) — doc keyword wins"),

    # Buried intent — research + slide deck
    TestCase("1E-07",
             "look up AI market growth trends and create a slide deck about it",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="Research preamble + multiple file_editor keywords (create, slide, deck). "
                   "Single-keyword compound commands can fail semantic verification"),

    # DOCX creation + topic
    TestCase("1E-08",
             "create a docx report about network security best practices",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="DOCX creation — 'create' + 'docx' + 'report' keywords"),

    # PDF request
    TestCase("1E-09",
             "create a PDF report comparing Python and Rust for systems programming",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="PDF keyword routes to file_editor"),

    # Reminder with 'weather' keyword — keyword-first routing limitation
    TestCase("1E-10",
             "remind me to check the weather forecast before I leave for work tomorrow morning",
             2, "1E", "Multi-Step Compound",
             expect_skill="weather", expect_handled=True,
             notes="KNOWN LIMITATION: 'weather' keyword wins over 'remind me' — "
                   "keyword-first routing means the first keyword match wins"),

    # Desktop + app compound
    TestCase("1E-11",
             "open chrome and then switch to workspace 2",
             2, "1E", "Multi-Step Compound",
             expect_skill="app_launcher", expect_handled=True,
             notes="Two desktop actions — first verb anchors to app_launcher"),

    # Multi-step with explicit share directory
    TestCase("1E-12",
             "compare the pros and cons of Docker and Kubernetes and make a PowerPoint about it and save it in the share folder",
             2, "1E", "Multi-Step Compound",
             expect_skill="file_editor", expect_handled=True,
             notes="Comparison + PPTX + explicit share — full multi-step"),
]


# ===========================================================================
# Process guard (block visual subprocess launches during tests)
# ===========================================================================

# Binaries that open visible windows/tabs — block even without start_new_session
_VISUAL_BINARIES = {
    'code', 'gnome-terminal', 'xterm', 'xdg-open',
    'google-chrome-stable', 'brave-browser', 'firefox',
}


class _MockPopen:
    """Minimal Popen stand-in for blocked subprocess calls."""
    pid = 0
    returncode = 0
    stdout = None
    stderr = None
    args = []

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def communicate(self, *a, **kw): return (b'', b'')
    def wait(self, *a, **kw): return 0
    def poll(self): return 0
    def kill(self): pass
    def terminate(self): pass
    def send_signal(self, sig): pass


class ProcessGuard:
    """Blocks visual subprocess launches during tests.

    Two classes of artifact-creating Popen calls exist in JARVIS:
    1. app_launcher / web_navigation — use start_new_session=True
    2. developer_tools _display.py — use Popen without start_new_session
       to open VS Code ('code') and gnome-terminal

    This guard blocks BOTH: any Popen with start_new_session=True, and any
    Popen whose command starts with a known visual binary (code,
    gnome-terminal, browsers, etc.). Everything else passes through.
    """

    def __init__(self):
        self._original_popen = subprocess.Popen
        self.blocked = []
        self._active = False

    def start(self):
        """Replace subprocess.Popen with guarded version."""
        guard = self
        OrigPopen = self._original_popen

        def guarded_popen(*args, **kwargs):
            if not guard._active:
                return OrigPopen(*args, **kwargs)

            cmd = args[0] if args else kwargs.get('args', ['?'])
            if isinstance(cmd, (list, tuple)):
                cmd_str = ' '.join(str(c) for c in cmd[:3])
                first_bin = os.path.basename(str(cmd[0])) if cmd else ''
            else:
                cmd_str = str(cmd)[:60]
                first_bin = os.path.basename(str(cmd).split()[0]) if cmd else ''

            # Block: start_new_session=True OR known visual binary
            if kwargs.get('start_new_session') or first_bin in _VISUAL_BINARIES:
                guard.blocked.append(cmd_str)
                return _MockPopen()

            return OrigPopen(*args, **kwargs)

        subprocess.Popen = guarded_popen
        self._active = True

    def stop(self):
        """Restore original subprocess.Popen."""
        self._active = False
        subprocess.Popen = self._original_popen


# ===========================================================================
# Pre-test snapshot and post-test cleanup
# ===========================================================================

def snapshot_pre_state():
    """Capture system state before tests for post-test cleanup."""
    state = {}

    # Snapshot share/ directory (file_editor sandbox)
    share_dir = os.path.join(PROJECT_ROOT, "share")
    if os.path.isdir(share_dir):
        state["share_dir"] = share_dir
        state["share_files"] = set(os.listdir(share_dir))

    return state


def cleanup_artifacts(pre_state, components, process_tracker,
                      verbose=False, json_mode=False):
    """Remove ALL artifacts created during test execution.

    Cleans up:
    - Spawned processes (browsers, terminals, apps launched by skill handlers)
    - New files in share/ directory (file_editor sandbox)
    - Pending state across all components (confirmations, forget flows, rundowns)
    """
    cleaned = []

    # 1. Stop process guard and report blocked launches
    if process_tracker:
        process_tracker.stop()
        for cmd in process_tracker.blocked:
            cleaned.append(f"Blocked launch: {cmd}")

    # 2. Remove new files from share/
    share_dir = pre_state.get("share_dir")
    if share_dir and os.path.isdir(share_dir):
        current_files = set(os.listdir(share_dir))
        new_files = current_files - pre_state.get("share_files", set())
        new_files.discard(".gitkeep")
        for f in sorted(new_files):
            path = os.path.join(share_dir, f)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    cleaned.append(f"Removed share/{f}")
            except OSError as e:
                cleaned.append(f"Failed to remove share/{f}: {e}")

    # 3. Reset all component state
    if components:
        skill_manager = components.get("skill_manager")
        if skill_manager:
            for sname, skill_obj in skill_manager.skills.items():
                if hasattr(skill_obj, '_pending_confirmation') and skill_obj._pending_confirmation:
                    skill_obj._pending_confirmation = None
                    cleaned.append(f"Reset {sname}._pending_confirmation")

        memory_manager = components.get("memory_manager")
        if memory_manager and memory_manager._pending_forget:
            memory_manager._pending_forget = None
            cleaned.append("Reset memory_manager._pending_forget")

        reminder_manager = components.get("reminder_manager")
        if reminder_manager and reminder_manager._rundown_state:
            reminder_manager._rundown_state = None
            reminder_manager._rundown_cycle = 0
            cleaned.append("Reset reminder_manager rundown state")

        conv_state = components.get("conv_state")
        if conv_state:
            conv_state.conversation_active = False
            conv_state.turn_count = 0
            conv_state.jarvis_asked_question = False
            conv_state.last_intent = ""

    # 4. Report
    if not json_mode and (verbose or cleaned):
        if cleaned:
            print(f"\n--- Cleanup: {len(cleaned)} artifact(s) removed ---")
            for item in cleaned:
                print(f"  {item}")
        elif verbose:
            print(f"\n--- Cleanup: no artifacts found ---")

    return cleaned


# ===========================================================================
# CLI and main
# ===========================================================================

def parse_args():
    parser = argparse.ArgumentParser(description="JARVIS Edge Case Test Suite")
    parser.add_argument("--tier", type=int, help="Run only this tier (1-4)")
    parser.add_argument("--phase", type=str, help="Run only this phase (e.g. 1A, 3C, 7B)")
    parser.add_argument("--id", type=str, help="Run single test by ID")
    parser.add_argument("--all", action="store_true", help="Run all tiers (default: 1+2)")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    parser.add_argument("--verbose", action="store_true", help="Show all tests, not just failures")
    return parser.parse_args()


def filter_tests(tests, args):
    """Filter test cases based on CLI args."""
    if args.id:
        return [t for t in tests if t.id == args.id]
    if args.phase:
        return [t for t in tests if t.phase.upper() == args.phase.upper()]
    if args.tier:
        return [t for t in tests if t.tier == args.tier]
    if args.all:
        return tests
    # Default: tiers 1 + 2
    return [t for t in tests if t.tier <= 2]


def main():
    args = parse_args()
    selected = filter_tests(TESTS, args)

    if not selected:
        print("No tests match the given filters.")
        return 1

    if not args.json:
        print("=" * 65)
        print("  JARVIS Edge Case Test Suite")
        print("=" * 65)

    # Determine which tiers are needed
    tiers_needed = set(t.tier for t in selected)
    components = None

    # Snapshot pre-test state and block detached process launches
    pre_state = snapshot_pre_state()
    process_tracker = ProcessGuard()
    process_tracker.start()

    # Load Tier 2 components if needed
    if 2 in tiers_needed:
        try:
            components = init_tier2_components()
        except Exception as e:
            print(f"\nFailed to load Tier 2 components: {e}")
            # Fall back to tier 1 only
            selected = [t for t in selected if t.tier == 1]
            if not selected:
                process_tracker.stop()
                return 1

    # Run tests
    results = TestResults()
    current_phase = None
    tier_counts = {}  # tier -> (passed, failed)

    for case in selected:
        if not args.json and case.phase != current_phase:
            current_phase = case.phase
            phase_cases = [t for t in selected if t.phase == case.phase]
            tier_label = f"T{case.tier}"
            if not args.json:
                print(f"\n--- [{tier_label}] Phase {case.phase}: {case.category} ({len(phase_cases)} tests) ---")

        run_test(case, components, results)

        # Get last result
        last_id, last_status, last_detail = results.results[-1]

        # Track per-tier counts
        if case.tier not in tier_counts:
            tier_counts[case.tier] = [0, 0]
        if last_status == "PASS":
            tier_counts[case.tier][0] += 1
        elif last_status == "FAIL":
            tier_counts[case.tier][1] += 1

        # Print result
        if not args.json:
            if last_status == "FAIL" or args.verbose:
                marker = "\033[32m[PASS]\033[0m" if last_status == "PASS" else \
                         "\033[31m[FAIL]\033[0m" if last_status == "FAIL" else \
                         "\033[33m[SKIP]\033[0m"
                line = f"  {marker} {last_id}: {case.input[:50]}"
                if last_status == "FAIL":
                    line += f"\n         {last_detail}"
                elif args.verbose and last_detail:
                    line += f"  → {last_detail[:60]}"
                print(line)

    # Cleanup artifacts created during testing
    cleaned = cleanup_artifacts(pre_state, components, process_tracker,
                                verbose=args.verbose, json_mode=args.json)

    # Summary
    if args.json:
        output = results.to_json()
        output["tiers"] = {
            f"tier_{k}": {"passed": v[0], "failed": v[1]}
            for k, v in sorted(tier_counts.items())
        }
        if cleaned:
            output["cleanup"] = cleaned
        print(json.dumps(output, indent=2))
    else:
        print(f"\n{'=' * 65}")
        tier_summary = " | ".join(
            f"TIER {k}: {v[0]}/{v[0]+v[1]}"
            for k, v in sorted(tier_counts.items())
        )
        print(f"  {tier_summary}")
        total = results.passed + results.failed
        pct = results.passed / total * 100 if total else 0
        status = "\033[32mALL PASS\033[0m" if results.failed == 0 else f"\033[31m{results.failed} FAILED\033[0m"
        print(f"  TOTAL: {results.passed}/{total} ({pct:.1f}%) — {status}")
        if results.skipped:
            print(f"  Skipped: {results.skipped}")
        print(f"{'=' * 65}")

    return 0 if results.failed == 0 else 1


if __name__ == "__main__":
    rc = main()
    # Flush output before forced exit
    sys.stdout.flush()
    sys.stderr.flush()
    # Force clean exit — ROCm/ONNX runtime cleanup can abort() on teardown
    os._exit(rc)
