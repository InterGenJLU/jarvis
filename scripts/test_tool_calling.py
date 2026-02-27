#!/usr/bin/env python3
"""
Test harness for LLM-centric tool calling (Phase 1 migration).

Validates that Qwen3.5 correctly selects tools when given skill-specific
queries via stream_with_tools() with dynamic tool injection.

Usage:
    python3 scripts/test_tool_calling.py                  # Full suite (all skills, 10 runs)
    python3 scripts/test_tool_calling.py --skill time     # Single skill
    python3 scripts/test_tool_calling.py --runs 3         # Quick check (3 runs)
    python3 scripts/test_tool_calling.py --sweep          # Temperature/penalty sweep
    python3 scripts/test_tool_calling.py --verbose        # Show each trial
    python3 scripts/test_tool_calling.py --json           # JSON output

Outcome taxonomy (7 categories):
    correct_tool      — Right tool called with valid params
    correct_no_tool   — LLM answered directly (appropriate for the query)
    correct_clarify   — LLM asked a clarifying question (appropriate)
    incorrect_tool    — Wrong tool selected or bad params
    incorrect_no_tool — Should have called a tool but didn't
    hallucinated_tool — Called a tool not in the provided list
    format_error      — Malformed JSON/XML, unparseable response
"""

import sys
import os
import json
import time
import argparse
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.disable(logging.INFO)  # Suppress INFO logs from llm_router during tests

from core.config import Config
from core.llm_router import (
    LLMRouter, ToolCallRequest,
    WEB_SEARCH_TOOL, GET_TIME_TOOL, GET_SYSTEM_INFO_TOOL, FIND_FILES_TOOL,
    GET_WEATHER_TOOL,
)


# ---------------------------------------------------------------------------
# Test queries with expected outcomes
# ---------------------------------------------------------------------------

@dataclass
class TestQuery:
    """A test query with its expected outcome."""
    query: str
    expected_tool: str | None   # Expected tool name, or None for "no tool"
    category: str               # "time", "system", "filesystem", "no_tool", "web"
    description: str = ""


# 15 queries per skill × 4 + 10 no-tool + 4 web-search + 1 weather-location = 75 total
TEST_QUERIES = [
    # --- TIME (15 queries, expect get_time) ---
    TestQuery("what time is it", "get_time", "time"),
    TestQuery("what's the time", "get_time", "time"),
    TestQuery("tell me the time", "get_time", "time"),
    TestQuery("current time please", "get_time", "time"),
    TestQuery("what time do you have", "get_time", "time"),
    TestQuery("what's the date", "get_time", "time"),
    TestQuery("what date is it today", "get_time", "time"),
    TestQuery("what day is it", "get_time", "time"),
    TestQuery("today's date", "get_time", "time"),
    TestQuery("what's today", "get_time", "time"),
    TestQuery("do you know what time it is", "get_time", "time"),
    TestQuery("give me the time and date", "get_time", "time"),
    TestQuery("what's the current time and date", "get_time", "time"),
    TestQuery("what year is it", "get_time", "time"),
    TestQuery("is it morning or afternoon", "get_time", "time"),

    # --- SYSTEM INFO (15 queries, expect get_system_info) ---
    TestQuery("what cpu do i have", "get_system_info", "system"),
    TestQuery("how much ram do i have", "get_system_info", "system"),
    TestQuery("what's my disk space", "get_system_info", "system"),
    TestQuery("what gpu do i have", "get_system_info", "system"),
    TestQuery("show me my system uptime", "get_system_info", "system"),
    TestQuery("what's my hostname", "get_system_info", "system"),
    TestQuery("who am i logged in as", "get_system_info", "system"),
    TestQuery("how much memory is installed", "get_system_info", "system"),
    TestQuery("check disk usage", "get_system_info", "system"),
    TestQuery("what processor am i running", "get_system_info", "system"),
    TestQuery("how much free disk space do i have", "get_system_info", "system"),
    TestQuery("list my hard drives", "get_system_info", "system"),
    TestQuery("what graphics card is in this machine", "get_system_info", "system"),
    TestQuery("how long has the system been running", "get_system_info", "system"),
    TestQuery("what's my username", "get_system_info", "system"),

    # --- FILESYSTEM (15 queries, expect find_files) ---
    TestQuery("find my resume.pdf", "find_files", "filesystem"),
    TestQuery("where is expenses.xlsx", "find_files", "filesystem"),
    TestQuery("locate the config file", "find_files", "filesystem"),
    TestQuery("how many files are in my documents folder", "find_files", "filesystem"),
    TestQuery("count files in downloads", "find_files", "filesystem"),
    TestQuery("how many lines of code in the codebase", "find_files", "filesystem"),
    TestQuery("how big is the project", "find_files", "filesystem"),
    TestQuery("find the file named report.pdf", "find_files", "filesystem"),
    TestQuery("where did i save that spreadsheet", "find_files", "filesystem"),
    TestQuery("search for backup.sh", "find_files", "filesystem"),
    TestQuery("how many files in my home directory", "find_files", "filesystem"),
    TestQuery("count the python files", "find_files", "filesystem"),
    TestQuery("find my presentation", "find_files", "filesystem"),
    TestQuery("where is my budget file", "find_files", "filesystem"),
    TestQuery("how many items in the desktop folder", "find_files", "filesystem"),

    # --- NO TOOL expected (10 queries) ---
    TestQuery("hello", None, "no_tool", "greeting"),
    TestQuery("tell me a joke", None, "no_tool", "creative"),
    TestQuery("good morning", None, "no_tool", "greeting"),
    TestQuery("thank you", None, "no_tool", "acknowledgment"),
    TestQuery("what's your name", None, "no_tool", "identity"),
    TestQuery("how are you doing", None, "no_tool", "social"),
    TestQuery("can you write a haiku about rain", None, "no_tool", "creative"),
    TestQuery("what can you help me with", None, "no_tool", "capabilities"),
    TestQuery("you're welcome", None, "no_tool", "social"),
    TestQuery("say something nice", None, "no_tool", "creative"),

    # --- WEATHER (15 queries, expect get_weather) ---
    TestQuery("what's the weather", "get_weather", "weather"),
    TestQuery("how's the weather today", "get_weather", "weather"),
    TestQuery("current temperature", "get_weather", "weather"),
    TestQuery("is it hot outside", "get_weather", "weather"),
    TestQuery("weather in Paris", "get_weather", "weather"),
    TestQuery("what's the temperature in London", "get_weather", "weather"),
    TestQuery("what's the forecast", "get_weather", "weather"),
    TestQuery("forecast for this week", "get_weather", "weather"),
    TestQuery("will it rain tomorrow", "get_weather", "weather"),
    TestQuery("is it going to rain", "get_weather", "weather"),
    TestQuery("what's the weather tomorrow", "get_weather", "weather"),
    TestQuery("tomorrow's forecast", "get_weather", "weather"),
    TestQuery("how cold is it", "get_weather", "weather"),
    TestQuery("do I need an umbrella", "get_weather", "weather"),
    TestQuery("what are the conditions outside", "get_weather", "weather"),

    # --- WEB SEARCH expected (5 queries) ---
    TestQuery("who won the super bowl", "web_search", "web"),
    TestQuery("latest news about SpaceX", "web_search", "web"),
    TestQuery("how far is it from New York to London", "web_search", "web"),
    TestQuery("what is the current price of bitcoin", "web_search", "web"),
    TestQuery("what's the weather in Tokyo right now", "get_weather", "weather"),
]


