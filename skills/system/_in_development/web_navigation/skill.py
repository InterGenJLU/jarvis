"""
Web Navigation Skill

Opens websites, performs searches, and provides intelligent web browsing control.
Supports incognito mode, multiple browsers, monitor selection, and query analysis.
"""

import subprocess
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import quote_plus
from typing import Dict, Tuple, Optional
import json

from core.base_skill import BaseSkill


class WebNavigationSkill(BaseSkill):
    """Web navigation and search skill"""
    
    def initialize(self) -> bool:
        """Initialize the skill"""
        # Load preferences
        self.prefs_path = os.path.join(
            self.config.get("system.storage_path"),
            "data/user_preferences.yaml"
        )
        self.load_preferences()
        
        # Initialize query database
        self.db_path = os.path.join(
            self.config.get("system.storage_path"),
            "data/web_queries.db"
        )
        self._init_database()
        
        # Browser commands
        self.browsers = {
            "chrome": "google-chrome",
            "firefox": "firefox",
            "brave": "brave-browser",
            "edge": "microsoft-edge"
        }
        
        # Search engines
        self.search_engines = {
            "google": "https://www.google.com/search?q={}",
            "duckduckgo": "https://duckduckgo.com/?q={}",
            "bing": "https://www.bing.com/search?q={}"
        }
        
        # Site-specific searches
        self.site_searches = {
            "youtube": "https://www.youtube.com/results?search_query={}",
            "amazon": "https://www.amazon.com/s?k={}",
            "wikipedia": "https://en.wikipedia.org/wiki/Special:Search?search={}",
            "reddit": "https://www.reddit.com/search/?q={}",
            "github": "https://github.com/search?q={}"
        }
        
        # Sites requiring authentication (no incognito)
        self.authenticated_sites = [
            "gmail.com", "mail.google.com",
            "calendar.google.com",
            "keep.google.com",
            "drive.google.com",
            "music.apple.com",
            "facebook.com",
            "twitter.com"
        ]
        
        # Register intents
        # General searches
        self.register_intent("search for {query}", self.search_web)
        self.register_intent("search {query}", self.search_web)
        self.register_intent("look up {query}", self.search_web)
        self.register_intent("find {query}", self.search_web)
        self.register_intent("google {query}", self.search_web)
        
        # Site-specific searches
        self.register_intent("search youtube for {query}", self.search_youtube)
        self.register_intent("show me youtube results for {query}", self.search_youtube)
        self.register_intent("find {query} on youtube", self.search_youtube)
        self.register_intent("youtube {query}", self.search_youtube)
        
        self.register_intent("search amazon for {query}", self.search_amazon)
        self.register_intent("look up {query} on amazon", self.search_amazon)
        self.register_intent("find {query} on amazon", self.search_amazon)
        self.register_intent("amazon {query}", self.search_amazon)
        
        self.register_intent("search wikipedia for {query}", self.search_wikipedia)
        self.register_intent("look up {query} on wikipedia", self.search_wikipedia)
        self.register_intent("wikipedia {query}", self.search_wikipedia)
        
        self.register_intent("search reddit for {query}", self.search_reddit)
        self.register_intent("find {query} on reddit", self.search_reddit)
        self.register_intent("reddit {query}", self.search_reddit)
        
        # Direct URL opening
        self.register_intent("open {url}", self.open_url)
        self.register_intent("go to {url}", self.open_url)
        self.register_intent("navigate to {url}", self.open_url)
        
        # Show last search
        self.register_intent("show me that again", self.repeat_last_search)
        self.register_intent("open that again", self.repeat_last_search)
        
        return True
    
    def load_preferences(self):
        """Load user preferences or use defaults"""
        self.preferences = {
            "browser": "chrome",
            "search_engine": "google",
            "monitor": "primary",
            "incognito_mode": "non_authenticated",
            "analysis_level": "basic",
            "shopping_site": "amazon",
            "video_site": "youtube",
            "auto_fullscreen": True,
            "result_announcement": "brief"
        }
        
        # TODO: Load from YAML if exists
        # For now, using defaults from design doc
    
    def _init_database(self):
        """Initialize query tracking database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS web_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                query_text TEXT NOT NULL,
                search_engine TEXT,
                results_found INTEGER,
                top_result_url TEXT,
                top_result_title TEXT,
                clicked_result_url TEXT,
                clicked_result_title TEXT,
                analysis_performed BOOLEAN DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp 
            ON web_queries(timestamp)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_query_text 
            ON web_queries(query_text)
        ''')
        
        conn.commit()
        conn.close()
    
    def _log_query(self, query: str, search_engine: str = None, url: str = None):
        """Log query to database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO web_queries 
                (query_text, search_engine, clicked_result_url)
                VALUES (?, ?, ?)
            ''', (query, search_engine, url))
            
            conn.commit()
            
            # Maintain rolling window of last 100 queries
            cursor.execute('''
                DELETE FROM web_queries 
                WHERE id NOT IN (
                    SELECT id FROM web_queries 
                    ORDER BY timestamp DESC 
                    LIMIT 100
                )
            ''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.error(f"Error logging query: {e}")
    
    def _should_use_incognito(self, url: str) -> bool:
        """Determine if incognito mode should be used"""
        if self.preferences["incognito_mode"] == "always":
            return True
        elif self.preferences["incognito_mode"] == "never":
            return False
        else:  # "non_authenticated"
            # Check if URL requires authentication
            for auth_site in self.authenticated_sites:
                if auth_site in url:
                    return False
            return True
    
    def _open_browser(self, url: str, incognito: bool = None) -> bool:
        """Open URL in browser with appropriate settings"""
        try:
            browser = self.preferences["browser"]
            browser_cmd = self.browsers.get(browser, "google-chrome")
            
            # Determine incognito mode
            if incognito is None:
                incognito = self._should_use_incognito(url)
            
            # Build command
            cmd = [browser_cmd]
            
            # Add incognito flag
            if incognito:
                if browser == "chrome":
                    cmd.append("--incognito")
                elif browser == "firefox":
                    cmd.append("--private-window")
                elif browser == "brave":
                    cmd.append("--incognito")
                elif browser == "edge":
                    cmd.append("--inprivate")
            
            # Add fullscreen flag
            if self.preferences["auto_fullscreen"]:
                cmd.append("--start-fullscreen")
            
            # Add URL
            cmd.append(url)
            
            # Open browser (detached process)
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error opening browser: {e}")
            return False
    
    def _build_search_url(self, query: str, engine: str = None) -> str:
        """Build search URL"""
        if engine is None:
            engine = self.preferences["search_engine"]
        
        template = self.search_engines.get(engine, self.search_engines["google"])
        return template.format(quote_plus(query))
    
    def search_web(self, query: str) -> str:
        """Perform general web search"""
        # Witty responses for silly queries
        silly_keywords = ["kitten", "cat", "puppy", "dog", "cute", "funny"]
        if any(keyword in query.lower() for keyword in silly_keywords):
            responses = [
                f"Right away sir, since I wasn't doing anything else.",
                f"Searching for {query} now, sir. I hope this is work-related.",
                f"Of course, sir. {query} it is."
            ]
            import random
            response = random.choice(responses)
            self.tts.speak(response)
        else:
            self.tts.speak("Searching now, sir.")
        
        # Build search URL
        url = self._build_search_url(query)
        
        # Log query
        self._log_query(query, self.preferences["search_engine"], url)
        
        # Open browser
        if self._open_browser(url):
            return self.respond("Search opened, sir.")
        else:
            return self.respond("I'm having trouble opening the browser, sir.")
    
    def search_youtube(self, query: str) -> str:
        """Search YouTube specifically"""
        self.tts.speak("Searching YouTube, sir.")
        
        url = self.site_searches["youtube"].format(quote_plus(query))
        self._log_query(f"youtube: {query}", "youtube", url)
        
        if self._open_browser(url):
            return self.respond("YouTube search opened, sir.")
        else:
            return self.respond("I'm having trouble opening YouTube, sir.")
    
    def search_amazon(self, query: str) -> str:
        """Search Amazon specifically"""
        self.tts.speak("Searching Amazon, sir.")
        
        url = self.site_searches["amazon"].format(quote_plus(query))
        self._log_query(f"amazon: {query}", "amazon", url)
        
        if self._open_browser(url):
            return self.respond("Amazon search opened, sir.")
        else:
            return self.respond("I'm having trouble opening Amazon, sir.")
    
    def search_wikipedia(self, query: str) -> str:
        """Search Wikipedia specifically"""
        self.tts.speak("Searching Wikipedia, sir.")
        
        url = self.site_searches["wikipedia"].format(quote_plus(query))
        self._log_query(f"wikipedia: {query}", "wikipedia", url)
        
        if self._open_browser(url):
            return self.respond("Wikipedia search opened, sir.")
        else:
            return self.respond("I'm having trouble opening Wikipedia, sir.")
    
    def search_reddit(self, query: str) -> str:
        """Search Reddit specifically"""
        self.tts.speak("Searching Reddit, sir.")
        
        url = self.site_searches["reddit"].format(quote_plus(query))
        self._log_query(f"reddit: {query}", "reddit", url)
        
        if self._open_browser(url):
            return self.respond("Reddit search opened, sir.")
        else:
            return self.respond("I'm having trouble opening Reddit, sir.")
    
    def open_url(self, url: str) -> str:
        """Open a direct URL"""
        # Add https:// if not present
        if not url.startswith(("http://", "https://")):
            # Check if it's a domain or search query
            if "." in url and " " not in url:
                url = f"https://{url}"
            else:
                # Treat as search query
                return self.search_web(url)
        
        self.tts.speak("Opening now, sir.")
        
        self._log_query(f"direct: {url}", None, url)
        
        if self._open_browser(url):
            return self.respond("Site opened, sir.")
        else:
            return self.respond("I'm having trouble opening that site, sir.")
    
    def repeat_last_search(self) -> str:
        """Repeat the last search query"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT query_text, clicked_result_url 
                FROM web_queries 
                ORDER BY timestamp DESC 
                LIMIT 1
            ''')
            
            result = cursor.fetchone()
            conn.close()
            
            if result:
                query, url = result
                self.tts.speak("Opening that again, sir.")
                
                if self._open_browser(url):
                    return self.respond("Reopened, sir.")
                else:
                    return self.respond("I'm having trouble reopening that, sir.")
            else:
                return self.respond("I don't have a previous search to repeat, sir.")
                
        except Exception as e:
            self.logger.error(f"Error repeating search: {e}")
            return self.respond("I encountered an error, sir.")


def create_skill(config, conversation, tts, responses, llm):
    """Factory function to create skill instance"""
    return WebNavigationSkill(config, conversation, tts, responses, llm)
