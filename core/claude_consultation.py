"""Claude Consultation Module — Self-Evolution Phase 2.

Sends observation collector findings to Claude for analysis and
receives structured proposals that flow through the governance
approval system. Claude never sees raw user data — only aggregated
findings and system metrics.

Architecture:
  ObservationCollector → findings → this module → Claude API
  Claude API → analysis + proposals → Governance.propose()
  Owner reviews proposals in dashboard → approves via console

The consultation prompt is a fixed template. The LLM that generates
proposals does NOT control the approval prompt the owner sees.
(Research: anti-manipulation pattern — structured schema in,
template-rendered presentation out.)
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)

_instance: Optional["ClaudeConsultation"] = None


def get_claude_consultation(config=None):
    """Get or create the singleton ClaudeConsultation."""
    global _instance
    if _instance is None and config is not None:
        _instance = ClaudeConsultation(config)
    return _instance


# -----------------------------------------------------------------------
# Consultation system prompt — the "you automagically know what to do"
# -----------------------------------------------------------------------

CONSULTATION_SYSTEM_PROMPT = """You are the analytical layer for JARVIS, a self-governing AI voice assistant. You receive operational findings from JARVIS's observation collector and produce structured analysis with actionable proposals.

## Who JARVIS Is
JARVIS is a GPU-accelerated personal voice assistant: Whisper STT + Qwen 35B local LLM + Claude API fallback + Kokoro TTS. He serves a household with voice interaction, web search, news briefings, reminders, and more. He is charismatic, respectful, and genuinely helpful.

## Your Role
You are a consultant, not a decision-maker. You analyze findings, identify root causes, and propose specific fixes. Every proposal goes through a governance approval system where the owner reviews and decides. You never execute changes directly.

## Constitutional Constraints
JARVIS operates under Ten Commandments. Your proposals must respect them:
- Serve the household (Commandment I) — every proposal must improve the user experience
- Never deceive (II) — if you're uncertain, say so
- Fail safely (IV) — propose changes that can be rolled back
- Respect authority tiers (V) — Tier 0-1 changes only unless explicitly escalated
- Maintain audit trail (VI) — every proposal is logged
- Roll back before rolling forward (VII) — include a rollback plan
- Never exceed the pace of trust (VIII) — small, incremental changes
- Personality is constitutional (IX) — never modify personality traits

## Response Format
Return a JSON object with this exact structure:
```json
{
  "summary": "1-2 sentence overview of what you found",
  "findings_analyzed": <number>,
  "proposals": [
    {
      "id": "proposal_001",
      "title": "Short descriptive title",
      "description": "What this change does and why",
      "category": "config|prompt|threshold|code|investigation",
      "risk_tier": 1,
      "priority": "high|medium|low",
      "change": {
        "file": "path/to/file.py",
        "type": "edit|config|threshold",
        "current_value": "what it is now (if applicable)",
        "proposed_value": "what it should be",
        "diff": "unified diff if code change"
      },
      "rollback_plan": "How to undo this if it doesn't work",
      "evidence": "Which findings support this proposal",
      "expected_impact": "What should improve and by how much"
    }
  ],
  "deferred": [
    {
      "title": "Things that need more investigation",
      "reason": "Why this can't be proposed yet"
    }
  ],
  "confidence": 0.0-1.0
}
```

