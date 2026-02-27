"""
Reminder Skill

Voice interface for JARVIS reminder system.
Handles setting, listing, cancelling, acknowledging, and snoozing reminders.
Delegates all storage and scheduling to the core ReminderManager.
"""

import re
import random
import time
from datetime import datetime
from core.base_skill import BaseSkill


class ReminderSkill(BaseSkill):
    """Voice interface for the reminder system."""

    def initialize(self) -> bool:
        """Register semantic intents for reminder commands."""

        self._pending_title = None
        self._pending_context = None

        # --- Set a one-time reminder ---
        self.register_semantic_intent(
            examples=[
                "remind me to take out the trash tomorrow at 6",
                "set a reminder for my meeting at 3 PM",
                "remind me about the dentist appointment next Tuesday",
                "create a reminder to call mom at 5",
                "remind me in 30 minutes to check the oven",
                "set a reminder to check on something in one minute",
                "remind me to pick up groceries at 5 PM",
                "remind me to water the plants in 2 hours",
                "urgent reminder to call the doctor tomorrow at 8 AM",
                "reminder to call someone tomorrow morning",
                "remind me to check the mail in 5 minutes",
                "remind me to stretch in 20 minutes",
                "remind me to start the laundry in an hour",
            ],
            handler=self.set_reminder,
            threshold=0.58
        )

        # --- Set a recurring reminder ---
        self.register_semantic_intent(
            examples=[
                "remind me every Tuesday to water the plants",
                "set a weekly reminder for laundry on Sundays",
                "remind me every day at 8 AM to take medication",
                "set a daily reminder at noon to stretch",
            ],
            handler=self.set_recurring,
            threshold=0.62
        )

        # --- List reminders ---
        self.register_semantic_intent(
            examples=[
                "what reminders do I have",
                "show my reminders",
                "list my reminders",
                "any upcoming reminders",
            ],
            handler=self.list_reminders,
            threshold=0.70
        )

        # --- Cancel a reminder ---
        self.register_semantic_intent(
            examples=[
                "cancel the reminder about the dentist",
                "delete my trash reminder",
                "delete my dentist reminder",
                "remove the meeting reminder",
                "remove the appointment reminder",
                "get rid of the reminder",
                "cancel reminder",
            ],
            handler=self.cancel_reminder,
            threshold=0.62
        )

        # --- Acknowledge a fired reminder ---
        self.register_semantic_intent(
            examples=[
                "yes I did it",
                "done",
                "already did that",
                "taken care of",
                "I already handled it",
                "yes I remembered",
                "understood",
                "got it",
                "okay",
                "acknowledged",
                "noted",
            ],
            handler=self.acknowledge_current,
            threshold=0.55
        )

        # --- Snooze ---
        self.register_semantic_intent(
            examples=[
                "snooze that",
                "remind me again in 10 minutes",
                "snooze the reminder",
                "tell me again later",
            ],
            handler=self.snooze_current,
            threshold=0.72
        )

        # --- Daily rundown ---
        self.register_semantic_intent(
            examples=[
                "what do I have today",
                "daily rundown",
                "what's on for today",
                "morning briefing",
                "my schedule for today",
            ],
            handler=self.daily_rundown,
            threshold=0.68
        )

        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Route pattern-based intents (not used — we use semantic intents)."""
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]["handler"]
            return handler()
        return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def manager(self):
        """Lazy access to the ReminderManager singleton."""
        if not hasattr(self, "_manager_ref") or self._manager_ref is None:
            from core.reminder_manager import get_reminder_manager
            self._manager_ref = get_reminder_manager()
        return self._manager_ref

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def set_reminder(self) -> str:
        """Handle 'remind me to X at Y' commands."""
        text = getattr(self, "_last_user_text", "")
        if not text:
            return self.respond("I didn't quite catch that. What would you like to be reminded about?")

        # Normalize spoken numbers to digits: "two minutes" -> "2 minutes"
        text = self._normalize_numbers(text)

        parsed = self._parse_reminder_command(text)
        if not parsed:
            return self.respond("I couldn't parse that reminder. Try something like: remind me to call mom tomorrow at 6 PM.")

        title = parsed["title"]
        time_text = parsed["time_text"]

        # Detect priority from explicit keywords
        priority = self._detect_priority(text)

        reminder_time = self.manager.parse_natural_time(time_text)
        if not reminder_time:
            return self.respond(f"I'll remind you to {title}, but I couldn't figure out when. Could you say the time again?")

        rid = self.manager.add_reminder(
            title=title,
            reminder_time=reminder_time,
            priority=priority,
        )

        time_desc = self._format_time_natural(reminder_time)
        priority_note = ""
        if priority <= 2:
            priority_note = " Marked as urgent."

        return self.respond(f"Reminder set, {self.honorific}. I'll remind you to {title} {time_desc}.{priority_note}")

    def set_recurring(self) -> str:
        """Handle recurring reminder commands."""
        text = getattr(self, "_last_user_text", "")
        if not text:
            return self.respond("What would you like to be reminded about regularly?")

        text = self._normalize_numbers(text)
        parsed = self._parse_recurring_command(text)
        if not parsed:
            return self.respond("I couldn't parse that recurring reminder. Try: remind me every Tuesday at 7 PM to take out the trash.")

        title = parsed["title"]
        rule = parsed["rule"]
        first_time = parsed["first_time"]
        priority = self._detect_priority(text)

        rid = self.manager.add_reminder(
            title=title,
            reminder_time=first_time,
            priority=priority,
            reminder_type="recurring",
            recurrence_rule=rule,
        )

        return self.respond(f"Recurring reminder set, {self.honorific}. I'll remind you to {title} {parsed['description']}.")

    def list_reminders(self) -> str:
        """List upcoming reminders."""
        pending = self.manager.list_reminders("pending", limit=10)
        fired = self.manager.list_reminders("fired", limit=5)
        all_reminders = fired + pending  # Show fired (awaiting ack) first

        if not all_reminders:
            return self.respond(f"You have no upcoming reminders, {self.honorific}.")

        count = len(all_reminders)
        if count == 1:
            r = all_reminders[0]
            time_desc = self._format_reminder_time(r["reminder_time"])
            status_note = " awaiting your confirmation" if r["status"] == "fired" else ""
            return self.respond(f"You have one reminder{status_note}: {r['title']}, {time_desc}.")

        lines = [f"You have {count} reminders, {self.honorific}."]
        for r in all_reminders:
            time_desc = self._format_reminder_time(r["reminder_time"])
            prefix = "Awaiting confirmation: " if r["status"] == "fired" else ""
            lines.append(f"{prefix}{r['title']}, {time_desc}.")

        return self.respond(" ".join(lines))

    def cancel_reminder(self) -> str:
        """Cancel a reminder by title match."""
        text = getattr(self, "_last_user_text", "")

        # Extract title fragment from command
        fragment = re.sub(
            r"^(?:cancel|delete|remove)\s+(?:the\s+)?(?:reminder\s+)?(?:about\s+|for\s+)?",
            "", text, flags=re.I
        ).strip()

        if not fragment or len(fragment) < 3:
            return self.respond("Which reminder would you like me to cancel?")

        cancelled = self.manager.cancel_by_title(fragment)
        if cancelled:
            return self.respond(f"Done, {self.honorific}. I've cancelled the reminder: {cancelled['title']}.")
        else:
            return self.respond(f"I couldn't find a reminder matching '{fragment}', {self.honorific}.")

    def acknowledge_current(self) -> str:
        """Acknowledge the most recently fired reminder."""
        if not self.manager.is_awaiting_ack():
            # No reminders awaiting ack — let this fall through to LLM
            return None

        reminder = self.manager.acknowledge_last()
        if reminder:
            responses = [
                f"Noted, {self.honorific}. '{reminder['title']}' cleared.",
                f"Very good, {self.honorific}. I've cleared that reminder.",
                f"Understood. '{reminder['title']}' marked as done.",
                f"Acknowledged, {self.honorific}.",
            ]
            return self.respond(random.choice(responses))
        return self.respond(f"I don't have any reminders awaiting confirmation, {self.honorific}.")

    def snooze_current(self) -> str:
        """Snooze the most recently fired reminder."""
        text = self._normalize_numbers(getattr(self, "_last_user_text", ""))

        # Try to extract snooze duration
        minutes = None
        m = re.search(r"(\d+)\s*(?:minute|min)", text, re.I)
        if m:
            minutes = int(m.group(1))

        if not self.manager.is_awaiting_ack():
            return self.respond(f"There's nothing to snooze at the moment, {self.honorific}.")

        reminder = self.manager.snooze_last(minutes)
        if reminder:
            snooze_min = minutes or self.manager.default_snooze
            return self.respond(f"Snoozed, {self.honorific}. I'll remind you again in {snooze_min} minutes.")
        return self.respond(f"I couldn't find a reminder to snooze, {self.honorific}.")

    def daily_rundown(self) -> str:
        """Provide the daily rundown of today's reminders."""
        rundown = self.manager.get_daily_rundown()
        return self.respond(rundown)

    # ------------------------------------------------------------------
    # Text normalization
    # ------------------------------------------------------------------

    _WORD_TO_DIGIT = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
        "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
        "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
        "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
        "forty": "40", "fifty": "50",
    }

    def _normalize_numbers(self, text: str) -> str:
        """Convert spoken number words to digits for time parsing.

        'in two minutes' -> 'in 2 minutes'
        'at six thirty PM' -> 'at 6:30 PM'
        'tomorrow at five' -> 'tomorrow at 5'
        """
        # Handle compound numbers: "twenty five" -> "25", "thirty minutes" -> "30 minutes"
        text_lower = text.lower()

        # "six thirty" -> "6:30" (time pattern)
        for hour_word, hour_digit in self._WORD_TO_DIGIT.items():
            for min_word, min_digit in self._WORD_TO_DIGIT.items():
                compound = f"{hour_word} {min_word}"
                if compound in text_lower:
                    h = int(hour_digit)
                    m = int(min_digit)
                    if 1 <= h <= 12 and m in (0, 10, 15, 20, 30, 40, 45, 50):
                        text_lower = text_lower.replace(compound, f"{h}:{m:02d}")

        # "twenty five" -> "25" (compound tens + ones)
        tens = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
        ones = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9}
        for t_word, t_val in tens.items():
            for o_word, o_val in ones.items():
                compound = f"{t_word} {o_word}"
                if compound in text_lower:
                    text_lower = text_lower.replace(compound, str(t_val + o_val))

        # Simple single-word replacements
        for word, digit in self._WORD_TO_DIGIT.items():
            # Use word boundary to avoid partial replacements
            text_lower = re.sub(rf"\b{word}\b", digit, text_lower)

        return text_lower

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_reminder_command(self, text: str) -> dict:
        """Parse 'remind me to X at/on/in Y' into title and time components."""
        text_clean = text.strip()

        # Strip command prefix + priority words in one pass
        # Includes fuzzy Whisper variants: "urge your" for "urgent", etc.
        text_clean = re.sub(
            r"^(?:(?:set(?:ting)?|create|make)\s+)?(?:a(?:n)?\s+)?(?:urgent|argent|urge\s+\w+|urging|critical|important)?\s*(?:reminder|remind me)\s*(?:to|for|about|that)?\s*",
            "", text_clean, flags=re.I
        ).strip()

        if not text_clean:
            return None

        # Try "in X minutes/hours" at the START: "in 30 minutes check the oven"
        m = re.match(r"(in\s+\d+\s+(?:minutes?|mins?|hours?|hrs?|days?))\s+(?:to\s+)?(.+)", text_clean, re.I)
        if m:
            return {"time_text": m.group(1).strip(), "title": m.group(2).strip()}

        # Try to find time expression at the END of the text
        # Common patterns: "tomorrow at 6", "at 3 PM", "next Tuesday", "in 2 hours", "tonight", "by 5 PM"
        time_patterns = [
            r"(tomorrow\s+(?:morning|afternoon|evening|night)(?:\s+at\s+\d.*)?)",
            r"(tomorrow\s+at\s+\d.*)",
            r"(tonight(?:\s+at\s+\d.*)?)",
            r"(today\s+at\s+\d.*)",
            r"((?:next|this)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+\d.*)?)",
            r"(on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+\d.*)?)",
            r"((?:at|by)\s+\d+(?:[.:]\d+)?\s*(?:am|pm|a\.m\.?|p\.m\.?)?\s*$)",
            r"(in\s+\d+\s+(?:minutes?|mins?|hours?|hrs?|days?))",
            r"(tomorrow)",
        ]

        for pattern in time_patterns:
            m = re.search(rf"(.+?)\s+({pattern})$", text_clean, re.I)
            if m:
                title = m.group(1).strip()
                time_text = m.group(2).strip()
                # Clean title
                title = re.sub(r"\s+$", "", title)
                if title:
                    return {"time_text": time_text, "title": title}

        # Last resort: if text contains a number that looks like a time, split there
        m = re.search(r"(.+?)\s+((?:at|by)\s+\d.*)$", text_clean, re.I)
        if m:
            return {"time_text": m.group(2).strip(), "title": m.group(1).strip()}

        return None

    def _parse_recurring_command(self, text: str) -> dict:
        """Parse recurring reminder commands.

        Examples:
            "remind me every Tuesday to water the plants"
            "remind me every day at 8 AM to take medication"
            "set a weekly reminder for laundry on Sundays at 10 AM"
        """
        text_clean = text.strip().lower()

        # "every day at HH:MM to TITLE"
        m = re.search(
            r"every\s+day\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to\s+)?(.+)",
            text_clean, re.I
        )
        if m:
            time_text = m.group(1).strip()
            title = m.group(2).strip()
            parsed_time = self.manager.parse_natural_time(f"today at {time_text}")
            if parsed_time:
                rule = f"daily:{parsed_time.hour:02d}:{parsed_time.minute:02d}"
                return {
                    "title": title,
                    "rule": rule,
                    "first_time": parsed_time,
                    "description": f"every day at {parsed_time.strftime('%-I:%M %p')}",
                }

        # "every [day_name(s)] [at TIME] [to TITLE]"
        day_pattern = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        m = re.search(
            rf"every\s+((?:{day_pattern})(?:\s+and\s+{day_pattern})*)"
            rf"(?:\s+at\s+(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?))?"
            rf"\s+(?:to\s+)?(.+)",
            text_clean, re.I
        )
        if m:
            days_text = m.group(1)
            time_text = m.group(2) or "9:00 AM"
            title = m.group(3).strip()

            # Parse day names
            day_abbrev = {
                "monday": "mon", "tuesday": "tue", "wednesday": "wed",
                "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
            }
            days = [day_abbrev[d.strip()] for d in re.split(r"\s+and\s+", days_text)
                    if d.strip() in day_abbrev]

            parsed_time = self.manager.parse_natural_time(f"today at {time_text}")
            if parsed_time and days:
                rule = f"weekly:{','.join(days)}:{parsed_time.hour:02d}:{parsed_time.minute:02d}"
                days_desc = " and ".join([d.capitalize() for d in re.split(r"\s+and\s+", days_text)])
                return {
                    "title": title,
                    "rule": rule,
                    "first_time": parsed_time,
                    "description": f"every {days_desc} at {parsed_time.strftime('%-I:%M %p')}",
                }

        return None

    def _detect_priority(self, text: str) -> int:
        """Detect priority from explicit keywords in the command.

        Includes fuzzy variants for common Whisper mishearings.
        """
        text_lower = text.lower()
        # Whisper often mangles "urgent" → "urge your", "urge it", "urging", etc.
        if any(w in text_lower for w in [
            "urgent", "argent", "urge ", "urging", "critical", "emergency",
        ]):
            return 1
        elif any(w in text_lower for w in ["important", "high priority"]):
            return 2
        return 3

    def _format_time_natural(self, dt: datetime) -> str:
        """Format a datetime as natural speech."""
        now = datetime.now()
        diff = dt - now

        if diff.total_seconds() < 90:
            return "in about a minute"
        elif diff.total_seconds() < 3600:
            minutes = int(diff.total_seconds() / 60)
            return f"in {minutes} minute{'s' if minutes != 1 else ''}"
        elif diff.total_seconds() < 7200:
            return "in about an hour"

        if dt.date() == now.date():
            return f"today at {dt.strftime('%-I:%M %p')}"
        elif (dt.date() - now.date()).days == 1:
            return f"tomorrow at {dt.strftime('%-I:%M %p')}"
        elif (dt.date() - now.date()).days < 7:
            return f"{dt.strftime('%A')} at {dt.strftime('%-I:%M %p')}"
        else:
            return f"on {dt.strftime('%B %-d')} at {dt.strftime('%-I:%M %p')}"

    def _format_reminder_time(self, time_str: str) -> str:
        """Format a stored reminder time for speech."""
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            return self._format_time_natural(dt)
        except ValueError:
            return time_str
