"""Tool gate — decides whether a query needs tool schemas injected.

Two-layer decision:
  1. Keyword override: explicit action words → always include tools
  2. Binary classifier: logistic regression on nomic embeddings → P(tool)

If the classifier is unavailable (model missing, load error), defaults to
including tools — never degrades the experience.

Usage:
    from core.tool_gate import should_include_tools
    if should_include_tools(query, embedding):
        # include tool schemas
    else:
        # skip tools, save ~1,500 tokens
"""

import logging
import re
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger("jarvis.tool_gate")

# ---------------------------------------------------------------------------
# Layer 1: Keyword override — explicit tool requests always get tools
# ---------------------------------------------------------------------------
# These patterns indicate the user is directly requesting an action that
# requires a tool.  No classifier needed — the intent is unambiguous.

_TOOL_KEYWORDS = [
    # Web search
    r"\bsearch\s+for\b",
    r"\blook\s+up\b",
    r"\bgoogle\b",
    r"\bfind\s+me\b",

    # News
    r"\bwhat'?s\s+happening\s+in\b",
    r"\bheadlines\b",
    r"\bnews\b",
    r"\bwhat'?s\s+trending\b",

    # Reminders
    r"\bremind\s+me\b",
    r"\bset\s+a\s+reminder\b",
    r"\bcancel\s+(my\s+)?reminder",
    r"\bsnooze\b",
    r"\bmy\s+reminders\b",
    r"\bmy\s+schedule\b",

    # Screenshots / webcam
    r"\bscreenshot\b",
    r"\bcapture\b",
    r"\bwebcam\b",
    r"\bwhat'?s\s+on\s+my\s+screen\b",
    r"\blook\s+at\s+me\b",
    r"\bwhat\s+do\s+you\s+see\b",

    # Apps / system control
    r"\b(open|launch|start|close|quit|kill)\s+\w+",
    r"\bvolume\b",
    r"\bmute\b",
    r"\bunmute\b",
    r"\bfullscreen\b",
    r"\bminimize\b",
    r"\bmaximize\b",
    r"\bworkspace\b",

    # Files / system
    r"\bfind\s+(my\s+)?files?\b",
    r"\bdisk\s+space\b",
    r"\bCPU\s+usage\b",
    r"\bRAM\b",
    r"\bGPU\s+(temp|usage|memory)\b",
    r"\bsystem\s+uptime\b",
    r"\bgit\s+(status|log|diff|branch)\b",
    r"\bservice\s+(status|running)\b",
    r"\bcodebase\b",

    # Weather (explicit)
    r"\bweather\b",
    r"\bforecast\b",
    r"\bsunrise\b",
    r"\bsunset\b",
    r"\brain\s+tomorrow\b",

    # Time (explicit)
    r"\bwhat\s+time\s+is\s+it\b",
    r"\bwhat'?s\s+today'?s?\s+date\b",
    r"\bwhat\s+day\s+(is\s+it|of\s+the\s+week)\b",

    # Memory recall (explicit)
    r"\bwhat\s+do\s+you\s+(remember|know)\s+about\b",
    r"\brecall\b",
    r"\bdo\s+you\s+remember\b",
]

_TOOL_KEYWORD_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _TOOL_KEYWORDS]

# ---------------------------------------------------------------------------
# Layer 2: Binary classifier — logistic regression on embeddings
# ---------------------------------------------------------------------------

_MODEL_PATH = Path("/mnt/storage/jarvis/models/tool_classifier/tool_classifier_v1.joblib")
_classifier = None
_classifier_loaded = False
_CONFIDENCE_THRESHOLD = 0.10  # Skip tools only when P(tool) < this


def _load_classifier():
    """Lazy-load the classifier model."""
    global _classifier, _classifier_loaded
    if _classifier_loaded:
        return _classifier

    _classifier_loaded = True
    try:
        import joblib
        if _MODEL_PATH.exists():
            _classifier = joblib.load(_MODEL_PATH)
            logger.info("Tool classifier loaded: %s", _MODEL_PATH)
        else:
            logger.warning("Tool classifier model not found: %s — tools always included", _MODEL_PATH)
    except Exception as e:
        logger.error("Tool classifier load failed: %s — tools always included", e)
        _classifier = None

    return _classifier


def should_include_tools(query: str, embedding=None) -> bool:
    """Decide whether tool schemas should be injected for this query.

    Args:
        query: The user's command text.
        embedding: Pre-computed nomic embedding (numpy array or tensor).
                   If None, skips classifier (keyword-only check).

    Returns:
        True if tools should be included, False if they can be skipped.
    """
    # Layer 1: Keyword override — explicit action words always get tools
    for pattern in _TOOL_KEYWORD_PATTERNS:
        if pattern.search(query):
            logger.debug("Tool gate: keyword match '%s' — tools included", pattern.pattern)
            return True

    # Layer 2: Classifier — skip tools only when highly confident it's non-tool
    if embedding is not None:
        clf = _load_classifier()
        if clf is not None:
            try:
                import numpy as np
                # Ensure embedding is 2D numpy array
                if hasattr(embedding, 'cpu'):
                    emb = embedding.cpu().numpy()
                elif not isinstance(embedding, np.ndarray):
                    emb = np.array(embedding)
                else:
                    emb = embedding

                if emb.ndim == 1:
                    emb = emb.reshape(1, -1)

                prob_tool = clf.predict_proba(emb)[0][1]

                if prob_tool < _CONFIDENCE_THRESHOLD:
                    logger.info(
                        "Tool gate: classifier P(tool)=%.3f < %.2f — tools SKIPPED for: %.80s",
                        prob_tool, _CONFIDENCE_THRESHOLD, query,
                    )
                    return False
                else:
                    logger.debug(
                        "Tool gate: classifier P(tool)=%.3f >= %.2f — tools included",
                        prob_tool, _CONFIDENCE_THRESHOLD,
                    )
                    return True
            except Exception as e:
                logger.error("Tool gate: classifier error: %s — tools included (safe default)", e)
                return True

    # Default: include tools (safe fallback)
    logger.debug("Tool gate: no classifier/embedding — tools included (default)")
    return True
