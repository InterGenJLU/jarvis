"""
Assertion engine for JARVIS Test Suite V3.

Every turn has a list of assertions. Each assertion is pass/fail.
Turn grade is computed from the full set:
  ALL pass → PASS
  ALL fail → FAIL
  Mixed    → MIXED

Honorifics are assertions like everything else — no separate track.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import TurnLog


# ── Filler phrases ────────────────────────────────────────────────────────

FILLER_ENDINGS = [
    "feel free to ask",
    "let me know",
    "if you have any questions",
    "if you need anything else",
    "don't hesitate to ask",
    "happy to help",
]

FILLER_OPENINGS = [
    "certainly",
    "of course",
    "absolutely",
    "sure thing",
    "great question",
]


# ── Assertion dataclass ──────────────────────────────────────────────────

@dataclass
class Assertion:
    """A single pass/fail check on a turn's response."""
    type: str           # e.g. "contains", "routes_to_skill", "has_honorific"
    expected: str       # Value to check against
    description: str    # Human-readable description
    passed: bool = True
    detail: str = ""    # Failure detail


@dataclass
class AssertionResult:
    """Result of evaluating one assertion."""
    name: str
    type: str
    expected: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.type, "expected": self.expected, "passed": self.passed}
        if self.detail:
            d["detail"] = self.detail
        return d


# ── Assertion builder helpers ────────────────────────────────────────────
# These return (type, expected, description) tuples — lightweight and easy
# to use in conversation definitions.

def contains(text: str, desc: str = "") -> tuple[str, str, str]:
    """Response contains text (case-insensitive)."""
    return ("contains", text, desc or f"contains '{text}'")


def not_contains(text: str, desc: str = "") -> tuple[str, str, str]:
    """Response does NOT contain text."""
    return ("not_contains", text, desc or f"lacks '{text}'")


def any_of(*texts: str, desc: str = "") -> tuple[str, str, str]:
    """Response contains at least one of the given texts (case-insensitive)."""
    return ("any_of", "|".join(texts), desc or f"contains one of: {', '.join(texts)}")


def min_words(n: int, desc: str = "") -> tuple[str, str, str]:
    """Response has >= n words."""
    return ("min_words", str(n), desc or f"≥{n} words")


def max_words(n: int, desc: str = "") -> tuple[str, str, str]:
    """Response has <= n words."""
    return ("max_words", str(n), desc or f"≤{n} words")


def is_empty(desc: str = "") -> tuple[str, str, str]:
    """Response is empty (bare ack test)."""
    return ("is_empty", "", desc or "empty response")


def routes_to_skill(name: str, desc: str = "") -> tuple[str, str, str]:
    """skill_name matches."""
    return ("routes_to_skill", name, desc or f"skill:{name}")


def routes_to_layer(name: str, desc: str = "") -> tuple[str, str, str]:
    """routing_layer matches."""
    return ("routes_to_layer", name, desc or f"layer:{name}")


def uses_tool(name: str, desc: str = "") -> tuple[str, str, str]:
    """Tool appears in tools_called."""
    return ("uses_tool", name, desc or f"uses {name}")


def no_tool(name: str, desc: str = "") -> tuple[str, str, str]:
    """Tool does NOT appear in tools_called."""
    return ("no_tool", name, desc or f"avoids {name}")


def routes_to_llm(desc: str = "") -> tuple[str, str, str]:
    """routing_layer is P4-LLM or Fallback."""
    return ("routes_to_llm", "", desc or "routes to LLM")


def has_honorific(text: str, desc: str = "") -> tuple[str, str, str]:
    """Honorific present in response (case-insensitive)."""
    return ("has_honorific", text, desc or f"'{text}' honorific")


def no_filler_ending(desc: str = "") -> tuple[str, str, str]:
    """No filler ending phrase."""
    return ("no_filler_ending", "", desc or "no filler ending")


def no_filler_opening(desc: str = "") -> tuple[str, str, str]:
    """No filler opening phrase."""
    return ("no_filler_opening", "", desc or "no filler opening")


def has_disclaimer(domain: str, desc: str = "") -> tuple[str, str, str]:
    """Domain-specific disclaimer present."""
    return ("has_disclaimer", domain, desc or f"{domain} disclaimer")


# ── Grading engine ───────────────────────────────────────────────────────

def _extract_tools(info_messages: list[str]) -> list[str]:
    """Extract tool names from info messages (same logic as V2)."""
    tools = []
    for info in info_messages:
        if info.startswith("Searching:"):
            tools.append("web_search")
        elif info.startswith("Running:"):
            tools.append(info.replace("Running:", "").strip())
    return tools


# Disclaimer keywords by domain
_DISCLAIMER_KEYWORDS = {
    "medical": ["doctor", "physician", "healthcare", "medical professional", "professional medical",
                "consult", "not a substitute", "qualified"],
    "legal": ["attorney", "lawyer", "legal professional", "legal advice", "consult",
              "not a substitute", "qualified"],
    "financial": ["financial advisor", "financial professional", "financial advice",
                  "consult", "not a substitute", "qualified"],
}