# ---------------------------------------------------------------------------
# Trial execution
# ---------------------------------------------------------------------------

OUTCOME_CORRECT_TOOL = "correct_tool"
OUTCOME_CORRECT_NO_TOOL = "correct_no_tool"
OUTCOME_CORRECT_CLARIFY = "correct_clarify"
OUTCOME_INCORRECT_TOOL = "incorrect_tool"
OUTCOME_INCORRECT_NO_TOOL = "incorrect_no_tool"
OUTCOME_HALLUCINATED_TOOL = "hallucinated_tool"
OUTCOME_FORMAT_ERROR = "format_error"

ALL_OUTCOMES = [
    OUTCOME_CORRECT_TOOL, OUTCOME_CORRECT_NO_TOOL, OUTCOME_CORRECT_CLARIFY,
    OUTCOME_INCORRECT_TOOL, OUTCOME_INCORRECT_NO_TOOL,
    OUTCOME_HALLUCINATED_TOOL, OUTCOME_FORMAT_ERROR,
]

# Tool names we provide to the LLM
VALID_TOOL_NAMES = {"web_search", "get_time", "get_system_info", "find_files", "get_weather"}


@dataclass
class TrialResult:
    query: str
    expected_tool: str | None
    actual_tool: str | None       # None if LLM answered with text
    actual_args: dict | None
    text_response: str            # Non-empty if LLM answered with text
    outcome: str
    latency_ms: float
    error: str = ""


def classify_outcome(expected_tool: str | None, actual_tool: str | None,
                     text_response: str, error: str) -> str:
    """Classify a trial into one of the 7 outcome categories."""
    if error:
        return OUTCOME_FORMAT_ERROR

    if actual_tool:
        # LLM called a tool
        if actual_tool not in VALID_TOOL_NAMES:
            return OUTCOME_HALLUCINATED_TOOL
        if expected_tool and actual_tool == expected_tool:
            return OUTCOME_CORRECT_TOOL
        if expected_tool and actual_tool != expected_tool:
            return OUTCOME_INCORRECT_TOOL
        if not expected_tool:
            # Called a tool when we expected no tool
            return OUTCOME_INCORRECT_TOOL
    else:
        # LLM answered with text (no tool call)
        if expected_tool is None:
            return OUTCOME_CORRECT_NO_TOOL
        # Check if it's a clarifying question
        question_markers = ["?", "which", "what kind", "could you", "do you mean",
                           "can you specify", "please clarify"]
        if any(m in text_response.lower() for m in question_markers):
            return OUTCOME_CORRECT_CLARIFY
        return OUTCOME_INCORRECT_NO_TOOL

    return OUTCOME_FORMAT_ERROR


