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

    def __init__(self):
        self._buffer = ""

    def feed(self, token: str) -> Optional[str]:
        """Feed a token. Returns a speakable chunk if one is ready, else None."""
        self._buffer += token

        match = self._SENTENCE_END.search(self._buffer)
        if match:
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
