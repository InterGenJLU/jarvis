#!/usr/bin/env python3
"""
Multi-turn conversational test suite for JARVIS.

Sends realistic multi-turn conversations through the live WebSocket endpoint
and captures routing decisions + LLM responses for analysis.

Observe first, assert later — initial runs generate data for human review.

Usage:
    python3 scripts/test_conversations.py --verbose              # All conversations
    python3 scripts/test_conversations.py --id C17               # Single conversation
    python3 scripts/test_conversations.py --ids C02,C05,C17      # Multiple conversations
    python3 scripts/test_conversations.py --category road-trip   # By category
    python3 scripts/test_conversations.py --core-only            # C01-C10 only
    python3 scripts/test_conversations.py --list                 # List all conversations
    python3 scripts/test_conversations.py --analyze              # Analyze last run
"""

import asyncio
import json
import os
import ssl
import sys
import time
import argparse
from dataclasses import dataclass, field
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp required — pip install aiohttp")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required — pip install pyyaml")
    sys.exit(1)


# ── Data Classes ───────────────────────────────────────────────────────────

@dataclass
class Turn:
    """A single user turn in a conversation."""
    user: str
    notes: str = ""


@dataclass
class TurnResult:
    """Captured result from a single turn."""
    turn_num: int
    user: str
    response: str
    routing_layer: str = ""
    skill_name: str = ""
    handler: str = ""
    llm_model: str = ""
    llm_tokens: int = 0
    total_ms: int = 0
    confidence: float = 0.0
    info_messages: list = field(default_factory=list)
    word_count: int = 0
    notes: str = ""
    raw_stats: dict = field(default_factory=dict)


@dataclass
class Conversation:
    """A multi-turn conversation test case."""
    id: str
    name: str
    category: str
    turns: list


@dataclass
class ConversationResult:
    """Full result of running a conversation."""
    id: str
    name: str
    category: str
    turn_results: list
    total_time_ms: int = 0
    error: str = ""


# ── Config ─────────────────────────────────────────────────────────────────

