"""
Weather Skill

Provides current weather information and forecasts using OpenWeatherMap API.
"""

import os
import requests
from datetime import datetime
from core.base_skill import BaseSkill


class WeatherSkill(BaseSkill):
    """Weather information skill"""
    
    def initialize(self) -> bool:
        """Initialize the skill"""
        # Get API key from environment
        self.api_key = os.environ.get('OPENWEATHER_API_KEY')
        if not self.api_key:
            self.logger.warning("OPENWEATHER_API_KEY not set - weather skill disabled")
            return False
        
        # Default location (Gardendale, Alabama)
        self.default_location = "Gardendale,AL,US"
        self.default_lat = 33.6662
        self.default_lon = -86.8128
        
        # Register intents
        # ===== EXACT PATTERNS (high priority) =====
        # These override semantic matching for ambiguous queries
        self.register_intent("what's the weather like today", self.get_current_weather, priority=10)
        self.register_intent("what's the weather today", self.get_current_weather, priority=10)
        self.register_intent("weather today", self.get_current_weather, priority=10)
        
        # ===== SEMANTIC INTENT MATCHING =====
        # Replaces 39 exact patterns with 5 semantic intents
        
        # Current weather (no location)
        self.register_semantic_intent(
            examples=[
                "what's the weather like today",
                "what's the weather today", 
                "how's the weather today",
                "weather right now",
                "current weather"
            ],
            handler=self.get_current_weather,
            threshold=0.68
        )
        
        # Weather for specific location
        self.register_semantic_intent(
            examples=[
                "what's the weather in paris",
                "how's the weather like in london",
                "weather for new york",
                "temperature in chicago",
                "tell me the weather in tokyo"
            ],
            handler=self.get_current_weather,
            threshold=0.70
        )
        
        # Temperature specific
        self.register_semantic_intent(
            examples=[
                "what's the temperature",
                "how hot is it",
                "how cold is it",
                "what's the temp"
            ],
            handler=self.get_current_weather,
            threshold=0.70
        )
        
        # Weather forecast
        self.register_semantic_intent(
            examples=[
                "what's the forecast",
                "weather forecast",
                "forecast for this week",
                "what is the forecast"
            ],
            handler=self.get_forecast,
            threshold=0.70
        )
        
        # Rain specific
        self.register_semantic_intent(
            examples=[
                "will it rain",
                "is it raining",
                "will it rain tomorrow",
                "is it going to rain"
            ],
            handler=self.check_rain_tomorrow,
            threshold=0.70
        )
        
        # Tomorrow's weather
        self.register_semantic_intent(
            examples=[
                "weather tomorrow",
                "tomorrow's forecast",
                "what will the weather be tomorrow",
                "how's it going to be tomorrow"
            ],
            handler=self.get_tomorrow_weather,
            threshold=0.70
        )
        
        
        # Location-based weather
        
        # Forecast intents
        
        return True
    
    def get_current_weather(self) -> str:
        """Get current weather for default location with conversational response"""
        try:
            url = "https://api.openweathermap.org/data/2.5/weather"
            params = {
                "lat": self.default_lat,
                "lon": self.default_lon,
                "appid": self.api_key,
                "units": "imperial"  # Fahrenheit
            }
            
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            # Extract weather info
            temp = round(data["main"]["temp"])
            feels_like = round(data["main"]["feels_like"])
            description = data["weather"][0]["description"]
            weather_main = data["weather"][0]["main"].lower()
            humidity = data["main"]["humidity"]
            wind_speed = round(data["wind"]["speed"])
            
            # Build conversational response based on conditions
            response_parts = []
            
            # Temperature commentary with feels-like
            if temp >= 95:
                if feels_like > temp + 5:
                    response_parts.append(f"It's sweltering outside, {self.honorific} - {temp} degrees but feels like {feels_like}.")
                else:
                    response_parts.append(f"It's quite hot outside, {self.honorific} - {temp} degrees.")
            elif temp >= 85:
                if feels_like > temp + 5:
                    response_parts.append(f"It's rather warm, {self.honorific} - {temp} degrees but feels like {feels_like}.")
                else:
                    response_parts.append(f"It's warm outside, {self.honorific} - {temp} degrees.")
            elif temp >= 70:
                response_parts.append(f"It's pleasant outside, {self.honorific} - {temp} degrees.")
            elif temp >= 50:
                if feels_like < temp - 5:
                    response_parts.append(f"It's mild, {self.honorific} - {temp} degrees but feels cooler, around {feels_like}.")
                else:
                    response_parts.append(f"It's mild outside, {self.honorific} - {temp} degrees.")
            elif temp >= 32:
                if feels_like < temp - 5:
                    response_parts.append(f"It's chilly, {self.honorific} - {temp} degrees but feels like {feels_like} with the wind.")
                else:
                    response_parts.append(f"It's rather chilly, {self.honorific} - {temp} degrees.")
            else:
                response_parts.append(f"It's quite cold, {self.honorific} - {temp} degrees.")
            
            # Weather conditions
            if "rain" in weather_main or "drizzle" in weather_main:
                response_parts.append("Currently raining.")
            elif "thunderstorm" in weather_main:
                response_parts.append("Thunderstorms in the area.")
            elif "snow" in weather_main:
                response_parts.append("Snow falling.")
            elif "clear" in weather_main:
                response_parts.append("Clear skies at the moment.")
            elif "cloud" in weather_main:
                if "few" in description or "scattered" in description:
                    response_parts.append("A few clouds overhead.")
                elif "overcast" in description:
                    response_parts.append("Overcast conditions.")
                else:
                    response_parts.append("Cloudy skies.")
            
            # Wind advisory if significant
            if wind_speed >= 20:
                response_parts.append(f"Quite windy - {wind_speed} miles per hour.")
            elif wind_speed >= 15:
                response_parts.append(f"Breezy conditions, {wind_speed} miles per hour.")
            
            response_text = " ".join(response_parts)
            return self.respond(response_text)
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Weather API error: {e}")
            return self.respond(f"I'm having trouble fetching the weather right now, {self.honorific}.")
        except Exception as e:
            self.logger.error(f"Weather processing error: {e}")
            return self.respond(f"I encountered an error getting the current weather, {self.honorific}.")
    
    def get_weather_for_location(self, location: str = None) -> str:
        """Get weather for a specific location"""
        if not location:
            return self.get_current_weather()
        
        try:
            # First, geocode the location
            geo_url = "http://api.openweathermap.org/geo/1.0/direct"
            geo_params = {
                "q": location,
                "limit": 1,
                "appid": self.api_key
            }
            
            geo_response = requests.get(geo_url, params=geo_params, timeout=5)
            geo_response.raise_for_status()
            geo_data = geo_response.json()
            
            if not geo_data:
                return self.respond(f"I couldn't find weather data for {location}, {self.honorific}.")
            
            lat = geo_data[0]["lat"]
            lon = geo_data[0]["lon"]
            city_name = geo_data[0]["name"]
            
            # Get weather for this location
            url = "https://api.openweathermap.org/data/2.5/weather"
            params = {
                "lat": lat,
                "lon": lon,
                "appid": self.api_key,
                "units": "imperial"
            }
            
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            # Extract weather info
            temp = round(data["main"]["temp"])
            feels_like = round(data["main"]["feels_like"])
            description = data["weather"][0]["description"]
            humidity = data["main"]["humidity"]
            
            # Build response
            response_text = f"In {city_name}, it's {temp} degrees"
            
            if abs(temp - feels_like) > 3:
                response_text += f", feels like {feels_like}"
            
            response_text += f", with {description}."
            
            return self.respond(response_text)
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Weather API error: {e}")
            return self.respond(f"I'm having trouble fetching weather for {location}, {self.honorific}.")
        except Exception as e:
            self.logger.error(f"Weather processing error: {e}")
            return self.respond(f"I encountered an error getting weather for {location}, {self.honorific}.")
    
    def get_forecast(self) -> str:
        """Get 5-day forecast summary with conversational response"""
        # Initial acknowledgment
        self.tts.speak(f"Let me pull up the extended forecast, {self.honorific}.")
        
        try:
            url = "https://api.openweathermap.org/data/2.5/forecast"
            params = {
                "lat": self.default_lat,
                "lon": self.default_lon,
                "appid": self.api_key,
                "units": "imperial"
            }
            
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            # Get unique days (forecast has 3-hour intervals)
            forecasts_by_day = {}
            for item in data["list"][:32]:  # Next ~4 days of data
                dt = datetime.fromtimestamp(item["dt"])
                day_name = dt.strftime("%A")
                weather_main = item["weather"][0]["main"].lower()
                
                if day_name not in forecasts_by_day:
                    forecasts_by_day[day_name] = {
                        "temp_high": item["main"]["temp_max"],
                        "temp_low": item["main"]["temp_min"],
                        "description": item["weather"][0]["description"],
                        "main": weather_main,
                        "has_rain": "rain" in weather_main or "drizzle" in weather_main,
                        "has_storm": "thunderstorm" in weather_main
                    }
                else:
                    # Update high/low
                    forecasts_by_day[day_name]["temp_high"] = max(
                        forecasts_by_day[day_name]["temp_high"], 
                        item["main"]["temp_max"]
                    )
                    forecasts_by_day[day_name]["temp_low"] = min(
                        forecasts_by_day[day_name]["temp_low"], 
                        item["main"]["temp_min"]
                    )
                    # Track rain/storms across the day
                    if "rain" in weather_main or "drizzle" in weather_main:
                        forecasts_by_day[day_name]["has_rain"] = True
                    if "thunderstorm" in weather_main:
                        forecasts_by_day[day_name]["has_storm"] = True
            
            # Build conversational summary (next 3 days)
            response_parts = []
            for day_name, forecast in list(forecasts_by_day.items())[:3]:
                high = round(forecast["temp_high"])
                low = round(forecast["temp_low"])
                
                # Build day summary
                day_parts = [day_name]
                
                # Temperature description
                if high >= 95:
                    day_parts.append(f"will be hot, reaching {high}")
                elif high >= 85:
                    day_parts.append(f"will be warm, with a high of {high}")
                elif high >= 70:
                    day_parts.append(f"looks pleasant, high of {high}")
                elif high >= 50:
                    day_parts.append(f"will be mild, high of {high}")
                else:
                    day_parts.append(f"will be cool, high of {high}")
                
                # Weather conditions
                if forecast["has_storm"]:
                    day_parts.append("with thunderstorms likely")
                elif forecast["has_rain"]:
                    day_parts.append("with rain expected")
                elif "clear" in forecast["main"]:
                    day_parts.append("and clear skies")
                elif "cloud" in forecast["main"]:
                    day_parts.append("with cloudy skies")
                
                response_parts.append(", ".join(day_parts))
            
            response_text = f"Here's what to expect, {self.honorific}. " + ". ".join(response_parts) + "."
            return self.respond(response_text)
            
        except Exception as e:
            self.logger.error(f"Forecast error: {e}")
            return self.respond(f"I'm having trouble getting the forecast right now, {self.honorific}.")
    
    def check_rain_tomorrow(self) -> str:
        """Check if it will rain tomorrow with conversational response"""
        # Initial acknowledgment
        self.tts.speak(f"Let me check the forecast, {self.honorific}.")
        
        try:
            url = "https://api.openweathermap.org/data/2.5/forecast"
            params = {
                "lat": self.default_lat,
                "lon": self.default_lon,
                "appid": self.api_key,
                "units": "imperial"
            }
            
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            # Check tomorrow's forecasts for rain
            tomorrow = datetime.now().day + 1
            will_rain = False
            rain_chance = 0
            has_thunderstorm = False
            
            for item in data["list"][:16]:  # Next ~2 days
                dt = datetime.fromtimestamp(item["dt"])
                if dt.day == tomorrow:
                    weather_main = item["weather"][0]["main"].lower()
                    if "thunderstorm" in weather_main:
                        has_thunderstorm = True
                        will_rain = True
                    elif "rain" in weather_main or "drizzle" in weather_main:
                        will_rain = True
                    # Get precipitation probability if available
                    if "pop" in item:
                        rain_chance = max(rain_chance, item["pop"] * 100)
            
            # Build conversational response
            if has_thunderstorm:
                if rain_chance > 70:
                    response_text = f"Yes {self.honorific}, thunderstorms are very likely tomorrow - about {round(rain_chance)}% chance. I'd recommend keeping plans flexible."
                else:
                    response_text = f"Thunderstorms are possible tomorrow, {self.honorific}. You may want to keep an eye on the forecast."
            elif will_rain:
                if rain_chance >= 80:
                    response_text = f"Yes {self.honorific}, rain is quite likely tomorrow - {round(rain_chance)}% chance. I'd bring an umbrella."
                elif rain_chance >= 50:
                    response_text = f"There's a {round(rain_chance)}% chance of rain tomorrow, {self.honorific}. An umbrella might be wise."
                elif rain_chance > 0:
                    response_text = f"There's a slight chance of rain tomorrow, {self.honorific} - about {round(rain_chance)}%. Probably nothing to worry about."
                else:
                    response_text = f"Rain is expected tomorrow, {self.honorific}. Best to be prepared."
            else:
                response_text = f"No {self.honorific}, it doesn't look like rain tomorrow. Should be dry."
            
            return self.respond(response_text)
            
        except Exception as e:
            self.logger.error(f"Rain check error: {e}")
            return self.respond(f"I'm having trouble checking tomorrow's forecast, {self.honorific}.")
    
    def get_tomorrow_weather(self) -> str:
        """Get tomorrow's weather summary with conversational response"""
        # Initial acknowledgment - checking the forecast
        initial_responses = [
            f"Let me see what the forecast calls for, {self.honorific}.",
            f"Checking tomorrow's forecast, {self.honorific}.",
            f"Let me check that for you, {self.honorific}.",
            f"One moment, {self.honorific}.",
        ]
        self.tts.speak(initial_responses[0])  # Speak while fetching
        
        try:
            url = "https://api.openweathermap.org/data/2.5/forecast"
            params = {
                "lat": self.default_lat,
                "lon": self.default_lon,
                "appid": self.api_key,
                "units": "imperial"
            }
            
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            # Get tomorrow's data
            tomorrow = datetime.now().day + 1
            tomorrow_data = []
            
            for item in data["list"][:16]:
                dt = datetime.fromtimestamp(item["dt"])
                if dt.day == tomorrow:
                    tomorrow_data.append(item)
            
            if not tomorrow_data:
                return self.respond(f"I don't have tomorrow's forecast available, {self.honorific}.")
            
            # Calculate high/low for tomorrow
            temps = [item["main"]["temp"] for item in tomorrow_data]
            high = round(max(temps))
            low = round(min(temps))
            description = tomorrow_data[0]["weather"][0]["description"]
            weather_main = tomorrow_data[0]["weather"][0]["main"].lower()
            
            # Build conversational response based on conditions
            response_parts = []
            
            # Temperature commentary
            if high >= 95:
                response_parts.append(f"Tomorrow will be a scorcher, {self.honorific} - a high of {high} degrees.")
            elif high >= 85:
                response_parts.append(f"Tomorrow will be quite warm, {self.honorific} - expect a high of {high}.")
            elif high >= 70:
                response_parts.append(f"Tomorrow looks pleasant, {self.honorific} - a high of {high} degrees.")
            elif high >= 50:
                response_parts.append(f"Tomorrow will be mild, {self.honorific} - a high of {high} degrees.")
            elif high >= 32:
                response_parts.append(f"Tomorrow will be rather chilly, {self.honorific} - only reaching {high} degrees.")
            else:
                response_parts.append(f"Tomorrow will be quite cold, {self.honorific} - a high of just {high} degrees.")
            
            # Low temperature if notable
            if abs(high - low) > 20:
                response_parts.append(f"It will drop to {low} overnight.")
            
            # Weather conditions
            if "rain" in weather_main or "drizzle" in weather_main:
                response_parts.append("Rain is expected, so you'll want an umbrella.")
            elif "thunderstorm" in weather_main:
                response_parts.append(f"Thunderstorms are in the forecast, {self.honorific}.")
            elif "snow" in weather_main:
                response_parts.append("Snow is expected.")
            elif "clear" in weather_main:
                response_parts.append("Clear skies expected.")
            elif "cloud" in weather_main:
                if "few" in description or "scattered" in description:
                    response_parts.append("Partly cloudy conditions.")
                else:
                    response_parts.append("Expect overcast skies.")
            
            response_text = " ".join(response_parts)
            return self.respond(response_text)
            
        except Exception as e:
            self.logger.error(f"Tomorrow weather error: {e}")
            return self.respond(f"I'm having trouble getting tomorrow's weather, {self.honorific}.")
    
    def handle_intent(self, intent: str, entities: dict) -> str:
        """Handle matched intent"""
        # Check if this is a semantic match
        if intent.startswith("<semantic:") and intent.endswith(">"):
            # Extract handler name
            handler_name = intent[10:-1]
            
            # Find the handler in semantic_intents
            for intent_id, data in self.semantic_intents.items():
                if data['handler'].__name__ == handler_name:
                    handler = data['handler']
                    # Extract location if present
                    location = entities.get("location")
                    if location:
                        return handler(location=location)
                    return handler()
            
            self.logger.error(f"Semantic handler not found: {handler_name}")
            return "I'm not sure how to help with that weather query."
        
        # Regular exact pattern match
        handler = self.intents.get(intent, {}).get("handler")
        if handler:
            # Extract location if present
            location = entities.get("location")
            if location:
                return handler(location=location)
            return handler()
        return "I'm not sure how to help with that weather query."


def create_skill(config, conversation, tts, responses, llm):
    """Factory function to create skill instance"""
    return WeatherSkill(config, conversation, tts, responses, llm)
