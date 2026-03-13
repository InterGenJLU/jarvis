#!/usr/bin/env python3
"""
Temporal weather parser + handler integration test.

Part 1: Unit tests for parse_temporal_phrase (no server needed)
Part 2: Live WebSocket tests through jarvis-web (desktop + mobile, both users)

Usage:
    python3 scripts/test_temporal_weather.py --unit          # Parser unit tests only
    python3 scripts/test_temporal_weather.py --live          # Live WS tests only
    python3 scripts/test_temporal_weather.py --all           # Both
    python3 scripts/test_temporal_weather.py --verbose       # Extra detail
"""

import asyncio
import json
import os
import ssl
import sys
import time
import argparse
from datetime import date, timedelta
from dataclasses import dataclass

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import yaml
except ImportError:
    yaml = None


# ── Helpers ───────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"
WARN = "\033[93mWARN\033[0m"


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    response: str = ""
    routing: str = ""
    handler: str = ""
    ms: int = 0


# ══════════════════════════════════════════════════════════════════════════
# PART 1: Unit Tests — parse_temporal_phrase
# ══════════════════════════════════════════════════════════════════════════

def run_unit_tests(verbose: bool) -> list[TestResult]:
    """Test parse_temporal_phrase against known inputs."""
    from core.weather_db import parse_temporal_phrase

    today = date.today()
    weekday = today.weekday()  # 0=Mon … 6=Sun

    # Compute expected dates
    days_to_sat = (5 - weekday) % 7
    if weekday < 5:
        this_sat = today + timedelta(days=days_to_sat)
    elif weekday == 5:
        this_sat = today
    else:  # Sunday
        this_sat = today - timedelta(days=1)
    this_sun = this_sat + timedelta(days=1)

    # Next weekend
    if weekday < 5:
        next_sat = today + timedelta(days=(5 - weekday) + 7)
    else:
        next_sat = today + timedelta(days=(5 - weekday) % 7 + 7)
    next_sun = next_sat + timedelta(days=1)

    # Next week (Mon-Sun)
    days_to_mon = (7 - weekday) % 7
    if days_to_mon == 0:
        days_to_mon = 7
    next_mon = today + timedelta(days=days_to_mon)
    next_week_sun = next_mon + timedelta(days=6)

    # This week (today through Sunday)
    if weekday == 6:
        this_week_sun = today
    else:
        this_week_sun = today + timedelta(days=(6 - weekday))

    cases = [
        # (input_text, expected_start, expected_end, description)
        ("what's the weather this weekend", this_sat, this_sun, "this weekend"),
        ("weather this weekend", this_sat, this_sun, "this weekend (short)"),
        ("how's the weather looking this weekend", this_sat, this_sun, "this weekend (long)"),
        ("next weekend weather", next_sat, next_sun, "next weekend"),
        ("what's next weekend looking like", next_sat, next_sun, "next weekend (long)"),
        ("weather next week", next_mon, next_week_sun, "next week"),
        ("what's the weather next week", next_mon, next_week_sun, "next week (long)"),
        ("this week weather", today, this_week_sun, "this week"),
        ("what's the weather for the next few days", today, today + timedelta(days=3), "next few days"),
        ("how's the next few days looking", today, today + timedelta(days=3), "next few days (alt)"),
        ("weather for the coming days", today, today + timedelta(days=3), "coming days"),
        ("weather for the next 5 days", today, today + timedelta(days=5), "next 5 days"),
        ("next 10 days", today, today + timedelta(days=10), "next 10 days"),
        ("what's the weather like today", None, None, "no temporal phrase (today=current)"),
        ("how hot is it", None, None, "no temporal phrase (current)"),
        ("weather in paris", None, None, "no temporal phrase (location)"),
        ("tell me a joke", None, None, "no temporal phrase (unrelated)"),
    ]

    # Add end-of-week tests (day-dependent)
    if weekday <= 4:  # Mon-Fri
        eow_fri = today + timedelta(days=(4 - weekday))
        eow_sun = eow_fri + timedelta(days=2)
    elif weekday == 5:  # Sat
        eow_fri = today
        eow_sun = today + timedelta(days=1)
    else:  # Sun
        eow_fri = today
        eow_sun = today
    cases.append(("end of the week weather", eow_fri, eow_sun, "end of the week"))
    cases.append(("weather at the end of this week", eow_fri, eow_sun, "end of this week"))

    results = []
    for text, exp_start, exp_end, desc in cases:
        result = parse_temporal_phrase(text)

        if exp_start is None:
            # Expect None
            passed = result is None
            detail = f"expected None, got {result}" if not passed else "correctly returned None"
        else:
            if result is None:
                passed = False
                detail = f"expected ({exp_start}, {exp_end}), got None"
            else:
                got_start, got_end = result
                passed = (got_start == exp_start and got_end == exp_end)
                if not passed:
                    detail = f"expected ({exp_start}, {exp_end}), got ({got_start}, {got_end})"
                else:
                    detail = f"({got_start} to {got_end})"

        results.append(TestResult(
            name=f"UNIT: {desc}",
            passed=passed,
            detail=detail,
        ))

        if verbose or not passed:
            status = PASS if passed else FAIL
            print(f"  {status}  {desc}")
            print(f"         input: \"{text}\"")
            print(f"         {detail}")
        elif passed:
            print(f"  {PASS}  {desc}")

    return results