## Rules
- Never propose Tier 3-4 changes (core logic, architecture) — flag them as deferred
- Prefer config/threshold changes over code changes
- Prefer prompt adjustments over code changes
- One proposal per finding — don't bundle unrelated changes
- Include specific file paths and line numbers when possible
- If a finding is ambiguous, defer it rather than guessing
- Be conservative — a missed improvement is better than a broken system
"""


# -----------------------------------------------------------------------
# ClaudeConsultation
# -----------------------------------------------------------------------

class ClaudeConsultation:
    """Sends findings to Claude API and receives structured proposals."""

    def __init__(self, config):
        self.config = config
        self.api_key_env = config.get("llm.api.api_key_env")
        self.model = config.get(
            "consultation.model",
            "claude-opus-4-20250514",
        )
        self.max_tokens = config.get("consultation.max_tokens", 4096)
        self._history: list[dict] = []
        logger.info(
            "ClaudeConsultation initialized: model=%s, max_tokens=%d",
            self.model, self.max_tokens,
        )

    def consult(self, findings: list, context: dict = None) -> dict:
        """Send findings to Claude and return structured analysis.

        Args:
            findings: list of Finding objects from observation collector
            context: optional dict with system state (health metrics, etc.)

        Returns:
            Parsed JSON response from Claude, or error dict
        """
        if not findings:
            return {"summary": "No findings to analyze", "proposals": []}

        # Build the consultation message
        message = self._build_message(findings, context)

        # Call Claude API
        t0 = time.time()
        try:
            response = self._call_claude(message)
            latency_ms = (time.time() - t0) * 1000
        except Exception as e:
            logger.error("Claude consultation failed: %s", e)
            return {
                "summary": f"Consultation failed: {e}",
                "proposals": [],
                "error": str(e),
            }

        # Parse response
        try:
            result = self._parse_response(response)
        except Exception as e:
            logger.error("Failed to parse consultation response: %s", e)
            result = {
                "summary": "Response parsing failed",
                "proposals": [],
                "raw_response": response[:2000],
                "error": str(e),
            }

        # Record in history
        self._history.append({
            "timestamp": time.time(),
            "findings_count": len(findings),
            "proposals_count": len(result.get("proposals", [])),
            "latency_ms": latency_ms,
            "model": self.model,
            "summary": result.get("summary", ""),
        })
        if len(self._history) > 50:
            self._history = self._history[-50:]

        # Log consultation event
        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    event="claude_consultation",
                    category="self_assessment",
                    status="success" if "error" not in result else "error",
                    message=f"Consultation: {len(findings)} findings → "
                            f"{len(result.get('proposals', []))} proposals",
                    latency_ms=latency_ms,
                    metadata={
                        "model": self.model,
                        "findings_count": len(findings),
                        "proposals_count": len(result.get("proposals", [])),
                        "confidence": result.get("confidence"),
                    },
                )
        except Exception:
            pass

        logger.info(
            "Consultation complete: %d findings → %d proposals (%.0fms, %s)",
            len(findings), len(result.get("proposals", [])),
            latency_ms, self.model,
        )

        return result

    def _build_message(self, findings: list, context: dict = None) -> str:
        """Build the user message from findings and optional context."""
        parts = []
        parts.append(f"## Observation Report — {time.strftime('%Y-%m-%d %H:%M')}")
        parts.append(f"\n{len(findings)} findings from automated observation:\n")

        for i, f in enumerate(findings, 1):
            parts.append(f"### Finding {i}: [{f.severity.upper()}] {f.title}")
            parts.append(f"**Detector:** {f.detector}")
            parts.append(f"**Category:** {f.category}")
            parts.append(f"**Count:** {f.count}")
            parts.append(f"**Detail:** {f.detail}")
            if f.suggested_action:
                parts.append(f"**Detector suggestion:** {f.suggested_action}")
            parts.append("")

        if context:
            parts.append("## System Context")
            for key, value in context.items():
                parts.append(f"- **{key}:** {value}")
            parts.append("")

        return "\n".join(parts)

    def _call_claude(self, message: str) -> str:
        """Call the Claude API and return the response text."""
        import anthropic

        api_key = self.config.get_env(self.api_key_env)
        if not api_key or api_key == "your_key_here":
            raise ValueError("Claude API key not configured")

        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=CONSULTATION_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": message}
            ],
        )

        # Record token usage
        try:
            from core.event_logger import get_event_logger
            el = get_event_logger()
            if el:
                el.emit(
                    event="llm_call",
                    category="inference",
                    stage="claude_consultation",
                    status="success",
                    message=f"Claude consultation: {response.usage.input_tokens}in/{response.usage.output_tokens}out",
                    latency_ms=None,  # captured by caller
                    model=self.model,
                    metadata={
                        "provider": "claude",
                        "method": "consultation",
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                )
        except Exception:
            pass

        return response.content[0].text

    def _parse_response(self, response: str) -> dict:
        """Parse Claude's JSON response, handling markdown fencing."""
        text = response.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        return json.loads(text)

    def submit_proposals(self, result: dict) -> list[str]:
        """Submit consultation proposals to the governance system.

        Returns list of proposal IDs.
        """
        from core.governance import get_governance
        gov = get_governance()
        if not gov:
            logger.warning("Cannot submit proposals — governance not initialized")
            return []

        proposal_ids = []
        for p in result.get("proposals", []):
            try:
                pid = gov.propose(
                    p.get("category", "investigation"),
                    description=p.get("description", p.get("title", "Untitled")),
                    diff=p.get("change", {}).get("diff", ""),
                    justification=p.get("evidence", ""),
                    rollback_plan=p.get("rollback_plan", "Revert the change"),
                    risk_tier=p.get("risk_tier", 2),
                )
                proposal_ids.append(pid)
                logger.info(
                    "Submitted proposal %s: %s (tier %d)",
                    pid, p.get("title", "?"), p.get("risk_tier", 2),
                )
            except Exception as e:
                logger.error("Failed to submit proposal '%s': %s",
                             p.get("title", "?"), e)

        return proposal_ids

    def get_history(self, limit: int = 10) -> list[dict]:
        """Return recent consultation history."""
        return self._history[-limit:]

    def consult_and_propose(self, findings: list, context: dict = None) -> dict:
        """Full cycle: consult Claude, then submit proposals to governance.

        This is the main entry point for automated consultation cycles.
        """
        result = self.consult(findings, context)

        if result.get("error"):
            return result

        proposals = result.get("proposals", [])
        if proposals:
            proposal_ids = self.submit_proposals(result)
            result["submitted_proposal_ids"] = proposal_ids
            logger.info(
                "Consultation cycle complete: %d proposals submitted to governance",
                len(proposal_ids),
            )
        else:
            logger.info("Consultation cycle complete: no proposals to submit")

        return result
