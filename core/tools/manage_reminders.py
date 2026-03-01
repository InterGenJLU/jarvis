"""Tool definition: manage_reminders — reminder CRUD operations."""

from datetime import datetime

TOOL_NAME = "manage_reminders"
SKILL_NAME = "reminders"

DEPENDENCIES = {"reminder_manager": "_reminder_manager"}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "manage_reminders",
        "description": (
            "Manage reminders: set new ones, list existing, cancel, "
            "acknowledge, or snooze. Use for ANY request about reminders."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "cancel", "acknowledge", "snooze"],
                    "description": (
                        "add: set a new reminder. "
                        "list: show upcoming reminders. "
                        "cancel: remove a reminder by name. "
                        "acknowledge: mark the last-fired reminder as done. "
                        "snooze: delay the last-fired reminder."
                    )
                },
                "title": {
                    "type": "string",
                    "description": (
                        "What to be reminded about (e.g. 'call mom', "
                        "'take out the trash'). Required for 'add'."
                    )
                },
                "time_text": {
                    "type": "string",
                    "description": (
                        "When to remind, in natural language "
                        "(e.g. 'tomorrow at 6 PM', 'in 30 minutes', "
                        "'next Tuesday'). Required for 'add'."
                    )
                },
                "priority": {
                    "type": "string",
                    "enum": ["urgent", "high", "normal"],
                    "description": (
                        "Importance level. Default: normal. "
                        "Urgent/high require acknowledgment when fired."
                    )
                },
                "snooze_minutes": {
                    "type": "integer",
                    "description": "Minutes to snooze. Default: 15."
                },
                "cancel_fragment": {
                    "type": "string",
                    "description": (
                        "Part of the reminder title to match for cancellation "
                        "(e.g. 'dentist'). Required for 'cancel'."
                    )
                }
            },
            "required": ["action"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For reminder requests (set, list, cancel, snooze, "
    "acknowledge), call manage_reminders. Extract the title and "
    "time from the user's words. "
    "Examples: 'remind me to call Mom at 3pm' → add, 'what reminders do I have?' → list, "
    "'cancel the dentist reminder' → cancel. "
    "NOT for: calendar events, alarms, timers, scheduling meetings."
)


# ---------------------------------------------------------------------------
# Runtime dependency — injected via tool_registry.inject_dependencies()
# ---------------------------------------------------------------------------

_reminder_manager = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(args: dict) -> str:
    """Dispatch reminder actions to sub-handlers."""
    if not _reminder_manager:
        return "Error: reminder system not initialized."

    action = args.get("action", "")
    if action == "add":
        return _reminders_add(_reminder_manager, args)
    elif action == "list":
        return _reminders_list(_reminder_manager)
    elif action == "cancel":
        return _reminders_cancel(_reminder_manager, args)
    elif action == "acknowledge":
        return _reminders_acknowledge(_reminder_manager)
    elif action == "snooze":
        return _reminders_snooze(_reminder_manager, args)
    else:
        return f"Error: unknown reminder action '{action}'."


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------

def _reminders_add(mgr, args: dict) -> str:
    """Add a new one-time reminder."""
    title = args.get("title", "").strip()
    time_text = args.get("time_text", "").strip()

    if not title:
        return "Error: title is required to set a reminder."
    if not time_text:
        return "Error: time_text is required (e.g. 'tomorrow at 6 PM')."

    priority_str = args.get("priority", "normal").lower()
    priority_map = {"urgent": 1, "high": 2, "normal": 3}
    priority = priority_map.get(priority_str, 3)

    # Use the manager's existing natural-time parser
    from core.reminder_manager import ReminderManager
    reminder_time = ReminderManager.parse_natural_time(time_text)
    if not reminder_time:
        return (f"Error: couldn't parse time '{time_text}'. "
                "Try formats like 'tomorrow at 6 PM' or 'in 30 minutes'.")

    rid = mgr.add_reminder(
        title=title,
        reminder_time=reminder_time,
        priority=priority,
    )

    time_desc = _format_reminder_time(reminder_time)
    priority_note = " (marked urgent)" if priority <= 2 else ""
    return f"Reminder #{rid} set: '{title}' {time_desc}{priority_note}."


def _reminders_list(mgr) -> str:
    """List upcoming and fired reminders."""
    pending = mgr.list_reminders("pending", limit=10)
    fired = mgr.list_reminders("fired", limit=5)
    all_reminders = fired + pending

    if not all_reminders:
        return "No upcoming reminders."

    lines = []
    for r in all_reminders:
        try:
            rt = datetime.strptime(r["reminder_time"], "%Y-%m-%d %H:%M:%S")
            time_desc = _format_reminder_time(rt)
        except (ValueError, KeyError):
            time_desc = r.get("reminder_time", "unknown time")
        status_note = " [awaiting acknowledgment]" if r["status"] == "fired" else ""
        lines.append(f"- {r['title']}{status_note}, {time_desc}")

    count = len(all_reminders)
    header = f"{count} reminder{'s' if count != 1 else ''}:"
    return header + "\n" + "\n".join(lines)


def _reminders_cancel(mgr, args: dict) -> str:
    """Cancel a reminder by title fragment."""
    fragment = args.get("cancel_fragment", "").strip()
    if not fragment:
        return "Error: cancel_fragment is required (e.g. 'dentist')."

    cancelled = mgr.cancel_by_title(fragment)
    if cancelled:
        return f"Cancelled: '{cancelled['title']}'."
    return f"No reminder found matching '{fragment}'."


def _reminders_acknowledge(mgr) -> str:
    """Acknowledge the last-fired reminder."""
    if not mgr.is_awaiting_ack():
        return "No reminders currently awaiting acknowledgment."

    reminder = mgr.acknowledge_last()
    if reminder:
        return f"Acknowledged: '{reminder['title']}' marked as done."
    return "Error acknowledging reminder."


def _reminders_snooze(mgr, args: dict) -> str:
    """Snooze the last-fired reminder."""
    if not mgr.is_awaiting_ack():
        return "No reminder to snooze at the moment."

    minutes = args.get("snooze_minutes")
    reminder = mgr.snooze_last(minutes)
    if reminder:
        snooze_min = minutes or mgr.default_snooze
        return f"Snoozed '{reminder['title']}' for {snooze_min} minutes."
    return "Error snoozing reminder."


def _format_reminder_time(dt) -> str:
    """Format a reminder datetime for LLM context (relative when possible)."""
    now = datetime.now()
    diff = dt - now

    total_seconds = diff.total_seconds()
    if total_seconds < 0:
        return f"at {dt.strftime('%-I:%M %p')} (past)"
    elif total_seconds < 90:
        return "in about a minute"
    elif total_seconds < 3600:
        minutes = int(total_seconds / 60)
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    elif total_seconds < 7200:
        return "in about an hour"

    if dt.date() == now.date():
        return f"today at {dt.strftime('%-I:%M %p')}"
    elif (dt.date() - now.date()).days == 1:
        return f"tomorrow at {dt.strftime('%-I:%M %p')}"
    elif (dt.date() - now.date()).days < 7:
        return f"{dt.strftime('%A')} at {dt.strftime('%-I:%M %p')}"
    else:
        return f"on {dt.strftime('%B %-d')} at {dt.strftime('%-I:%M %p')}"