def grade_turn(turn_log: TurnLog, assertions: list[tuple[str, str, str]],
               user_id: str = "primary_user",
               skip_honorific: bool = False,
               skip_filler: bool = False,
               skip_non_empty: bool = False,
               is_greeting: bool = False,
               is_farewell: bool = False) -> list[AssertionResult]:
    """
    Evaluate assertions against a TurnLog. Returns list of AssertionResult.

    Auto-assertions are added unless skipped:
      - no_filler_ending / no_filler_opening
      - has_honorific("sir") for the user
      - has_honorific("ma'am") for secondary user mid-conversation
      - has_honorific("Ms. Guest") for secondary user greeting/farewell
      - non-empty response
    """
    effective = list(assertions)

    # Determine explicit assertion types present
    explicit_types = {a[0] for a in assertions}

    # Auto: non-empty (unless skipped or explicit is_empty)
    if not skip_non_empty and "is_empty" not in explicit_types:
        effective.append(("non_empty", "", "non-empty response"))

    # Auto: honorific
    if not skip_honorific and "is_empty" not in explicit_types:
        if user_id in (None, "primary_user"):
            effective.append(("has_honorific", "sir", "'sir' honorific"))
        elif user_id == "secondary_user":
            # Greeting/farewell → Ms. Guest; mid-conversation → mum
            if is_greeting or is_farewell:
                effective.append(("has_honorific", "ms. guest", "'Ms. Guest' honorific"))
            else:
                effective.append(("has_honorific", "ma'am", "'mum' honorific"))

    # Auto: no filler (unless skipped or explicit is_empty)
    if not skip_filler and "is_empty" not in explicit_types:
        effective.append(("no_filler_ending", "", "no filler ending"))
        effective.append(("no_filler_opening", "", "no filler opening"))

    # Evaluate
    response_lower = turn_log.response_text.lower().strip()
    tools = _extract_tools(turn_log.info_messages)
    results = []

    for atype, avalue, adesc in effective:
        passed = True
        detail = ""

        if atype == "contains":
            if avalue.lower() not in response_lower:
                passed = False
                detail = "not found in response"

        elif atype == "not_contains":
            if avalue.lower() in response_lower:
                passed = False
                detail = "found in response"

        elif atype == "any_of":
            alternatives = avalue.split("|")
            if not any(alt.lower() in response_lower for alt in alternatives):
                passed = False
                detail = "none found in response"

        elif atype == "min_words":
            threshold = int(avalue)
            if turn_log.word_count < threshold:
                passed = False
                detail = f"got {turn_log.word_count} words"

        elif atype == "max_words":
            threshold = int(avalue)
            if turn_log.word_count > threshold:
                passed = False
                detail = f"got {turn_log.word_count} words"

        elif atype == "is_empty":
            if turn_log.word_count > 0:
                passed = False
                detail = f"got {turn_log.word_count} words"

        elif atype == "non_empty":
            if turn_log.word_count == 0:
                passed = False
                detail = "response is empty"

        elif atype == "routes_to_skill":
            if turn_log.skill_name != avalue:
                passed = False
                actual = turn_log.skill_name or turn_log.llm_model or "unknown"
                detail = f"got {actual}"

        elif atype == "routes_to_layer":
            if turn_log.routing_layer != avalue:
                passed = False
                detail = f"got {turn_log.routing_layer}"

        elif atype == "uses_tool":
            if avalue not in tools:
                passed = False
                detail = f"tools: {tools or 'none'}"

        elif atype == "no_tool":
            if avalue in tools:
                passed = False
                detail = f"{avalue} was called"

        elif atype == "routes_to_llm":
            if turn_log.routing_layer not in ("P4-LLM", "Fallback"):
                passed = False
                detail = f"got {turn_log.routing_layer}"

        elif atype == "has_honorific":
            if avalue.lower() not in response_lower:
                passed = False
                detail = "not found in response"

        elif atype == "no_filler_ending":
            for filler in FILLER_ENDINGS:
                if filler in response_lower:
                    passed = False
                    detail = f"contains '{filler}'"
                    break

        elif atype == "no_filler_opening":
            # Check if response starts with a filler opening
            for filler in FILLER_OPENINGS:
                if response_lower.startswith(filler):
                    passed = False
                    detail = f"starts with '{filler}'"
                    break

        elif atype == "has_disclaimer":
            keywords = _DISCLAIMER_KEYWORDS.get(avalue, [])
            if not any(kw in response_lower for kw in keywords):
                passed = False
                detail = f"no {avalue} disclaimer keywords found"

        results.append(AssertionResult(
            name=adesc,
            type=atype,
            expected=avalue,
            passed=passed,
            detail=detail,
        ))

    return results


def compute_turn_grade(results: list[AssertionResult]) -> str:
    """PASS if all pass, FAIL if all fail, MIXED otherwise."""
    if not results:
        return "PASS"
    all_pass = all(r.passed for r in results)
    all_fail = all(not r.passed for r in results)
    if all_pass:
        return "PASS"
    if all_fail:
        return "FAIL"
    return "MIXED"


def compute_conversation_grade(turn_grades: list[str]) -> str:
    """PASS if all PASS, FAIL if all FAIL, MIXED otherwise."""
    if not turn_grades:
        return "PASS"
    if all(g == "PASS" for g in turn_grades):
        return "PASS"
    if all(g == "FAIL" for g in turn_grades):
        return "FAIL"
    return "MIXED"
