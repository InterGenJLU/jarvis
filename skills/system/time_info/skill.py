"""
Time Info Skill

Provides current time and date information.
"""

from datetime import datetime
from core.base_skill import BaseSkill


class TimeInfoSkill(BaseSkill):
    """Time and date information skill"""
    
    def initialize(self) -> bool:
        """Initialize the skill"""
        # Register time intents
        # ===== SEMANTIC INTENT MATCHING =====
        # Replaces 15 exact patterns with 2 semantic intents
        
        # Time queries
        self.register_semantic_intent(
            examples=[
                "what time is it",
                "what's the time",
                "tell me the time",
                "current time"
            ],
            handler=self.get_time,
            threshold=0.70
        )
        
        # Date queries
        self.register_semantic_intent(
            examples=[
                "what's the date",
                "what is the date",
                "what day is it",
                "today's date"
            ],
            handler=self.get_date,
            threshold=0.70
        )
        
        
        # Register date intents
        
        return True
    
    def handle_intent(self, intent: str, entities: dict) -> str:
        """Handle matched intent"""
        # Check if this is a semantic match
        if intent.startswith("<semantic:") and intent.endswith(">"):
            handler_name = intent[10:-1]
            
            for intent_id, data in self.semantic_intents.items():
                if data['handler'].__name__ == handler_name:
                    return data['handler']()
            
            self.logger.error(f"Semantic handler not found: {handler_name}")
            return "I'm sorry, I couldn't process that request."
        
        # Regular exact pattern match
        handler = self.intents.get(intent, {}).get("handler")
        if handler:
            return handler()
        return "I'm sorry, I couldn't process that request."
    
    def get_time(self) -> str:
        """Get current time"""
        try:
            now = datetime.now()
            
            # Format time in 12-hour format
            hour = now.hour % 12
            if hour == 0:
                hour = 12
            minute = now.minute
            period = "AM" if now.hour < 12 else "PM"
            
            # Build natural response
            if minute == 0:
                time_str = f"{hour} {period}"
            elif minute < 10:
                time_str = f"{hour} oh {minute} {period}"
            else:
                time_str = f"{hour} {minute} {period}"
            
            response = f"The time is {time_str}."
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting time: {e}")
            return self.respond("I'm sorry, I couldn't retrieve the time.")
    
    def get_date(self) -> str:
        """Get current date"""
        try:
            now = datetime.now()
            
            # Format date naturally
            day_name = now.strftime("%A")
            month_name = now.strftime("%B")
            day = now.day
            year = now.year
            
            # Add ordinal suffix (1st, 2nd, 3rd, 4th, etc.)
            if 10 <= day % 100 <= 20:
                suffix = "th"
            else:
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            
            response = f"Today is {day_name}, {month_name} {day}{suffix}, {year}."
            return self.respond(response)
            
        except Exception as e:
            self.logger.error(f"Error getting date: {e}")
            return self.respond("I'm sorry, I couldn't retrieve the date.")
