#!/usr/bin/env python3
"""
Delivery Mode system tests — unit + simulated integration.

Tests:
  Part 1: _detect_delivery_mode() pure unit tests (no I/O)
  Part 2: Handler integration tests (mock WS, mock LLM, mock TTS, mock subprocess)
  Part 3: Two-turn clarification flow simulation
  Part 4: Edge cases (no content, no printer, no URL)

Usage:
  python3 /tmp/test_delivery_modes.py --verbose
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from typing import Optional

# Add project root to path
sys.path.insert(0, "/home/user/jarvis")

# ─── Mock heavy imports before they're pulled in ───────────────────────────
# Stub out modules that need GPU/hardware
sys.modules.setdefault("porcupine", MagicMock())
sys.modules.setdefault("pvporcupine", MagicMock())
sys.modules.setdefault("resemblyzer", MagicMock())
sys.modules.setdefault("resemblyzer.VoiceEncoder", MagicMock())

# ─── Imports ───────────────────────────────────────────────────────────────

from core.conversation_state import ConversationState
from core.readback_session import ReadbackSession, ReadbackChunk

# Import detection function directly
from jarvis_web import (
    _detect_delivery_mode,
    _AFFIRM_WORDS,
    _DELIVERY_MODES,
    _DELIVERY_CLARIFY,
    _SHOW_ME_RESOLVERS,
    _OFFER_PHRASES,
    _CLARIFY_PHRASES,
)

# ─── Test infrastructure ──────────────────────────────────────────────────

VERBOSE = "--verbose" in sys.argv

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""

results: list[TestResult] = []

def log(msg):
    if VERBOSE:
        print(msg)

def assert_eq(name, actual, expected, context=""):
    ok = actual == expected
    detail = f"expected={expected!r}, got={actual!r}"
    if context:
        detail += f" [{context}]"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok

def assert_true(name, condition, detail=""):
    ok = bool(condition)
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok

def assert_in(name, needle, haystack, context=""):
    ok = needle.lower() in haystack.lower() if isinstance(haystack, str) else needle in haystack
    detail = f"{needle!r} not in response"
    if context:
        detail += f" [{context}]"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


# ─── Mock classes ─────────────────────────────────────────────────────────

class MockWebSocket:
    """Captures all send_json calls for inspection."""
    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(data)

    def get_tokens(self):
        return [m.get("token", "") for m in self.messages if m.get("type") == "stream_token"]

    def get_types(self):
        return [m.get("type") for m in self.messages]

    def has_stream(self):
        types = self.get_types()
        return "stream_start" in types and "stream_end" in types


class MockTTSProxy:
    """Records TTS speak calls."""
    def __init__(self):
        self.spoken = []
        self.hybrid = True
        self.real_tts = MagicMock()
        self.real_tts.speak = lambda text: self.spoken.append(text)


class MockLLM:
    """Returns pre-set JSON for readback parsing."""
    def __init__(self, parse_response=None):
        self._response = parse_response

    def chat(self, prompt, max_tokens=4096):
        return self._response


def make_recipe_json():
    """Standard test recipe as a JSON string — what the LLM returns."""
    return json.dumps({
        "title": "Classic Banana Bread",
        "source": "AllRecipes",
        "preamble": "A simple banana bread recipe that's moist and delicious.",
        "sections": [
            {
                "type": "ingredients",
                "title": "Ingredients",
                "items": [
                    "3 ripe bananas",
                    "1/3 cup melted butter",
                    "3/4 cup sugar",
                    "1 egg, beaten",
                    "1 tsp vanilla extract",
                    "1 tsp baking soda",
                    "Pinch of salt",
                    "1 1/2 cups all-purpose flour"
                ]
            },
            {
                "type": "instructions",
                "title": "Instructions",
                "steps": [
                    {"step": 1, "text": "Preheat oven to 350°F (175°C)."},
                    {"step": 2, "text": "Mash the ripe bananas in a mixing bowl."},
                    {"step": 3, "text": "Stir in melted butter."},
                    {"step": 4, "text": "Mix in the baking soda and salt."},
                    {"step": 5, "text": "Stir in the sugar, beaten egg, and vanilla extract."},
                    {"step": 6, "text": "Mix in the flour."},
                    {"step": 7, "text": "Pour batter into a greased loaf pan."},
                    {"step": 8, "text": "Bake for 60-65 minutes."}
                ]
            },
            {
                "type": "notes",
                "title": "Tips",
                "text": "The riper the bananas, the better the flavor."
            }
        ]
    })


def make_conv_state_with_content(last_response="", offer=True):
    """Create a ConversationState pre-loaded with cached tool result text."""
    cs = ConversationState()
    cs.jarvis_asked_question = True
    cs.conversation_active = True
    cs.last_tool_result_text = "Raw search results with banana bread recipe from AllRecipes..."
    if offer:
        cs.last_response_text = last_response or (
            "I found a banana bread recipe from AllRecipes. "
            "Would you like me to read through it?"
        )
    else:
        cs.last_response_text = last_response or ""
    cs.window_id = ""  # No artifact cache in tests
    return cs


# ═══════════════════════════════════════════════════════════════════════════
# PART 1: _detect_delivery_mode() unit tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part1_detection():
    print("\n" + "=" * 70)
    print("PART 1: _detect_delivery_mode() — Detection Logic")
    print("=" * 70)

    offer = "I found a banana bread recipe. Would you like me to read through it?"
    clarify = "I can read it to you, show it here in our chat, print it out, or open the page online. What would you prefer, sir?"
    no_offer = "Here's what I found about banana bread."

    # ── 1A: Generic affirms → "read" when offer present ──
    print("\n--- 1A: Generic affirms → read (with offer context) ---")
    for phrase in ["yes", "yeah", "sure", "go ahead", "yes please", "ok",
                   "okay", "yep", "definitely", "absolutely", "please"]:
        assert_eq(f"affirm '{phrase}' + offer → read",
                  _detect_delivery_mode(phrase, offer), "read")

    # ── 1B: Generic affirms → None when NO offer ──
    print("\n--- 1B: Generic affirms → None (no offer context) ---")
    for phrase in ["yes", "yeah", "sure"]:
        assert_eq(f"affirm '{phrase}' + no offer → None",
                  _detect_delivery_mode(phrase, no_offer), None)

    # ── 1C: Explicit read mode keywords ──
    print("\n--- 1C: Explicit read keywords ---")
    for phrase in ["read it", "read it to me", "read through it",
                   "go through it", "read it out", "read that"]:
        assert_eq(f"'{phrase}' → read",
                  _detect_delivery_mode(phrase, offer), "read")

    # ── 1D: Explicit display mode keywords ──
    print("\n--- 1D: Explicit display keywords ---")
    for phrase in ["display it", "show it in chat", "in the chat",
                   "put it in chat", "display it in chat", "in chat"]:
        assert_eq(f"'{phrase}' → display",
                  _detect_delivery_mode(phrase, offer), "display")

    # ── 1E: Explicit print mode keywords ──
    print("\n--- 1E: Explicit print keywords ---")
    for phrase in ["print it", "print it out", "send it to the printer",
                   "print that", "print that out"]:
        assert_eq(f"'{phrase}' → print",
                  _detect_delivery_mode(phrase, offer), "print")

    # ── 1F: Explicit browse mode keywords ──
    print("\n--- 1F: Explicit browse keywords ---")
    for phrase in ["open it online", "see it online", "open the link",
                   "open the page", "open it in the browser", "open the url",
                   "pull it up"]:
        assert_eq(f"'{phrase}' → browse",
                  _detect_delivery_mode(phrase, offer), "browse")

    # ── 1G: Clarify mode keywords ──
    print("\n--- 1G: Clarify keywords ---")
    for phrase in ["show me", "show it to me", "can i see it",
                   "let me see", "show it", "let me see it"]:
        assert_eq(f"'{phrase}' → clarify",
                  _detect_delivery_mode(phrase, offer), "clarify")

    # ── 1H: Affirm + trailing delivery mode ──
    print("\n--- 1H: Affirm prefix + delivery mode ---")
    assert_eq("'yes read it to me' → read",
              _detect_delivery_mode("yes read it to me", offer), "read")
    assert_eq("'sure print it out' → print",
              _detect_delivery_mode("sure print it out", offer), "print")
    assert_eq("'yeah show it in chat' → display",
              _detect_delivery_mode("yeah show it in chat", offer), "display")
    assert_eq("'yes open the link' → browse",
              _detect_delivery_mode("yes open the link", offer), "browse")
    assert_eq("'yes please show me' → clarify",
              _detect_delivery_mode("yes please show me", offer), "clarify")

    # ── 1I: Punctuation stripping ──
    print("\n--- 1I: Punctuation handling ---")
    assert_eq("'yes!' → read (with offer)",
              _detect_delivery_mode("yes!", offer), "read")
    assert_eq("'print it.' → print",
              _detect_delivery_mode("print it.", offer), "print")
    assert_eq("'show me?' → clarify",
              _detect_delivery_mode("show me?", offer), "clarify")

    # ── 1J: Totally unrelated → None ──
    print("\n--- 1J: Unrelated commands → None ---")
    for phrase in ["what's the weather", "tell me a joke",
                   "turn off the lights", "no thanks", "never mind"]:
        assert_eq(f"'{phrase}' → None",
                  _detect_delivery_mode(phrase, offer), None)

    # ── 1K: Two-turn clarification — affirm after clarify response ──
    print("\n--- 1K: Two-turn clarification flow ---")
    # After JARVIS asked "I can read it to you, show it here in chat..."
    assert_eq("'read it to me' after clarify → read",
              _detect_delivery_mode("read it to me", clarify), "read")
    assert_eq("'in the chat' after clarify → display",
              _detect_delivery_mode("in the chat", clarify), "display")
    assert_eq("'print it' after clarify → print",
              _detect_delivery_mode("print it", clarify), "print")
    assert_eq("'open it online' after clarify → browse",
              _detect_delivery_mode("open it online", clarify), "browse")
    # Bare affirm after clarify → default to read
    assert_eq("'yes' after clarify → read (default)",
              _detect_delivery_mode("yes", clarify), "read")
    assert_eq("'sure' after clarify → read (default)",
              _detect_delivery_mode("sure", clarify), "read")
    # Affirm + specific mode after clarify
    assert_eq("'yes print it' after clarify → print",
              _detect_delivery_mode("yes print it", clarify), "print")
    assert_eq("'sure show it in chat' after clarify → display",
              _detect_delivery_mode("sure show it in chat", clarify), "display")

    # ── 1L: Explicit keywords work with NO offer context too ──
    print("\n--- 1L: Explicit keywords don't need offer context ---")
    assert_eq("'print it' + no offer → print",
              _detect_delivery_mode("print it", no_offer), "print")
    assert_eq("'show it in chat' + no offer → display",
              _detect_delivery_mode("show it in chat", no_offer), "display")
    assert_eq("'open the link' + no offer → browse",
              _detect_delivery_mode("open the link", no_offer), "browse")
    assert_eq("'show me' + no offer → clarify",
              _detect_delivery_mode("show me", no_offer), "clarify")

    # ── 1M: Case insensitivity ──
    print("\n--- 1M: Case insensitivity ---")
    assert_eq("'Print It Out' → print",
              _detect_delivery_mode("Print It Out", offer), "print")
    assert_eq("'SHOW ME' → clarify",
              _detect_delivery_mode("SHOW ME", offer), "clarify")
    assert_eq("'YES' + offer → read",
              _detect_delivery_mode("YES", offer), "read")

    # ── 1N: Empty / whitespace edge cases ──
    print("\n--- 1N: Edge cases ---")
    assert_eq("empty string → None",
              _detect_delivery_mode("", offer), None)
    assert_eq("whitespace → None",
              _detect_delivery_mode("   ", offer), None)
    assert_eq("None last_response → handled",
              _detect_delivery_mode("print it", None), "print")


# ═══════════════════════════════════════════════════════════════════════════
# PART 2: Handler integration tests (async, mocked deps)
# ═══════════════════════════════════════════════════════════════════════════

async def test_part2_handlers():
    print("\n" + "=" * 70)
    print("PART 2: Delivery Handler Integration Tests")
    print("=" * 70)

    # Import handlers
    from jarvis_web import (
        _display_in_chat,
        _print_content,
        _open_in_browser,
        _start_structured_readback,
        _get_cached_content,
    )

    recipe_json = make_recipe_json()

    # ── 2A: _get_cached_content with conv_state fallback ──
    print("\n--- 2A: _get_cached_content ---")
    cs = make_conv_state_with_content()
    # Patch get_interaction_cache to return None (no artifact cache)
    with patch("jarvis_web.get_interaction_cache", return_value=None):
        text, prov = _get_cached_content(cs)
        assert_true("cached text from conv_state fallback",
                    text is not None and "banana bread" in text.lower(),
                    f"got: {text[:50] if text else None}")
        assert_eq("provenance is None (no cache)", prov, None)

    # ── 2B: _display_in_chat — structured parse succeeds ──
    print("\n--- 2B: _display_in_chat (structured parse) ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _display_in_chat(ws, llm, cs, tts)

    assert_true("display: response not empty", len(response) > 0)
    assert_in("display: has source title", "AllRecipes", response)
    assert_in("display: has Ingredients header", "### Ingredients", response)
    assert_in("display: has Instructions header", "### ", response)
    assert_true("display: streamed=True", streamed)
    assert_true("display: ws stream sent", ws.has_stream())
    assert_true("display: TTS spoke brief ack",
                len(tts.spoken) > 0 and "chat" in tts.spoken[0].lower(),
                f"spoken: {tts.spoken}")
    assert_eq("display: conv_state.last_tool_result_text cleared", cs.last_tool_result_text, "")

    # ── 2C: _display_in_chat — parse fails, falls back ──
    print("\n--- 2C: _display_in_chat (parse fallback) ---")
    ws = MockWebSocket()
    llm_fail = MockLLM("not json at all")
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    cs.last_response_text = "Here's a banana bread recipe summary."

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _display_in_chat(ws, llm_fail, cs, tts)

    assert_true("display fallback: uses last_response_text",
                "banana bread recipe summary" in response.lower(),
                f"got: {response[:100]}")

    # ── 2D: _display_in_chat — no cached content ──
    print("\n--- 2D: _display_in_chat (no content) ---")
    ws = MockWebSocket()
    cs = ConversationState()  # Empty state
    tts = MockTTSProxy()

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _display_in_chat(ws, llm, cs, tts)

    assert_in("display no content: error message", "don't have any content", response)
    assert_eq("display no content: streamed=False", streamed, False)

    # ── 2E: _print_content — simulated printer success ──
    print("\n--- 2E: _print_content (printer success) ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    mock_lpstat = MagicMock()
    mock_lpstat.stdout = "printer HP_LaserJet is idle.\nsystem default destination: HP_LaserJet"
    mock_lpstat.returncode = 0

    mock_lp = MagicMock()
    mock_lp.returncode = 0
    mock_lp.stderr = ""

    def mock_run(cmd, **kwargs):
        if cmd[0] == "lpstat":
            return mock_lpstat
        elif cmd[0] == "lp":
            # Verify the file was passed
            assert_true("print: lp received file path", len(cmd) >= 4 and cmd[2] == "HP_LaserJet")
            return mock_lp
        return MagicMock(returncode=1, stderr="unknown command")

    with patch("jarvis_web.get_interaction_cache", return_value=None), \
         patch("subprocess.run", side_effect=mock_run):
        response, streamed = await _print_content(ws, llm, cs, tts)

    assert_in("print success: response", "printer", response)
    assert_eq("print: streamed=False", streamed, False)
    assert_eq("print: conv_state cleared", cs.last_tool_result_text, "")

    # ── 2F: _print_content — no printer found ──
    print("\n--- 2F: _print_content (no printer) ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    mock_lpstat_empty = MagicMock()
    mock_lpstat_empty.stdout = ""
    mock_lpstat_empty.returncode = 0

    with patch("jarvis_web.get_interaction_cache", return_value=None), \
         patch("subprocess.run", return_value=mock_lpstat_empty):
        response, streamed = await _print_content(ws, llm, cs, tts)

    assert_in("no printer: error msg", "no printers found", response)
    assert_eq("no printer: streamed=False", streamed, False)

    # ── 2G: _print_content — lpstat crashes ──
    print("\n--- 2G: _print_content (lpstat exception) ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    with patch("jarvis_web.get_interaction_cache", return_value=None), \
         patch("subprocess.run", side_effect=OSError("lpstat not found")):
        response, streamed = await _print_content(ws, llm, cs, tts)

    assert_in("lpstat crash: error msg", "couldn't detect a printer", response)

    # ── 2H: _print_content — no content ──
    print("\n--- 2H: _print_content (no content) ---")
    ws = MockWebSocket()
    cs = ConversationState()
    tts = MockTTSProxy()

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _print_content(ws, llm, cs, tts)

    assert_in("print no content: error msg", "don't have any content", response)

    # ── 2I: _open_in_browser — success with artifact provenance ──
    print("\n--- 2I: _open_in_browser (success with URL) ---")
    ws = MockWebSocket()
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    cs.window_id = "test-window-browse"  # Must be truthy for cache lookup
    config = {"web_navigation": {"default_browser": "brave"}}

    # Mock the cache to return an artifact with provenance
    mock_cache = MagicMock()
    mock_art = MagicMock()
    mock_art.content = "search results text"
    mock_art.provenance = {"result_urls": [
        {"title": "Classic Banana Bread", "url": "https://www.allrecipes.com/recipe/20144/"},
    ]}
    mock_cache.get_latest.return_value = mock_art

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache), \
         patch("subprocess.Popen") as mock_popen:
        response, streamed = await _open_in_browser(ws, cs, tts, config)

    assert_in("browse: response", "opening", response)
    mock_popen.assert_called_once_with(["brave-browser", "https://www.allrecipes.com/recipe/20144/"])
    assert_eq("browse: streamed=False", streamed, False)

    # ── 2J: _open_in_browser — no URL available ──
    print("\n--- 2J: _open_in_browser (no URL) ---")
    ws = MockWebSocket()
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    config = {}

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _open_in_browser(ws, cs, tts, config)

    assert_in("browse no URL: error msg", "don't have a url", response)

    # ── 2K: _open_in_browser — browser config variants ──
    print("\n--- 2K: _open_in_browser (firefox config) ---")
    ws = MockWebSocket()
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    cs.window_id = "test-window-ff"
    config_ff = {"web_navigation": {"default_browser": "firefox"}}

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache), \
         patch("subprocess.Popen") as mock_popen:
        response, streamed = await _open_in_browser(ws, cs, tts, config_ff)

    mock_popen.assert_called_once_with(["firefox-browser", "https://www.allrecipes.com/recipe/20144/"])

    # ── 2L: _start_structured_readback — still works (regression) ──
    print("\n--- 2L: _start_structured_readback (regression check) ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _start_structured_readback(ws, llm, cs, tts)

    assert_true("readback regression: response not empty", len(response) > 0,
                f"response length: {len(response)}")
    assert_true("readback regression: ws stream sent", ws.has_stream())
    assert_true("readback regression: TTS spoke preface",
                len(tts.spoken) > 0,
                f"spoken count: {len(tts.spoken)}")
    assert_true("readback regression: session stored",
                cs.readback_session is not None)


# ═══════════════════════════════════════════════════════════════════════════
# PART 3: Two-turn clarification flow simulation
# ═══════════════════════════════════════════════════════════════════════════

async def test_part3_clarification_flow():
    print("\n" + "=" * 70)
    print("PART 3: Two-Turn Clarification Flow")
    print("=" * 70)

    from jarvis_web import _display_in_chat, _print_content, _open_in_browser

    recipe_json = make_recipe_json()

    # Simulate: Turn 1 — user says "show me" → JARVIS asks clarification
    # Turn 2 — user says specific mode → dispatched correctly
    print("\n--- 3A: 'show me' → clarify → 'in the chat' → display ---")

    # Turn 1: detect "show me"
    offer = "I found a banana bread recipe. Would you like me to read through it?"
    mode1 = _detect_delivery_mode("show me", offer)
    assert_eq("turn1: 'show me' → clarify", mode1, "clarify")

    # Simulate JARVIS response (from persona pool)
    clarify_response = ("I can read it to you, show it here in our chat, "
                       "print it out, or open the page online. What would you prefer, sir?")

    # Turn 2: user says "in the chat"
    mode2 = _detect_delivery_mode("in the chat", clarify_response)
    assert_eq("turn2: 'in the chat' after clarify → display", mode2, "display")

    print("\n--- 3B: 'show me' → clarify → 'print it' → print ---")
    mode2b = _detect_delivery_mode("print it", clarify_response)
    assert_eq("turn2: 'print it' after clarify → print", mode2b, "print")

    print("\n--- 3C: 'show me' → clarify → 'open it online' → browse ---")
    mode2c = _detect_delivery_mode("open it online", clarify_response)
    assert_eq("turn2: 'open it online' after clarify → browse", mode2c, "browse")

    print("\n--- 3D: 'show me' → clarify → 'read it to me' → read ---")
    mode2d = _detect_delivery_mode("read it to me", clarify_response)
    assert_eq("turn2: 'read it to me' after clarify → read", mode2d, "read")

    print("\n--- 3E: 'show me' → clarify → 'yes please, in the chat' → display ---")
    mode2e = _detect_delivery_mode("yes please, in the chat", clarify_response)
    # "yes please" is an affirm, remainder should be "in the chat" → display
    assert_eq("turn2: 'yes please, in the chat' → display", mode2e, "display")

    print("\n--- 3F: 'can i see it' → clarify (alternative phrasing) ---")
    mode_alt = _detect_delivery_mode("can i see it", offer)
    assert_eq("'can i see it' → clarify", mode_alt, "clarify")

    print("\n--- 3G: 'let me see' → clarify ---")
    mode_alt2 = _detect_delivery_mode("let me see", offer)
    assert_eq("'let me see' → clarify", mode_alt2, "clarify")

    # ── 3H: Full async flow: clarify → display ──
    print("\n--- 3H: Full async clarify → display flow ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    # Step 1: detect clarify
    mode = _detect_delivery_mode("show me", cs.last_response_text)
    assert_eq("flow step1: mode=clarify", mode, "clarify")

    # Step 2: simulate persona response
    from core import persona
    clarify_resp = persona.readback_delivery_options()
    assert_true("flow step2: clarify response ends with ?",
                clarify_resp.strip().endswith("?"),
                f"response: {clarify_resp}")

    # Step 3: update conv_state as the dispatch would
    cs.last_response_text = clarify_resp
    cs.jarvis_asked_question = True

    # Step 4: user says "display it in chat"
    mode2 = _detect_delivery_mode("display it in chat", cs.last_response_text)
    assert_eq("flow step4: mode=display", mode2, "display")

    # Step 5: run display handler
    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _display_in_chat(ws, llm, cs, tts)

    assert_true("flow step5: display response has content", len(response) > 50,
                f"response length: {len(response)}")


# ═══════════════════════════════════════════════════════════════════════════
# PART 4: Edge cases & regression tests
# ═══════════════════════════════════════════════════════════════════════════

async def test_part4_edge_cases():
    print("\n" + "=" * 70)
    print("PART 4: Edge Cases & Regression")
    print("=" * 70)

    from jarvis_web import _display_in_chat, _print_content, _open_in_browser

    recipe_json = make_recipe_json()

    # ── 4A: Negations should NOT trigger delivery ──
    print("\n--- 4A: Negations → None ---")
    offer = "Would you like me to read through it?"
    no_offer = "Here's what I found about banana bread."
    assert_eq("'no' → None", _detect_delivery_mode("no", offer), None)
    assert_eq("'no thanks' → None", _detect_delivery_mode("no thanks", offer), None)
    assert_eq("'not right now' → None", _detect_delivery_mode("not right now", offer), None)
    assert_eq("'nah' → None", _detect_delivery_mode("nah", offer), None)
    assert_eq("'never mind' → None", _detect_delivery_mode("never mind", offer), None)

    # ── 4B: Mid-conversation queries should NOT trigger delivery ──
    print("\n--- 4B: Normal queries → None ---")
    assert_eq("'what temperature' → None",
              _detect_delivery_mode("what temperature should the oven be", offer), None)
    assert_eq("'how long does it take' → None",
              _detect_delivery_mode("how long does it take", offer), None)
    assert_eq("'thanks' → None",
              _detect_delivery_mode("thanks", offer), None)

    # ── 4C: Display with ws=None (console mode) ──
    print("\n--- 4C: Display with no WebSocket ---")
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response, streamed = await _display_in_chat(None, llm, cs, tts)

    assert_true("display no-ws: response still has content", len(response) > 50,
                f"response length: {len(response)}")
    # TTS should still fire
    assert_true("display no-ws: TTS still spoke", len(tts.spoken) > 0)

    # ── 4D: Print lp command failure ──
    print("\n--- 4D: Print lp failure ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    mock_lpstat = MagicMock()
    mock_lpstat.stdout = "printer HP_LaserJet is idle."
    mock_lpstat.returncode = 0

    mock_lp_fail = MagicMock()
    mock_lp_fail.returncode = 1
    mock_lp_fail.stderr = "Unable to connect to printer"

    def mock_run_fail(cmd, **kwargs):
        if cmd[0] == "lpstat":
            return mock_lpstat
        return mock_lp_fail

    with patch("jarvis_web.get_interaction_cache", return_value=None), \
         patch("subprocess.run", side_effect=mock_run_fail):
        response, streamed = await _print_content(ws, llm, cs, tts)

    assert_in("lp failure: error in response", "failed", response)

    # ── 4E: Browse with Popen failure ──
    print("\n--- 4E: Browse Popen failure ---")
    ws = MockWebSocket()
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    cs.window_id = "test-window-popen-fail"

    mock_cache = MagicMock()
    mock_art = MagicMock()
    mock_art.content = "text"
    mock_art.provenance = {"result_urls": [{"title": "t", "url": "https://example.com"}]}
    mock_cache.get_latest.return_value = mock_art

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache), \
         patch("subprocess.Popen", side_effect=FileNotFoundError("brave-browser not found")):
        response, streamed = await _open_in_browser(ws, cs, tts, {})

    assert_in("popen failure: error msg", "couldn't open the browser", response)

    # ── 4F: Prefix matching edge — "go ahead" shouldn't match "go through it" ──
    print("\n--- 4F: Prefix collision avoidance ---")
    # "go ahead" is in AFFIRM_WORDS — should match as affirm, not as "go through it" (read)
    mode = _detect_delivery_mode("go ahead", offer)
    assert_eq("'go ahead' + offer → read (via affirm, not 'go through')", mode, "read")

    # ── 4G: "text it" → display ──
    print("\n--- 4G: 'text it' → display ---")
    assert_eq("'text it' → display",
              _detect_delivery_mode("text it", offer), "display")

    # ── 4H: Artifact cache returns content + provenance ──
    print("\n--- 4H: _get_cached_content with artifact cache ---")
    from jarvis_web import _get_cached_content

    mock_cache = MagicMock()
    mock_art = MagicMock()
    mock_art.content = "Full search results..."
    mock_art.provenance = {"query": "banana bread", "result_urls": [
        {"title": "Recipe", "url": "https://example.com/recipe"}
    ]}
    mock_cache.get_latest.return_value = mock_art

    cs = make_conv_state_with_content()
    cs.window_id = "test-window-123"

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache):
        text, prov = _get_cached_content(cs)

    assert_eq("cache: text from artifact", text, "Full search results...")
    assert_true("cache: provenance has urls",
                prov is not None and "result_urls" in prov)

    # ── 4I: Persona pool validity ──
    print("\n--- 4I: Persona pool validation ---")
    from core import persona
    for _ in range(10):
        resp = persona.readback_delivery_options()
        assert_true("persona: response ends with ?", resp.endswith("?"),
                    f"response: {resp}")
        assert_true("persona: contains 'read' or 'go through'",
                    "read" in resp.lower() or "go through" in resp.lower())
        assert_true("persona: contains 'chat' or 'text'",
                    "chat" in resp.lower() or "text" in resp.lower(),
                    f"response: {resp}")
        assert_true("persona: contains 'print'", "print" in resp.lower())

    # ── 4J: "go for it" is an affirm → read with offer ──
    print("\n--- 4J: More affirm variants ---")
    assert_eq("'go for it' + offer → read",
              _detect_delivery_mode("go for it", offer), "read")
    assert_eq("'go for it read it to me' + offer → read",
              _detect_delivery_mode("go for it read it to me", offer), "read")

    # ── 4K: Multiple result_urls — should use first ──
    print("\n--- 4K: Browse uses first URL from multiple ---")
    ws = MockWebSocket()
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    cs.window_id = "test-window-multi-url"

    mock_cache2 = MagicMock()
    mock_art2 = MagicMock()
    mock_art2.content = "text"
    mock_art2.provenance = {"result_urls": [
        {"title": "First", "url": "https://first.com"},
        {"title": "Second", "url": "https://second.com"},
    ]}
    mock_cache2.get_latest.return_value = mock_art2

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache2), \
         patch("subprocess.Popen") as mock_popen:
        response, streamed = await _open_in_browser(ws, cs, tts, {})

    mock_popen.assert_called_once()
    called_url = mock_popen.call_args[0][0][1]
    assert_eq("browse: uses first URL", called_url, "https://first.com")

    # ── 4L: "read it" without offer context → read (explicit keyword) ──
    print("\n--- 4L: 'read it' without offer → read (explicit keyword) ---")
    assert_eq("'read it' + no offer → read",
              _detect_delivery_mode("read it", no_offer), "read")
    assert_eq("'read it to me' + no offer → read",
              _detect_delivery_mode("read it to me", no_offer), "read")

    # ── 4M: "show me/it to me" + trailing resolver → specific mode ──
    print("\n--- 4M: 'show me X' resolvers ---")
    assert_eq("'show me in the chat' → display",
              _detect_delivery_mode("show me in the chat", offer), "display")
    assert_eq("'show it to me in the chat' → display",
              _detect_delivery_mode("show it to me in the chat", offer), "display")
    assert_eq("'show me online' → browse",
              _detect_delivery_mode("show me online", offer), "browse")
    assert_eq("'show it to me in the browser' → browse",
              _detect_delivery_mode("show it to me in the browser", offer), "browse")
    assert_eq("'let me see it in chat' → display",
              _detect_delivery_mode("let me see it in chat", offer), "display")
    assert_eq("'can i see it online' → browse",
              _detect_delivery_mode("can i see it online", offer), "browse")
    assert_eq("'show me on paper' → print",
              _detect_delivery_mode("show me on paper", offer), "print")
    # Bare "show me" still → clarify
    assert_eq("'show me' (bare) still → clarify",
              _detect_delivery_mode("show me", offer), "clarify")

    # ── 4N: Compound affirms with commas ──
    print("\n--- 4N: Compound affirms with commas ---")
    assert_eq("'yes please, print it out' → print",
              _detect_delivery_mode("yes please, print it out", offer), "print")
    assert_eq("'sure, show it in chat' → display",
              _detect_delivery_mode("sure, show it in chat", offer), "display")
    assert_eq("'yeah, open the link' → browse",
              _detect_delivery_mode("yeah, open the link", offer), "browse")

    # ── 4O: Post-consumption re-request → no content ──
    print("\n--- 4O: Post-consumption re-request ---")
    ws = MockWebSocket()
    llm = MockLLM(recipe_json)
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()

    # First display consumes the content
    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response1, _ = await _display_in_chat(ws, llm, cs, tts)
    assert_true("first display succeeds", len(response1) > 50)

    # Second attempt — content is cleared
    ws2 = MockWebSocket()
    with patch("jarvis_web.get_interaction_cache", return_value=None):
        response2, streamed2 = await _display_in_chat(ws2, llm, cs, tts)
    assert_in("re-request: no content error", "don't have any content", response2)

    # ── 4P: Ambiguous bare phrases → None ──
    print("\n--- 4P: Ambiguous bare phrases → None ---")
    assert_eq("'open it' (bare) → None",
              _detect_delivery_mode("open it", offer), None)
    assert_eq("'send it' (bare) → None",
              _detect_delivery_mode("send it", offer), None)
    assert_eq("'do it' (bare) → None",
              _detect_delivery_mode("do it", offer), None)

    # ── 4Q: Malformed provenance — empty result_urls ──
    print("\n--- 4Q: Malformed provenance ---")
    ws = MockWebSocket()
    tts = MockTTSProxy()
    cs = make_conv_state_with_content()
    cs.window_id = "test-window-empty-urls"

    mock_cache_empty = MagicMock()
    mock_art_empty = MagicMock()
    mock_art_empty.content = "text"
    mock_art_empty.provenance = {"result_urls": []}
    mock_cache_empty.get_latest.return_value = mock_art_empty

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache_empty):
        response, streamed = await _open_in_browser(ws, cs, tts, {})
    assert_in("empty urls: no URL error", "don't have a url", response)

    # result_urls with missing url key
    mock_art_nokey = MagicMock()
    mock_art_nokey.content = "text"
    mock_art_nokey.provenance = {"result_urls": [{"title": "No URL here"}]}
    mock_cache_nokey = MagicMock()
    mock_cache_nokey.get_latest.return_value = mock_art_nokey
    cs2 = make_conv_state_with_content()
    cs2.window_id = "test-window-no-url-key"

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache_nokey):
        response2, _ = await _open_in_browser(ws, cs2, tts, {})
    assert_in("missing url key: no URL error", "don't have a url", response2)

    # No provenance at all but cache hit
    mock_art_noprov = MagicMock()
    mock_art_noprov.content = "text"
    mock_art_noprov.provenance = None
    mock_cache_noprov = MagicMock()
    mock_cache_noprov.get_latest.return_value = mock_art_noprov
    cs3 = make_conv_state_with_content()
    cs3.window_id = "test-window-no-prov"

    with patch("jarvis_web.get_interaction_cache", return_value=mock_cache_noprov):
        response3, _ = await _open_in_browser(ws, cs3, tts, {})
    assert_in("null provenance: no URL error", "don't have a url", response3)

    # ── 4R: "text it to me" prefix match → display ──
    print("\n--- 4R: Prefix extensions ---")
    assert_eq("'text it to me' → display",
              _detect_delivery_mode("text it to me", offer), "display")
    assert_eq("'read it out loud' → read",
              _detect_delivery_mode("read it out loud", offer), "read")
    assert_eq("'print it for me' → print",
              _detect_delivery_mode("print it for me", offer), "print")
    assert_eq("'open the page for me' → browse",
              _detect_delivery_mode("open the page for me", offer), "browse")

    # ── 4S: Stale reference check — _is_readback_affirm gone ──
    print("\n--- 4S: Stale reference check ---")
    import jarvis_web
    assert_true("_is_readback_affirm removed",
                not hasattr(jarvis_web, "_is_readback_affirm"),
                "function still exists")


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("DELIVERY MODE SYSTEM — COMPREHENSIVE TEST SUITE")
    print("=" * 70)

    t0 = time.time()

    # Part 1: synchronous
    test_part1_detection()

    # Parts 2-4: async
    asyncio.run(test_part2_handlers())
    asyncio.run(test_part3_clarification_flow())
    asyncio.run(test_part4_edge_cases())

    elapsed = time.time() - t0

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed}/{total} passed, {failed} failed  ({elapsed:.1f}s)")
    print("=" * 70)

    if failed:
        print("\nFAILURES:")
        for r in results:
            if not r.passed:
                print(f"  \033[91mFAIL\033[0m {r.name}: {r.detail}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