# ══════════════════════════════════════════════════════════════════════════
# PART 2: Live WebSocket Tests
# ══════════════════════════════════════════════════════════════════════════

def load_config():
    """Load auth token and connection details from config.yaml."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    with open(os.path.abspath(config_path)) as f:
        config = yaml.safe_load(f)
    web = config.get('web', {})
    tls = web.get('tls', {})
    token = web.get('auth_token', '')
    tls_enabled = tls.get('enabled', False)
    port = tls.get('port', 8443) if tls_enabled else web.get('port', 8088)
    scheme = 'wss' if tls_enabled else 'ws'
    return {'url': f'{scheme}://localhost:{port}/ws', 'token': token, 'tls': tls_enabled}


class WSClient:
    """Minimal WebSocket client for testing."""

    def __init__(self, url: str, token: str, tls: bool = True,
                 screen_width: int = 1920, ua: str = ""):
        self.url = f"{url}?token={token}"
        self.tls = tls
        self.ws = None
        self.session = None
        self.screen_width = screen_width
        self.ua = ua

    async def connect(self):
        ssl_ctx = None
        if self.tls:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        self.session = aiohttp.ClientSession()
        headers = {}
        if self.ua:
            headers['User-Agent'] = self.ua
        self.ws = await self.session.ws_connect(
            self.url, ssl=ssl_ctx, heartbeat=30, headers=headers
        )
        await self._consume_handshake()
        # Send client_info to set client_type
        await self.ws.send_json({
            "type": "client_info",
            "screen_width": self.screen_width,
        })
        # Brief pause for server to process
        await asyncio.sleep(0.3)

    async def _consume_handshake(self):
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

    async def set_user(self, user_id: str):
        await self.ws.send_json({"type": "set_user", "user_id": user_id})
        await self._consume_handshake()

    async def send_location(self, lat: float, lon: float):
        """Send GPS location (simulating mobile client)."""
        await self.ws.send_json({
            "type": "client_location",
            "latitude": lat,
            "longitude": lon,
        })
        await asyncio.sleep(0.5)

    async def send(self, content: str, timeout: int = 60) -> dict:
        """Send message and collect response + routing metadata."""
        await self.ws.send_json({"type": "message", "content": content})

        tokens = []
        response_text = ""
        stats = {}
        info_msgs = []

        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=timeout)
            except asyncio.TimeoutError:
                if tokens:
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
                stats = data.get('stats', {})
                break
            elif mtype == 'response':
                response_text = data.get('content', '')
                stats = data.get('stats', {})
                break
            elif mtype == 'info':
                info_msgs.append(data.get('content', ''))
            elif mtype == 'error':
                response_text = f"ERROR: {data.get('content', 'unknown')}"
                break

        return {
            'response': response_text,
            'routing': stats.get('routing_layer', ''),
            'handler': stats.get('handler', ''),
            'skill': stats.get('skill', ''),
            'ms': stats.get('total_ms', 0),
            'info': info_msgs,
        }

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()


# Mobile UA string
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3 "
    "Mobile/15E148 Safari/604.1"
)

# Home coords (Gardendale, AL)
HOME_LAT, HOME_LON = 33.6662, -86.8128


@dataclass
class LiveTest:
    id: str
    query: str
    description: str
    user_id: str = "primary_user"
    client_type: str = "desktop"  # desktop or mobile
    send_location: bool = False
    lat: float = HOME_LAT
    lon: float = HOME_LON
    # Expectations
    expect_handler_contains: str = ""
    expect_response_contains: list = None
    expect_no_error: bool = True


LIVE_TESTS = [
    # --- Desktop / the user: temporal queries ---
    LiveTest("T01", "What's the weather like this weekend?",
             "weekend query - desktop christopher",
             expect_handler_contains="period",
             expect_response_contains=["Saturday", "Sunday"]),
    LiveTest("T02", "How's the weather looking next week?",
             "next week - desktop christopher",
             expect_handler_contains="period",
             expect_response_contains=["Monday"]),
    LiveTest("T03", "Weather for the next few days",
             "next few days - desktop christopher",
             expect_handler_contains="period"),
    LiveTest("T04", "What's the forecast?",
             "plain forecast - should NOT hit period handler",
             expect_handler_contains="forecast"),
    LiveTest("T05", "What's the weather right now?",
             "current weather - should NOT hit period handler",
             expect_handler_contains="current"),
    LiveTest("T06", "Next weekend weather",
             "next weekend - desktop christopher",
             expect_handler_contains="period"),
    LiveTest("T07", "What's the weather at the end of the week?",
             "end of week - desktop christopher",
             expect_handler_contains="period"),
    LiveTest("T08", "Weather for the next 5 days",
             "next N days - desktop christopher",
             expect_handler_contains="period"),

    # --- Mobile / the user at home: temporal queries ---
    LiveTest("T09", "What's the weather this weekend?",
             "weekend - mobile christopher at home",
             client_type="mobile", send_location=True,
             lat=HOME_LAT, lon=HOME_LON,
             expect_handler_contains="period",
             expect_response_contains=["Saturday", "Sunday"]),

    # --- Desktop / Secondary User: temporal queries ---
    LiveTest("T10", "What's the weather this weekend?",
             "weekend - desktop erica",
             user_id="secondary_user",
             expect_handler_contains="period",
             expect_response_contains=["Saturday", "Sunday"]),
    LiveTest("T11", "How's the weather next week?",
             "next week - desktop erica",
             user_id="secondary_user",
             expect_handler_contains="period"),

    # --- Regression: existing weather queries still work ---
    LiveTest("T12", "Will it rain tomorrow?",
             "rain check - regression",
             expect_handler_contains="rain"),
    LiveTest("T13", "What time is sunrise?",
             "sunrise - regression",
             expect_handler_contains="sunrise"),
    LiveTest("T14", "Weather tomorrow",
             "tomorrow - regression",
             expect_handler_contains="tomorrow"),
]


async def run_live_tests(verbose: bool) -> list[TestResult]:
    """Run live WebSocket tests."""
    if not aiohttp or not yaml:
        print(f"  {SKIP}  Live tests require aiohttp and PyYAML")
        return []

    config = load_config()
    results = []

    for test in LIVE_TESTS:
        client = None
        try:
            # Choose client type
            if test.client_type == 'mobile':
                client = WSClient(config['url'], config['token'], config['tls'],
                                  screen_width=440, ua=IPHONE_UA)
            else:
                client = WSClient(config['url'], config['token'], config['tls'],
                                  screen_width=1920)
            await client.connect()

            # Set user
            if test.user_id != "primary_user":
                await client.set_user(test.user_id)

            # Send location for mobile
            if test.send_location:
                await client.send_location(test.lat, test.lon)

            # Send query
            resp = await client.send(test.query)
            response_text = resp['response']
            handler = resp['handler']
            routing = resp['routing']
            ms = resp['ms']

            # Evaluate
            passed = True
            fail_reasons = []

            if test.expect_no_error and response_text.startswith("ERROR"):
                passed = False
                fail_reasons.append(f"got error: {response_text[:100]}")

            if test.expect_handler_contains:
                handler_lower = (handler or '').lower()
                routing_lower = (routing or '').lower()
                combined = f"{handler_lower} {routing_lower} {response_text.lower()}"
                # For period checks, also accept the response having day names
                if test.expect_handler_contains == "period":
                    has_period_handler = "period" in handler_lower or "get_weather_for_period" in handler_lower
                    has_period_content = any(d in response_text for d in
                                             ["Saturday", "Sunday", "Monday", "Tuesday",
                                              "Wednesday", "Thursday", "Friday",
                                              "outlook for", "Here's the outlook"])
                    if not (has_period_handler or has_period_content):
                        passed = False
                        fail_reasons.append(
                            f"expected period handler/content, got handler='{handler}' "
                            f"routing='{routing}'")
                elif test.expect_handler_contains not in combined:
                    passed = False
                    fail_reasons.append(
                        f"expected '{test.expect_handler_contains}' in handler, "
                        f"got handler='{handler}' routing='{routing}'")

            if test.expect_response_contains:
                for expected in test.expect_response_contains:
                    if expected.lower() not in response_text.lower():
                        passed = False
                        fail_reasons.append(f"response missing '{expected}'")

            detail = "; ".join(fail_reasons) if fail_reasons else "ok"
            tr = TestResult(
                name=f"LIVE {test.id}: {test.description}",
                passed=passed,
                detail=detail,
                response=response_text[:200],
                routing=routing,
                handler=handler,
                ms=ms,
            )
            results.append(tr)

            status = PASS if passed else FAIL
            print(f"  {status}  [{test.id}] {test.description} ({ms}ms)")
            if verbose or not passed:
                print(f"         query: \"{test.query}\"")
                print(f"         handler: {handler} | routing: {routing}")
                print(f"         response: {response_text[:150]}")
                if fail_reasons:
                    for r in fail_reasons:
                        print(f"         !! {r}")
                print()

        except Exception as e:
            results.append(TestResult(
                name=f"LIVE {test.id}: {test.description}",
                passed=False,
                detail=f"exception: {e}",
            ))
            print(f"  {FAIL}  [{test.id}] {test.description}")
            print(f"         EXCEPTION: {e}")
        finally:
            if client:
                await client.close()
            # Brief pause between tests to avoid overwhelming the server
            await asyncio.sleep(0.5)

    return results


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Temporal weather parser tests")
    parser.add_argument('--unit', action='store_true', help='Run unit tests only')
    parser.add_argument('--live', action='store_true', help='Run live WS tests only')
    parser.add_argument('--all', action='store_true', help='Run all tests')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    args = parser.parse_args()

    # Default to --all if nothing specified
    if not args.unit and not args.live:
        args.all = True

    all_results = []

    if args.unit or args.all:
        print("\n═══ PART 1: parse_temporal_phrase Unit Tests ═══")
        print(f"    Today: {date.today()} ({date.today().strftime('%A')})\n")
        unit_results = run_unit_tests(args.verbose)
        all_results.extend(unit_results)

    if args.live or args.all:
        print("\n═══ PART 2: Live WebSocket Integration Tests ═══\n")
        live_results = asyncio.run(run_live_tests(args.verbose))
        all_results.extend(live_results)

    # Summary
    passed = sum(1 for r in all_results if r.passed)
    failed = sum(1 for r in all_results if not r.passed)
    total = len(all_results)

    print(f"\n{'═' * 50}")
    print(f"  Total: {total}  |  {PASS}: {passed}  |  {FAIL}: {failed}")
    if failed:
        print(f"\n  Failed tests:")
        for r in all_results:
            if not r.passed:
                print(f"    - {r.name}: {r.detail}")
    print(f"{'═' * 50}\n")

    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
