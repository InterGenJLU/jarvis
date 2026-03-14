"""
CLI entry point and orchestrator for JARVIS Test Suite V3.

Usage:
    python3 -m scripts.test_suite_v3.runner --verbose --save tests/v3_results/run_001
    python3 -m scripts.test_suite_v3.runner --ids V06,M01 --verbose --save tests/v3_results/run_002
    python3 -m scripts.test_suite_v3.runner --category routing --verbose
    python3 -m scripts.test_suite_v3.runner --tag tool:web_search --verbose
    python3 -m scripts.test_suite_v3.runner --compare tests/v3_results/run_001 tests/v3_results/run_002
    python3 -m scripts.test_suite_v3.runner --no-cleanup --verbose
    python3 -m scripts.test_suite_v3.runner --auto-clean --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import aiohttp

from .client import JarvisClient, TurnLog, load_config
from .conversations import get_conversations, Conversation, Turn
from .grader import grade_turn, compute_turn_grade, compute_conversation_grade
from .logger import V3Logger
from .cleanup import snapshot_state, verify_clean_state
from .comparator import compare_runs
from .report import generate_summary


# ── Console output helpers ───────────────────────────────────────────────

_GRADE_COLORS = {
    "PASS": "\033[92m",   # green
    "MIXED": "\033[93m",  # yellow
    "FAIL": "\033[91m",   # red
    "ERROR": "\033[91m",
}
_RESET = "\033[0m"


def _color(grade: str) -> str:
    return f"{_GRADE_COLORS.get(grade, '')}{grade}{_RESET}"


def _print_turn(turn_num: int, user_input: str, turn_log: TurnLog,
                assertion_results: list, grade: str, verbose: bool):
    """Print a single turn result to console."""
    layer = turn_log.routing_layer or "?"
    skill = turn_log.skill_name or turn_log.llm_model or ""
    ms = turn_log.total_ms

    print(f"  T{turn_num}: [{layer}] {skill} ({ms}ms) {_color(grade)}")
    print(f"      User: {user_input}")

    if verbose:
        resp = turn_log.response_text
        print(f"      JARVIS: {resp}")

    # Show failed assertions
    failed = [r for r in assertion_results if not r.passed]
    if failed:
        for r in failed:
            detail = f" ({r.detail})" if r.detail else ""
            print(f"      \033[91m✗ {r.name}{detail}{_RESET}")


# ── Core orchestration ───────────────────────────────────────────────────

async def run_conversation(client: JarvisClient, conv: Conversation,
                           delay: float, verbose: bool,
                           logger: V3Logger | None = None) -> dict:
    """Run a single conversation and return graded results."""
    turn_grades = []
    total_assertions = 0
    total_passed = 0
    total_failed = 0
    start = time.time()

    for i, turn in enumerate(conv.turns):
        turn_num = i + 1

        # Always set user on first turn (server shared state may not match)
        if i == 0:
            await client.set_user(turn.user_id)
        elif turn.user_id != conv.turns[i - 1].user_id:
            await client.set_user(turn.user_id)

        # Send turn
        turn_log = await client.send_turn(
            content=turn.user_input,
            conversation_id=conv.id,
            turn_num=turn_num,
            user_id=turn.user_id,
        )

        # Grade
        assertion_results = grade_turn(
            turn_log=turn_log,
            assertions=turn.assertions,
            user_id=turn.user_id,
            skip_honorific=turn.skip_honorific,
            skip_filler=turn.skip_filler,
            skip_non_empty=turn.skip_non_empty,
            is_greeting=turn.is_greeting,
            is_farewell=turn.is_farewell,
        )
        grade = compute_turn_grade(assertion_results)
        turn_grades.append(grade)

        passed = sum(1 for r in assertion_results if r.passed)
        failed = sum(1 for r in assertion_results if not r.passed)
        total_assertions += len(assertion_results)
        total_passed += passed
        total_failed += failed

        # Log
        if logger:
            logger.log_turn(turn_log, assertion_results, grade)

        # Print
        _print_turn(turn_num, turn.user_input, turn_log, assertion_results,
                    grade, verbose)

        # Delay between turns (not after last)
        if i < len(conv.turns) - 1:
            await asyncio.sleep(delay)

    duration_ms = int((time.time() - start) * 1000)
    conv_grade = compute_conversation_grade(turn_grades)

    # Log conversation summary
    if logger:
        logger.log_conversation_summary(
            conversation_id=conv.id,
            name=conv.name,
            category=conv.category,
            turn_count=len(conv.turns),
            grade=conv_grade,
            turn_grades=turn_grades,
            assertions_total=total_assertions,
            assertions_passed=total_passed,
            assertions_failed=total_failed,
            duration_ms=duration_ms,
            cleanup_actions=[],
            tags=conv.tags,
        )

    return {
        "id": conv.id,
        "name": conv.name,
        "category": conv.category,
        "turn_count": len(conv.turns),
        "grade": conv_grade,
        "turn_grades": turn_grades,
        "assertions_total": total_assertions,
        "assertions_passed": total_passed,
        "assertions_failed": total_failed,
        "duration_ms": duration_ms,
    }


async def run_suite(conversations: list[Conversation], config: dict,
                    delay: float = 2.0, verbose: bool = True,
                    output_dir: str | None = None,
                    no_cleanup: bool = False,
                    auto_clean: bool = False) -> dict:
    """Run all conversations and return run-level results."""
    run_start = datetime.now(timezone.utc)

    # Initialize logger
    logger = None
    if output_dir:
        logger = V3Logger(output_dir)
        logger.open()

    # Base URL for REST API (cleanup)
    base_url = config['url'].replace('/ws', '').replace('ws://', 'http://').replace('wss://', 'https://')

    # Pre-run snapshot
    pre_snapshot = None
    if not no_cleanup:
        try:
            pre_snapshot = await snapshot_state(base_url, config['token'])
            print(f"Pre-run snapshot: {len(pre_snapshot.memory_fact_ids)} facts, "
                  f"{len(pre_snapshot.share_files)} share files")
        except Exception as e:
            print(f"Warning: snapshot failed: {e}")

    # Connect
    client = JarvisClient(
        url=config['url'], token=config['token'], tls=config['tls_enabled'],
    )

    conversation_results = []

    try:
        print(f"Connecting to {config['url']}...")
        max_retries = 10
        for attempt in range(1, max_retries + 1):
            try:
                await client.connect()
                break
            except (aiohttp.ClientError, OSError) as e:
                if attempt == max_retries:
                    raise
                wait = min(attempt * 2, 10)
                print(f"  Attempt {attempt}/{max_retries} failed: {e}")
                print(f"  Retrying in {wait}s...")
                await asyncio.sleep(wait)
                client = JarvisClient(
                    url=config['url'], token=config['token'],
                    tls=config['tls_enabled'],
                )

        total_turns = sum(len(c.turns) for c in conversations)
        print(f"Connected. Running {len(conversations)} conversations, "
              f"{total_turns} turns.\n")

        for i, conv in enumerate(conversations):
            print(f"\n[{i+1}/{len(conversations)}] {conv.id}: {conv.name} "
                  f"({len(conv.turns)} turns)")

            result = await run_conversation(client, conv, delay, verbose, logger)
            conversation_results.append(result)

            print(f"  → {_color(result['grade'])}")

            # Reconnect between conversations for session isolation
            if i < len(conversations) - 1:
                await client.close()
                await asyncio.sleep(1)
                client = JarvisClient(
                    url=config['url'], token=config['token'],
                    tls=config['tls_enabled'],
                )
                await client.connect()

    except aiohttp.ClientError as e:
        print(f"\nConnection error: {e}")
        print("Is JARVIS web service running? (systemctl --user status jarvis-web)")
    finally:
        await client.close()

    # Run-level cleanup / safety net
    cleanup_report = None
    if not no_cleanup and pre_snapshot:
        try:
            cleanup_report = await verify_clean_state(
                pre_snapshot, base_url, config['token'], auto_clean=auto_clean,
            )
            if cleanup_report.leaks:
                print(f"\n⚠ Cleanup report: {len(cleanup_report.leaks)} leak(s) detected:")
                for leak in cleanup_report.leaks:
                    print(f"  - {leak}")
                if auto_clean:
                    for action in cleanup_report.actions:
                        print(f"  ✓ {action}")
            else:
                print("\nCleanup: no leaks detected ✓")
        except Exception as e:
            print(f"\nWarning: cleanup verification failed: {e}")

    # Build run data
    run_end = datetime.now(timezone.utc)
    total_duration = int((run_end - run_start).total_seconds() * 1000)

    run_data = {
        "run_id": os.path.basename(output_dir) if output_dir else f"run_{int(time.time())}",
        "timestamp": run_start.isoformat(),
        "suite_version": "3.0",
        "conversations_total": len(conversations),
        "conversations_pass": sum(1 for r in conversation_results if r["grade"] == "PASS"),
        "conversations_mixed": sum(1 for r in conversation_results if r["grade"] == "MIXED"),
        "conversations_fail": sum(1 for r in conversation_results if r["grade"] == "FAIL"),
        "turns_total": sum(r["turn_count"] for r in conversation_results),
        "assertions_total": sum(r["assertions_total"] for r in conversation_results),
        "assertions_passed": sum(r["assertions_passed"] for r in conversation_results),
        "assertions_failed": sum(r["assertions_failed"] for r in conversation_results),
        "total_duration_ms": total_duration,
        "cleanup_report": {
            "artifacts_created": cleanup_report.artifacts_created if cleanup_report else 0,
            "artifacts_cleaned": cleanup_report.artifacts_cleaned if cleanup_report else 0,
            "leaks": len(cleanup_report.leaks) if cleanup_report else 0,
        },
        "conversations": conversation_results,
    }

    # Write results
    if logger:
        logger.write_results(run_data)
        logger.close()

    # Write summary
    if output_dir:
        summary = generate_summary(run_data)
        summary_path = os.path.join(output_dir, "summary.txt")
        with open(summary_path, 'w') as f:
            f.write(summary)
        print(f"\nResults saved to {output_dir}/")

    return run_data


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="JARVIS Test Suite V3 — Comprehensive conversation testing"
    )
    parser.add_argument('--ids', help="Run specific conversations (comma-separated, e.g., V06,M01)")
    parser.add_argument('--category', help="Run conversations by category (e.g., routing)")
    parser.add_argument('--tag', help="Run conversations by tag (e.g., tool:web_search)")
    parser.add_argument('--list', action='store_true', help="List all conversations and exit")
    parser.add_argument('--verbose', action='store_true', default=True,
                        help="Show full responses (default: true)")
    parser.add_argument('--brief', action='store_true',
                        help="Show only routing info, not full responses")
    parser.add_argument('--delay', type=float, default=2.0,
                        help="Seconds between turns (default: 2.0)")
    parser.add_argument('--save', help="Output directory (e.g., tests/v3_results/run_001)")
    parser.add_argument('--no-cleanup', action='store_true',
                        help="Skip artifact cleanup (debugging)")
    parser.add_argument('--auto-clean', action='store_true',
                        help="Auto-clean any leaks at end of run")
    parser.add_argument('--compare', nargs=2, metavar=('RUN_A', 'RUN_B'),
                        help="Compare two run directories")

    args = parser.parse_args()
    verbose = not args.brief

    # --compare mode
    if args.compare:
        report = compare_runs(args.compare[0], args.compare[1])
        print(report)
        return

    all_convs = get_conversations()

    # --list
    if args.list:
        categories = {}
        for c in all_convs:
            if c.category not in categories:
                categories[c.category] = []
            categories[c.category].append(c)

        total_turns = sum(len(c.turns) for c in all_convs)
        print(f"\nJARVIS Test Suite V3: {len(all_convs)} conversations, "
              f"{total_turns} total turns\n")
        for cat, convs in categories.items():
            turns = sum(len(c.turns) for c in convs)
            print(f"  {cat} ({len(convs)} convs, {turns} turns):")
            for c in convs:
                tag_str = f" [{', '.join(c.tags)}]" if c.tags else ""
                print(f"    {c.id}: {c.name} ({len(c.turns)} turns){tag_str}")
            print()
        return

    # Filter conversations
    if args.ids:
        requested = {x.strip().upper() for x in args.ids.split(',')}
        convs = [c for c in all_convs if c.id.upper() in requested]
        found = {c.id.upper() for c in convs}
        missing = requested - found
        if missing:
            print(f"Unknown conversation IDs: {', '.join(sorted(missing))}")
            print(f"Valid IDs: {', '.join(c.id for c in all_convs)}")
            return
    elif args.category:
        convs = [c for c in all_convs
                  if args.category.lower() in c.category.lower()]
        if not convs:
            cats = sorted(set(c.category for c in all_convs))
            print(f"No conversations match category: {args.category}")
            print(f"Valid categories: {', '.join(cats)}")
            return
    elif args.tag:
        convs = [c for c in all_convs
                  if c.tags and args.tag in c.tags]
        if not convs:
            all_tags = sorted(set(t for c in all_convs if c.tags for t in c.tags))
            print(f"No conversations match tag: {args.tag}")
            print(f"Valid tags: {', '.join(all_tags)}")
            return
    else:
        convs = all_convs

    # Require --save for actual runs
    if not args.save:
        print("Error: --save is required (e.g., --save tests/v3_results/run_001)")
        print("V3 always writes results.json + log.jsonl + summary.txt")
        return

    total_turns = sum(len(c.turns) for c in convs)
    print(f"\nJARVIS Test Suite V3")
    print(f"Conversations: {len(convs)} | Turns: {total_turns} | Delay: {args.delay}s")
    print(f"Output: {args.save}")
    print(f"{'='*70}")

    # Load config and run
    try:
        config = load_config()
    except Exception as e:
        print(f"Failed to load config: {e}")
        return

    run_data = asyncio.run(run_suite(
        convs, config,
        delay=args.delay,
        verbose=verbose,
        output_dir=args.save,
        no_cleanup=args.no_cleanup,
        auto_clean=args.auto_clean,
    ))

    # Print final summary
    print(f"\n{'='*70}")
    p = run_data["conversations_pass"]
    m = run_data["conversations_mixed"]
    f = run_data["conversations_fail"]
    total = run_data["conversations_total"]
    print(f"Results: {_color('PASS') if p else '0'} {p}/{total} PASS, "
          f"{_color('MIXED') if m else '0'} {m}/{total} MIXED, "
          f"{_color('FAIL') if f else '0'} {f}/{total} FAIL")
    print(f"Assertions: {run_data['assertions_passed']}/{run_data['assertions_total']} passed")
    print(f"Duration: {run_data['total_duration_ms'] / 1000:.1f}s")


if __name__ == '__main__':
    main()
