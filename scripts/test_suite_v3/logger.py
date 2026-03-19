"""
Structured JSONL logger for JARVIS Test Suite V3.

Writes one JSONL line per turn with complete TurnLog + grading results.
Also writes per-conversation summaries and per-run summaries.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import TextIO

from .client import TurnLog
from .grader import AssertionResult


class V3Logger:
    """Writes structured JSONL logs for a test run."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self._log_path = os.path.join(output_dir, "log.jsonl")
        self._fh: TextIO | None = None

    def open(self):
        """Open the JSONL log file for writing."""
        os.makedirs(self.output_dir, exist_ok=True)
        self._fh = open(self._log_path, 'w')

    def close(self):
        """Close the log file."""
        if self._fh:
            self._fh.close()
            self._fh = None

    def _write(self, entry: dict):
        """Write a single JSON line."""
        if self._fh:
            self._fh.write(json.dumps(entry, default=str) + '\n')
            self._fh.flush()

    def log_turn(self, turn_log: TurnLog, assertion_results: list[AssertionResult],
                 grade: str):
        """Log a single turn with full data + grading."""
        entry = {
            "type": "turn",
            "conversation_id": turn_log.conversation_id,
            "turn_num": turn_log.turn_num,
            "user_input": turn_log.user_input,
            "user_id": turn_log.user_id,
            "response_text": turn_log.response_text,
            "routing_layer": turn_log.routing_layer,
            "skill_name": turn_log.skill_name,
            "handler": turn_log.handler,
            "confidence": turn_log.confidence,
            "tools_called": turn_log.tools_called,
            "info_messages": turn_log.info_messages,
            "llm_model": turn_log.llm_model,
            "llm_tokens": turn_log.llm_tokens,
            "input_tokens": turn_log.input_tokens,
            "synthesis_category": turn_log.synthesis_category,
            "synthesis_temperature": turn_log.synthesis_temperature,
            "total_ms": turn_log.total_ms,
            "llm_calls": turn_log.llm_calls,
            "llm_provider": turn_log.llm_provider,
            "llm_routing_model": turn_log.llm_routing_model,
            "routing_ttft_ms": turn_log.routing_ttft_ms,
            "synthesis_ttft_ms": turn_log.synthesis_ttft_ms,
            "word_count": turn_log.word_count,
            "raw_stats": turn_log.raw_stats,
            "assertions": [r.to_dict() for r in assertion_results],
            "grade": grade,
            "timestamp": turn_log.timestamp_received or datetime.now(timezone.utc).isoformat(),
        }
        self._write(entry)

    def log_conversation_summary(self, conversation_id: str, name: str, category: str,
                                  turn_count: int, grade: str, turn_grades: list[str],
                                  assertions_total: int, assertions_passed: int,
                                  assertions_failed: int, duration_ms: int,
                                  cleanup_actions: list[str], tags: list[str] | None = None):
        """Log a per-conversation summary entry."""
        entry = {
            "type": "conversation_summary",
            "conversation_id": conversation_id,
            "name": name,
            "category": category,
            "turn_count": turn_count,
            "grade": grade,
            "turn_grades": turn_grades,
            "assertions_total": assertions_total,
            "assertions_passed": assertions_passed,
            "assertions_failed": assertions_failed,
            "duration_ms": duration_ms,
            "cleanup_actions": cleanup_actions,
        }
        if tags:
            entry["tags"] = tags
        self._write(entry)

    def write_results(self, run_data: dict):
        """Write the run-level results.json file."""
        results_path = os.path.join(self.output_dir, "results.json")
        with open(results_path, 'w') as f:
            json.dump(run_data, f, indent=2, default=str)
