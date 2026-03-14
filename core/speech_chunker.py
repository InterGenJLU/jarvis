"""
Speech Chunker

Accumulates streamed LLM tokens into speakable sentence chunks.
Used by the streaming LLM-to-TTS pipeline (Phase 3 latency refactor).

Emits a chunk when:
  1. Buffer contains a sentence boundary (. ? ! followed by space/newline)
  2. Stream ends (flush remaining buffer)
"""

import re
from typing import Optional


class SpeechChunker:
    """Accumulate streamed tokens into speakable sentence chunks."""

    # Sentence-ending punctuation followed by whitespace.
    # Do NOT match end-of-string ($) — that caused premature splits when
    # a decimal period (e.g. "$115.") was at the buffer boundary.
    # flush() handles the end-of-stream case instead.
    _SENTENCE_END = re.compile(r'[.!?]\s')

    # Common abbreviations whose trailing period is NOT a sentence boundary.
    # Lowercase, without the trailing dot.
    _ABBREVIATIONS = frozenset({
        'dr', 'mr', 'mrs', 'ms', 'jr', 'sr', 'st', 'vs', 'etc', 'prof',
        'gen', 'gov', 'sgt', 'cpl', 'pvt', 'lt', 'col', 'capt', 'cmdr',
        'adm', 'rev', 'approx', 'dept', 'est', 'inc', 'corp', 'ave',
    })

    def __init__(self):
        self._buffer = ""

    def _is_abbreviation(self, text_before_dot: str) -> bool:
        """Check if the word immediately before the dot is an abbreviation."""
        word = text_before_dot.rsplit(None, 1)[-1] if text_before_dot.strip() else ""
        return word.lower().rstrip('.') in self._ABBREVIATIONS

    def feed(self, token: str) -> Optional[str]:
        """Feed a token. Returns a speakable chunk if one is ready, else None."""
        self._buffer += token

        # Find the first real sentence boundary (skip abbreviation periods)
        for match in self._SENTENCE_END.finditer(self._buffer):
            if self._buffer[match.start()] == '.' and self._is_abbreviation(self._buffer[:match.start()]):
                continue
            split_pos = match.end()
            chunk = self._buffer[:split_pos].strip()
            self._buffer = self._buffer[split_pos:]
            if chunk:
                return chunk

        return None

    def flush(self) -> Optional[str]:
        """Flush any remaining buffered text."""
        chunk = self._buffer.strip()
        self._buffer = ""
        return chunk if chunk else None