def load_config():
    """Load auth token and connection details from config.yaml."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    config_path = os.path.abspath(config_path)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    web = config.get('web', {})
    tls = web.get('tls', {})

    token = web.get('auth_token', '')
    tls_enabled = tls.get('enabled', False)
    port = tls.get('port', 8443) if tls_enabled else web.get('port', 8088)
    scheme = 'wss' if tls_enabled else 'ws'

    return {
        'url': f'{scheme}://localhost:{port}/ws',
        'token': token,
        'tls_enabled': tls_enabled,
    }


# ── WebSocket Client ──────────────────────────────────────────────────────

class JarvisWSClient:
    """WebSocket client for JARVIS web endpoint."""

    def __init__(self, url: str, token: str, tls: bool = True):
        self.url = f"{url}?token={token}"
        self.tls = tls
        self.ws = None
        self.session = None

    async def connect(self):
        ssl_ctx = None
        if self.tls:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(
            self.url, ssl=ssl_ctx, heartbeat=30
        )
        await self._consume_handshake()

    async def _consume_handshake(self):
        """Consume connection handshake: session_list, history, system_stats."""
        seen = set()
        expected = {'session_list', 'history', 'system_stats'}
        deadline = time.time() + 10

        while seen != expected and time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=5)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    seen.add(data.get('type', ''))
            except asyncio.TimeoutError:
                break

    async def send_turn(self, content: str) -> TurnResult:
        """Send user message, collect full response + metadata."""
        await self.ws.send_json({"type": "message", "content": content})

        tokens = []
        response_text = ""
        stats_data = {}
        info_messages = []
        got_terminal = False

        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=120)
            except asyncio.TimeoutError:
                if not response_text and tokens:
                    response_text = ''.join(tokens)
                break

            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue

            data = json.loads(msg.data)
            mtype = data.get('type', '')

            if mtype == 'stream_token':
                tokens.append(data.get('token', ''))
            elif mtype == 'stream_end':
                response_text = data.get('full_response', ''.join(tokens))
                got_terminal = True
            elif mtype == 'response':
                response_text = data.get('content', '')
                got_terminal = True
            elif mtype == 'stats':
                stats_data = data.get('data', {})
            elif mtype == 'info':
                info_messages.append(data.get('content', ''))
            elif mtype == 'system_stats':
                # Last message in per-command sequence
                if got_terminal or stats_data:
                    break
            elif mtype == 'error':
                response_text = f"[ERROR] {data.get('content', '')}"
                break
            # Ignore: announcement, doc_status, stream_start, health_report, voice_status

        word_count = len(response_text.split()) if response_text else 0

        return TurnResult(
            turn_num=0,
            user=content,
            response=response_text,
            routing_layer=stats_data.get('layer', ''),
            skill_name=stats_data.get('skill_name', ''),
            handler=stats_data.get('handler', ''),
            llm_model=stats_data.get('llm_model', ''),
            llm_tokens=stats_data.get('llm_tokens', 0),
            total_ms=stats_data.get('total_ms', 0),
            confidence=stats_data.get('confidence', 0.0),
            info_messages=info_messages,
            word_count=word_count,
            raw_stats=stats_data,
        )

    async def close(self):
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session and not self.session.closed:
            await self.session.close()


# ── Conversation Definitions ──────────────────────────────────────────────

def _t(user, notes=""):
    return Turn(user=user, notes=notes)


def _c(id, name, category, turns):
    return Conversation(id=id, name=name, category=category, turns=turns)


def get_all_conversations():
    """Return all 40 test conversations."""
    return [

        # ── Core Routing Patterns (C01-C10) ──────────────────────────

        _c("C01", "Rapid Topic Shift", "routing", [
            _t("what's the weather"),
            _t("check git status"),
            _t("was it supposed to rain today?", "callback to turn 1 weather"),
        ]),

        _c("C02", "Anaphoric Chain", "routing", [
            _t("how many files are in my documents folder"),
            _t("list them for me", "anaphoric reference"),
            _t("which ones are the biggest", "continuation"),
            _t("delete the largest one", "implied context"),
        ]),

        _c("C03", "Compound + Follow-up", "routing", [
            _t("research the top 5 programming languages and create a presentation"),
            _t("add a slide about salary data"),
            _t("now open it"),
        ]),

        _c("C04", "Mid-Conversation Correction", "routing", [
            _t("set a reminder for 3pm to call the dentist"),
            _t("actually make it 4pm", "correction"),
            _t("and change dentist to doctor", "second correction"),
        ]),

        _c("C05", "Cross-Topic Callback", "routing", [
            _t("what's the weather"),
            _t("any cybersecurity news"),
            _t("tell me more about the first one", "news continuation"),
            _t("search the web for more details on that"),
            _t("going back to the weather, should I bring an umbrella?", "callback to turn 1"),
        ]),

        _c("C06", "Short Ambiguous", "routing", [
            _t("how much disk space do I have"),
            _t("what about memory", "implied: system memory, not JARVIS memory"),
            _t("and CPU"),
            _t("is that normal?"),
        ]),

        _c("C07", "File Operations Chain", "routing", [
            _t("what files are in the share folder"),
            _t("read test.txt"),
            _t("delete it"),
            _t("what's in there now"),
        ]),

        _c("C08", "Knowledge + Tool Mix", "routing", [
            _t("what's a VPN", "pure LLM knowledge"),
            _t("search the web for the best free VPNs", "tool: web_search"),
            _t("create a comparison document", "skill: file_editor"),
        ]),

        _c("C09", "Greeting → Task → Dismiss", "routing", [
            _t("hey jarvis"),
            _t("what time is it"),
            _t("thanks, that's all", "dismissal detection"),
        ]),

        _c("C10", "Cybersecurity Deep Dive", "routing", [
            _t("tell me about lateral movement in cybersecurity"),
            _t("what tools do attackers typically use for that"),
            _t("how do you detect it"),
            _t("what about in a cloud environment specifically"),
            _t("summarize everything you just told me in 3 bullet points"),
        ]),

        # ── Long-Form Knowledge (C11-C13) ────────────────────────────

        _c("C11", "Zero-Day Deep Explanation", "long-form", [
            _t("what exactly is a zero-day vulnerability? give me a real thorough breakdown",
               "testing substantive multi-paragraph answers"),
            _t("how is that different from a regular CVE"),
            _t("give me some famous examples from the last few years"),
            _t("how do organizations defend against something they literally don't know about yet"),
        ]),

        _c("C12", "DNS & Networking Deep Dive", "long-form", [
            _t("explain how DNS works end to end, don't skimp on the details"),
            _t("what happens when DNS goes down"),
            _t("what's the difference between DNS over HTTPS and regular DNS"),
            _t("how would I set up a Pi-hole at home and is it worth it"),
        ]),

        _c("C13", "ML vs AI Breakdown", "long-form", [
            _t("what's the difference between machine learning, deep learning, and AI — break it down for someone technical but not in the field"),
            _t("where does a large language model fit into that"),
            _t("what are the main limitations of current LLMs that people don't talk about enough"),
        ]),

        # ── Code Analysis (C14-C16) ──────────────────────────────────

        _c("C14", "Python Performance Debug", "code-analysis", [
            _t("I've got a Python script that iterates over two large lists with a nested for loop to find matching items — it's slow as hell. what's a better approach"),
            _t("show me what that would look like with a dictionary lookup"),
            _t("what if I need to match on multiple fields, not just one"),
            _t("what's the big-O difference between the two approaches"),
        ]),

        _c("C15", "Security Code Review", "code-analysis", [
            _t("if I have a Flask endpoint that takes user input and drops it straight into a SQL query string, what could go wrong"),
            _t("show me what the vulnerable version looks like versus the fixed version"),
            _t("what other OWASP top 10 issues should I check for in a Flask app"),
            _t("write me a quick checklist I could use before deploying"),
        ]),

        _c("C16", "Bash Script Analysis", "code-analysis", [
            _t("I wrote a bash script that does find . -name '*.log' -exec rm {} and it's deleting stuff I don't want — what am I doing wrong"),
            _t("how do I make it only hit files older than 30 days and add a dry run mode"),
            _t("turn that into a cron job that runs weekly"),
        ]),

        # ── Road Trip & Distance Planning (C17-C19) ──────────────────

        _c("C17", "Mexico Road Trip — Full Chain", "road-trip", [
            _t("how far is it from here to Cancun Mexico if I'm driving"),
            _t("I'm in a 2026 Jeep Wrangler JL — with its gas mileage, how many fill-ups would that take"),
            _t("plan out roughly where I should stop for gas along the way"),
            _t("if I want to avoid stopping for sit-down meals, how many snacks should I grab at each gas stop to keep me going"),
            _t("what about border crossing — what paperwork do I need"),
            _t("is my Wrangler going to handle the highways down there okay or should I be worried about anything"),
            _t("give me a rough total cost estimate for the whole trip — gas, tolls, food, a week of hotels"),
        ]),

        _c("C18", "Weekend Getaway Distance Math", "road-trip", [
            _t("how far is Gatlinburg Tennessee from here"),
            _t("with my Wrangler getting about 22 mpg highway and gas at $3.50 a gallon, what's my fuel cost round trip"),
            _t("if I put 35 inch tires on it and that drops my fuel economy by about 10 percent, how much more does the trip cost"),
            _t("what's worth seeing along the way if I want to break the drive up"),
        ]),

        _c("C19", "Moab Road Trip Logistics", "road-trip", [
            _t("I want to drive from here to Moab Utah — how long is that drive"),
            _t("with my Wrangler's 21.5 gallon tank and about 20 mpg, how far can I go between fill-ups"),
            _t("map out where I'd need to stop for gas — are there any long empty stretches I should worry about"),
            _t("what should I pack for a week of offroading in the desert"),
            _t("what's the weather usually like in Moab in late April"),
        ]),

        # ── Jeep Research (C20-C22) ──────────────────────────────────

        _c("C20", "Jeep Specs and Comparisons", "vehicle-research", [
            _t("what's the towing capacity on a 2026 Jeep Wrangler JL"),
            _t("look that up online for me to make sure you're right"),
            _t("how does the Wrangler compare to the Gladiator for towing"),
            _t("what about the new Ford Bronco — how does it stack up against the Wrangler overall"),
            _t("if I'm mainly doing weekend trail rides and the occasional tow, which one makes the most sense"),
        ]),

        _c("C21", "Jeep Maintenance & Problems", "vehicle-research", [
            _t("what's the recommended maintenance schedule for a Wrangler JL in the first 50,000 miles"),
            _t("are there any common problems or recalls I should know about"),
            _t("look that up to make sure — I want current info"),
            _t("what aftermarket warranty would you recommend and how much do those usually run"),
        ]),

        _c("C22", "Jeep Modification Research", "vehicle-research", [
            _t("I want to lift my Wrangler 2.5 inches — what's the best lift kit for daily driving and trails"),
            _t("search for the top rated ones and their prices"),
            _t("would I need new shocks too or do most kits include those"),
            _t("after the lift, what's the biggest tire I can run without rubbing"),
            _t("total cost for lift plus tires plus install — give me a ballpark"),
        ]),

        # ── Practical Math & Conversions (C23-C27) ───────────────────

        _c("C23", "Recipe Scaling", "math-conversions", [
            _t("I'm making a recipe that calls for 3/4 cup of flour but I need to triple it — how much flour is that"),
            _t("I only have a 1/3 cup measuring cup — how many scoops do I need"),
            _t("the recipe also calls for 2 tablespoons of butter — how many grams is that"),
            _t("if the recipe serves 4 and I need to feed 7, what's my multiplier"),
            _t("scale the whole thing for me — 3/4 cup flour, 2 tbsp butter, 1.5 cups milk, 3 eggs, pinch of salt"),
        ]),

        _c("C24", "Home Painting Project", "math-conversions", [
            _t("I need to paint a room that's 12 by 14 feet with 9 foot ceilings — how much wall area is that"),
            _t("subtract two 3x4 foot windows and one 3x7 foot door"),
            _t("if a gallon of paint covers about 350 square feet, how many gallons do I need for two coats"),
            _t("Sherwin-Williams Duration is about $75 a gallon — what's my total paint cost"),
        ]),

        _c("C25", "Home Budget & Expenses", "math-conversions", [
            _t("my electricity bill has been running about $180 a month — if I switch all my bulbs to LED, roughly how much could I save per year"),
            _t("what about adding a smart thermostat — what's the typical savings there"),
            _t("give me a rough annual maintenance budget for a 2,000 square foot house"),
            _t("what are the most expensive surprise repairs I should be saving for"),
            _t("if I set aside $300 a month for home maintenance and repairs, is that enough"),
        ]),

        _c("C26", "Brisket Smoking Conversions", "math-conversions", [
            _t("how long do I smoke a brisket per pound at 225 degrees"),
            _t("I've got a 14 pounder — what time should I start if we're eating at 6 pm"),
            _t("the rub recipe calls for 2 tablespoons paprika, 1 tablespoon garlic powder, 1 tablespoon onion powder, 2 teaspoons black pepper, 2 teaspoons salt, 1 teaspoon cayenne — convert all of that to grams"),
            _t("if I double the rub to make extra, give me the gram measurements for the doubled version"),
            _t("what internal temp am I aiming for and how long should it rest after"),
        ]),

        _c("C27", "Deck Building Material Estimate", "math-conversions", [
            _t("I'm building a 12 by 16 foot deck — how many square feet of decking material do I need"),
            _t("if composite decking boards are 5.5 inches wide and 16 feet long, how many boards is that"),
            _t("add 10 percent for waste — how many boards should I actually buy"),
            _t("at about $45 per board plus screws and framing lumber, give me a rough materials cost"),
        ]),

        # ── Movies & Entertainment (C28-C30) ─────────────────────────

        _c("C28", "Current Movies & Actor Chains", "entertainment", [
            _t("what movies are playing in theaters right now"),
            _t("who's the lead in that first one", "anaphoric — depends on turn 1 result"),
            _t("what other movies have they been in that are worth watching"),
            _t("are any of those streaming anywhere"),
            _t("what big movies have come out for home streaming recently"),
            _t("anything good on Netflix specifically that came out this month"),
        ]),

        _c("C29", "Mission Impossible Deep Dive", "entertainment", [
            _t("has there been a new Mission Impossible movie recently"),
            _t("how did it do at the box office compared to the previous ones"),
            _t("rank all the Mission Impossible movies from best to worst"),
            _t("what other spy/action franchises are still putting out good movies"),
        ]),

        _c("C30", "Movie Recommendation Chain", "entertainment", [
            _t("I liked Sicario and Wind River — recommend me something in that same vein"),
            _t("who directed those and what else have they done"),
            _t("are any of those suggestions available to stream right now"),
            _t("which one should I start with tonight"),
        ]),

        # ── Home Technology (C31-C34) ────────────────────────────────

        _c("C31", "WiFi Router Research", "home-tech", [
            _t("I need a new wifi router — what's good right now"),
            _t("my house is about 2,000 square feet, two stories — look up what would work best for that size"),
            _t("what about mesh systems versus a single router — what's the trade-off"),
            _t("search for the top rated mesh systems under $400"),
            _t("can I set it up myself or is it complicated"),
            _t("my current internet is 500 megabit — would any of these bottleneck that"),
        ]),

        _c("C32", "SimpliSafe Home Security", "home-tech", [
            _t("how would I go about setting up a home security system from SimpliSafe"),
            _t("what does their equipment package include and how much does it cost"),
            _t("what's the monthly monitoring fee"),
            _t("can I integrate it with Alexa or Google Home"),
            _t("what about just doing cameras myself without a monitoring service — what would you recommend"),
        ]),

        _c("C33", "External Camera Research", "home-tech", [
            _t("what are the best outdoor security cameras for a house right now"),
            _t("look that up online — I want current recommendations"),
            _t("do any of them work without a subscription for cloud storage"),
            _t("how much would it cost to put four cameras around my house — equipment plus install"),
            _t("can I run the wiring myself or do I need an electrician"),
        ]),

        _c("C34", "Smart Home Starter", "home-tech", [
            _t("if I wanted to start making my house smarter, where should I begin"),
            _t("should I go with Alexa, Google, or Apple ecosystem"),
            _t("what smart switches and plugs work best with that and how much do they cost"),
            _t("how hard is it to swap out regular light switches for smart ones — can I do it myself"),
        ]),

        # ── Offroading & Outdoor (C35-C37) ───────────────────────────

        _c("C35", "Best Offroad Trails", "outdoor", [
            _t("what are the best places to go offroading in a Jeep near North Carolina"),
            _t("what about Moab — what trails would you recommend for someone with moderate experience"),
            _t("look up the trail difficulty ratings for me"),
            _t("what modifications should I have on my Wrangler before tackling those"),
            _t("how much would those mods cost if I do a 2.5 inch lift, skid plates, and 33 inch tires"),
        ]),

        _c("C36", "Offroad Trip Planning", "outdoor", [
            _t("are there any Jeep meetups or offroad events happening this spring"),
            _t("search for Jeep Jamboree events near me"),
            _t("what do those events usually include and how much are they"),
            _t("what recovery gear should I always have in my Jeep for trail riding"),
            _t("give me a full packing list for a weekend offroad trip"),
        ]),

        _c("C37", "Overlanding Research", "outdoor", [
            _t("what's the difference between offroading and overlanding"),
            _t("what would I need to set up my Wrangler for overlanding"),
            _t("search for rooftop tent options that fit a JL and their prices"),
            _t("plan me a 3-day overlanding route from here through the Blue Ridge Mountains"),
        ]),

        # ── Concerts & Events (C38-C40) ──────────────────────────────

        _c("C38", "Tool Concert Planning", "events", [
            _t("is Tool touring this year"),
            _t("are they playing anywhere near North Carolina"),
            _t("how much are tickets going for"),
            _t("if the closest show is a few hours away, where should I stay the night"),
            _t("what's the best way to get there — drive or is there a closer airport"),
        ]),

        _c("C39", "Concert Discovery Chain", "events", [
            _t("what big rock or metal concerts are happening this summer within driving distance"),
            _t("I like Tool, A Perfect Circle, Deftones, and Mastodon — any of them touring"),
            _t("compare ticket prices across those shows"),
            _t("which venue is the best experience"),
            _t("if I could only pick two shows this summer, which would you recommend and why"),
        ]),

        _c("C40", "Festival Research", "events", [
            _t("what music festivals are happening in the southeast this summer"),
            _t("which ones have a good rock or metal lineup"),
            _t("how much are tickets and camping passes"),
            _t("what should I know before going to my first music festival"),
        ]),
    ]


# ── Output Formatting ─────────────────────────────────────────────────────

def format_routing(result):
    """Format routing info into a concise string."""
    parts = []
    if result.skill_name:
        parts.append(f"skill:{result.skill_name}")
        if result.handler:
            parts.append(f"({result.handler})")
    elif result.llm_model:
        parts.append(f"llm:{result.llm_model}")
    elif result.routing_layer:
        parts.append(result.routing_layer)
    else:
        parts.append("unknown")

    # Tools detected from info messages
    tools = []
    for info in result.info_messages:
        if info.startswith("Searching:"):
            tools.append("web_search")
        elif info.startswith("Running:"):
            tool = info.replace("Running:", "").strip()
            tools.append(tool)
    if tools:
        parts.append(f"tools:[{','.join(tools)}]")

    if result.confidence:
        parts.append(f"conf:{result.confidence:.2f}")

    return " | ".join(parts)


def truncate(text, max_len=200):
    """Truncate text for display."""
    text = text.replace('\n', ' ').strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def print_conversation_result(cr, verbose=True):
    """Print formatted results for one conversation."""
    print(f"\n{'='*70}")
    print(f"  {cr.id}: {cr.name}  [{cr.category}]")
    print(f"{'='*70}")

    if cr.error:
        print(f"  ERROR: {cr.error}")
        return

    for tr in cr.turn_results:
        print(f"\n  [{tr.turn_num}] USER: \"{tr.user}\"")
        if verbose:
            resp_display = tr.response.replace('\n', ' ').strip()
            print(f"      JARVIS: \"{resp_display}\"")
        routing = format_routing(tr)
        print(f"      routing: {routing} | {tr.word_count} words | {tr.total_ms}ms")
        if tr.notes:
            print(f"      NOTE: {tr.notes}")
        if tr.info_messages and verbose:
            for info in tr.info_messages:
                print(f"      INFO: {info}")

    # Summary
    if cr.turn_results:
        avg_ms = sum(t.total_ms for t in cr.turn_results) // len(cr.turn_results)
        avg_words = sum(t.word_count for t in cr.turn_results) // len(cr.turn_results)
        tools_used = []
        for tr in cr.turn_results:
            for info in tr.info_messages:
                if info.startswith("Searching:"):
                    tools_used.append("web_search")
                elif info.startswith("Running:"):
                    tools_used.append(info.replace("Running:", "").strip())
        tool_summary = f" | tools: {', '.join(sorted(set(tools_used)))}" if tools_used else ""
        print(f"\n  Summary: {len(cr.turn_results)} turns | avg {avg_ms}ms"
              f" | avg {avg_words} words/response{tool_summary}")
        print(f"  Total time: {cr.total_time_ms}ms")


def print_analysis(results):
    """Print aggregate analysis across all conversations."""
    if not results:
        print("No results to analyze.")
        return

    print(f"\n{'='*70}")
    print(f"  AGGREGATE ANALYSIS — {len(results)} conversations")
    print(f"{'='*70}")

    all_turns = []
    for cr in results:
        all_turns.extend(cr.turn_results)

    if not all_turns:
        print("  No turns recorded.")
        return

    total_turns = len(all_turns)
    avg_ms = sum(t.total_ms for t in all_turns) // total_turns
    avg_words = sum(t.word_count for t in all_turns) // total_turns
    max_words = max(t.word_count for t in all_turns)
    min_words = min(t.word_count for t in all_turns)

    # Routing breakdown
    skill_count = sum(1 for t in all_turns if t.skill_name)
    llm_count = sum(1 for t in all_turns if t.llm_model and not t.skill_name)
    other_count = total_turns - skill_count - llm_count

    # Tool usage
    tool_counts = {}
    for t in all_turns:
        for info in t.info_messages:
            if info.startswith("Searching:"):
                tool_counts["web_search"] = tool_counts.get("web_search", 0) + 1
            elif info.startswith("Running:"):
                name = info.replace("Running:", "").strip()
                tool_counts[name] = tool_counts.get(name, 0) + 1

    # Category breakdown
    cat_stats = {}
    for cr in results:
        cat = cr.category
        if cat not in cat_stats:
            cat_stats[cat] = {"convs": 0, "turns": 0, "total_ms": 0, "total_words": 0}
        cat_stats[cat]["convs"] += 1
        for tr in cr.turn_results:
            cat_stats[cat]["turns"] += 1
            cat_stats[cat]["total_ms"] += tr.total_ms
            cat_stats[cat]["total_words"] += tr.word_count

    # Word count buckets
    short = sum(1 for t in all_turns if t.word_count < 30)
    medium = sum(1 for t in all_turns if 30 <= t.word_count < 100)
    long_resp = sum(1 for t in all_turns if 100 <= t.word_count < 200)
    very_long = sum(1 for t in all_turns if t.word_count >= 200)

    # LLM model distribution
    model_counts = {}
    for t in all_turns:
        if t.llm_model:
            model_counts[t.llm_model] = model_counts.get(t.llm_model, 0) + 1

    # Errors
    errors = [cr for cr in results if cr.error]
    empty_responses = [t for t in all_turns if not t.response or t.response.startswith("[ERROR]")]

    print(f"\n  Turns: {total_turns}")
    print(f"  Avg latency: {avg_ms}ms")
    print(f"  Avg response: {avg_words} words (min {min_words}, max {max_words})")

    print(f"\n  Response length distribution:")
    print(f"    Short (<30 words):    {short:3d}  ({100*short//total_turns}%)")
    print(f"    Medium (30-99):       {medium:3d}  ({100*medium//total_turns}%)")
    print(f"    Long (100-199):       {long_resp:3d}  ({100*long_resp//total_turns}%)")
    print(f"    Very long (200+):     {very_long:3d}  ({100*very_long//total_turns}%)")

    print(f"\n  Routing:")
    print(f"    Skill-handled: {skill_count}")
    print(f"    LLM-handled:   {llm_count}")
    print(f"    Other:         {other_count}")

    if model_counts:
        print(f"\n  LLM models used:")
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            print(f"    {model}: {count}")

    if tool_counts:
        print(f"\n  Tool usage:")
        for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            print(f"    {tool}: {count}")

    print(f"\n  By category:")
    for cat, stats in sorted(cat_stats.items()):
        avg_cat_ms = stats["total_ms"] // stats["turns"] if stats["turns"] else 0
        avg_cat_words = stats["total_words"] // stats["turns"] if stats["turns"] else 0
        print(f"    {cat:20s}  {stats['convs']} convs, {stats['turns']:3d} turns,"
              f" avg {avg_cat_ms}ms, avg {avg_cat_words} words")

    if errors:
        print(f"\n  Errors: {len(errors)} conversations failed")
        for cr in errors:
            print(f"    {cr.id}: {cr.error}")

    if empty_responses:
        print(f"\n  Empty/error responses: {len(empty_responses)} turns")
        for t in empty_responses[:5]:
            print(f"    Turn {t.turn_num}: \"{truncate(t.user, 60)}\"")

    # Total time
    total_time = sum(cr.total_time_ms for cr in results)
    mins = total_time // 60000
    secs = (total_time // 1000) % 60
    print(f"\n  Total run time: {total_time // 1000}s ({mins}m {secs}s)")


# ── Runner ─────────────────────────────────────────────────────────────────

async def run_conversation(client, conv, delay=2.0, verbose=True):
    """Run a single conversation through the WebSocket."""
    turn_results = []
    conv_start = time.time()

    try:
        for i, turn in enumerate(conv.turns):
            if i > 0:
                await asyncio.sleep(delay)

            if verbose:
                sys.stdout.write(
                    f"    [{conv.id}] Turn {i+1}/{len(conv.turns)}: "
                    f"\"{truncate(turn.user, 50)}\"")
                sys.stdout.flush()

            result = await client.send_turn(turn.user)
            result.turn_num = i + 1
            result.notes = turn.notes
            turn_results.append(result)

            if verbose:
                print(f" → {result.word_count} words, {result.total_ms}ms")

    except Exception as e:
        return ConversationResult(
            id=conv.id, name=conv.name, category=conv.category,
            turn_results=turn_results,
            total_time_ms=int((time.time() - conv_start) * 1000),
            error=str(e),
        )

    return ConversationResult(
        id=conv.id, name=conv.name, category=conv.category,
        turn_results=turn_results,
        total_time_ms=int((time.time() - conv_start) * 1000),
    )


SHARE_DIR = os.path.expanduser("~/jarvis/share")


async def run_suite(conversations, config, delay=2.0, verbose=True,
                    reconnect_between=True):
    """Run all conversations and return results."""
    results = []

    # Capture timestamp before run so cleanup can delete test-created data
    run_start_ts = time.time()

    # Snapshot share/ before run so we can remove new files after
    pre_run_files = set()
    if os.path.isdir(SHARE_DIR):
        for f in os.listdir(SHARE_DIR):
            fp = os.path.join(SHARE_DIR, f)
            if os.path.isfile(fp):
                pre_run_files.add(f)

    client = JarvisWSClient(
        url=config['url'], token=config['token'], tls=config['tls_enabled'],
    )

    try:
        print(f"Connecting to {config['url']}...")
        max_retries = 10
        for attempt in range(1, max_retries + 1):
            try:
                await client.connect()
                break
            except (aiohttp.ClientError, OSError) as conn_err:
                if attempt == max_retries:
                    raise
                wait = min(attempt * 2, 10)
                print(f"  Connection attempt {attempt}/{max_retries} failed: {conn_err}")
                print(f"  Retrying in {wait}s...")
                await asyncio.sleep(wait)
                client = JarvisWSClient(
                    url=config['url'], token=config['token'],
                    tls=config['tls_enabled'],
                )
        print(f"Connected. Running {len(conversations)} conversations.\n")

        for i, conv in enumerate(conversations):
            print(f"[{i+1}/{len(conversations)}] {conv.id}: {conv.name} "
                  f"({len(conv.turns)} turns)")

            result = await run_conversation(client, conv, delay=delay, verbose=verbose)
            results.append(result)

            if result.error:
                print(f"  ERROR: {result.error}")

            # Reconnect between conversations for session isolation
            if reconnect_between and i < len(conversations) - 1:
                await client.close()
                await asyncio.sleep(1)
                client = JarvisWSClient(
                    url=config['url'], token=config['token'],
                    tls=config['tls_enabled'],
                )
                await client.connect()

    except aiohttp.ClientError as e:
        print(f"\nConnection error: {e}")
        print("Is JARVIS web service running? (systemctl --user status jarvis-web)")
    finally:
        await client.close()

    # Clean up artifacts created during testing
    await cleanup_test_artifacts(config, pre_run_files, run_start_ts)

    return results


# ── Artifact Cleanup ──────────────────────────────────────────────────────

# Conversations that create side effects:
#   C03: creates a presentation (programming languages)
#   C04: creates reminders (dentist/doctor at 3pm/4pm)
#   C08: creates a comparison document (VPN)
#   C10: may create lateral movement presentation
#   Others: LLM may spontaneously generate documents

CLEANUP_COMMANDS = [
    "cancel any reminders about calling the dentist",
    "cancel any reminders about calling the doctor",
]


async def cleanup_test_artifacts(config, pre_run_files, run_start_ts):
    """Remove reminders, files, and memory artifacts created by test conversations."""
    import sqlite3
    import glob as glob_mod

    print(f"\n{'─'*70}")
    print("  Cleaning up test artifacts...")

    # 1. Cancel test-created reminders via WebSocket
    client = JarvisWSClient(
        url=config['url'], token=config['token'], tls=config['tls_enabled'],
    )
    try:
        await client.connect()
        for cmd in CLEANUP_COMMANDS:
            result = await client.send_turn(cmd)
            status = "ok" if result.response and "sorry" not in result.response.lower() else "skipped"
            print(f"    [{status}] {cmd}")
            await asyncio.sleep(1)
    except Exception as e:
        print(f"    Cleanup error (non-fatal): {e}")
    finally:
        await client.close()

    # 2. Remove any files in share/ that didn't exist before the run
    if os.path.isdir(SHARE_DIR):
        for f in os.listdir(SHARE_DIR):
            fp = os.path.join(SHARE_DIR, f)
            if os.path.isfile(fp) and f not in pre_run_files:
                try:
                    os.remove(fp)
                    print(f"    [removed] {f}")
                except OSError:
                    pass

    # 3. Purge memory artifacts created during this test run
    data_dir = "/mnt/storage/jarvis/data"

    # 3a. memory.db — facts, interaction_log, topic_segments
    memory_db = os.path.join(data_dir, "memory.db")
    if os.path.exists(memory_db):
        try:
            conn = sqlite3.connect(memory_db)
            cur = conn.cursor()
            cur.execute("DELETE FROM facts WHERE created_at >= ?", (run_start_ts,))
            facts_del = cur.rowcount
            cur.execute("DELETE FROM interaction_log WHERE created_at >= ?", (run_start_ts,))
            ilog_del = cur.rowcount
            cur.execute("DELETE FROM topic_segments WHERE created_at >= ?", (run_start_ts,))
            tseg_del = cur.rowcount
            cur.execute("DELETE FROM extraction_state")
            conn.commit()
            conn.close()
            print(f"    [memory.db] deleted {facts_del} facts, {ilog_del} interaction_log, {tseg_del} topic_segments")
        except Exception as e:
            print(f"    [memory.db] cleanup error: {e}")

    # 3b. FAISS index — rebuild would require loading the model, just delete the files
    #     They'll be rebuilt on next JARVIS startup from remaining facts
    faiss_dir = os.path.join(data_dir, "memory_faiss")
    if os.path.isdir(faiss_dir):
        removed = 0
        for f in glob_mod.glob(os.path.join(faiss_dir, "*")):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"    [memory_faiss] removed {removed} index files (will rebuild on next startup)")

    # 3c. interaction_cache.db — artifacts and links
    cache_db = os.path.join(data_dir, "interaction_cache.db")
    if os.path.exists(cache_db):
        try:
            conn = sqlite3.connect(cache_db)
            cur = conn.cursor()
            cur.execute("DELETE FROM artifact_links WHERE created_at >= ?", (run_start_ts,))
            links_del = cur.rowcount
            cur.execute("DELETE FROM artifacts WHERE created_at >= ?", (run_start_ts,))
            arts_del = cur.rowcount
            conn.commit()
            conn.close()
            print(f"    [interaction_cache.db] deleted {arts_del} artifacts, {links_del} links")
        except Exception as e:
            print(f"    [interaction_cache.db] cleanup error: {e}")

    # 3d. web_queries.db — search history
    wq_db = os.path.join(data_dir, "web_queries.db")
    if os.path.exists(wq_db):
        try:
            conn = sqlite3.connect(wq_db)
            cur = conn.cursor()
            cur.execute("DELETE FROM web_queries WHERE timestamp >= datetime(?, 'unixepoch')", (run_start_ts,))
            wq_del = cur.rowcount
            conn.commit()
            conn.close()
            if wq_del:
                print(f"    [web_queries.db] deleted {wq_del} queries")
        except Exception as e:
            print(f"    [web_queries.db] cleanup error: {e}")

    print("  Cleanup complete.")
    print(f"{'─'*70}")


# ── Persistence ────────────────────────────────────────────────────────────

def save_results(results, path):
    """Save results to JSON."""
    data = []
    for cr in results:
        data.append({
            'id': cr.id,
            'name': cr.name,
            'category': cr.category,
            'total_time_ms': cr.total_time_ms,
            'error': cr.error,
            'turns': [
                {
                    'turn_num': tr.turn_num,
                    'user': tr.user,
                    'response': tr.response,
                    'routing_layer': tr.routing_layer,
                    'skill_name': tr.skill_name,
                    'handler': tr.handler,
                    'llm_model': tr.llm_model,
                    'llm_tokens': tr.llm_tokens,
                    'total_ms': tr.total_ms,
                    'confidence': tr.confidence,
                    'info_messages': tr.info_messages,
                    'word_count': tr.word_count,
                    'notes': tr.notes,
                    'raw_stats': tr.raw_stats,
                }
                for tr in cr.turn_results
            ],
        })

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {path}")


def load_results(path):
    """Load results from JSON for analysis."""
    with open(path) as f:
        data = json.load(f)

    results = []
    for item in data:
        turns = [
            TurnResult(
                turn_num=t['turn_num'],
                user=t['user'],
                response=t['response'],
                routing_layer=t.get('routing_layer', ''),
                skill_name=t.get('skill_name', ''),
                handler=t.get('handler', ''),
                llm_model=t.get('llm_model', ''),
                llm_tokens=t.get('llm_tokens', 0),
                total_ms=t.get('total_ms', 0),
                confidence=t.get('confidence', 0.0),
                info_messages=t.get('info_messages', []),
                word_count=t.get('word_count', 0),
                notes=t.get('notes', ''),
                raw_stats=t.get('raw_stats', {}),
            )
            for t in item['turns']
        ]
        results.append(ConversationResult(
            id=item['id'],
            name=item['name'],
            category=item['category'],
            turn_results=turns,
            total_time_ms=item.get('total_time_ms', 0),
            error=item.get('error', ''),
        ))

    return results


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Multi-turn conversational test suite for JARVIS"
    )
    parser.add_argument('--id', help="Run single conversation by ID (e.g., C17)")
    parser.add_argument('--ids', help="Run multiple conversations by ID (comma-separated, e.g., C02,C05,C17)")
    parser.add_argument('--category',
                        help="Run conversations by category (e.g., road-trip)")
    parser.add_argument('--list', action='store_true',
                        help="List all conversations and exit")
    parser.add_argument('--analyze', action='store_true',
                        help="Analyze last run results (no new tests)")
    parser.add_argument('--verbose', action='store_true', default=True,
                        help="Show full responses (default: true)")
    parser.add_argument('--brief', action='store_true',
                        help="Show only routing info, not full responses")
    parser.add_argument('--delay', type=float, default=2.0,
                        help="Seconds between turns (default: 2.0)")
    parser.add_argument('--no-reconnect', action='store_true',
                        help="Keep same WS connection across conversations")
    parser.add_argument('--save',
                        default='/tmp/conversation_test_results.json',
                        help="Path to save results JSON")
    parser.add_argument('--core-only', action='store_true',
                        help="Run only C01-C10 core routing conversations")

    args = parser.parse_args()
    verbose = not args.brief

    all_convs = get_all_conversations()

    # --list
    if args.list:
        categories = {}
        for c in all_convs:
            if c.category not in categories:
                categories[c.category] = []
            categories[c.category].append(c)

        total_turns = sum(len(c.turns) for c in all_convs)
        print(f"\n{len(all_convs)} conversations, {total_turns} total turns\n")
        for cat, convs in categories.items():
            turns = sum(len(c.turns) for c in convs)
            print(f"  {cat} ({len(convs)} convs, {turns} turns):")
            for c in convs:
                print(f"    {c.id}: {c.name} ({len(c.turns)} turns)")
            print()
        return

    # --analyze
    if args.analyze:
        if not os.path.exists(args.save):
            print(f"No results file found at {args.save}")
            print("Run the test suite first, then use --analyze")
            return
        results = load_results(args.save)
        for cr in results:
            print_conversation_result(cr, verbose=verbose)
        print_analysis(results)
        return

    # Filter conversations
    if args.id:
        convs = [c for c in all_convs if c.id.upper() == args.id.upper()]
        if not convs:
            print(f"Unknown conversation ID: {args.id}")
            print(f"Valid IDs: {', '.join(c.id for c in all_convs)}")
            return
    elif args.ids:
        requested = {x.strip().upper() for x in args.ids.split(',')}
        convs = [c for c in all_convs if c.id.upper() in requested]
        found = {c.id.upper() for c in convs}
        missing = requested - found
        if missing:
            print(f"Unknown conversation IDs: {', '.join(sorted(missing))}")
            print(f"Valid IDs: {', '.join(c.id for c in all_convs)}")
            return
        if not convs:
            print("No conversations matched")
            return
    elif args.category:
        convs = [c for c in all_convs
                  if args.category.lower() in c.category.lower()]
        if not convs:
            cats = sorted(set(c.category for c in all_convs))
            print(f"No conversations match category: {args.category}")
            print(f"Valid categories: {', '.join(cats)}")
            return
    elif args.core_only:
        convs = [c for c in all_convs if int(c.id[1:]) <= 10]
    else:
        convs = all_convs

    total_turns = sum(len(c.turns) for c in convs)
    print(f"\nJARVIS Conversational Test Suite")
    print(f"Conversations: {len(convs)} | Turns: {total_turns} | Delay: {args.delay}s")
    print(f"{'='*70}")

    # Load config and run
    try:
        config = load_config()
    except Exception as e:
        print(f"Failed to load config: {e}")
        return

    # Snapshot memory state before test run (auto-restore after)
    _snapshot_tag = f"pre_test_{int(time.time())}"
    try:
        from scripts.memory_snapshot import snapshot as mem_snapshot, restore as mem_restore
        mem_snapshot(_snapshot_tag)
        print(f"Memory snapshot: {_snapshot_tag}")
    except Exception as e:
        print(f"Warning: memory snapshot failed: {e}")
        _snapshot_tag = None

    # Activate debug logger via sentinel file
    _debug_log_path = args.save.replace('.json', '_debug.jsonl')
    _sentinel = "/tmp/.jarvis_debug_active"
    try:
        with open(_sentinel, 'w') as f:
            f.write(_debug_log_path)
        print(f"Debug logger: {_debug_log_path}")
    except OSError as e:
        print(f"Warning: could not activate debug logger: {e}")

    results = asyncio.run(run_suite(
        convs, config, delay=args.delay, verbose=verbose,
        reconnect_between=not args.no_reconnect,
    ))

    # Deactivate debug logger
    try:
        os.remove(_sentinel)
    except OSError:
        pass

    # Display results
    for cr in results:
        print_conversation_result(cr, verbose=verbose)

    # Analysis
    print_analysis(results)

    # Save
    save_results(results, args.save)

    # Restore memory state from pre-test snapshot
    if _snapshot_tag:
        try:
            mem_restore(_snapshot_tag)
            print(f"Memory restored from snapshot: {_snapshot_tag}")
        except Exception as e:
            print(f"WARNING: memory restore failed: {e}")
            print(f"  Manual restore: python3 scripts/memory_snapshot.py restore --tag {_snapshot_tag}")

    # Copy debug log to iterative_results if it exists
    if os.path.exists(_debug_log_path):
        _results_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                     'tests', 'iterative_results')
        if os.path.isdir(_results_dir):
            import shutil
            _dest = os.path.join(_results_dir,
                                  os.path.basename(_debug_log_path))
            try:
                shutil.copy2(_debug_log_path, _dest)
                print(f"Debug log copied to {_dest}")
            except OSError as e:
                print(f"Warning: could not copy debug log: {e}")


if __name__ == '__main__':
    main()
