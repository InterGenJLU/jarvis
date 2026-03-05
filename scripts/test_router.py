#!/usr/bin/env python3
"""
Standalone test for ConversationRouter — Phase 3 of conversational flow refactor.

Loads real JARVIS components and runs commands through the router,
asserting on RouteResult fields. Tests priority chain ordering,
skill routing, dismissals, bare ack filtering, memory ops, and LLM fallback.

Usage:
    python3 scripts/test_router.py
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

import sys
import time
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def check(label, condition, detail=""):
    """Print PASS/FAIL and update global counters."""
    global _total_passed, _total_failed
    if condition:
        print(f"  [PASS] {label}")
        _total_passed += 1
    else:
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f"  ({detail})"
        print(msg)
        _total_failed += 1
    return condition


def section(title):
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# Component initialization (one-time, ~5s)
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

    # Profile manager (for speaker labels)
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

    return router, conv_state, memory_manager, reminder_manager, news_manager


# ---------------------------------------------------------------------------
# Test categories
# ---------------------------------------------------------------------------

def test_greetings(router):
    section("Greetings")

    r = router.route("jarvis_only")
    check("jarvis_only → greeting", r.handled and r.intent == "greeting" and r.source == "canned")
    check("  opens 8s window", r.open_window == 8.0)
    check("  has text", len(r.text) > 0, f"text={r.text!r}")

    r = router.route("hi")
    check("'hi' (2 chars) → greeting", r.handled and r.intent == "greeting")

    r = router.route("a")
    check("'a' (1 char) → greeting", r.handled and r.intent == "greeting")


def test_dismissals(router):
    section("Dismissals (in_conversation=True)")

    for phrase in ["no thanks", "that's all", "i'm good", "nevermind", "maybe later"]:
        r = router.route(phrase, in_conversation=True)
        check(f"'{phrase}' → dismissal",
              r.handled and r.intent == "dismissal" and r.close_window,
              f"handled={r.handled}, intent={r.intent}, close={r.close_window}")

    # Courtesy suffix stripping
    r = router.route("no, that's all, thank you", in_conversation=True)
    check("'no, that's all, thank you' → dismissal",
          r.handled and r.intent == "dismissal")

    # Should NOT dismiss outside conversation window
    r = router.route("no thanks", in_conversation=False)
    check("'no thanks' outside conversation → NOT dismissal",
          r.intent != "dismissal",
          f"intent={r.intent}")


def test_bare_ack_filter(router, conv_state):
    section("Bare Ack Filter (in_conversation=True)")

    # Reset state
    conv_state.jarvis_asked_question = False

    for word in ["yeah", "okay", "sure", "hmm", "yep"]:
        r = router.route(word, in_conversation=True)
        check(f"'{word}' → skip (noise)",
              r.skip,
              f"skip={r.skip}, handled={r.handled}, intent={r.intent}")

    # When JARVIS asked a question, bare acks should pass through
    conv_state.jarvis_asked_question = True
    r = router.route("yeah", in_conversation=True)
    check("'yeah' with jarvis_asked_question=True → NOT skip",
          not r.skip,
          f"skip={r.skip}")
    conv_state.jarvis_asked_question = False

    # Non-bare-ack should never skip
    r = router.route("tell me about python", in_conversation=True)
    check("'tell me about python' → NOT skip", not r.skip)

    # Outside conversation, bare acks should route normally
    r = router.route("yeah", in_conversation=False)
    check("'yeah' outside conversation → NOT skip", not r.skip)


def test_skill_routing(router):
    section("Skill Routing — P4 legacy (non-migrated skills)")

    # Non-migrated skills still route through keyword/semantic matching
    legacy_tests = [
        ("what time is it", "time_info"),
    ]
    for cmd, expected_skill in legacy_tests:
        r = router.route(cmd)
        skill_name = r.match_info.get("skill_name", "") if r.match_info else ""
        check(f"'{cmd}' → {expected_skill}",
              r.handled and r.source == "skill" and expected_skill in skill_name.lower(),
              f"handled={r.handled}, source={r.source}, skill={skill_name}")

    section("Skill Routing — P4-LLM tool-calling (migrated skills)")

    # Migrated skills go through LLM tool-calling: handled=False, intent=tool_calling,
    # use_tools contains the expected tool schema.
    tool_tests = [
        ("what's the weather", "get_weather"),
        ("show me the git log", "developer_tools"),
        ("how many files in my documents folder", "find_files"),
    ]
    for cmd, expected_tool in tool_tests:
        r = router.route(cmd)
        tool_names = [t["function"]["name"] for t in r.use_tools] if r.use_tools else []
        check(f"'{cmd}' → tool_calling ({expected_tool})",
              r.intent == "tool_calling" and expected_tool in tool_names,
              f"intent={r.intent}, tools={tool_names}")

    section("Skill Routing — LLM-native (no skill/tool needed)")

    # Conversation skill is disabled — LLM handles greetings, thanks, farewells
    # natively. These should fall through to LLM fallback (handled=False, llm_command set).
    native_llm = ["how are you", "thank you", "goodbye"]
    for cmd in native_llm:
        r = router.route(cmd)
        # Either LLM fallback or tool_calling is acceptable — both mean the LLM handles it
        fell_through = (not r.handled and r.llm_command) or r.intent == "tool_calling"
        check(f"'{cmd}' → LLM (no skill)",
              fell_through,
              f"handled={r.handled}, intent={r.intent}")


def test_memory_ops(router, memory_manager):
    section("Memory Operations")

    if not memory_manager:
        print("  [SKIP] Memory manager not available")
        return

    # Transparency request
    r = router.route("what do you know about me")
    check("'what do you know about me' → memory_transparency",
          r.handled and r.intent == "memory_transparency",
          f"intent={r.intent}")

    # Fact store (explicit "remember" phrasing)
    r = router.route("remember that my favorite color is blue")
    check("'remember that...' → fact_stored",
          r.handled and r.intent == "fact_stored",
          f"intent={r.intent}")


def test_forget_confirmation(router, memory_manager, conv_state):
    section("Forget Confirmation (P2.5)")

    if not memory_manager:
        print("  [SKIP] Memory manager not available")
        return

    # Simulate pending forget state
    original_pending = memory_manager._pending_forget
    # Build a realistic _pending_forget with a fake fact that won't exist in DB
    fake_pending = {
        "facts": [{"fact_id": -1, "content": "test fact"}],
        "user_id": "test",
        "expires": time.time() + 300,
    }
    memory_manager._pending_forget = fake_pending.copy()

    try:
        r = router.route("yes")
        check("'yes' with pending forget → forget_confirm",
              r.handled and r.intent == "forget_confirm",
              f"intent={r.intent}")
    except Exception as e:
        check("'yes' with pending forget → forget_confirm", False, f"crashed: {e}")

    # Reset and test cancel
    memory_manager._pending_forget = fake_pending.copy()
    r = router.route("no")
    check("'no' with pending forget → forget_cancel",
          r.handled and r.intent == "forget_cancel",
          f"intent={r.intent}")

    # Restore
    memory_manager._pending_forget = original_pending


def test_priority_ordering(router, memory_manager, conv_state):
    section("Priority Ordering")

    if not memory_manager:
        print("  [SKIP] Memory manager not available")
        return

    # P2.5 (forget confirm) should beat P2.8 (bare ack filter)
    original_pending = memory_manager._pending_forget
    memory_manager._pending_forget = {
        "facts": [{"fact_id": -1, "content": "test"}],
        "user_id": "test",
        "expires": time.time() + 300,
    }
    conv_state.jarvis_asked_question = False

    r = router.route("yes", in_conversation=True)
    check("P2.5 beats P2.8: 'yes' with pending forget → forget_confirm (not bare ack skip)",
          r.handled and r.intent == "forget_confirm" and not r.skip,
          f"intent={r.intent}, skip={r.skip}")

    memory_manager._pending_forget = original_pending

    # P2.7 (dismissal) should beat P4 (skill routing)
    # "no" could match conversation skill, but should dismiss in conversation
    r = router.route("no thanks", in_conversation=True)
    check("P2.7 beats P4: 'no thanks' in conversation → dismissal (not skill)",
          r.intent == "dismissal" and r.source == "canned",
          f"intent={r.intent}, source={r.source}")


def test_llm_fallback(router):
    section("LLM Fallback")

    r = router.route("explain quantum entanglement in simple terms")
    check("unhandled query → LLM fallback",
          not r.handled and r.llm_command != "",
          f"handled={r.handled}")
    check("  has llm_command",
          "quantum" in r.llm_command.lower(),
          f"llm_command={r.llm_command[:50]!r}")
    check("  has llm_history (string)",
          isinstance(r.llm_history, str))

    # Document buffer test
    from core.document_buffer import DocumentBuffer
    doc = DocumentBuffer()
    doc.load("This is a test document about Python decorators.", "test")

    r = router.route("summarize this document", doc_buffer=doc)
    check("with doc_buffer → llm_max_tokens=600",
          r.llm_max_tokens == 600,
          f"max_tokens={r.llm_max_tokens}")
    check("  doc_buffer skips skill routing → LLM fallback",
          not r.handled,
          f"handled={r.handled}")

    doc.clear()


def test_news_continuation(router, news_manager):
    section("News Continuation")

    if not news_manager:
        print("  [SKIP] News manager not available")
        return

    remaining = news_manager.get_unread_count()
    total_unread = sum(remaining.values()) if remaining else 0

    # "read more" can be handled by P4-LLM tool-calling (get_news tool),
    # P5 news_continue handler, or fall through to LLM.
    r = router.route("read more")
    if total_unread == 0:
        check("'read more' with no unread → not news_continue",
              r.intent != "news_continue",
              f"intent={r.intent}")
    else:
        # With unread articles: tool-calling, news_continue, or skill are all valid
        routed = r.handled or r.intent == "tool_calling"
        check("'read more' with unread → routed (tool_calling or news_continue)",
              routed,
              f"handled={r.handled}, intent={r.intent}")


def test_guest_mode(router):
    """Test guest (unrecognized speaker) security boundary."""
    section("Guest Mode (#16)")

    conversation = router.conversation

    # Activate guest mode
    conversation.current_user = "__guest__"
    from core.honorific import set_honorific
    set_honorific("friend")

    try:
        # 1. Guest greeting — HAL 9000 easter egg
        r = router.route("jarvis_only")
        check("guest greeting → intent=guest_greeting",
              r.intent == "guest_greeting",
              f"intent={r.intent}")
        check("  handled + canned",
              r.handled and r.source == "canned")
        check("  opens conversation window",
              r.open_window is not None and r.open_window > 0)

        # 2. Weather query — should route to tool_calling with get_weather
        r = router.route("what's the weather like")
        if r.use_tools:
            tool_names = {t["function"]["name"] for t in r.use_tools}
            check("guest weather → tool_calling with get_weather",
                  "get_weather" in tool_names,
                  f"tools={tool_names}")
            check("  no personal tools (recall_memory blocked)",
                  "recall_memory" not in tool_names,
                  f"tools={tool_names}")
        else:
            # Might fall through to LLM — still acceptable
            check("guest weather → LLM fallback (acceptable)",
                  not r.handled,
                  f"intent={r.intent}")

        # 3. Reminder request — should NOT route to manage_reminders
        r = router.route("set a reminder for 5pm")
        if r.use_tools:
            tool_names = {t["function"]["name"] for t in r.use_tools}
            check("guest reminder → no manage_reminders tool",
                  "manage_reminders" not in tool_names,
                  f"tools={tool_names}")
        else:
            check("guest reminder → no skill routing (LLM fallback)",
                  not r.handled or r.intent not in ("skill",),
                  f"intent={r.intent}, handled={r.handled}")

        # 4. Memory recall — should NOT have recall_memory tool
        r = router.route("what's my favorite color")
        if r.use_tools:
            tool_names = {t["function"]["name"] for t in r.use_tools}
            check("guest memory → no recall_memory tool",
                  "recall_memory" not in tool_names,
                  f"tools={tool_names}")
        else:
            check("guest memory → LLM fallback (no memory ops)",
                  r.intent != "memory_recall",
                  f"intent={r.intent}")

        # 5. LLM fallback — guest context injected, no personal context
        r = router.route("tell me a joke")
        check("guest general Q&A → LLM fallback",
              not r.handled,
              f"handled={r.handled}, intent={r.intent}")
        check("  memory_context has GUEST MODE",
              r.memory_context and "GUEST MODE" in r.memory_context,
              f"memory_context={r.memory_context[:100] if r.memory_context else None!r}")

        # 6. Dismissal still works for guests
        r = router.route("that's all thanks", in_conversation=True)
        check("guest dismissal works",
              r.handled and r.close_window,
              f"handled={r.handled}, close={r.close_window}")

    finally:
        # Restore primary user context
        conversation.current_user = "user"
        set_honorific("sir")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def test_multi_speaker(router):
    """Test multi-speaker conversation tracking (#30)."""
    section("Multi-Speaker Conversation (#30)")

    conversation = router.conversation

    # Save and reset session state
    saved_user = conversation.current_user
    saved_participants = conversation.session_participants.copy()
    saved_history = conversation.session_history[:]
    conversation.session_participants.clear()
    conversation.session_history.clear()

    try:
        # 1. Single speaker — no multi-speaker labeling
        conversation.current_user = "user"
        conversation.add_message("user", "what's the weather")
        conversation.add_message("assistant", "Sunny and 72 degrees")

        check("single speaker → is_multi_speaker=False",
              not conversation.is_multi_speaker)

        history = conversation.format_history_for_llm(include_system_prompt=False)
        check("single speaker → no [Name] prefix in history",
              "[User]" not in history,
              f"history={history[:120]}")

        # 2. Second speaker joins — multi-speaker activated
        conversation.current_user = "secondary_user"
        conversation.add_message("user", "set a reminder for 5pm")

        check("second speaker → is_multi_speaker=True",
              conversation.is_multi_speaker)
        check("session_participants has both",
              conversation.session_participants == {"primary_user", "secondary_user"})

        # 3. History formatting includes speaker names
        conversation.add_message("assistant", "Reminder set for 5pm")
        history = conversation.format_history_for_llm(include_system_prompt=False)
        check("multi-speaker history has [User]",
              "[User]" in history,
              f"history={history[:250]}")
        check("multi-speaker history has [Secondary User]",
              "[Secondary User]" in history,
              f"history={history[:250]}")
        check("multi-speaker history preserves USER: tag",
              "USER: [User]" in history or "USER: [Secondary User]" in history,
              f"history={history[:250]}")

        # 4. Router injects multi-speaker context (use LLM-fallback query)
        r = router.route("tell me a joke")
        check("multi-speaker → MULTI-SPEAKER SESSION in context",
              r.memory_context and "MULTI-SPEAKER" in r.memory_context,
              f"context={r.memory_context[:200] if r.memory_context else None!r}")

        # 5. speaker_confidence persisted
        conversation.add_message("user", "hello", speaker_confidence=0.92)
        last = conversation.session_history[-1]
        check("speaker_confidence stored in message",
              last.get("speaker_confidence") == 0.92)

        # 6. Clear resets participants
        conversation.clear_session_history()
        check("clear resets participants",
              len(conversation.session_participants) == 0)

        # 7. Guest + known speaker → both labeled
        conversation.current_user = "user"
        conversation.add_message("user", "hello jarvis")
        conversation.current_user = "__guest__"
        conversation.add_message("user", "what time is it")
        history = conversation.format_history_for_llm(include_system_prompt=False)
        check("guest + known → [Guest] label present",
              "[Guest]" in history,
              f"history={history[:200]}")
        check("guest + known → [User] label present",
              "[User]" in history,
              f"history={history[:200]}")

    finally:
        # Restore session state
        conversation.current_user = saved_user
        conversation.session_participants = saved_participants
        conversation.session_history = saved_history


def main():
    print("=" * 60)
    print("  ConversationRouter Test Suite")
    print("=" * 60)

    router, conv_state, memory_manager, reminder_manager, news_manager = init_components()

    test_greetings(router)
    test_dismissals(router)
    test_bare_ack_filter(router, conv_state)
    test_skill_routing(router)
    test_memory_ops(router, memory_manager)
    test_forget_confirmation(router, memory_manager, conv_state)
    test_priority_ordering(router, memory_manager, conv_state)
    test_llm_fallback(router)
    test_news_continuation(router, news_manager)
    test_guest_mode(router)
    test_multi_speaker(router)

    # Summary
    total = _total_passed + _total_failed
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY: {_total_passed} passed, {_total_failed} failed out of {total}")
    print(f"{'=' * 60}")

    return 0 if _total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
