"""Tool definition: get_time — current local time and date."""

from datetime import datetime

TOOL_NAME = "get_time"
SKILL_NAME = "time_info"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": (
            "Get the current local time. Also handles date questions. "
            "Call this for any question about what time it is, today's date, "
            "the current day of the week, or the current year."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_date": {
                    "type": "boolean",
                    "description": "Set true ONLY when the user explicitly asks for the date. Default false — omit for time-only questions."
                }
            },
            "required": []
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For ANY question about time, date, day, or year, call "
    "get_time. NEVER answer time/date questions from the prompt."
)


def handler(args: dict) -> str:
    """Return current local time and optionally the date."""
    now = datetime.now()

    # Time in 12-hour format
    hour = now.hour % 12 or 12
    minute = now.minute
    period = "AM" if now.hour < 12 else "PM"
    time_str = f"{hour}:{minute:02d} {period}"

    include_date = args.get("include_date", False)
    if include_date:
        day_name = now.strftime("%A")
        month_name = now.strftime("%B")
        day = now.day
        year = now.year
        return f"Current time: {time_str}. Date: {day_name}, {month_name} {day}, {year}."
    return f"Current time: {time_str}."
