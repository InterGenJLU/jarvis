#!/usr/bin/env python3
"""Fast smoke test — validates core functionality without GPU or running services.

10 seconds, no GPU, no running services, exit code 0 (pass) / 1 (fail).
Designed for JARVIS to run after code changes before deploying.

Usage:
    python3 tests/test_smoke.py
"""

import os
import sys
import time
import tempfile
import traceback

# Prevent GPU/model initialization
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'
os.environ.setdefault('HSA_OVERRIDE_GFX_VERSION', '11.0.0')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_results = []
_start_time = time.monotonic()


def test(name):
    """Decorator that registers and runs a test function."""
    def decorator(fn):
        try:
            fn()
            _results.append(("PASS", name, None))
        except Exception as e:
            _results.append(("FAIL", name, f"{e}\n{traceback.format_exc()}"))
        return fn
    return decorator


# ---------------------------------------------------------------------------
# 1. Core module imports
# ---------------------------------------------------------------------------

@test("import core.config")
def _():
    from core.config import Config

@test("import core.logger")
def _():
    from core.logger import get_logger, Logger

@test("import core.event_logger")
def _():
    from core.event_logger import EventLogger, get_event_logger, CATEGORIES, SEVERITIES

@test("import core.metrics_tracker")
def _():
    from core.metrics_tracker import MetricsTracker, get_metrics_tracker

@test("import core.conversation_router")
def _():
    from core.conversation_router import ConversationRouter, RouteResult, RouteContext

@test("import core.llm_router")
def _():
    from core.llm_router import LLMRouter, ToolCallRequest

@test("import core.tool_registry")
def _():
    from core.tool_registry import ALL_TOOLS, TOOL_HANDLERS, SKILL_TOOLS

@test("import core.health_check")
def _():
    from core.health_check import get_full_health

@test("import core.persona")
def _():
    from core import persona

@test("import core.honorific")
def _():
    from core.honorific import get_honorific, set_honorific

@test("import core.conversation_state")
def _():
    from core.conversation_state import ConversationState

@test("import core.watchdog")
def _():
    from core.watchdog import Watchdog

@test("import core.speaker_id")
def _():
    from core.speaker_id import SpeakerIdentifier


# ---------------------------------------------------------------------------
# 2. Config validation
# ---------------------------------------------------------------------------

@test("config loads and parses")
def _():
    from core.config import Config
    config = Config()
    # Config must load without error and have basic structure
    assert config is not None, "Config failed to initialize"
    # LLM section must exist
    assert config.get("llm.local.model_path") is not None, "Missing llm.local.model_path"

@test("config has audio settings")
def _():
    from core.config import Config
    config = Config()
    assert config.get("audio.mic_device") is not None, "Missing audio.mic_device"


# ---------------------------------------------------------------------------
# 3. Tool registry validation
# ---------------------------------------------------------------------------

@test("tool registry discovers tools")
def _():
    from core.tool_registry import ALL_TOOLS, TOOL_HANDLERS
    assert len(ALL_TOOLS) >= 10, f"Expected 10+ tools, got {len(ALL_TOOLS)}"
    assert len(TOOL_HANDLERS) >= 10, f"Expected 10+ handlers, got {len(TOOL_HANDLERS)}"

@test("tool schemas are valid")
def _():
    from core.tool_registry import ALL_TOOLS
    for name, schema in ALL_TOOLS.items():
        assert schema.get("type") == "function", f"{name}: missing type=function"
        fn = schema.get("function", {})
        assert fn.get("name") == name, f"{name}: schema name mismatch"
        assert fn.get("description"), f"{name}: missing description"
        assert fn.get("parameters"), f"{name}: missing parameters"

@test("always-included tools exist")
def _():
    from core.tool_registry import ALWAYS_INCLUDED_TOOLS
    required = {"web_search", "recall_memory"}
    actual = set(ALWAYS_INCLUDED_TOOLS.keys())
    missing = required - actual
    assert not missing, f"Missing always-included tools: {missing}"


