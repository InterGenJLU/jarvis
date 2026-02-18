"""
Response Variations

Provides varied responses to avoid robotic repetition.
MCU Jarvis-inspired: professional, understated, sophisticated.
Occasional dry wit — never forced, always in character.
"""

import random
from typing import List, Dict
from collections import deque

from core.honorific import resolve_honorific


class ResponseLibrary:
    """Library of varied responses for different situations"""

    def __init__(self, history_size: int = 5):
        """
        Initialize response library

        Args:
            history_size: Number of recent responses to track (avoid repetition)
        """
        self.history_size = history_size
        self.recent_responses: Dict[str, deque] = {}

        # Response libraries by category
        self.responses = {
            # Acknowledgments
            "acknowledgment": [
                "Certainly, {honorific}.",
                "Right away, {honorific}.",
                "Of course.",
                "Very well.",
                "Understood.",
                "Immediately, {honorific}.",
                "As you wish.",
                "Consider it done, {honorific}.",
                "On it, {honorific}.",
                "Straightaway, {honorific}.",
            ],

            # Confirmations (task complete)
            "confirmation": [
                "Done, {honorific}.",
                "Complete.",
                "All set, {honorific}.",
                "Finished, {honorific}.",
                "Task complete.",
                "That's taken care of, {honorific}.",
                "All done.",
                "Sorted, {honorific}.",
            ],

            # Errors - didn't hear/understand
            "error_no_audio": [
                "I'm sorry, I didn't catch that. Could you repeat that, {honorific}?",
                "Pardon me, {honorific}. I didn't hear you clearly.",
                "I'm afraid I missed that, {honorific}. Would you mind saying that again?",
                "My apologies, I didn't quite catch that. Once more, {honorific}?",
                "Sorry, {honorific}. Could you say that again?",
            ],

            "error_no_transcription": [
                "I'm sorry, I didn't understand that. Could you try again, {honorific}?",
                "I couldn't make that out, {honorific}. Could you repeat that?",
                "My apologies, {honorific}. I didn't quite get that. Once more?",
                "I'm afraid that didn't come through clearly, {honorific}.",
            ],

            # Greetings (time-aware)
            "greeting_morning": [
                "Good morning, {honorific}.",
                "Morning, {honorific}.",
                "Good morning. I trust you slept well.",
                "Morning, {honorific}. Another day, another opportunity.",
            ],

            "greeting_afternoon": [
                "Good afternoon, {honorific}.",
                "Afternoon, {honorific}.",
                "Good afternoon, {honorific}. I hope the day is treating you well.",
            ],

            "greeting_evening": [
                "Good evening, {honorific}.",
                "Evening, {honorific}.",
                "Good evening, {honorific}. I trust the day went well.",
            ],

            "greeting_night": [
                "Good evening, {honorific}.",
                "Evening, {honorific}.",
                "Burning the midnight oil, I see.",
                "Still at it, {honorific}? I admire the dedication.",
            ],

            # Processing
            "processing": [
                "One moment, {honorific}.",
                "Just a moment.",
                "Working on it, {honorific}.",
                "Right away.",
                "Give me just a second, {honorific}.",
                "On it.",
                "Bear with me, {honorific}.",
            ],

            # Thinking/searching
            "searching": [
                "Let me check on that, {honorific}.",
                "Searching now.",
                "Looking into it.",
                "Let me see what I can find.",
                "One moment while I look that up, {honorific}.",
            ],

            # Farewells
            "farewell": [
                "Very good, {honorific}.",
                "Until next time, {honorific}.",
                "Goodbye, {honorific}.",
                "Take care, {honorific}.",
                "I'll be here when you need me, {honorific}.",
                "Goodnight, {honorific}. I'll keep an eye on things.",
            ],

            # Affirmative responses
            "affirmative": [
                "Yes, {honorific}.",
                "Indeed.",
                "That's correct, {honorific}.",
                "Affirmative.",
                "Quite right, {honorific}.",
                "Precisely.",
            ],

            # Negative responses
            "negative": [
                "I'm afraid not, {honorific}.",
                "No, {honorific}.",
                "Unfortunately, no.",
                "Not at the moment, {honorific}.",
                "Not as far as I can tell, {honorific}.",
            ],

            # Unable to help
            "unable": [
                "I'm sorry, {honorific}. That's beyond my current capabilities.",
                "I'm afraid I can't do that right now, {honorific}.",
                "That's not something I'm equipped to handle just yet, {honorific}.",
                "I don't have access to that functionality yet, {honorific}. But give it time.",
                "I wish I could help with that, {honorific}. Perhaps in a future update.",
            ],
        }

    def get_response(self, category: str, avoid_recent: bool = True) -> str:
        """
        Get a varied response from a category

        Args:
            category: Response category
            avoid_recent: Whether to avoid recently used responses

        Returns:
            Response string
        """
        if category not in self.responses:
            return ""

        options = self.responses[category]

        if not options:
            return ""

        # Initialize history for this category if needed
        if category not in self.recent_responses:
            self.recent_responses[category] = deque(maxlen=self.history_size)

        recent = self.recent_responses[category]

        if avoid_recent and len(options) > 1:
            available = [r for r in options if r not in recent]
            if not available:
                available = options
        else:
            available = options

        response = random.choice(available)
        recent.append(response)

        return resolve_honorific(response)

    def get_greeting(self, hour: int = None) -> str:
        """
        Get time-appropriate greeting

        Args:
            hour: Hour of day (0-23), if None uses current time

        Returns:
            Greeting string
        """
        if hour is None:
            from datetime import datetime
            hour = datetime.now().hour

        if 5 <= hour < 12:
            category = "greeting_morning"
        elif 12 <= hour < 17:
            category = "greeting_afternoon"
        elif 17 <= hour < 21:
            category = "greeting_evening"
        else:
            category = "greeting_night"

        return self.get_response(category)

    def acknowledgment(self) -> str:
        """Get acknowledgment response"""
        return self.get_response("acknowledgment")

    def confirmation(self) -> str:
        """Get confirmation response"""
        return self.get_response("confirmation")

    def error_no_audio(self) -> str:
        """Get 'didn't hear you' error response"""
        return self.get_response("error_no_audio")

    def error_no_transcription(self) -> str:
        """Get 'didn't understand' error response"""
        return self.get_response("error_no_transcription")

    def processing(self) -> str:
        """Get 'working on it' response"""
        return self.get_response("processing")

    def searching(self) -> str:
        """Get 'searching' response"""
        return self.get_response("searching")

    def farewell(self) -> str:
        """Get farewell response"""
        return self.get_response("farewell")

    def affirmative(self) -> str:
        """Get affirmative response"""
        return self.get_response("affirmative")

    def negative(self) -> str:
        """Get negative response"""
        return self.get_response("negative")

    def unable(self) -> str:
        """Get 'unable to help' response"""
        return self.get_response("unable")


# Global instance
_response_library = None


def get_response_library() -> ResponseLibrary:
    """Get or create global response library"""
    global _response_library
    if _response_library is None:
        _response_library = ResponseLibrary()
    return _response_library
