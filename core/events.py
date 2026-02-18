"""
Event types for the JARVIS event pipeline.

Phase 4 of the latency refactor: all inter-component communication
flows through typed events on queue.Queue channels.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
import time


class EventType(Enum):
    """All event types in the JARVIS pipeline."""

    # Audio / STT pipeline
    TRANSCRIPTION_READY = auto()        # STT produced text from audio
    COMMAND_DETECTED = auto()           # Wake word found or conversation window match

    # Command processing
    SKILL_RESPONSE = auto()             # Skill returned a response
    LLM_COMPLETE = auto()               # LLM streaming finished (full response text)

    # TTS pipeline
    SPEAK_REQUEST = auto()              # Request TTS playback (data: str or dict)
    SPEAK_ACK = auto()                  # Play pre-cached acknowledgment phrase
    SPEECH_STARTED = auto()             # TTS playback has begun
    SPEECH_FINISHED = auto()            # TTS playback has ended

    # Listener control
    PAUSE_LISTENING = auto()            # Pause mic collection
    RESUME_LISTENING = auto()           # Resume mic collection
    OPEN_CONVERSATION_WINDOW = auto()   # Open follow-up window (data: duration float)
    CLOSE_CONVERSATION_WINDOW = auto()  # Close conversation window

    # Background services
    REMINDER_FIRE = auto()              # A reminder is due (data: reminder dict)
    NEWS_ANNOUNCEMENT = auto()          # Urgent news to announce

    # System
    SHUTDOWN = auto()                   # Graceful shutdown requested
    ERROR = auto()                      # Error propagation (data: dict with source, error)


class PipelineState(Enum):
    """State machine for the coordinator's command processing."""

    IDLE = auto()                   # Listening for speech
    PROCESSING_COMMAND = auto()     # Routing command, executing skill
    STREAMING_LLM = auto()         # LLM generating + TTS playing chunks
    SPEAKING = auto()               # TTS playing a non-streaming response


@dataclass
class Event:
    """A typed event flowing through the pipeline."""

    type: EventType
    data: Any = None
    timestamp: float = field(default_factory=time.monotonic)
    source: str = ""

    def __repr__(self):
        data_repr = repr(self.data)
        if len(data_repr) > 80:
            data_repr = data_repr[:77] + "..."
        return f"Event({self.type.name}, data={data_repr}, source={self.source!r})"