# ---------------------------------------------------------------------------
# 4. Event logger roundtrip
# ---------------------------------------------------------------------------

@test("event logger creates DB and emits")
def _():
    from core.event_logger import EventLogger
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        class C:
            def get(self, k, d=None):
                if k == 'events.db_path': return db_path
                return d
        el = EventLogger(C())

        # Emit and query back
        trace = el.new_trace_id()
        obs_id = el.emit(category="decision", event="test_event",
                         message="smoke test", trace_id=trace,
                         latency_ms=42.0, status="success")
        assert obs_id, "emit() returned empty ID"

        results = el.query(event="test_event", hours=1)
        assert len(results) == 1, f"Expected 1 result, got {len(results)}"
        assert results[0]["latency_ms"] == 42.0

        # Trace retrieval
        trace_obs = el.get_trace(trace)
        assert len(trace_obs) == 1

        # Count
        assert el.count(category="decision", hours=1) == 1

        # Summary
        summary = el.get_summary(hours=1)
        assert summary["total"] == 1
    finally:
        os.unlink(db_path)

@test("event logger scores and reflections")
def _():
    from core.event_logger import EventLogger
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        class C:
            def get(self, k, d=None):
                if k == 'events.db_path': return db_path
                return d
        el = EventLogger(C())

        obs_id = el.emit(category="inference", event="test",
                         trace_id=el.new_trace_id())

        score_id = el.add_score(observation_id=obs_id, name="quality",
                                value=0.85, source="automated")
        scores = el.get_scores(obs_id)
        assert len(scores) == 1
        assert scores[0]["value"] == 0.85

        ref_id = el.add_reflection(category="strategy_update",
                                    content="Test reflection")
        refs = el.get_reflections()
        assert len(refs) == 1
    finally:
        os.unlink(db_path)

@test("event logger percentile queries")
def _():
    from core.event_logger import EventLogger
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        class C:
            def get(self, k, d=None):
                if k == 'events.db_path': return db_path
                return d
        el = EventLogger(C())
        trace = el.new_trace_id()
        for lat in [100, 200, 300, 400, 500]:
            el.emit(category="inference", event="llm_call",
                    trace_id=trace, latency_ms=lat)

        stats = el.get_latency_stats(event="llm_call", hours=1)
        assert stats["count"] == 5
        assert stats["p50"] > 0
        assert stats["mean"] == 300.0
    finally:
        os.unlink(db_path)

@test("event logger odd events")
def _():
    from core.event_logger import EventLogger
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        class C:
            def get(self, k, d=None):
                if k == 'events.db_path': return db_path
                return d
        el = EventLogger(C())

        odd_id = el.capture_odd_event("Test odd event", category="test")
        event = el.get_odd_event(odd_id)
        assert event is not None
        assert event["description"] == "Test odd event"
        assert event["resolved"] == 0

        el.update_odd_event(odd_id, resolved=True, root_cause="Testing")
        event = el.get_odd_event(odd_id)
        assert event["resolved"] == 1

        patterns = el.get_odd_event_patterns()
        assert patterns["total"] == 1
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 5. Event categories and constants
# ---------------------------------------------------------------------------

@test("9 event categories defined")
def _():
    from core.event_logger import CATEGORIES
    assert len(CATEGORIES) == 9, f"Expected 9 categories, got {len(CATEGORIES)}"
    required = {"user_interaction", "decision", "inference", "tool_execution",
                "memory", "error_recovery", "performance", "self_assessment", "learning"}
    assert CATEGORIES == required, f"Category mismatch: {CATEGORIES ^ required}"

@test("6 severity levels defined")
def _():
    from core.event_logger import SEVERITIES
    assert len(SEVERITIES) == 6
    assert "info" in SEVERITIES
    assert "fatal" in SEVERITIES


# ---------------------------------------------------------------------------
# 6. Governance module
# ---------------------------------------------------------------------------

