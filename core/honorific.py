"""
Honorific Context

Thread-safe module for managing the current speaker's preferred honorific.
Used throughout JARVIS to address users appropriately (e.g., "sir", "ma'am").

Default is "sir" (<primary_user>) until speaker identification sets it otherwise.
"""

import threading

_lock = threading.Lock()
_current_honorific = "sir"


def get_honorific() -> str:
    """Get the current speaker's honorific (thread-safe)."""
    with _lock:
        return _current_honorific


def set_honorific(honorific: str):
    """Set the current speaker's honorific (thread-safe)."""
    global _current_honorific
    with _lock:
        _current_honorific = honorific


def resolve_honorific(text: str) -> str:
    """Replace {honorific} placeholders in text with the current honorific."""
    if "{honorific}" in text:
        return text.replace("{honorific}", get_honorific())
    return text