def run_trial(llm: LLMRouter, query: str, tools: list,
              temperature: float = 0.3,
              presence_penalty: float = 1.5) -> TrialResult:
    """Run a single trial: send query to LLM with tools, classify outcome."""
    actual_tool = None
    actual_args = None
    text_response = ""
    error = ""

    start = time.time()
    try:
        for item in llm.stream_with_tools(
            user_message=query,
            tools=tools,
            tool_temperature=temperature,
            tool_presence_penalty=presence_penalty,
        ):
            if isinstance(item, ToolCallRequest):
                actual_tool = item.name
                actual_args = item.arguments
            elif isinstance(item, str):
                text_response += item
    except AssertionError as e:
        error = f"assertion: {e}"
    except Exception as e:
        error = str(e)

    latency_ms = (time.time() - start) * 1000
    return TrialResult(
        query=query,
        expected_tool=None,  # filled by caller
        actual_tool=actual_tool,
        actual_args=actual_args,
        text_response=text_response.strip(),
        outcome="",  # filled by caller
        latency_ms=latency_ms,
        error=error,
    )


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_test_suite(llm: LLMRouter, queries: list[TestQuery], runs: int = 10,
                   temperature: float = 0.3, presence_penalty: float = 1.5,
                   verbose: bool = False, skill_filter: str = None) -> dict:
    """Run the full test suite and return results summary."""
    tools = [WEB_SEARCH_TOOL, GET_TIME_TOOL, GET_SYSTEM_INFO_TOOL, FIND_FILES_TOOL, GET_WEATHER_TOOL]

    if skill_filter:
        queries = [q for q in queries if q.category == skill_filter]

    total_trials = len(queries) * runs
    print(f"\n{'='*60}")
    print(f"LLM Tool-Calling Test Harness (Phase 1)")
    print(f"{'='*60}")
    print(f"Queries: {len(queries)} | Runs: {runs} | Total trials: {total_trials}")
    print(f"Tools: {len(tools)} ({', '.join(t['function']['name'] for t in tools)})")
    print(f"Temperature: {temperature} | Presence penalty: {presence_penalty}")
    print(f"{'='*60}\n")

    # Counters
    outcomes = defaultdict(int)
    per_category = defaultdict(lambda: defaultdict(int))
    per_query = defaultdict(list)
    all_results = []
    latencies = []

    trial_num = 0
    for run_idx in range(runs):
        for tq in queries:
            trial_num += 1
            if verbose:
                print(f"  [{trial_num}/{total_trials}] Run {run_idx+1}: {tq.query[:50]}...", end=" ")

            result = run_trial(llm, tq.query, tools, temperature, presence_penalty)
            result.expected_tool = tq.expected_tool
            result.outcome = classify_outcome(
                tq.expected_tool, result.actual_tool,
                result.text_response, result.error,
            )

            outcomes[result.outcome] += 1
            per_category[tq.category][result.outcome] += 1
            per_query[tq.query].append(result)
            all_results.append(result)
            latencies.append(result.latency_ms)

            if verbose:
                icon = "✓" if result.outcome.startswith("correct") else "✗"
                tool_info = result.actual_tool or f"text: {result.text_response[:40]}"
                print(f"{icon} {result.outcome} ({result.latency_ms:.0f}ms) → {tool_info}")

    # --- Summary ---
    correct = outcomes[OUTCOME_CORRECT_TOOL] + outcomes[OUTCOME_CORRECT_NO_TOOL] + outcomes[OUTCOME_CORRECT_CLARIFY]
    accuracy = (correct / total_trials * 100) if total_trials else 0
    avg_latency = sum(latencies) / len(latencies) if latencies else 0

    print(f"\n{'='*60}")
    print(f"RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"\nOverall accuracy: {accuracy:.1f}% ({correct}/{total_trials})")
    print(f"Average latency: {avg_latency:.0f}ms")
    print()

    # Outcome breakdown
    print("Outcome breakdown:")
    for outcome in ALL_OUTCOMES:
        count = outcomes[outcome]
        pct = (count / total_trials * 100) if total_trials else 0
        bar = "█" * int(pct / 2)
        print(f"  {outcome:20s}  {count:4d}  ({pct:5.1f}%) {bar}")

    # Per-category breakdown
    print("\nPer-category accuracy:")
    for cat in ["time", "system", "filesystem", "no_tool", "web"]:
        cat_outcomes = per_category.get(cat, {})
        cat_total = sum(cat_outcomes.values())
        cat_correct = (cat_outcomes.get(OUTCOME_CORRECT_TOOL, 0)
                      + cat_outcomes.get(OUTCOME_CORRECT_NO_TOOL, 0)
                      + cat_outcomes.get(OUTCOME_CORRECT_CLARIFY, 0))
        cat_acc = (cat_correct / cat_total * 100) if cat_total else 0
        print(f"  {cat:15s}  {cat_acc:5.1f}%  ({cat_correct}/{cat_total})")

    # Worst queries (most errors)
    print("\nQueries with errors:")
    error_queries = []
    for query, results in per_query.items():
        errors = sum(1 for r in results if not r.outcome.startswith("correct"))
        if errors > 0:
            error_queries.append((query, errors, len(results)))
    error_queries.sort(key=lambda x: -x[1])
    for q, errs, total in error_queries[:10]:
        print(f"  {errs}/{total} errors: {q}")

    if not error_queries:
        print("  (none)")

    return {
        "accuracy": accuracy,
        "total_trials": total_trials,
        "correct": correct,
        "outcomes": dict(outcomes),
        "per_category": {k: dict(v) for k, v in per_category.items()},
        "avg_latency_ms": avg_latency,
        "temperature": temperature,
        "presence_penalty": presence_penalty,
        "runs": runs,
    }


def run_sweep(llm: LLMRouter, queries: list[TestQuery], runs: int = 3,
              verbose: bool = False) -> list[dict]:
    """Run temperature x presence_penalty sweep."""
    temperatures = [0.0, 0.2, 0.3, 0.6]
    penalties = [0.0, 1.5]

    results = []
    for temp in temperatures:
        for pp in penalties:
            print(f"\n{'#'*60}")
            print(f"SWEEP: temperature={temp}, presence_penalty={pp}")
            print(f"{'#'*60}")
            result = run_test_suite(
                llm, queries, runs=runs,
                temperature=temp, presence_penalty=pp,
                verbose=verbose,
            )
            results.append(result)

    # Summary table
    print(f"\n{'='*60}")
    print(f"SWEEP SUMMARY")
    print(f"{'='*60}")
    print(f"{'Temp':>6s} {'PP':>6s} {'Accuracy':>10s} {'Latency':>10s}")
    print(f"{'-'*6} {'-'*6} {'-'*10} {'-'*10}")
    for r in results:
        print(f"{r['temperature']:6.1f} {r['presence_penalty']:6.1f} "
              f"{r['accuracy']:9.1f}% {r['avg_latency_ms']:9.0f}ms")

    best = max(results, key=lambda r: r["accuracy"])
    print(f"\nBest: temp={best['temperature']}, pp={best['presence_penalty']} "
          f"({best['accuracy']:.1f}%)")

    return results


def main():
    parser = argparse.ArgumentParser(description="LLM Tool-Calling Test Harness")
    parser.add_argument("--runs", type=int, default=10, help="Runs per query (default: 10)")
    parser.add_argument("--skill", choices=["time", "system", "filesystem", "weather", "no_tool", "web"],
                       help="Test only one category")
    parser.add_argument("--sweep", action="store_true", help="Run temperature/penalty sweep")
    parser.add_argument("--temp", type=float, default=0.3, help="Temperature (default: 0.3)")
    parser.add_argument("--pp", type=float, default=1.5, help="Presence penalty (default: 1.5)")
    parser.add_argument("--verbose", action="store_true", help="Show each trial")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    # Load config and create LLM router
    config = Config()
    llm = LLMRouter(config)

    if not llm.tool_calling:
        print("ERROR: Tool calling is not enabled in config. Set llm.local.tool_calling=true")
        sys.exit(1)

    # Verify LLM server is reachable
    try:
        import requests
        resp = requests.get("http://127.0.0.1:8080/health", timeout=5)
        if resp.status_code != 200:
            print("ERROR: llama-server not healthy")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: Cannot reach llama-server: {e}")
        sys.exit(1)

    print("llama-server is healthy")

    if args.sweep:
        results = run_sweep(llm, TEST_QUERIES, runs=args.runs, verbose=args.verbose)
        if args.json:
            print(json.dumps(results, indent=2))
    else:
        result = run_test_suite(
            llm, TEST_QUERIES, runs=args.runs,
            temperature=args.temp, presence_penalty=args.pp,
            verbose=args.verbose, skill_filter=args.skill,
        )
        if args.json:
            print(json.dumps(result, indent=2))

    # ROCm/ONNX teardown: use os._exit to prevent abort() on cleanup
    os._exit(0)


if __name__ == "__main__":
    main()