@test("import core.governance")
def _():
    from core.governance import Governance, Tier, ACTION_TIERS, get_governance

@test("governance tier definitions")
def _():
    from core.governance import Tier
    assert Tier.READ == 0
    assert Tier.CONFIG == 1
    assert Tier.PROMPT == 2
    assert Tier.LOGIC == 3
    assert Tier.ARCHITECTURE == 4

@test("governance check — tier 0 approved")
def _():
    from core.governance import Governance
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        class C:
            def get(self, k, d=None):
                if k == 'governance.commandments_path': return os.path.join(td, 'cmd.md')
                if k == 'governance.hash_path': return os.path.join(td, '.hash')
                return d
        gov = Governance(C())
        r = gov.check('query_metrics')
        assert r.approved, f"Tier 0 should be approved: {r.reason}"
        gov.stop()

@test("governance check — tier 3 denied")
def _():
    from core.governance import Governance
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        class C:
            def get(self, k, d=None):
                if k == 'governance.commandments_path': return os.path.join(td, 'cmd.md')
                if k == 'governance.hash_path': return os.path.join(td, '.hash')
                return d
        gov = Governance(C())
        r = gov.check('modify_routing')
        assert not r.approved, "Tier 3 should be denied"
        gov.stop()

@test("governance fail-closed on unknown action")
def _():
    from core.governance import Governance
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        class C:
            def get(self, k, d=None):
                if k == 'governance.commandments_path': return os.path.join(td, 'cmd.md')
                if k == 'governance.hash_path': return os.path.join(td, '.hash')
                return d
        gov = Governance(C())
        r = gov.check('totally_unknown_action')
        assert not r.approved, "Unknown action should be denied (fail-closed)"
        assert r.tier == 3, "Unknown action should default to Tier 3"
        gov.stop()

@test("commandments file exists")
def _():
    from pathlib import Path
    cmd_path = Path(__file__).parent.parent / "governance" / "commandments.md"
    assert cmd_path.exists(), f"Commandments file missing: {cmd_path}"
    text = cmd_path.read_text()
    assert "Serve the household" in text, "Commandment I missing"
    assert "owner's voice is final" in text, "Commandment X missing"


# ---------------------------------------------------------------------------
# 7. Persona and conversation state
# ---------------------------------------------------------------------------

@test("persona has required pools")
def _():
    from core import persona
    # Persona must have ack_cache pool for voice pipeline TTS pre-synthesis
    acks = persona.pool("ack_cache")
    assert len(acks) > 0, "Persona ack_cache pool is empty"

@test("conversation state initializes cleanly")
def _():
    from core.conversation_state import ConversationState
    cs = ConversationState()
    assert cs.readback_session is None
    assert cs.last_tool_result_text == ""


# ---------------------------------------------------------------------------
# 7. RouteResult dataclass
# ---------------------------------------------------------------------------

@test("RouteResult defaults are sane")
def _():
    from core.conversation_router import RouteResult
    r = RouteResult()
    assert r.text == ""
    assert r.handled is False
    assert r.used_llm is False
    assert r.skip is False
    assert r.use_tools is None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

elapsed = time.monotonic() - _start_time
pass_count = sum(1 for r in _results if r[0] == "PASS")
fail_count = sum(1 for r in _results if r[0] == "FAIL")

print(f"\n{'='*60}")
print(f"  JARVIS Smoke Test — {pass_count}/{len(_results)} passed in {elapsed:.1f}s")
print(f"{'='*60}\n")

for status, name, error in _results:
    icon = "✓" if status == "PASS" else "✗"
    print(f"  {icon} {name}")
    if error:
        # Show first line of error only
        print(f"    → {error.splitlines()[0]}")

print()

if fail_count > 0:
    print(f"FAILED: {fail_count} test(s)")
    sys.exit(1)
else:
    print(f"ALL {pass_count} TESTS PASSED ({elapsed:.1f}s)")
    sys.exit(0)
