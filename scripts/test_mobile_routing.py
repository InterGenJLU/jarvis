#!/usr/bin/env python3
"""
Mobile routing test suite — verifies skill/tool filtering, LLM context
injection, and web_search/web_navigation guardrails for mobile sessions.

Covers Fixes A, B, C from session 163 handoff:
  A: Tightened web_navigation semantic examples
  B: web_search SYSTEM_PROMPT_RULE guardrails
  C: Mobile session detection & filtering

Usage:
    python3 scripts/test_mobile_routing.py --verbose > /tmp/test_output.txt 2>&1
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

import sys
import time
import warnings
import argparse

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
_verbose = False


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


def _tool_names(result):
    """Extract tool name set from a RouteResult."""
    if not result.use_tools:
        return set()
    return {t["function"]["name"] for t in result.use_tools}


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

    return router, skill_manager


# ---------------------------------------------------------------------------
# Section 1: Mobile Detection Basics
# ---------------------------------------------------------------------------

def test_detection_basics(router):
    """Verify _is_mobile property tracks conversation.client_type."""
    section("Section 1: Mobile Detection Basics")

    conversation = router.conversation
    saved = conversation.client_type

    try:
        # Default should be desktop
        conversation.client_type = "desktop"
        check("client_type=desktop → _is_mobile=False",
              not router._is_mobile)

        # Set to mobile
        conversation.client_type = "mobile"
        check("client_type=mobile → _is_mobile=True",
              router._is_mobile)

        # Back to desktop
        conversation.client_type = "desktop"
        check("client_type=desktop again → _is_mobile=False",
              not router._is_mobile)
    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 2: Mobile Skill Exclusion
# ---------------------------------------------------------------------------

def test_mobile_skill_exclusion(router, skill_manager):
    """Desktop-only skills must be blocked on mobile."""
    section("Section 2: Mobile Skill Exclusion")

    conversation = router.conversation
    saved = conversation.client_type

    # Queries that should match specific desktop-only skills
    # We test skill_manager.match_intent() directly first, then route()
    skill_queries = [
        ("search for pizza near Gardendale", "web_navigation", "web search"),
        ("google ROCm driver downloads", "web_navigation", "google search"),
    ]

    try:
        # Verify these match on desktop via skill_manager
        conversation.client_type = "desktop"
        for query, expected_skill, label in skill_queries:
            match = skill_manager.match_intent(query)
            if match:
                check(f"desktop: '{label}' → matches {expected_skill}",
                      match[0] == expected_skill,
                      f"got {match[0]}")
            else:
                check(f"desktop: '{label}' → matches {expected_skill}",
                      False, "no match at all")

        # Now verify they're blocked on mobile via route()
        conversation.client_type = "mobile"
        for query, expected_skill, label in skill_queries:
            r = router.route(query)
            matched_skill = (r.match_info or {}).get("skill", "")
            check(f"mobile: '{label}' → NOT handled by {expected_skill}",
                  matched_skill != expected_skill,
                  f"matched_skill={matched_skill}, intent={r.intent}")

        # app_launcher: match_intent on desktop, route() on mobile
        conversation.client_type = "desktop"
        match = skill_manager.match_intent("open firefox")
        desktop_skill = match[0] if match else None
        check("desktop: 'open firefox' → matches app_launcher",
              desktop_skill == "app_launcher",
              f"matched={desktop_skill}")

        conversation.client_type = "mobile"
        r_mobile = router.route("open firefox")
        mobile_skill = (r_mobile.match_info or {}).get("skill", "")
        check("mobile: 'open firefox' → NOT handled by app_launcher",
              mobile_skill != "app_launcher",
              f"mobile_skill={mobile_skill}")

        # file_editor: match_intent on desktop, route() on mobile
        conversation.client_type = "desktop"
        match = skill_manager.match_intent("edit my bashrc file")
        desktop_skill = match[0] if match else None
        check("desktop: 'edit my bashrc' → matches file_editor",
              desktop_skill == "file_editor",
              f"matched={desktop_skill}")

        conversation.client_type = "mobile"
        r_mobile = router.route("edit my bashrc file")
        mobile_skill = (r_mobile.match_info or {}).get("skill", "")
        check("mobile: 'edit my bashrc' → NOT handled by file_editor",
              mobile_skill != "file_editor",
              f"mobile_skill={mobile_skill}")

        # --- FALLBACK VERIFICATION ---
        # When search queries are blocked from web_navigation on mobile,
        # the user must STILL get web_search as a fallback.  Blocking the
        # skill is only useful if the alternative path delivers value.
        from core.llm_router import ToolCallRequest as _TCR

        conversation.client_type = "mobile"
        r = router.route("search for pizza near Gardendale")
        tools = _tool_names(r)
        check("mobile: blocked search → web_search tool available as fallback",
              "web_search" in tools,
              f"tools={tools}")

        if r.use_tools:
            called_tool = None
            for item in router.llm.stream_with_tools(
                user_message=r.llm_command or "search for pizza near Gardendale",
                tools=r.use_tools,
                memory_context=r.memory_context,
                conversation_messages=r.context_messages,
                tool_temperature=r.tool_temperature or 0.0,
                tool_presence_penalty=r.tool_presence_penalty or 0.0,
            ):
                if isinstance(item, _TCR):
                    called_tool = item.name
                    break
            check("mobile: blocked search → LLM calls web_search as fallback",
                  called_tool == "web_search",
                  f"called_tool={called_tool}")
        else:
            check("mobile: blocked search → LLM calls web_search as fallback",
                  False, "no tools available at all!")

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 2b: Pre-exec Skill Blocking (no side effects)
# ---------------------------------------------------------------------------

def test_preexec_blocking(router, skill_manager):
    """Verify desktop-only skill handlers do NOT execute on mobile.

    The pre-exec fix calls match_intent() (read-only) before execute_intent()
    (side-effecting).  If this is broken, web_navigation opens a browser and
    app_launcher starts a process on the server — invisible to the mobile
    user but real damage to the desktop session.
    """
    section("Section 2b: Pre-exec Skill Blocking (no side effects)")

    import subprocess as _sp
    conversation = router.conversation
    saved = conversation.client_type
    original_popen = _sp.Popen
    popen_calls = []

    class _MockProcess:
        returncode = 0
        pid = 99999
        def wait(self): return 0
        def communicate(self, *a, **kw): return (b"", b"")
        def poll(self): return 0
        def kill(self): pass
        def terminate(self): pass

    def _tracking_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        popen_calls.append(cmd)
        return _MockProcess()

    try:
        conversation.client_type = "mobile"
        _sp.Popen = _tracking_popen

        # web_navigation: "google X" would launch a browser on desktop
        popen_calls.clear()
        router.route("google ROCm driver downloads")
        check("mobile: 'google ROCm drivers' → no subprocess launched",
              len(popen_calls) == 0,
              f"subprocess.Popen called {len(popen_calls)} time(s): {popen_calls}")

        # app_launcher: "open firefox" would launch a process on desktop
        popen_calls.clear()
        router.route("open firefox")
        check("mobile: 'open firefox' → no subprocess launched",
              len(popen_calls) == 0,
              f"subprocess.Popen called {len(popen_calls)} time(s): {popen_calls}")

        # file_editor: "edit bashrc" could spawn an editor
        popen_calls.clear()
        router.route("edit my bashrc file")
        check("mobile: 'edit my bashrc' → no subprocess launched",
              len(popen_calls) == 0,
              f"subprocess.Popen called {len(popen_calls)} time(s): {popen_calls}")

    finally:
        _sp.Popen = original_popen
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 3: Mobile Tool Exclusion
# ---------------------------------------------------------------------------

def test_mobile_tool_exclusion(router):
    """developer_tools tool must be excluded on mobile; others still present."""
    section("Section 3: Mobile Tool Exclusion")

    conversation = router.conversation
    saved = conversation.client_type

    try:
        # Query that would trigger developer_tools on desktop
        query = "check the system health"

        conversation.client_type = "desktop"
        r_desktop = router.route(query)
        desktop_tools = _tool_names(r_desktop)
        if _verbose:
            print(f"    desktop tools: {desktop_tools}")

        conversation.client_type = "mobile"
        r_mobile = router.route(query)
        mobile_tools = _tool_names(r_mobile)
        if _verbose:
            print(f"    mobile tools: {mobile_tools}")

        check("desktop: developer_tools present",
              "developer_tools" in desktop_tools,
              f"tools={desktop_tools}")
        check("mobile: developer_tools excluded",
              "developer_tools" not in mobile_tools,
              f"tools={mobile_tools}")

        # Verify allowed tools still present on mobile
        # (weather query to ensure get_weather passes through)
        r = router.route("what's the weather in Gardendale")
        tools = _tool_names(r)
        check("mobile: get_weather still available",
              "get_weather" in tools,
              f"tools={tools}")
        check("mobile: web_search still available",
              "web_search" in tools,
              f"tools={tools}")

        # Reminder tool should still work on mobile
        r = router.route("set a reminder for 5pm")
        tools = _tool_names(r)
        check("mobile: manage_reminders still available",
              "manage_reminders" in tools,
              f"tools={tools}")

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 4: Mobile LLM Context Injection
# ---------------------------------------------------------------------------

def test_mobile_llm_context(router):
    """LLM context must include MOBILE SESSION note on mobile, not on desktop."""
    section("Section 4: Mobile LLM Context Injection")

    conversation = router.conversation
    saved = conversation.client_type

    query = "tell me about the history of Alabama"

    try:
        # Desktop: no mobile note
        conversation.client_type = "desktop"
        r_desktop = router.route(query)
        check("desktop: no MOBILE SESSION in context",
              not r_desktop.memory_context or "MOBILE SESSION" not in r_desktop.memory_context,
              f"context starts: {r_desktop.memory_context[:100] if r_desktop.memory_context else 'None'}")

        # Mobile: has mobile note
        conversation.client_type = "mobile"
        r_mobile = router.route(query)
        check("mobile: MOBILE SESSION in context",
              r_mobile.memory_context and "MOBILE SESSION" in r_mobile.memory_context,
              f"context starts: {r_mobile.memory_context[:100] if r_mobile.memory_context else 'None'}")

        # Verify it mentions not opening browsers
        check("mobile context mentions browsers/desktop restriction",
              r_mobile.memory_context and "browser" in r_mobile.memory_context.lower(),
              f"context: {r_mobile.memory_context[:200] if r_mobile.memory_context else 'None'}")

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 5: Web Search Guardrails (Fix B)
# ---------------------------------------------------------------------------

def test_web_search_guardrails(router):
    """Verify web_search SYSTEM_PROMPT_RULE has proper guardrail language."""
    section("Section 5: Web Search Guardrails (Fix B)")

    # Check the prompt rule text directly (deterministic, no LLM needed)
    from core.tools.web_search import SYSTEM_PROMPT_RULE

    check("SYSTEM_PROMPT_RULE mentions CURRENT or SPECIFIC",
          "CURRENT" in SYSTEM_PROMPT_RULE or "SPECIFIC" in SYSTEM_PROMPT_RULE,
          f"rule: {SYSTEM_PROMPT_RULE[:100]}")

    check("SYSTEM_PROMPT_RULE has negative: general knowledge",
          "general knowledge" in SYSTEM_PROMPT_RULE.lower(),
          f"rule: {SYSTEM_PROMPT_RULE[:200]}")

    check("SYSTEM_PROMPT_RULE has negative: science",
          "science" in SYSTEM_PROMPT_RULE.lower(),
          f"rule: {SYSTEM_PROMPT_RULE[:200]}")

    check("SYSTEM_PROMPT_RULE has negative: history",
          "history" in SYSTEM_PROMPT_RULE.lower(),
          f"rule: {SYSTEM_PROMPT_RULE[:200]}")

    check("SYSTEM_PROMPT_RULE has negative: definitions",
          "definition" in SYSTEM_PROMPT_RULE.lower(),
          f"rule: {SYSTEM_PROMPT_RULE[:200]}")

    # LLM-level probes: general knowledge should NOT trigger web_search
    # These go through the full route() → LLM stream_with_tools() path
    section("Section 5b: LLM Web Search Probes (via tool-calling)")

    from core.llm_router import ToolCallRequest

    conversation = router.conversation
    saved = conversation.client_type
    conversation.client_type = "desktop"

    general_knowledge_queries = [
        ("what are black holes", "general science"),
        ("who was Abraham Lincoln", "history"),
        ("what is photosynthesis", "biology"),
        ("how does gravity work", "physics"),
    ]

    # Queries that need current/real-world data — web_search must be available
    # Third element is the SPECIFIC tool the LLM should call.
    current_data_queries = [
        ("what's the weather in Your City State", "current weather", "get_weather"),
        ("latest Alabama football score", "live score", "web_search"),
        ("what time does the Alabama game start", "event time", "web_search"),
        ("is Costco open right now", "business hours", "web_search"),
        ("current gas prices in Gardendale", "current prices", "web_search"),
    ]

    try:
        for query, label in general_knowledge_queries:
            r = router.route(query)
            if r.use_tools:
                # Feed to LLM and check if it calls web_search
                called_tool = None
                for item in router.llm.stream_with_tools(
                    user_message=r.llm_command or query,
                    tools=r.use_tools,
                    memory_context=r.memory_context,
                    conversation_messages=r.context_messages,
                    tool_temperature=r.tool_temperature or 0.0,
                    tool_presence_penalty=r.tool_presence_penalty or 0.0,
                ):
                    if isinstance(item, ToolCallRequest):
                        called_tool = item.name
                        break

                check(f"general knowledge: '{label}' → LLM does NOT call web_search",
                      called_tool != "web_search",
                      f"called_tool={called_tool}")
            else:
                # No tools selected at all — that's fine for general knowledge
                check(f"general knowledge: '{label}' → no tools (LLM fallback)",
                      True)

        for query, label, expected_tool in current_data_queries:
            r = router.route(query)
            tools = _tool_names(r)
            check(f"current data: '{label}' → {expected_tool} available",
                  expected_tool in tools,
                  f"tools={tools}")

            if r.use_tools:
                called_tool = None
                for item in router.llm.stream_with_tools(
                    user_message=r.llm_command or query,
                    tools=r.use_tools,
                    memory_context=r.memory_context,
                    conversation_messages=r.context_messages,
                    tool_temperature=r.tool_temperature or 0.0,
                    tool_presence_penalty=r.tool_presence_penalty or 0.0,
                ):
                    if isinstance(item, ToolCallRequest):
                        called_tool = item.name
                        break

                check(f"current data: '{label}' → LLM calls {expected_tool}",
                      called_tool == expected_tool,
                      f"called_tool={called_tool}")

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 6: Web Navigation Semantic Examples (Fix A)
# ---------------------------------------------------------------------------

def test_web_navigation_semantics(router, skill_manager):
    """Tightened semantic examples must not false-positive on general knowledge."""
    section("Section 6: Web Navigation Semantic Examples (Fix A)")

    conversation = router.conversation
    saved = conversation.client_type
    conversation.client_type = "desktop"

    try:
        # Should NOT match web_navigation (general knowledge, no search intent)
        no_match_queries = [
            ("tell me about black holes", "general science — the original bug"),
            ("explain quantum mechanics", "general science"),
            ("what causes thunder", "weather knowledge"),
            ("who was the first president", "history"),
            ("what is the meaning of life", "philosophy"),
        ]

        for query, label in no_match_queries:
            match = skill_manager.match_intent(query)
            matched_skill = match[0] if match else None
            check(f"no match: '{label}' → NOT web_navigation",
                  matched_skill != "web_navigation",
                  f"matched={matched_skill}")

        # SHOULD match web_navigation (explicit search intent)
        match_queries = [
            ("search for pizza near me", "local search"),
            ("google ROCm drivers", "explicit google"),
            ("look up python tutorials", "look up"),
            ("search for reviews of the new iPhone", "product search"),
        ]

        for query, label in match_queries:
            match = skill_manager.match_intent(query)
            matched_skill = match[0] if match else None
            check(f"match: '{label}' → web_navigation",
                  matched_skill == "web_navigation",
                  f"matched={matched_skill}")

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 7: Desktop Unaffected (Sanity)
# ---------------------------------------------------------------------------

def test_desktop_unaffected(router, skill_manager):
    """Desktop sessions must be completely unaffected by mobile changes."""
    section("Section 7: Desktop Unaffected (Sanity)")

    conversation = router.conversation
    saved = conversation.client_type
    conversation.client_type = "desktop"

    try:
        # Greeting still works
        r = router.route("jarvis_only")
        check("desktop greeting still works",
              r.handled and r.intent == "greeting",
              f"intent={r.intent}")

        # Weather tool-calling still works
        r = router.route("what's the weather")
        tools = _tool_names(r)
        check("desktop: weather routes to tool_calling",
              "get_weather" in tools,
              f"tools={tools}")

        # developer_tools present on desktop
        r = router.route("check system health")
        tools = _tool_names(r)
        check("desktop: developer_tools present",
              "developer_tools" in tools,
              f"tools={tools}")

        # web_navigation skill routes on desktop
        match = skill_manager.match_intent("search for pizza near me")
        check("desktop: web_navigation skill routes normally",
              match and match[0] == "web_navigation",
              f"match={match}")

        # No mobile context injected
        r = router.route("tell me a joke")
        check("desktop: no MOBILE SESSION in LLM context",
              not r.memory_context or "MOBILE SESSION" not in r.memory_context)

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 8: Fix D — Always-On Tool Fallback
# ---------------------------------------------------------------------------

def test_fix_d_always_on_fallback(router):
    """Queries with no domain tool match must still get always-on tools.

    Fix D addresses the gap where queries like "Alabama football score" got
    zero domain tool matches → _select_tools_for_command returned None →
    LLM fallback with no tools → couldn't search.  After Fix D, always-on
    tools (web_search, recall_memory) are always attached.
    """
    section("Section 8: Fix D — Always-On Tool Fallback")

    conversation = router.conversation
    saved = conversation.client_type

    # These queries deliberately have NO matching domain tool.
    # Before Fix D they'd get None tools.  After Fix D, they must get
    # at least web_search + recall_memory.
    no_domain_queries = [
        ("Alabama football score", "sports score"),
        ("gas prices in Gardendale", "local prices"),
        ("is Costco on Highway 31 still open", "business hours"),
        ("what time does the Bama game start Saturday", "event time"),
    ]

    try:
        conversation.client_type = "desktop"
        for query, label in no_domain_queries:
            r = router.route(query)
            tools = _tool_names(r)
            check(f"Fix D desktop: '{label}' → has web_search",
                  "web_search" in tools,
                  f"tools={tools}")
            check(f"Fix D desktop: '{label}' → has recall_memory",
                  "recall_memory" in tools,
                  f"tools={tools}")

        # Same on mobile — always-on tools must survive mobile filtering
        conversation.client_type = "mobile"
        for query, label in no_domain_queries:
            r = router.route(query)
            tools = _tool_names(r)
            check(f"Fix D mobile: '{label}' → has web_search",
                  "web_search" in tools,
                  f"tools={tools}")

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Section 9: End-to-End Mobile Experience
# ---------------------------------------------------------------------------

def test_e2e_mobile_experience(router):
    """Verify the ACTUAL experience for common mobile queries.

    This is the most important section.  Every test here represents a real
    query a mobile user might ask.  For each one we verify:
    1. route() returns appropriate tools
    2. The LLM calls the CORRECT tool (not just any tool)
    3. For blocked queries, the LLM gets mobile context so the response
       is helpful rather than trying to launch desktop actions.

    If these tests fail, the mobile experience is broken — period.
    """
    section("Section 9: End-to-End Mobile Experience")

    from core.llm_router import ToolCallRequest

    conversation = router.conversation
    saved = conversation.client_type
    conversation.client_type = "mobile"

    # (query, label, expected_tool)
    # expected_tool = None means LLM should answer from knowledge (no tool call)
    mobile_queries = [
        ("what's the weather tomorrow", "weather forecast", "get_weather"),
        ("set a reminder for 5pm to pick up groceries", "set reminder", "manage_reminders"),
        ("search for pizza places near me", "local search", "web_search"),
        ("latest Alabama football score", "live sports", "web_search"),
        ("is Target in Gardendale open right now", "store hours", "web_search"),
        ("what is photosynthesis", "general knowledge", None),
        ("who was the first president", "history knowledge", None),
    ]

    try:
        for query, label, expected_tool in mobile_queries:
            r = router.route(query)
            tools = _tool_names(r)

            if expected_tool is None:
                # General knowledge: LLM should answer directly, NOT call web_search
                if r.use_tools:
                    called_tool = None
                    for item in router.llm.stream_with_tools(
                        user_message=r.llm_command or query,
                        tools=r.use_tools,
                        memory_context=r.memory_context,
                        conversation_messages=r.context_messages,
                        tool_temperature=r.tool_temperature or 0.0,
                        tool_presence_penalty=r.tool_presence_penalty or 0.0,
                    ):
                        if isinstance(item, ToolCallRequest):
                            called_tool = item.name
                            break
                    check(f"mobile e2e: '{label}' → LLM answers directly (no web_search)",
                          called_tool != "web_search",
                          f"called_tool={called_tool}")
                else:
                    # No tools at all — LLM answers from knowledge.  Good.
                    check(f"mobile e2e: '{label}' → LLM answers directly (no tools)",
                          True)
            else:
                # Tool query: correct tool must be available AND called
                check(f"mobile e2e: '{label}' → {expected_tool} available",
                      expected_tool in tools,
                      f"tools={tools}")

                if r.use_tools:
                    called_tool = None
                    for item in router.llm.stream_with_tools(
                        user_message=r.llm_command or query,
                        tools=r.use_tools,
                        memory_context=r.memory_context,
                        conversation_messages=r.context_messages,
                        tool_temperature=r.tool_temperature or 0.0,
                        tool_presence_penalty=r.tool_presence_penalty or 0.0,
                    ):
                        if isinstance(item, ToolCallRequest):
                            called_tool = item.name
                            break
                    check(f"mobile e2e: '{label}' → LLM calls {expected_tool}",
                          called_tool == expected_tool,
                          f"called_tool={called_tool}")
                else:
                    check(f"mobile e2e: '{label}' → LLM calls {expected_tool}",
                          False, "no tools available!")

        # Verify mobile context is always present for tool-calling queries
        r = router.route("what's the weather")
        check("mobile e2e: tool-calling response has MOBILE SESSION context",
              r.memory_context and "MOBILE SESSION" in r.memory_context)

    finally:
        conversation.client_type = saved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _verbose

    parser = argparse.ArgumentParser(description="Mobile routing test suite")
    parser.add_argument("--verbose", action="store_true", help="Show extra debug info")
    parser.add_argument("--phase", type=int, default=0,
                        help="Run only a specific section (1-9), 0=all")
    args = parser.parse_args()
    _verbose = args.verbose

    print("=" * 60)
    print("  Mobile Routing Test Suite")
    print("=" * 60)

    router, skill_manager = init_components()

    sections = {
        1: ("Detection Basics", lambda: test_detection_basics(router)),
        2: ("Skill Exclusion", lambda: test_mobile_skill_exclusion(router, skill_manager)),
        3: ("Tool Exclusion", lambda: test_mobile_tool_exclusion(router)),
        4: ("LLM Context", lambda: test_mobile_llm_context(router)),
        5: ("Web Search Guardrails", lambda: test_web_search_guardrails(router)),
        6: ("Web Nav Semantics", lambda: test_web_navigation_semantics(router, skill_manager)),
        7: ("Desktop Sanity", lambda: test_desktop_unaffected(router, skill_manager)),
        8: ("Fix D Fallback", lambda: test_fix_d_always_on_fallback(router)),
        9: ("E2E Mobile UX", lambda: test_e2e_mobile_experience(router)),
    }

    # Section 2b is always run with section 2
    phase = args.phase
    if phase == 0:
        for num, (name, fn) in sections.items():
            fn()
            if num == 2:
                test_preexec_blocking(router, skill_manager)
    elif phase in sections:
        sections[phase][1]()
        if phase == 2:
            test_preexec_blocking(router, skill_manager)
    else:
        print(f"Unknown phase {phase}. Valid: 1-9 or 0 (all)")
        return 1

    # Summary
    total = _total_passed + _total_failed
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY: {_total_passed} passed, {_total_failed} failed out of {total}")
    print(f"{'=' * 60}")

    return 0 if _total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
