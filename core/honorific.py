"""
Honorific Context

Thread-safe module for managing the current speaker's preferred honorific.
Used throughout JARVIS to address users appropriately (e.g., "sir", "ma'am").

Default is "sir" (<primary_user>) until speaker identification sets it otherwise.
"""

import threading

_lock = threading.Lock()
_current_honorific = "sir"
_current_formal_address = None  # e.g., "Ms. Guest" — richer form for LLM prompts


def get_honorific() -> str:
    """Get the current speaker's honorific (thread-safe)."""
    with _lock:
        return _current_honorific


def get_formal_address():
    """Get the formal address (e.g., 'Ms. Guest') or None if not set."""
    with _lock:
        return _current_formal_address


def set_honorific(honorific: str, formal_address: str = None):
    """Set the current speaker's honorific and optional formal address."""
    global _current_honorific, _current_formal_address
    with _lock:
        _current_honorific = honorific
        _current_formal_address = formal_address


def resolve_honorific(text: str) -> str:
    """Replace {honorific} placeholders in text with the current honorific."""
    if "{honorific}" in text:
        return text.replace("{honorific}", get_honorific())
    return text
