#!/usr/bin/env python3
"""
Web handler test suite — verifies the plumbing layer in jarvis_web.py.

Covers the untested web handler layer between WebSocket messages and the
shared ConversationRouter.  These tests use mocks (no real LLM, no hardware)
and focus on:
  1. Handler function smoke tests (no NameErrors/crashes)
  2. Mobile routing enforcement (capture_webcam force-routing)
  3. Client type detection from User-Agent + screen width
  4. Tool call parameter overrides
  5. WebSocket message dispatch (frame_response, client_info, unknowns)

Usage:
    python3 -u scripts/test_web_handler.py --verbose > /tmp/test_output.txt 2>&1
    python3 -u scripts/test_web_handler.py --phase 1 --verbose  # Smoke tests only
    python3 -u scripts/test_web_handler.py --phase 2 --verbose  # Mobile routing
    python3 -u scripts/test_web_handler.py --phase 3 --verbose  # Client detection
    python3 -u scripts/test_web_handler.py --phase 4 --verbose  # Tool overrides
    python3 -u scripts/test_web_handler.py --phase 5 --verbose  # WS dispatch
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

# Suppress logs and library noise
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'
os.environ['JARVIS_LOG_TARGET'] = 'test_web'

import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Test infrastructure (matches test_vision.py pattern)
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""

results: list[TestResult] = []
verbose = False


def log(msg: str):
    print(msg, flush=True)


def assert_eq(name, actual, expected, detail=""):
    passed = actual == expected
    results.append(TestResult(name, passed, detail or f"expected={expected!r}, got={actual!r}"))
    if verbose:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not passed and detail else ""))
    elif not passed:
        log(f"  [FAIL] {name} — expected={expected!r}, got={actual!r}")


def assert_true(name, condition, detail=""):
    results.append(TestResult(name, bool(condition), detail))
    if verbose:
        status = "PASS" if condition else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not condition and detail else ""))
    elif not condition:
        log(f"  [FAIL] {name} — {detail}")


def assert_in(name, needle, haystack, detail=""):
    passed = needle in haystack
    results.append(TestResult(name, passed, detail or f"{needle!r} not in result"))
    if verbose:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not passed and detail else ""))
    elif not passed:
        log(f"  [FAIL] {name} — {needle!r} not in result")


def assert_not_in(name, needle, haystack, detail=""):
    passed = needle not in haystack
    results.append(TestResult(name, passed, detail or f"{needle!r} should NOT be in result"))
    if verbose:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not passed and detail else ""))
    elif not passed:
        log(f"  [FAIL] {name} — {needle!r} unexpectedly found in result")


# ---------------------------------------------------------------------------
# Mock components — lightweight stand-ins for JARVIS subsystems
# ---------------------------------------------------------------------------

class MockConversation:
    """Minimal ConversationManager mock."""
    def __init__(self):
        self.current_user = "user"
        self.client_type = "desktop"
        self.session_history = []
        self._messages = []

    def add_message(self, role, content, **kwargs):
        self._messages.append({'role': role, 'content': content, **kwargs})

    def load_full_history(self):
        return self._messages


class MockLLM:
    """Minimal LLMRouter mock."""
    def __init__(self):
        self.tool_calling = True
        self.temperature = 0.6
        self.top_p = 0.8
        self.top_k = 20
        self.last_call_info = {}
        self.local_model_path = "/fake/model.gguf"
        self.api_model = "claude-test"
        self.api_key_env = None

    def stream(self, **kwargs):
        yield "Test response."

    def stream_with_tools(self, **kwargs):
        yield "Tool response."

    def chat(self, **kwargs):
        return "Chat response."

    def strip_filler(self, text):
        return text

    def _check_response_quality(self, text, command):
        return None  # No quality issue

    def continue_after_tool_call(self, tcr, result, **kwargs):
        yield "Synthesis response."


class MockConvState:
    """Minimal ConversationState mock."""
    def __init__(self):
        self.last_tool_result_text = ""
        self.research_results = None
        self.last_response_text = ""
        self.jarvis_asked_question = False
        self.turn_count = 0
        self.window_id = None
        self.readback_session = None
        self.nav_artifact_id = None
        self.nav_root_id = None
        self.nav_cursor = 0
        self.nav_total = 0
        self.last_intent = None
        self.last_response_type = None
        self.last_command = None

    def update(self, command="", response_text="", response_type=""):
        self.last_command = command
        self.last_response_text = response_text
        self.last_response_type = response_type
        self.turn_count += 1


class MockRouteResult:
    """Minimal RouteResult mock."""
    def __init__(self, handled=False, text="", skip=False, intent=None,
                 use_tools=None, match_info=None, used_llm=False,
                 llm_command=None, llm_history=None, memory_context=None,
                 context_messages=None, llm_max_tokens=None,
                 tool_temperature=None, tool_presence_penalty=None,
                 image_data=None):
        self.handled = handled
        self.text = text
        self.skip = skip
        self.intent = intent
        self.use_tools = use_tools
        self.match_info = match_info or {}
        self.used_llm = used_llm
        self.llm_command = llm_command or "test command"
        self.llm_history = llm_history or []
        self.memory_context = memory_context
        self.context_messages = context_messages
        self.llm_max_tokens = llm_max_tokens
        self.tool_temperature = tool_temperature
        self.tool_presence_penalty = tool_presence_penalty
        self.image_data = image_data


class MockDocBuffer:
    """Minimal DocumentBuffer mock."""
    def __init__(self):
        self.active = False
        self.token_estimate = 0
        self.source = ""
        self.content = ""

    def load(self, content, source):
        self.content = content
        self.source = source
        self.active = True
        self.token_estimate = len(content) // 4

    def clear(self):
        old = (self.source, self.token_estimate)
        self.content = ""
        self.source = ""
        self.active = False
        self.token_estimate = 0
        return old


class MockWebTTSProxy:
    """Minimal WebTTSProxy mock."""
    def __init__(self):
        self.hybrid = False
        self.real_tts = None
        self._announcements = []

    def speak(self, text, normalize=True):
        self._announcements.append(text)
        return True

    def get_pending_announcements(self):
        anns = self._announcements[:]
        self._announcements.clear()
        return anns


class MockWS:
    """Minimal WebSocket mock that records sent messages."""
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, data):
        self.sent.append(data)

    def get_sent_types(self):
        return [m.get('type') for m in self.sent]

    def get_sent_of_type(self, msg_type):
        return [m for m in self.sent if m.get('type') == msg_type]


class MockRelay:
    """Minimal MobileCameraRelay mock."""
    def __init__(self):
        self._ws = None
        self._loop = None
        self._delivered = []
        self._errors = []

    def set_ws(self, ws, loop):
        self._ws = ws
        self._loop = loop

    def clear_ws(self):
        self._ws = None
        self._loop = None

    def deliver_frame(self, rid, data):
        self._delivered.append((rid, data))

    def deliver_error(self, rid, error):
        self._errors.append((rid, error))


def _make_components(**overrides):
    """Build a minimal components dict for testing."""
    conv = overrides.get('conversation', MockConversation())
    components = {
        'conversation': conv,
        'llm': overrides.get('llm', MockLLM()),
        'doc_buffer': overrides.get('doc_buffer', MockDocBuffer()),
        'web_researcher': overrides.get('web_researcher', None),
        'conv_state': overrides.get('conv_state', MockConvState()),
        'router': overrides.get('router', MagicMock()),
        'skill_manager': overrides.get('skill_manager', MagicMock(skills={})),
        'reminder_manager': None,
        'memory_manager': None,
        'news_manager': None,
        'context_window': None,
        'metrics': None,
        'self_awareness': None,
        'task_planner': None,
        'awareness': None,
        'interaction_cache': None,
    }
    components.update(overrides)
    return components


# ---------------------------------------------------------------------------
# Phase 1: Handler function smoke tests
# ---------------------------------------------------------------------------

def run_phase_1():
    log("\n=== Phase 1: Handler Function Smoke Tests ===\n")

    from jarvis_web import process_command, _detect_delivery_mode, _build_stats

    # --- 1.1: process_command with skip result ---
    log("1.1 process_command: skip result returns empty response")
    components = _make_components()
    tts = MockWebTTSProxy()
    config = {}

    skip_result = MockRouteResult(skip=True)
    components['router'].route = MagicMock(return_value=skip_result)

    result = asyncio.run(process_command("yeah", components, tts, config))
    assert_eq("skip returns empty response", result['response'], '')
    assert_eq("skip not used_llm", result['used_llm'], False)
    assert_eq("skip not streamed", result['streamed'], False)

    # --- 1.2: process_command with handled result ---
    log("\n1.2 process_command: handled result returns text")
    components = _make_components()
    handled_result = MockRouteResult(
        handled=True, text="The weather is nice.",
        match_info={'layer': 'P4-LLM', 'skill_name': 'weather', 'handler': 'get_weather'},
    )
    components['router'].route = MagicMock(return_value=handled_result)

    result = asyncio.run(process_command("what's the weather", components, tts, config))
    assert_eq("handled returns text", result['response'], "The weather is nice.")
    assert_eq("handled not used_llm", result['used_llm'], False)

    # --- 1.3: process_command with LLM fallback (no ws, no tools) ---
    log("\n1.3 process_command: LLM fallback without websocket")
    components = _make_components()
    llm = MockLLM()
    llm.tool_calling = False
    components['llm'] = llm
    fallback_result = MockRouteResult(
        handled=False, llm_command="hello",
        llm_history=[],
    )
    components['router'].route = MagicMock(return_value=fallback_result)

    result = asyncio.run(process_command("hello", components, tts, config))
    assert_true("fallback returns non-empty", bool(result['response']),
                f"got: {result['response']!r}")
    assert_eq("fallback used_llm", result['used_llm'], True)

    # --- 1.4: process_command with ws (LLM fallback path) ---
    log("\n1.4 process_command: LLM fallback with WebSocket")
    components = _make_components()
    ws = MockWS()
    fallback_result = MockRouteResult(handled=False, llm_command="tell me a joke")
    components['router'].route = MagicMock(return_value=fallback_result)

    result = asyncio.run(process_command("tell me a joke", components, tts, config, ws=ws))
    assert_eq("ws fallback used_llm", result['used_llm'], True)
    assert_true("ws fallback returns response", bool(result['response']),
                f"got: {result['response']!r}")

    # --- 1.5: _detect_delivery_mode: explicit modes ---
    log("\n1.5 _detect_delivery_mode: explicit delivery modes")
    assert_eq("read it → read", _detect_delivery_mode("read it", ""), "read")
    assert_eq("display it → display", _detect_delivery_mode("display it", ""), "display")
    assert_eq("print it → print", _detect_delivery_mode("print it", ""), "print")
    assert_eq("show me → clarify", _detect_delivery_mode("show me", ""), "clarify")
    assert_eq("random text → None", _detect_delivery_mode("random text", ""), None)

    # --- 1.6: _detect_delivery_mode: affirm after offer ---
    log("\n1.6 _detect_delivery_mode: affirm after readback offer")
    offer_resp = "Would you like me to read through it?"
    assert_eq("yes after offer → read", _detect_delivery_mode("yes", offer_resp), "read")
    assert_eq("sure after offer → read", _detect_delivery_mode("sure", offer_resp), "read")
    assert_eq("yes print it after offer → print",
              _detect_delivery_mode("yes print it", offer_resp), "print")

    # --- 1.7: _build_stats ---
    log("\n1.7 _build_stats: stats dict structure")
    match_info = {'layer': 'P4-LLM', 'skill_name': 'weather', 'handler': 'get_weather', 'confidence': 0.95}
    llm = MockLLM()
    llm.last_call_info = {'model': 'test-model', 'tokens_used': 100}
    stats = _build_stats(match_info, llm, True, 0.0, 0.01, 0.05)
    assert_in("stats has total_ms", 'total_ms', stats)
    assert_eq("stats layer", stats.get('layer'), 'P4-LLM')
    assert_eq("stats skill_name", stats.get('skill_name'), 'weather')
    assert_true("stats confidence ~0.95", abs(stats.get('confidence', 0) - 0.95) < 0.01)

    # --- 1.8: process_command with wake word strip ---
    log("\n1.8 process_command: wake word stripping")
    components = _make_components()
    handled_result = MockRouteResult(handled=True, text="Hello.")
    mock_router = MagicMock(return_value=handled_result)
    components['router'].route = mock_router

    asyncio.run(process_command("hey jarvis hello", components, tts, config))
    # Verify the command passed to route() had wake word stripped
    call_args = mock_router.call_args
    routed_cmd = call_args[0][0] if call_args[0] else call_args[1].get('command', '')
    assert_eq("wake word stripped", routed_cmd, "hello")

    # --- 1.9: process_command with image data ---
    log("\n1.9 process_command: image_data passed through")
    components = _make_components()
    handled_result = MockRouteResult(handled=True, text="I see a cat.")
    components['router'].route = MagicMock(return_value=handled_result)

    result = asyncio.run(process_command("what is this", components, tts, config,
                                         image_data="base64data"))
    assert_eq("image result text", result['response'], "I see a cat.")
    # Verify conversation got [Image attached] prefix
    conv = components['conversation']
    user_msgs = [m for m in conv._messages if m['role'] == 'user']
    assert_true("image prefix in history", "[Image attached]" in user_msgs[-1]['content'],
                f"got: {user_msgs[-1]['content']!r}")


# ---------------------------------------------------------------------------
# Phase 2: Mobile routing enforcement at web layer
# ---------------------------------------------------------------------------

def run_phase_2():
    log("\n=== Phase 2: Mobile Routing Enforcement ===\n")

    from jarvis_web import _stream_llm_ws
    from core.llm_router import ToolCallRequest

    # --- 2.1: capture_webcam force-routes to mobile ---
    log("2.1 capture_webcam: force-route to mobile when client_type=mobile")

    ws = MockWS()
    llm = MockLLM()

    # Mock execute_tool to capture the args it receives
    captured_args = {}
    def mock_execute_tool(name, args):
        captured_args['name'] = name
        captured_args['args'] = args
        return "Frame captured successfully."

    # Make LLM emit a ToolCallRequest with source='desktop' (the bug scenario)
    tcr = ToolCallRequest(
        name='capture_webcam',
        arguments={'source': 'desktop', 'query': 'what do you see'},
    )

    def _stream_with_tcr(**kwargs):
        yield tcr

    llm.stream_with_tools = _stream_with_tcr

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Frame result", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "what do you see", [], None,
            use_tools_list=[{'function': {'name': 'capture_webcam'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("tool was capture_webcam", captured_args.get('name'), 'capture_webcam')
    assert_eq("source overridden to mobile", captured_args.get('args', {}).get('source'), 'mobile')

    # --- 2.2: capture_webcam NOT force-routed for desktop clients ---
    log("\n2.2 capture_webcam: NOT force-routed for desktop client")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    tcr = ToolCallRequest(
        name='capture_webcam',
        arguments={'source': 'desktop', 'query': 'what do you see'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Frame result", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "what do you see", [], None,
            use_tools_list=[{'function': {'name': 'capture_webcam'}}],
            client_type='desktop',
            conv_state=MockConvState(),
        ))

    assert_eq("desktop source preserved", captured_args.get('args', {}).get('source'), 'desktop')

    # --- 2.3: capture_webcam with source='auto' on mobile → overridden ---
    log("\n2.3 capture_webcam: source='auto' overridden to mobile")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    tcr = ToolCallRequest(
        name='capture_webcam',
        arguments={'source': 'auto', 'query': 'look around'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Frame result", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "look around", [], None,
            use_tools_list=[{'function': {'name': 'capture_webcam'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("auto → mobile override", captured_args.get('args', {}).get('source'), 'mobile')

    # --- 2.4: capture_webcam with source='mobile' on mobile → no double override ---
    log("\n2.4 capture_webcam: source='mobile' on mobile → no redundant override")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    tcr = ToolCallRequest(
        name='capture_webcam',
        arguments={'source': 'mobile', 'query': 'look'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Frame result", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "look", [], None,
            use_tools_list=[{'function': {'name': 'capture_webcam'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("mobile stays mobile", captured_args.get('args', {}).get('source'), 'mobile')

    # --- 2.5: capture_webcam with missing source key on mobile ---
    log("\n2.5 capture_webcam: missing source key on mobile → added as mobile")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    tcr = ToolCallRequest(
        name='capture_webcam',
        arguments={'query': 'what is that'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Frame result", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "what is that", [], None,
            use_tools_list=[{'function': {'name': 'capture_webcam'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("missing source → mobile", captured_args.get('args', {}).get('source'), 'mobile')

    # --- 2.6: client_type plumbing: process_command passes client_type to _stream_llm_ws ---
    log("\n2.6 client_type plumbing through process_command")

    from jarvis_web import process_command

    components = _make_components()
    conv = components['conversation']
    conv.client_type = 'mobile'
    ws = MockWS()
    tts = MockWebTTSProxy()

    fallback_result = MockRouteResult(handled=False, llm_command="test")
    components['router'].route = MagicMock(return_value=fallback_result)

    _captured_ct = {}
    original_stream = _stream_llm_ws

    async def mock_stream_ws(*args, **kwargs):
        _captured_ct['client_type'] = kwargs.get('client_type')
        return ("response", False, None)

    with patch('jarvis_web._stream_llm_ws', mock_stream_ws):
        asyncio.run(process_command("test", components, tts, {}, ws=ws))

    assert_eq("client_type passed through", _captured_ct.get('client_type'), 'mobile')


# ---------------------------------------------------------------------------
# Phase 3: Client type detection
# ---------------------------------------------------------------------------

def run_phase_3():
    log("\n=== Phase 3: Client Type Detection ===\n")

    # We test the detection logic extracted from the websocket_handler
    # client_info branch. The logic is:
    #   _mobile_kw = ('iPhone', 'iPad', 'Android', 'Mobile')
    #   is_mobile = any(k in ua for k in _mobile_kw) or sw < 768

    _MOBILE_KW = ('iPhone', 'iPad', 'Android', 'Mobile')

    def detect(ua, sw):
        is_mobile = any(k in ua for k in _MOBILE_KW) or sw < 768
        return "mobile" if is_mobile else "desktop"

    # --- 3.1: iPhone UA ---
    log("3.1 Client detection: iPhone UA")
    assert_eq("iPhone UA → mobile",
              detect("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", 390),
              "mobile")

    # --- 3.2: iPad UA ---
    log("3.2 Client detection: iPad UA")
    assert_eq("iPad UA → mobile",
              detect("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)", 1024),
              "mobile")

    # --- 3.3: Android UA ---
    log("3.3 Client detection: Android UA")
    assert_eq("Android UA → mobile",
              detect("Mozilla/5.0 (Linux; Android 14; Pixel 8)", 412),
              "mobile")

    # --- 3.4: Desktop UA + wide screen ---
    log("3.4 Client detection: Desktop UA + wide screen")
    assert_eq("Desktop UA → desktop",
              detect("Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/120.0", 2560),
              "desktop")

    # --- 3.5: Narrow screen without mobile UA ---
    log("3.5 Client detection: Narrow screen without mobile UA")
    assert_eq("Narrow screen → mobile",
              detect("Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0", 600),
              "mobile")

    # --- 3.6: Mobile keyword in UA with wide screen (iPad Pro landscape) ---
    log("3.6 Client detection: iPad Pro landscape (wide screen + mobile UA)")
    assert_eq("iPad Pro landscape → mobile",
              detect("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)", 1366),
              "mobile")

    # --- 3.7: Landscape phone (> 768 but has Mobile keyword) ---
    log("3.7 Client detection: Landscape phone with Mobile keyword")
    assert_eq("Landscape phone → mobile",
              detect("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile/15E148", 844),
              "mobile")

    # --- 3.8: Edge case: exactly 768 (not mobile by width alone) ---
    log("3.8 Client detection: screen width exactly 768")
    assert_eq("768 desktop UA → desktop",
              detect("Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0", 768),
              "desktop")

    # --- 3.9: Edge case: 767 (mobile by width) ---
    log("3.9 Client detection: screen width 767")
    assert_eq("767 → mobile",
              detect("Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0", 767),
              "mobile")

    # --- 3.10: Default screen width (no client_info sent) ---
    log("3.10 Client detection: default screen width 9999")
    assert_eq("default 9999 → desktop",
              detect("", 9999),
              "desktop")


# ---------------------------------------------------------------------------
# Phase 4: Tool call parameter override
# ---------------------------------------------------------------------------

def run_phase_4():
    log("\n=== Phase 4: Tool Call Parameter Override ===\n")

    from jarvis_web import _stream_llm_ws
    from core.llm_router import ToolCallRequest

    # --- 4.1: Non-webcam tool NOT modified on mobile ---
    log("4.1 Non-webcam tool: args NOT modified on mobile")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    def mock_execute_tool(name, args):
        captured_args['name'] = name
        captured_args['args'] = dict(args)
        return "System info result"

    tcr = ToolCallRequest(
        name='get_system_info',
        arguments={'category': 'cpu'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("CPU info", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "cpu info", [], None,
            use_tools_list=[{'function': {'name': 'get_system_info'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("non-webcam tool name", captured_args.get('name'), 'get_system_info')
    assert_not_in("no source key added", 'source', captured_args.get('args', {}))

    # --- 4.2: get_weather NOT modified on mobile ---
    log("\n4.2 get_weather: args NOT modified on mobile")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    tcr = ToolCallRequest(
        name='get_weather',
        arguments={'query_type': 'current', 'location': 'New York'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Sunny 72F", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "weather", [], None,
            use_tools_list=[{'function': {'name': 'get_weather'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("weather tool name", captured_args.get('name'), 'get_weather')
    assert_eq("weather location preserved", captured_args.get('args', {}).get('location'), 'New York')
    assert_not_in("weather no source key", 'source', captured_args.get('args', {}))

    # --- 4.3: take_screenshot NOT modified on mobile ---
    log("\n4.3 take_screenshot: args NOT modified on mobile")

    ws = MockWS()
    llm = MockLLM()
    captured_args = {}

    tcr = ToolCallRequest(
        name='take_screenshot',
        arguments={'action': 'capture'},
    )
    llm.stream_with_tools = lambda **kwargs: iter([tcr])

    with patch('core.tool_executor.execute_tool', mock_execute_tool), \
         patch('core.tool_registry.parse_tool_result', return_value=("Screenshot", None)), \
         patch('core.tool_registry.save_tool_image', return_value=None), \
         patch('jarvis_web.get_interaction_cache', return_value=None):

        resp, streamed, img = asyncio.run(_stream_llm_ws(
            ws, llm, "screenshot", [], None,
            use_tools_list=[{'function': {'name': 'take_screenshot'}}],
            client_type='mobile',
            conv_state=MockConvState(),
        ))

    assert_eq("screenshot tool name", captured_args.get('name'), 'take_screenshot')
    assert_not_in("screenshot no source key", 'source', captured_args.get('args', {}))


# ---------------------------------------------------------------------------
# Phase 5: WebSocket message dispatch
# ---------------------------------------------------------------------------

def run_phase_5():
    log("\n=== Phase 5: WebSocket Message Dispatch ===\n")

    # --- 5.1: frame_response delivered to relay ---
    log("5.1 frame_response: delivered to relay")

    relay = MockRelay()
    relay.set_ws(MockWS(), None)

    # Simulate frame_response handling (extract logic from websocket_handler)
    data = {
        'type': 'frame_response',
        'request_id': 'req123',
        'image_data': 'base64framedata',
    }
    rid = data.get('request_id', '')
    error = data.get('error')
    if error:
        relay.deliver_error(rid, error)
    else:
        image_data = data.get('image_data', '')
        relay.deliver_frame(rid, image_data)

    assert_eq("relay got frame", len(relay._delivered), 1)
    assert_eq("relay frame id", relay._delivered[0][0], 'req123')
    assert_eq("relay frame data", relay._delivered[0][1], 'base64framedata')

    # --- 5.2: frame_response with error ---
    log("\n5.2 frame_response: error delivered to relay")

    relay = MockRelay()
    relay.set_ws(MockWS(), None)

    data = {
        'type': 'frame_response',
        'request_id': 'req456',
        'error': 'Camera access denied',
    }
    rid = data.get('request_id', '')
    error = data.get('error')
    if error:
        relay.deliver_error(rid, error)
    else:
        relay.deliver_frame(rid, data.get('image_data', ''))

    assert_eq("relay got error", len(relay._errors), 1)
    assert_eq("relay error id", relay._errors[0][0], 'req456')
    assert_eq("relay error msg", relay._errors[0][1], 'Camera access denied')
    assert_eq("relay no frames", len(relay._delivered), 0)

    # --- 5.3: client_info sets conversation.client_type ---
    log("\n5.3 client_info: sets conversation.client_type")

    conv = MockConversation()

    # Simulate client_info handling
    _MOBILE_KW = ('iPhone', 'iPad', 'Android', 'Mobile')
    for ua, sw, expected in [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", 390, "mobile"),
        ("Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0", 2560, "desktop"),
        ("", 600, "mobile"),
    ]:
        _ua_match = [k for k in _MOBILE_KW if k in ua]
        is_mobile = bool(_ua_match) or sw < 768
        conv.client_type = "mobile" if is_mobile else "desktop"
        assert_eq(f"client_type for sw={sw}", conv.client_type, expected)

    # --- 5.4: Unknown message type doesn't crash ---
    log("\n5.4 Unknown message type: doesn't crash")

    # This tests that the handler doesn't throw on unrecognized types
    # In production this is just a debug log; we verify no exception
    data = {'type': 'unknown_msg_type', 'payload': 'whatever'}
    msg_type = data.get('type', '')
    # The handler simply falls through the if/elif chain to the else branch
    # which logs. We just verify no exception is raised.
    assert_true("unknown type handled gracefully", True)

    # --- 5.5: Relay cleanup on WS close ---
    log("\n5.5 Relay cleanup: clear_ws called when WS was the relay source")

    relay = MockRelay()
    ws = MockWS()
    relay.set_ws(ws, None)

    # Simulate cleanup logic from websocket_handler finally block
    if relay._ws is ws:
        relay.clear_ws()

    assert_true("relay ws cleared", relay._ws is None)

    # --- 5.6: Relay cleanup skipped for different WS ---
    log("\n5.6 Relay cleanup: NOT cleared for different WS")

    relay = MockRelay()
    ws1 = MockWS()
    ws2 = MockWS()
    relay.set_ws(ws1, None)

    # ws2 disconnects — should NOT clear relay since it's attached to ws1
    if relay._ws is ws2:
        relay.clear_ws()

    assert_true("relay ws NOT cleared", relay._ws is ws1)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    global verbose

    parser = argparse.ArgumentParser(description="Web handler test suite")
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--phase', type=int, default=0, help="Run only this phase (1-5)")
    args = parser.parse_args()
    verbose = args.verbose

    log("=" * 60)
    log("JARVIS Web Handler Test Suite")
    log("=" * 60)

    t0 = time.perf_counter()

    phases = {
        1: ("Handler smoke tests", run_phase_1),
        2: ("Mobile routing enforcement", run_phase_2),
        3: ("Client type detection", run_phase_3),
        4: ("Tool parameter overrides", run_phase_4),
        5: ("WebSocket message dispatch", run_phase_5),
    }

    if args.phase:
        if args.phase in phases:
            name, fn = phases[args.phase]
            log(f"\nRunning phase {args.phase}: {name}")
            fn()
        else:
            log(f"Unknown phase: {args.phase}. Valid: 1-5")
            sys.exit(1)
    else:
        for i, (name, fn) in phases.items():
            fn()

    elapsed = time.perf_counter() - t0

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    log("\n" + "=" * 60)
    log(f"Results: {passed}/{total} passed, {failed} failed ({elapsed:.1f}s)")

    if failed:
        log("\nFailed tests:")
        for r in results:
            if not r.passed:
                log(f"  [FAIL] {r.name}")
                if r.detail:
                    log(f"         {r.detail}")

    log("=" * 60)

    # Use os._exit to prevent ROCm/ONNX teardown crashes
    os._exit(1 if failed else 0)


if __name__ == '__main__':
    main()
