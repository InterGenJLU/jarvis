"""
Honorific Context

Thread-safe module for managing the current speaker's preferred honorific.
Used throughout JARVIS to address users appropriately (e.g., "sir", "ma'am").

Default is "sir" (<primary_user>) until speaker identification sets it otherwise.

Session isolation: when a per-request user_id is set via set_thread_user()
(called by ConversationRouter.route()), get_honorific() and get_formal_address()
resolve from the ProfileManager for that user instead of the module-level global.
Voice/console paths continue using the global via set_honorific()/get_honorific().
"""

import threading

_lock = threading.Lock()
_current_honorific = "sir"
_current_formal_address = None  # e.g., "Ms. Guest" — richer form for LLM prompts

# Per-request thread-local user_id (set by router.route(), cleared in finally)
_thread_ctx = threading.local()

# ProfileManager reference (set once at init via init_profile_manager())
_profile_manager = None


def init_profile_manager(pm):
    """Wire up the ProfileManager for per-user honorific lookups.

    Called once during startup after init_components().
    """
    global _profile_manager
    _profile_manager = pm


def set_thread_user(user_id: str):
    """Set the active user_id for this thread (per-request context)."""
    _thread_ctx.user_id = user_id


def clear_thread_user():
    """Clear the active user_id for this thread."""
    _thread_ctx.user_id = None


def get_honorific() -> str:
    """Get the current speaker's honorific (thread-safe).

    When a thread-local user_id is active (web path), resolves from
    ProfileManager. Otherwise returns the module-level global (voice path).
    """
    uid = getattr(_thread_ctx, 'user_id', None)
    if uid and _profile_manager:
        return _profile_manager.get_honorific_for(uid)
    with _lock:
        return _current_honorific


def get_formal_address():
    """Get the formal address (e.g., 'Ms. Guest') or None if not set.

    When a thread-local user_id is active, resolves from ProfileManager.
    """
    uid = getattr(_thread_ctx, 'user_id', None)
    if uid and _profile_manager:
        return _profile_manager.get_formal_address_for(uid)
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
