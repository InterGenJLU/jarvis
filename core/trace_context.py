"""Trace Context — thread-local propagation for request tracing.

Generates a trace_id and session_id at the start of each request
and makes them available to all emit() calls within that thread.
This is how we link STT → routing → LLM → TTS into one trace.

Usage:
    from core.trace_context import trace_ctx

    # At request entry point:
    trace_ctx.start(session_id="ws_abc123", speaker_id="primary_user")

    # During the request (any thread-local code):
    trace_ctx.trace_id    # auto-generated UUID
    trace_ctx.session_id  # from start()
    trace_ctx.speaker_id  # from start()

    # At request completion:
    trace_ctx.clear()
"""

import threading
import uuid

_local = threading.local()


class _TraceContext:
    """Thread-local trace context for request-scoped event linking."""

    def start(self, *, session_id: str = None, speaker_id: str = None,
              trace_id: str = None):
        """Begin a new trace for this request."""
        _local.trace_id = trace_id or str(uuid.uuid4())
        _local.session_id = session_id
        _local.speaker_id = speaker_id
        _local.parent_id = None

    def clear(self):
        """Clear trace context at end of request."""
        _local.trace_id = None
        _local.session_id = None
        _local.speaker_id = None
        _local.parent_id = None

    @property
    def trace_id(self) -> str | None:
        return getattr(_local, 'trace_id', None)

    @property
    def session_id(self) -> str | None:
        return getattr(_local, 'session_id', None)

    @property
    def speaker_id(self) -> str | None:
        return getattr(_local, 'speaker_id', None)

    @property
    def parent_id(self) -> str | None:
        return getattr(_local, 'parent_id', None)

    @parent_id.setter
    def parent_id(self, value: str | None):
        _local.parent_id = value

    @property
    def active(self) -> bool:
        return getattr(_local, 'trace_id', None) is not None


trace_ctx = _TraceContext()
