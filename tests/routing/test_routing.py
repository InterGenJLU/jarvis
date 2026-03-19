#!/usr/bin/env python3
"""
Routing test suite — validates the full priority chain, CAL-L0 fast-path,
and tool gate decisions.

Loads real JARVIS components and runs commands through the router, asserting
on RouteResult fields. Covers: greetings, dismissals, bare acks, CAL-L0
reflexive layer, tool gate (classifier + keywords), skill routing, tool
calling, and LLM fallback.

Based on the original scripts/test_router.py harness.

Usage:
    python3 tests/routing/test_routing.py
    python3 tests/routing/test_routing.py --section cal_l0
    python3 tests/routing/test_routing.py --section tool_gate
    python3 tests/routing/test_routing.py --section all
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['TQDM_DISABLE'] = '1'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'
os.environ['JARVIS_LOG_TARGET'] = 'test'

import sys
import time
import argparse
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# ---------------------------------------------------------------------------
# Minimal TTS stub
# ---------------------------------------------------------------------------

class TTSStub:
    """No-op TTS for testing — skills may call tts.speak()."""
    _spoke = False

    def speak(self, text, normalize=True):
        self._spoke = True
        return True

    def get_pending_announcements(self):
        return []


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_total_passed = 0
_total_failed = 0
_section_passed = 0
_section_failed = 0


def check(label, condition, detail=""):
    """Print PASS/FAIL and update counters."""
    global _total_passed, _total_failed, _section_passed, _section_failed
    if condition:
        print(f"  [PASS] {label}")
        _total_passed += 1
        _section_passed += 1
    else:
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f"  ({detail})"
        print(msg)
        _total_failed += 1
        _section_failed += 1
    return condition


def section(title):
    global _section_passed, _section_failed
    _section_passed = 0
    _section_failed = 0
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def section_summary(title):
    total = _section_passed + _section_failed
    status = "✓" if _section_failed == 0 else "✗"
    print(f"  {status} {title}: {_section_passed}/{total} passed")


# ---------------------------------------------------------------------------
# Component initialization (one-time)
# ---------------------------------------------------------------------------

def init_components():
    """Load real JARVIS components for testing."""
    from core.config import load_config
    from core.conversation import ConversationManager
    from core.llm_router import LLMRouter
    from core.skill_manager import SkillManager
    from core.responses import get_response_library
    from core.conversation_state import ConversationState
    from core.conversation_router import ConversationRouter

    print("Loading components...")
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
            config=config,
            conversation=conversation,
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

    # Profile manager
    try:
        from core.user_profile import get_profile_manager
        pm = get_profile_manager(config)
        if pm:
            conversation.set_profile_manager(pm)
    except Exception:
        pass

    # Context window
    context_window = None
    if config.get("context_window.enabled", False):
        from core.context_window import get_context_window
        context_window = get_context_window(
            config=config,
            embedding_model=skill_manager._embedding_model,
            llm=llm,
        )
        conversation.set_context_window(context_window)

    # Web researcher
    web_researcher = None
    if config.get("llm.local.tool_calling", False):
        from core.web_research import WebResearcher
        web_researcher = WebResearcher(config)

    conv_state = ConversationState()
    router = ConversationRouter(
        skill_manager=skill_manager,
        conversation=conversation,
        llm=llm,
        reminder_manager=reminder_manager,
        memory_manager=memory_manager,
        news_manager=news_manager,
        context_window=context_window,
        conv_state=conv_state,
        config=config,
        web_researcher=web_researcher,
    )

    elapsed = time.perf_counter() - t0
    skill_count = len(skill_manager.skills)
    print(f"Ready — {skill_count} skills loaded in {elapsed:.1f}s\n")

    return router, conv_state, skill_manager


# ---------------------------------------------------------------------------
# CAL-L0 Tests
# ---------------------------------------------------------------------------

def test_cal_l0_greetings(router):
    section("CAL-L0: Greetings")

    for phrase in ["hello", "good morning", "hey there", "evening", "greetings"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 greeting",
            r.handled and r.source == "cal_l0" and "greeting" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )
        check(f"  has response text", len(r.text or "") > 0, f"text={r.text!r}")

    section_summary("CAL-L0 Greetings")


def test_cal_l0_farewells(router):
    section("CAL-L0: Farewells")

    for phrase in ["goodbye", "see you later", "good night", "take care", "farewell"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 farewell",
            r.handled and r.source == "cal_l0" and "goodbye" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Farewells")


def test_cal_l0_thanks(router):
    section("CAL-L0: Thanks / Gratitude")

    for phrase in ["thank you", "thanks a lot", "much appreciated", "appreciate it", "cheers"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 thanks",
            r.handled and r.source == "cal_l0" and "thank" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Thanks")


def test_cal_l0_pleasantries(router):
    section("CAL-L0: Pleasantries")

    for phrase in ["how are you", "how's it going", "how are things", "what's new"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 pleasantry",
            r.handled and r.source == "cal_l0" and "how_are_you" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Pleasantries")


def test_cal_l0_compliments(router):
    section("CAL-L0: Compliments / Praise")

    for phrase in ["nice work", "great job", "you're the best", "brilliant", "well done"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 compliment",
            r.handled and r.source == "cal_l0" and "compliment" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Compliments")


def test_cal_l0_apologies(router):
    section("CAL-L0: Apologies")

    for phrase in ["sorry", "my bad", "my apologies", "oops", "excuse me"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 apology",
            r.handled and r.source == "cal_l0" and "apology" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Apologies")


def test_cal_l0_small_talk(router):
    section("CAL-L0: Small Talk")

    for phrase in ["i'm bored", "tell me a joke", "you're funny"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 small talk",
            r.handled and r.source == "cal_l0" and "small_talk" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Small Talk")


def test_cal_l0_meta_questions(router):
    section("CAL-L0: Meta-Questions")

    for phrase in ["who are you", "what can you do", "are you a robot", "who made you"]:
        r = router.route(phrase)
        check(
            f"'{phrase}' → CAL-L0 meta",
            r.handled and r.source == "cal_l0" and "meta" in (r.intent or ""),
            f"handled={r.handled}, source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Meta-Questions")


def test_cal_l0_compound_guard(router):
    section("CAL-L0: Compound Utterance Guard")

    # These should NOT be intercepted — they contain task content beyond the social pattern
    # Includes both punctuated and unpunctuated variants
    compound_queries = [
        "good morning, what's the weather",
        "good morning what's the weather",
        "thank you, but can you also check my email",
        "hello, remind me to call mom at three",
        "hello remind me to call mom at three",
        "hey, search for python tutorials",
        "hey search for python tutorials",
        "thanks, now tell me about quantum computing",
        "good morning can you check the news",
    ]
    for phrase in compound_queries:
        r = router.route(phrase)
        check(
            f"'{phrase}' → NOT CAL-L0 (compound)",
            r.source != "cal_l0",
            f"source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Compound Guard")


def test_cal_l0_non_intercept(router):
    section("CAL-L0: Should NOT Intercept")

    # Knowledge queries, tool queries, and anything that needs the LLM
    non_intercept = [
        "tell me about black holes",
        "what's the weather today",
        "remind me to call mom at 3pm",
        "explain quantum computing",
        "search for restaurants near me",
        "how much disk space do I have",
        "what happened during world war 2",
        "take a screenshot",
        "open firefox",
    ]
    for phrase in non_intercept:
        r = router.route(phrase)
        check(
            f"'{phrase}' → NOT CAL-L0",
            r.source != "cal_l0",
            f"source={r.source}, intent={r.intent}",
        )

    section_summary("CAL-L0 Non-Intercept")


# ---------------------------------------------------------------------------
# Tool Gate Tests
# ---------------------------------------------------------------------------

def test_tool_gate_skip(router):
    section("Tool Gate: Should Skip Tools (non-tool queries)")

    # These should route through LLM WITHOUT tool schemas
    non_tool = [
        "tell me about black holes",
        "explain the french revolution",
        "how does gravity work",
        "what is the meaning of life",
        "what do you think about modern art",
        "who was the first president",
        "explain quantum computing",
        "what causes earthquakes",
    ]
    for phrase in non_tool:
        r = router.route(phrase)
        has_tools = r.use_tools is not None and len(r.use_tools) > 0
        # Tool gate should have skipped tools — but the query may have been
        # handled by CAL-L0 or a skill before reaching the tool gate.
        # If it reached LLM fallback, use_tools should be None/empty.
        if r.handled:
            check(f"'{phrase}' → handled before LLM (skip N/A)", True)
        else:
            check(
                f"'{phrase}' → LLM without tools",
                not has_tools or len(r.use_tools) <= 1,  # web_search may still be included
                f"tools={[t['function']['name'] for t in r.use_tools] if r.use_tools else 'None'}",
            )

    section_summary("Tool Gate Skip")


def test_tool_gate_include(router):
    section("Tool Gate: Should Include Tools (tool queries)")

    # These should retain tool schemas
    tool_queries = [
        ("what's the weather today", "weather"),
        ("remind me to call mom at 3pm", "remind"),
        ("take a screenshot", "screenshot"),
        ("search for python tutorials", "search"),
        ("how much disk space do I have", "disk"),
        ("what time is it", "time"),
        ("open firefox", "open"),
        ("what's in the news", "news"),
    ]
    for phrase, label in tool_queries:
        r = router.route(phrase)
        # These may be handled by skills directly (no LLM needed)
        # or routed to LLM with tools. Either is correct.
        if r.handled:
            check(f"'{phrase}' → handled by skill ({label})", True)
        else:
            has_tools = r.use_tools is not None and len(r.use_tools) > 0
            check(
                f"'{phrase}' → LLM with tools ({label})",
                has_tools,
                f"tools={[t['function']['name'] for t in r.use_tools] if r.use_tools else 'None'}",
            )

    section_summary("Tool Gate Include")


def test_tool_gate_keywords(router):
    section("Tool Gate: Keyword Override")

    # These should ALWAYS include tools regardless of classifier score
    keyword_queries = [
        "search for best restaurants in Your City",
        "remind me about the meeting",
        "what's the weather forecast",
        "take a screenshot of my desktop",
        "look up Alabama football scores",
        "what's happening in sports",
        "mute the audio",
    ]
    for phrase in keyword_queries:
        r = router.route(phrase)
        if r.handled:
            check(f"'{phrase}' → handled by skill (keyword)", True)
        else:
            has_tools = r.use_tools is not None and len(r.use_tools) > 0
            check(
                f"'{phrase}' → has tools (keyword override)",
                has_tools,
                f"tools={[t['function']['name'] for t in r.use_tools] if r.use_tools else 'None'}",
            )

    section_summary("Tool Gate Keywords")


# ---------------------------------------------------------------------------
# Latency measurement
# ---------------------------------------------------------------------------

def test_cal_l0_latency(router):
    section("CAL-L0: Latency Measurement")

    queries = [
        ("thank you", "thanks"),
        ("good morning", "greeting"),
        ("who are you", "meta"),
        ("nice work", "compliment"),
        ("goodbye", "farewell"),
    ]

    for phrase, label in queries:
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            r = router.route(phrase)
            elapsed = (time.perf_counter() - t0) * 1000  # ms
            times.append(elapsed)

        avg = sum(times) / len(times)
        median = sorted(times)[len(times) // 2]
        check(
            f"'{phrase}' ({label}): avg={avg:.1f}ms median={median:.1f}ms",
            avg < 100,  # Should be well under 100ms
            f"too slow" if avg >= 100 else "",
        )

    section_summary("CAL-L0 Latency")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="JARVIS Routing Test Suite")
    parser.add_argument(
        "--section", default="all",
        help="Run specific section: cal_l0, tool_gate, latency, all (default: all)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  JARVIS Routing Test Suite")
    print("=" * 60)

    router, conv_state, skill_manager = init_components()

    sections = {
        "cal_l0": [
            test_cal_l0_greetings,
            test_cal_l0_farewells,
            test_cal_l0_thanks,
            test_cal_l0_pleasantries,
            test_cal_l0_compliments,
            test_cal_l0_apologies,
            test_cal_l0_small_talk,
            test_cal_l0_meta_questions,
            test_cal_l0_compound_guard,
            test_cal_l0_non_intercept,
        ],
        "tool_gate": [
            test_tool_gate_skip,
            test_tool_gate_include,
            test_tool_gate_keywords,
        ],
        "latency": [
            test_cal_l0_latency,
        ],
    }

    if args.section == "all":
        run_sections = list(sections.keys())
    elif args.section in sections:
        run_sections = [args.section]
    else:
        print(f"Unknown section: {args.section}")
        print(f"Available: {', '.join(sections.keys())}, all")
        return 1

    for section_name in run_sections:
        for test_fn in sections[section_name]:
            test_fn(router)

    # Summary
    total = _total_passed + _total_failed
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY: {_total_passed} passed, {_total_failed} failed out of {total}")
    print(f"{'=' * 60}")

    # Clean up GPU resources to prevent ROCm/HIP crash on exit
    _cleanup_gpu(skill_manager)

    return 0 if _total_failed == 0 else 1


def _cleanup_gpu(skill_manager):
    """Release GPU tensors before process exit to prevent ROCm core dump."""
    try:
        import gc
        # Clear embedding model GPU tensors
        if hasattr(skill_manager, '_embedding_model') and skill_manager._embedding_model:
            del skill_manager._embedding_model
            skill_manager._embedding_model = None
        # Clear semantic embedding cache
        if hasattr(skill_manager, '_semantic_embedding_cache'):
            skill_manager._semantic_embedding_cache.clear()
        gc.collect()
        # Release CUDA/HIP memory
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
