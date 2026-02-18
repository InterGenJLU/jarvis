"""
Web Navigation Skill

Opens websites, performs searches, and provides intelligent web browsing control.
Supports incognito mode, multiple browsers, site-specific searches, and query history.

Phase 1: Browser opening with semantic intents and butler personality.
Phase 2 (future): Programmatic web research, comparison, and analysis.
"""

import subprocess
import os
import re
import random
import sqlite3
import threading
from collections import deque
from datetime import datetime
from urllib.parse import quote_plus
from typing import Optional, List, Dict

from core.base_skill import BaseSkill


class WebNavigationSkill(BaseSkill):
    """Web navigation and search skill"""

    def initialize(self) -> bool:
        """Initialize the skill"""
        # Storage path from config
        storage_path = self.config.get("system.storage_path")

        # Initialize query database
        self.db_path = os.path.join(storage_path, "data/web_queries.db")
        self._init_database()

        # Browser commands (only installed browsers)
        self.browsers = {
            "brave": "brave-browser",
            "chrome": "google-chrome",
            "firefox": "firefox",
        }

        # Preferences (from config with sensible defaults)
        self.default_browser = self.config.get("web_navigation.default_browser", "brave")
        self.default_search_engine = self.config.get("web_navigation.default_search_engine", "google")
        self.incognito_mode = "non_authenticated"
        self.follow_up_duration = self.config.get("web_navigation.follow_up_duration", 10.0)

        # Screen dimensions for half-screen positioning
        self.screen_width = self.config.get("web_navigation.screen.width", 2560)
        self.screen_height = self.config.get("web_navigation.screen.height", 1440)

        # Search engines
        self.search_engines = {
            "google": "https://www.google.com/search?q={}",
            "duckduckgo": "https://duckduckgo.com/?q={}",
            "bing": "https://www.bing.com/search?q={}",
        }

        # Site-specific searches
        self.site_searches = {
            "youtube": "https://www.youtube.com/results?search_query={}",
            "amazon": "https://www.amazon.com/s?k={}",
            "wikipedia": "https://en.wikipedia.org/wiki/Special:Search?search={}",
            "reddit": "https://www.reddit.com/search/?q={}",
            "github": "https://github.com/search?q={}",
        }

        # Sites requiring authentication (no incognito)
        self.authenticated_sites = [
            "gmail.com", "mail.google.com",
            "calendar.google.com",
            "keep.google.com",
            "drive.google.com",
            "music.apple.com",
            "facebook.com", "twitter.com", "x.com",
        ]

        # Response pools — butler cadence, single voice line per action
        self._response_pools = {
            "search": [
                "Let me look into that, {honorific}.",
                "Searching now.",
                "One moment while I pull that up.",
                "Looking into it, {honorific}.",
                "Let me see what I can find.",
            ],
            "youtube": [
                "Pulling up YouTube now, {honorific}.",
                "Let's see what YouTube has on that.",
                "Checking YouTube for you.",
            ],
            "amazon": [
                "Checking Amazon for you, {honorific}.",
                "Let me see what's available.",
                "Pulling up Amazon now.",
            ],
            "wikipedia": [
                "Let me consult Wikipedia on that.",
                "Checking Wikipedia, {honorific}.",
                "Pulling up Wikipedia now.",
            ],
            "reddit": [
                "Let me check Reddit for you.",
                "Pulling up Reddit now, {honorific}.",
                "Searching Reddit, {honorific}.",
            ],
            "github": [
                "Searching GitHub now, {honorific}.",
                "Let me check GitHub for that.",
                "Pulling up GitHub, {honorific}.",
            ],
            "open_url": [
                "Opening that now, {honorific}.",
                "Right away, {honorific}.",
                "On it, {honorific}.",
            ],
            "repeat": [
                "Right where we left off, {honorific}.",
                "Bringing that back up.",
                "Opening that again, {honorific}.",
            ],
            "error": [
                "I seem to be having trouble with the browser, {honorific}. My apologies.",
                "The browser isn't cooperating at the moment, {honorific}.",
                "I'm having difficulty opening that, {honorific}.",
            ],
            "easter_egg": [
                "Right away, {honorific}. Since I wasn't doing anything else.",
                "Certainly, {honorific}. I live for these moments.",
                "At once, {honorific}. My schedule just cleared up.",
                "Consider it done, {honorific}. This is the highlight of my day.",
            ],
            "resize_full": [
                "Going full screen, {honorific}.",
                "Full screen, right away.",
                "Expanding to full screen now.",
            ],
            "resize_half": [
                "Half screen it is, {honorific}.",
                "Resizing to half screen now.",
                "Moving to half screen, {honorific}.",
            ],
            "resize_no_window": [
                "I don't see a browser window to resize, {honorific}.",
                "There doesn't appear to be a window to adjust, {honorific}.",
            ],
            "minimize": [
                "Minimized, {honorific}.",
                "Put that away for you, {honorific}.",
                "Window hidden, {honorific}.",
            ],
        }
        # Track recent responses to avoid repetition
        self._recent: dict[str, deque] = {}

        # Result scraping state (for "show me the Nth result" follow-ups)
        self._last_search_results: List[Dict[str, str]] = []  # [{title, url}, ...]
        self._last_search_type: Optional[str] = None
        self._last_query: Optional[str] = None
        self._current_page: int = 1
        self._results_per_page: int = 10
        self._scrape_lock = threading.Lock()

        # Sites that use scroll-based scraping + local pagination
        self._scroll_sites = {"youtube", "reddit"}

        # Pagination URL builders per site type
        # Each takes (query, page_number) and returns the paginated URL
        self._pagination = {
            "google": lambda q, p: f"https://www.google.com/search?q={quote_plus(q)}&start={(p - 1) * 10}",
            "amazon": lambda q, p: f"https://www.amazon.com/s?k={quote_plus(q)}&page={p}",
            "github": lambda q, p: f"https://github.com/search?q={quote_plus(q)}&p={p}",
            "wikipedia": lambda q, p: f"https://en.wikipedia.org/wiki/Special:Search?search={quote_plus(q)}&offset={(p - 1) * 20}",
            "reddit": lambda q, p: f"https://www.reddit.com/search/?q={quote_plus(q)}",  # no real pagination
            "youtube": lambda q, p: f"https://www.youtube.com/results?search_query={quote_plus(q)}",  # infinite scroll
        }

        # CSS selectors for extracting result links per site
        self._result_selectors = {
            "youtube": {
                "selector": "a#video-title",
                "wait": "ytd-video-renderer",
                "base_url": "https://www.youtube.com",
            },
            "google": {
                "selector": "div.g a[href]:first-child",
                "wait": "div.g",
                "base_url": "",
            },
            "amazon": {
                "selector": "div[data-component-type='s-search-result'] a.a-text-normal.s-line-clamp-4",
                "wait": "div[data-component-type='s-search-result']",
                "base_url": "https://www.amazon.com",
            },
            "reddit": {
                "selector": "div.thing a.title",
                "wait": "div.thing",
                "base_url": "https://old.reddit.com",
                "scrape_url": "https://old.reddit.com/search?q={query}",
            },
            "github": {
                "selector": "div.search-title a",
                "wait": "div.search-title",
                "base_url": "https://github.com",
            },
            "wikipedia": {
                "selector": "div.mw-search-result-heading a",
                "wait": "div.mw-search-result-heading",
                "base_url": "https://en.wikipedia.org",
            },
        }

        # Ordinal/cardinal number mapping
        self._ordinals = {
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }

        # ===== SEMANTIC INTENT MATCHING =====

        # General web search
        self.register_semantic_intent(
            examples=[
                "search for quantum computing",
                "look up ROCm drivers",
                "google best pizza near me",
                "find information about black holes",
                "search the web for python tutorials",
                "can you look up how to install docker",
                "I need to find a good mechanic",
            ],
            handler=self.search_web,
            threshold=0.55,
        )

        # YouTube search
        self.register_semantic_intent(
            examples=[
                "search YouTube for jeep alignment",
                "find cat videos on YouTube",
                "pull up YouTube for cooking tutorials",
                "show me YouTube results for guitar lessons",
                "show me husky videos on YouTube",
                "play guitar covers on YouTube",
            ],
            handler=self.search_youtube,
            threshold=0.50,
        )

        # Amazon search
        self.register_semantic_intent(
            examples=[
                "look up SSDs on Amazon",
                "search Amazon for standing desk",
                "find headphones on Amazon",
                "check Amazon for USB microphones",
            ],
            handler=self.search_amazon,
            threshold=0.55,
        )

        # Wikipedia search
        self.register_semantic_intent(
            examples=[
                "look up quantum mechanics on Wikipedia",
                "search Wikipedia for Abraham Lincoln",
                "check Wikipedia for the history of computing",
            ],
            handler=self.search_wikipedia,
            threshold=0.60,
        )

        # Reddit search
        self.register_semantic_intent(
            examples=[
                "search Reddit for home automation",
                "find programming tips on Reddit",
                "check Reddit for GPU benchmarks",
            ],
            handler=self.search_reddit,
            threshold=0.60,
        )

        # GitHub search
        self.register_semantic_intent(
            examples=[
                "search GitHub for python libraries",
                "find that repo on GitHub",
                "look up open source projects on GitHub",
            ],
            handler=self.search_github,
            threshold=0.60,
        )

        # Direct URL opening
        self.register_semantic_intent(
            examples=[
                "open github.com",
                "go to reddit.com",
                "navigate to youtube.com",
                "open google.com",
            ],
            handler=self.open_url,
            threshold=0.65,
        )

        # Repeat last search
        self.register_semantic_intent(
            examples=[
                "show me that again",
                "open that again",
                "pull that back up",
                "go back to that last search",
            ],
            handler=self.repeat_last_search,
            threshold=0.65,
        )

        # Select Nth result from last search
        self.register_semantic_intent(
            examples=[
                "play the first one",
                "open the second result",
                "click the third one",
                "show me the fourth one",
                "the second one",
                "number three",
                "open the fifth result",
                "play number one",
                "show me the first result",
                "pull up the third one",
                "open that one",
                "the first video",
            ],
            handler=self.select_result,
            threshold=0.50,
        )

        # Resize active browser window
        self.register_semantic_intent(
            examples=[
                "fullscreen",
                "full screen please",
                "make it full screen",
                "go full screen",
                "put that full screen",
                "maximize the window",
                "make it bigger",
                "half screen please",
                "make it half screen",
                "put that in half screen",
                "shrink that down",
                "make that smaller",
                "resize the window",
            ],
            handler=self.resize_window,
            threshold=0.50,
        )

        # Minimize / hide browser window
        self.register_semantic_intent(
            examples=[
                "minimize that",
                "minimize the window",
                "hide that",
                "put that away",
                "get that off the screen",
                "close the browser",
            ],
            handler=self.minimize_window,
            threshold=0.55,
        )

        # Next page / previous page navigation
        self.register_semantic_intent(
            examples=[
                "next page",
                "next page please",
                "show me the next page",
                "go to the next page",
                "more results",
                "page two",
                "show more",
            ],
            handler=self.next_page,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "previous page",
                "go back a page",
                "back to the first page",
                "last page",
                "go back",
                "previous results",
            ],
            handler=self.previous_page,
            threshold=0.55,
        )

        self.logger.info("Web navigation skill initialized (Brave default)")
        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Route pattern-based intents (semantic intents are primary)."""
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]["handler"]
            return handler()
        return None

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _pick_response(self, category: str) -> str:
        """Pick a varied response from a pool, avoiding recent repeats."""
        pool = self._response_pools.get(category, [])
        if not pool:
            return ""

        if category not in self._recent:
            self._recent[category] = deque(maxlen=3)

        recent = self._recent[category]
        available = [r for r in pool if r not in recent]
        if not available:
            available = pool

        choice = random.choice(available)
        recent.append(choice)
        return choice

    def _maybe_easter_egg(self) -> Optional[str]:
        """~12% chance of a dry wit Easter egg instead of normal response."""
        if random.random() < 0.12:
            return self._pick_response("easter_egg")
        return None

    # ------------------------------------------------------------------
    # Query extraction
    # ------------------------------------------------------------------

    def _extract_query(self, strip_site: str = None) -> str:
        """Extract the meaningful search query from raw user text.

        Strips wake word, command verbs, and site-specific words.
        'Jarvis search YouTube for jeep alignment videos' → 'jeep alignment videos'
        """
        text = getattr(self, "_last_user_text", "") or ""
        text = text.strip()

        # Remove wake word
        text = re.sub(r'\bjarvis\b', '', text, flags=re.IGNORECASE)

        # Remove command verbs/phrases (order matters — longer first)
        strip_phrases = [
            "search the web for", "search for", "search on",
            "look up", "look for",
            "pull up", "show me", "find me",
            "search", "google", "find",
            "navigate to", "go to", "open",
            "check for", "check",
        ]
        for phrase in strip_phrases:
            text = re.sub(r'\b' + re.escape(phrase) + r'\b', '', text, flags=re.IGNORECASE)

        # Remove site-specific words
        if strip_site:
            text = re.sub(r'\b' + re.escape(strip_site) + r'\b', '', text, flags=re.IGNORECASE)

        # Remove dangling prepositions
        text = re.sub(r'\b(on|for|in|at|the)\s*$', '', text.strip(), flags=re.IGNORECASE)
        text = re.sub(r'^\s*(on|for|in|at|the)\b', '', text.strip(), flags=re.IGNORECASE)

        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        return text

    # ------------------------------------------------------------------
    # Window mode detection
    # ------------------------------------------------------------------

    def _detect_window_mode(self, text: str) -> tuple:
        """Detect and strip window mode keywords from command text.

        Returns:
            (cleaned_text, mode) where mode is 'fullscreen', 'half_left', or 'half_right'
        """
        mode = "fullscreen"  # default

        # Check for half-screen variants
        half_patterns = [
            r'\bin\s+(?:a\s+)?half\s+screen\b',
            r'\bhalf\s+screen\b',
            r'\bwindowed\b',
            r'\bin\s+(?:a\s+)?window\b',
            r'\bhalf\s+(?:the\s+)?screen\b',
        ]

        for pattern in half_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                mode = "half_left"
                text = re.sub(pattern, '', text, flags=re.IGNORECASE)
                break

        # Check for explicit full-screen override
        full_patterns = [
            r'\bin\s+(?:a\s+)?full\s+screen\b',
            r'\bfull\s+screen\b',
            r'\bfullscreen\b',
        ]

        for pattern in full_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                mode = "fullscreen"
                text = re.sub(pattern, '', text, flags=re.IGNORECASE)
                break

        # Check for right-side preference (only if half mode)
        if mode == "half_left":
            right_patterns = [
                r'\bright\s+(?:side|half)\b',
                r'\bother\s+side\b',
                r'\bon\s+the\s+right\b',
            ]
            for pattern in right_patterns:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    mode = "half_right"
                    text = re.sub(pattern, '', text, flags=re.IGNORECASE)
                    break

        text = re.sub(r'\s+', ' ', text).strip()
        return text, mode

    def _prepare_command(self) -> str:
        """Pre-process user text: detect window mode and clean text.

        Sets self._current_window_mode and updates self._last_user_text.
        Returns the window mode.
        """
        raw_text = getattr(self, "_last_user_text", "") or ""
        cleaned, mode = self._detect_window_mode(raw_text)
        self._last_user_text = cleaned
        self._current_window_mode = mode
        return mode

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

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
                url TEXT
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_wq_timestamp
            ON web_queries(timestamp)
        ''')

        conn.commit()
        conn.close()

    def _log_query(self, query: str, search_engine: str = None, url: str = None):
        """Log query to database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                'INSERT INTO web_queries (query_text, search_engine, url) VALUES (?, ?, ?)',
                (query, search_engine, url),
            )
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

    # ------------------------------------------------------------------
    # Browser control
    # ------------------------------------------------------------------

    def _should_use_incognito(self, url: str) -> bool:
        """Determine if incognito mode should be used"""
        if self.incognito_mode == "always":
            return True
        if self.incognito_mode == "never":
            return False
        # "non_authenticated" — incognito unless site needs auth
        for auth_site in self.authenticated_sites:
            if auth_site in url:
                return False
        return True

    def _open_browser(self, url: str, incognito: bool = None, window_mode: str = "fullscreen") -> bool:
        """Open URL in browser with appropriate settings"""
        try:
            browser_cmd = self.browsers.get(
                self.default_browser,
                "brave-browser",
            )

            cmd = [browser_cmd]

            # Force X11 (XWayland) so wmctrl can manage the window later
            if self.default_browser != "firefox":
                cmd.append("--ozone-platform=x11")

            # Incognito flag
            if incognito is None:
                incognito = self._should_use_incognito(url)
            if incognito:
                if self.default_browser == "firefox":
                    cmd.append("--private-window")
                else:
                    cmd.append("--incognito")

            # Window mode
            if window_mode == "fullscreen":
                if self.default_browser == "firefox":
                    cmd.append("-fullscreen")
                else:
                    cmd.append("--start-fullscreen")
            elif window_mode in ("half_left", "half_right"):
                half_w = self.screen_width // 2
                x_pos = 0 if window_mode == "half_left" else half_w

                if self.default_browser == "firefox":
                    cmd.extend(["-width", str(half_w), "-height", str(self.screen_height)])
                else:
                    cmd.extend([
                        "--new-window",
                        f"--window-size={half_w},{self.screen_height}",
                        f"--window-position={x_pos},0",
                    ])

            cmd.append(url)

            # Ensure DISPLAY is set for X11/XWayland windows
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")

            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            self.logger.info(f"Browser opened: {window_mode} mode, url={url[:80]}")
            return True

        except Exception as e:
            self.logger.error(f"Error opening browser: {e}")
            return False

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def search_web(self) -> str:
        """Perform general web search"""
        mode = self._prepare_command()
        query = self._extract_query()
        if not query:
            return self.respond(f"What would you like me to search for, {self.honorific}?")

        url = self.search_engines[self.default_search_engine].format(quote_plus(query))
        self._log_query(query, self.default_search_engine, url)

        response = self._maybe_easter_egg() or self._pick_response("search")

        if self._open_browser(url, window_mode=mode):
            self._start_scrape(url, "google", query)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(response)
        return self.respond(self._pick_response("error"))

    def search_youtube(self) -> str:
        """Search YouTube"""
        mode = self._prepare_command()
        query = self._extract_query(strip_site="youtube")
        if not query:
            return self.respond(f"What shall I look up on YouTube, {self.honorific}?")

        url = self.site_searches["youtube"].format(quote_plus(query))
        self._log_query(f"youtube: {query}", "youtube", url)

        response = self._maybe_easter_egg() or self._pick_response("youtube")

        if self._open_browser(url, window_mode=mode):
            self._start_scrape(url, "youtube", query)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(response)
        return self.respond(self._pick_response("error"))

    def search_amazon(self) -> str:
        """Search Amazon"""
        mode = self._prepare_command()
        query = self._extract_query(strip_site="amazon")
        if not query:
            return self.respond(f"What are we looking for on Amazon, {self.honorific}?")

        url = self.site_searches["amazon"].format(quote_plus(query))
        self._log_query(f"amazon: {query}", "amazon", url)

        response = self._maybe_easter_egg() or self._pick_response("amazon")

        if self._open_browser(url, window_mode=mode):
            self._start_scrape(url, "amazon", query)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(response)
        return self.respond(self._pick_response("error"))

    def search_wikipedia(self) -> str:
        """Search Wikipedia"""
        mode = self._prepare_command()
        query = self._extract_query(strip_site="wikipedia")
        if not query:
            return self.respond(f"What shall I look up on Wikipedia, {self.honorific}?")

        url = self.site_searches["wikipedia"].format(quote_plus(query))
        self._log_query(f"wikipedia: {query}", "wikipedia", url)

        response = self._maybe_easter_egg() or self._pick_response("wikipedia")

        if self._open_browser(url, window_mode=mode):
            self._start_scrape(url, "wikipedia", query)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(response)
        return self.respond(self._pick_response("error"))

    def search_reddit(self) -> str:
        """Search Reddit"""
        mode = self._prepare_command()
        query = self._extract_query(strip_site="reddit")
        if not query:
            return self.respond(f"What shall I look up on Reddit, {self.honorific}?")

        url = self.site_searches["reddit"].format(quote_plus(query))
        self._log_query(f"reddit: {query}", "reddit", url)

        response = self._maybe_easter_egg() or self._pick_response("reddit")

        if self._open_browser(url, window_mode=mode):
            self._start_scrape(url, "reddit", query)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(response)
        return self.respond(self._pick_response("error"))

    def search_github(self) -> str:
        """Search GitHub"""
        mode = self._prepare_command()
        query = self._extract_query(strip_site="github")
        if not query:
            return self.respond(f"What shall I look up on GitHub, {self.honorific}?")

        url = self.site_searches["github"].format(quote_plus(query))
        self._log_query(f"github: {query}", "github", url)

        response = self._maybe_easter_egg() or self._pick_response("github")

        if self._open_browser(url, window_mode=mode):
            self._start_scrape(url, "github", query)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(response)
        return self.respond(self._pick_response("error"))

    def open_url(self) -> str:
        """Open a direct URL"""
        mode = self._prepare_command()
        text = getattr(self, "_last_user_text", "") or ""
        # Remove wake word and command verbs
        url = re.sub(r'\bjarvis\b', '', text, flags=re.IGNORECASE)
        url = re.sub(r'\b(open|go to|navigate to|pull up)\b', '', url, flags=re.IGNORECASE)
        url = url.strip()

        if not url:
            return self.respond(f"What site would you like me to open, {self.honorific}?")

        # Add https:// if it looks like a domain
        if not url.startswith(("http://", "https://")):
            if "." in url and " " not in url:
                url = f"https://{url}"
            else:
                # Treat as search query
                return self.search_web()

        self._log_query(f"direct: {url}", None, url)

        if self._open_browser(url, window_mode=mode):
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(self._pick_response("open_url"))
        return self.respond(self._pick_response("error"))

    def repeat_last_search(self) -> str:
        """Repeat the last search query"""
        mode = self._prepare_command()
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                'SELECT query_text, url FROM web_queries ORDER BY timestamp DESC LIMIT 1'
            )
            result = cursor.fetchone()
            conn.close()

            if result:
                query, url = result
                if url and self._open_browser(url, window_mode=mode):
                    self.conversation.request_follow_up = self.follow_up_duration
                    return self.respond(self._pick_response("repeat"))
                return self.respond(self._pick_response("error"))
            else:
                return self.respond(f"I don't have a previous search to repeat, {self.honorific}.")

        except Exception as e:
            self.logger.error(f"Error repeating search: {e}")
            return self.respond(self._pick_response("error"))

    # ------------------------------------------------------------------
    # Result scraping (Playwright headless)
    # ------------------------------------------------------------------

    def _scrape_results(self, url: str, site_type: str, scroll_count: int = 0):
        """Scrape search results in background thread using headless Playwright.

        Args:
            url: The search results URL to scrape.
            site_type: Site identifier (youtube, google, reddit, etc.).
            scroll_count: Number of times to scroll down for infinite-scroll sites.
                          0 = no scrolling (normal pagination sites).
        """
        try:
            from playwright.sync_api import sync_playwright

            site_config = self._result_selectors.get(site_type)
            if not site_config:
                self.logger.warning(f"No scraper config for site: {site_type}")
                return

            max_results = 50 if scroll_count > 0 else 10

            # Use alternate scrape URL if configured (e.g. old.reddit.com)
            scrape_url_template = site_config.get("scrape_url")
            if scrape_url_template and hasattr(self, '_last_query') and self._last_query:
                scrape_url = scrape_url_template.format(query=quote_plus(self._last_query))
            else:
                scrape_url = url

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )

                page.goto(scrape_url, wait_until="domcontentloaded", timeout=15000)

                # Wait for results to appear
                try:
                    page.wait_for_selector(
                        site_config["wait"], timeout=8000
                    )
                except Exception:
                    self.logger.warning(f"Timeout waiting for {site_type} results")

                # Scroll down for infinite-scroll sites (YouTube, Reddit)
                for i in range(scroll_count):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    self.logger.debug(f"Scroll {i + 1}/{scroll_count} for {site_type}")

                # Extract result links
                elements = page.query_selector_all(site_config["selector"])
                results = []
                seen_urls = set()
                base = site_config["base_url"]

                for el in elements[:max_results]:
                    href = el.get_attribute("href") or ""
                    title = (el.get_attribute("title") or el.inner_text() or "").strip()

                    if not href:
                        continue

                    # Make relative URLs absolute
                    if href.startswith("/") and base:
                        href = base + href

                    # Skip non-result links
                    if href.startswith("javascript:") or href == "#":
                        continue

                    # Deduplicate
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    results.append({"title": title, "url": href})

                browser.close()

            with self._scrape_lock:
                self._last_search_results = results

            self.logger.info(
                f"Scraped {len(results)} results from {site_type}"
                + (f" (after {scroll_count} scrolls)" if scroll_count else "")
            )

        except Exception as e:
            self.logger.error(f"Result scraping failed for {site_type}: {e}")

    def _start_scrape(self, url: str, site_type: str, query: str,
                      page: int = 1):
        """Launch background scrape and update state."""
        with self._scrape_lock:
            self._last_search_results = []
            self._last_search_type = site_type
            self._last_query = query
            self._current_page = page

        # Scroll-based sites get extra scrolls to load more results
        scroll_count = 3 if site_type in self._scroll_sites else 0

        thread = threading.Thread(
            target=self._scrape_results,
            args=(url, site_type, scroll_count),
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------
    # Result selection handler
    # ------------------------------------------------------------------

    def _current_page_results(self) -> List[Dict[str, str]]:
        """Return the result slice for the current page.

        For scroll-based sites (YouTube/Reddit), _last_search_results holds
        all scraped results and we slice by _current_page.  For normal sites,
        all results *are* the current page (scraped per-page).
        """
        results = self._last_search_results
        site_type = self._last_search_type

        if site_type in self._scroll_sites and len(results) > self._results_per_page:
            start = (self._current_page - 1) * self._results_per_page
            end = start + self._results_per_page
            return results[start:end]
        return results

    def _total_local_pages(self) -> int:
        """Total number of local pages for scroll-based sites."""
        n = len(self._last_search_results)
        if n == 0:
            return 0
        return (n + self._results_per_page - 1) // self._results_per_page

    def _extract_number(self, text: str) -> Optional[int]:
        """Extract ordinal or cardinal number from text."""
        text_lower = text.lower()

        # Check word-based numbers
        for word, num in self._ordinals.items():
            if word in text_lower:
                return num

        # Check digit-based: "number 3", "#3", just "3"
        match = re.search(r'(?:number\s*|#)?(\d+)', text_lower)
        if match:
            return int(match.group(1))

        return None

    def select_result(self) -> str:
        """Open the Nth result from the last search (current page)."""
        text = getattr(self, "_last_user_text", "") or ""
        n = self._extract_number(text)

        if n is None:
            return self.respond(f"Which result number would you like, {self.honorific}?")

        with self._scrape_lock:
            results = self._current_page_results()
            site_type = self._last_search_type

        if not results:
            return self.respond(
                f"I'm still loading the results, {self.honorific}. Give me just a moment."
            )

        if n < 1 or n > len(results):
            return self.respond(
                f"I only have {len(results)} results on this page, {self.honorific}. "
                f"Pick a number between one and {len(results)}."
            )

        result = results[n - 1]
        url = result["url"]
        title = result.get("title", "")

        # Site-specific verb
        if site_type == "youtube":
            action = "Playing"
        elif site_type == "amazon":
            action = "Opening"
        else:
            action = "Opening"

        ordinal = self._number_to_ordinal(n)

        if self._open_browser(url):
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(f"{action} the {ordinal} one for you, {self.honorific}.")
        return self.respond(self._pick_response("error"))

    def resize_window(self) -> str:
        """Resize the active browser window (full screen or half screen)."""
        text = (getattr(self, "_last_user_text", "") or "").lower()

        # Determine target mode from the voice command
        full_patterns = [
            r'\bfull\s*screen\b', r'\bfullscreen\b', r'\bmaximize\b',
            r'\bbigger\b', r'\bexpand\b', r'\blarger\b',
        ]
        half_patterns = [
            r'\bhalf\s*screen\b', r'\bsmaller\b', r'\bshrink\b',
            r'\bwindowed\b', r'\bhalf\b', r'\bside\b',
        ]

        target = None
        for p in full_patterns:
            if re.search(p, text):
                target = "fullscreen"
                break
        if target is None:
            for p in half_patterns:
                if re.search(p, text):
                    target = "half_left"
                    break

        # Check for right-side preference
        if target == "half_left" and re.search(r'\bright\b', text):
            target = "half_right"

        # Default to fullscreen if we can't tell
        if target is None:
            target = "fullscreen"

        # wmctrl needs DISPLAY to talk to X11/XWayland
        wm_env = os.environ.copy()
        wm_env.setdefault("DISPLAY", ":0")

        # Find the active browser window via wmctrl
        try:
            result = subprocess.run(
                ["wmctrl", "-l"], capture_output=True, text=True, timeout=3,
                env=wm_env,
            )
            if result.returncode != 0:
                self.logger.error(f"wmctrl -l failed: {result.stderr}")
                return self.respond(self._pick_response("resize_no_window"))

            self.logger.info(f"wmctrl -l output: {result.stdout.strip()}")

            # Look for browser windows (Brave, Chrome, Firefox)
            browser_keywords = ["Brave", "brave", "Chrome", "chrome", "Firefox", "firefox"]
            window_id = None
            for line in reversed(result.stdout.strip().split("\n")):
                if any(kw in line for kw in browser_keywords):
                    window_id = line.split()[0]
                    break

            if not window_id:
                self.logger.warning("No browser window found in wmctrl output")
                return self.respond(self._pick_response("resize_no_window"))

            if target == "fullscreen":
                # Remove any half-screen state, then add fullscreen
                subprocess.run(
                    ["wmctrl", "-i", "-r", window_id, "-b", "remove,maximized_vert,maximized_horz"],
                    capture_output=True, timeout=3, env=wm_env,
                )
                subprocess.run(
                    ["wmctrl", "-i", "-r", window_id, "-b", "add,fullscreen"],
                    capture_output=True, timeout=3, env=wm_env,
                )
                self.conversation.request_follow_up = self.follow_up_duration
                return self.respond(self._pick_response("resize_full"))
            else:
                # Half screen: remove fullscreen, then resize and move
                subprocess.run(
                    ["wmctrl", "-i", "-r", window_id, "-b", "remove,fullscreen"],
                    capture_output=True, timeout=3, env=wm_env,
                )
                half_w = self.screen_width // 2
                x_pos = 0 if target == "half_left" else half_w
                # wmctrl -e: gravity,x,y,width,height (gravity 0 = use default)
                subprocess.run(
                    ["wmctrl", "-i", "-r", window_id, "-e",
                     f"0,{x_pos},0,{half_w},{self.screen_height}"],
                    capture_output=True, timeout=3, env=wm_env,
                )
                self.conversation.request_follow_up = self.follow_up_duration
                return self.respond(self._pick_response("resize_half"))

        except FileNotFoundError:
            self.logger.error("wmctrl not installed — run: sudo apt install wmctrl")
            return self.respond(f"I need wmctrl installed to resize windows, {self.honorific}.")
        except Exception as e:
            self.logger.error(f"Window resize failed: {e}")
            return self.respond(self._pick_response("resize_no_window"))

    def next_page(self) -> str:
        """Navigate to the next page of search results."""
        with self._scrape_lock:
            site_type = self._last_search_type
            query = self._last_query
            page = self._current_page

        if not query or not site_type:
            return self.respond(f"I don't have a recent search to page through, {self.honorific}.")

        # Scroll-based sites: local pagination over cached results
        if site_type in self._scroll_sites:
            with self._scrape_lock:
                total_pages = self._total_local_pages()
                if total_pages == 0:
                    return self.respond(
                        f"I'm still loading the results, {self.honorific}. Give me just a moment."
                    )
                if page >= total_pages:
                    return self.respond(
                        f"That's all {len(self._last_search_results)} results "
                        f"I have, {self.honorific}. No more pages."
                    )
                self._current_page = page + 1
                new_page = self._current_page
                page_results = self._current_page_results()

            self.conversation.request_follow_up = self.follow_up_duration
            count = len(page_results)
            return self.respond(
                f"Page {new_page}, {count} results, {self.honorific}."
            )

        # URL-based pagination for other sites
        paginator = self._pagination.get(site_type)
        if not paginator:
            return self.respond(
                f"I'm not sure how to paginate {site_type} results, {self.honorific}."
            )

        new_page = page + 1
        url = paginator(query, new_page)

        if self._open_browser(url):
            self._start_scrape(url, site_type, query, page=new_page)
            self.conversation.request_follow_up = self.follow_up_duration
            return self.respond(f"Page {new_page}, {self.honorific}.")
        return self.respond(self._pick_response("error"))

    def previous_page(self) -> str:
        """Navigate to the previous page of search results."""
        with self._scrape_lock:
            site_type = self._last_search_type
            query = self._last_query
            page = self._current_page

        if not query or not site_type:
            return self.respond(f"I don't have a recent search to go back on, {self.honorific}.")

        if page <= 1:
            return self.respond(f"We're already on the first page, {self.honorific}.")

        # Scroll-based sites: local pagination
        if site_type in self._scroll_sites:
            with self._scrape_lock:
                self._current_page = page - 1
                new_page = self._current_page
                page_results = self._current_page_results()

            self.conversation.request_follow_up = self.follow_up_duration
            if new_page == 1:
                return self.respond(f"Back to the first page, {self.honorific}.")
            return self.respond(
                f"Page {new_page}, {len(page_results)} results, {self.honorific}."
            )

        # URL-based pagination for other sites
        paginator = self._pagination.get(site_type)
        if not paginator:
            return self.respond(
                f"I'm not sure how to paginate {site_type} results, {self.honorific}."
            )

        new_page = page - 1
        url = paginator(query, new_page)

        if self._open_browser(url):
            self._start_scrape(url, site_type, query, page=new_page)
            self.conversation.request_follow_up = self.follow_up_duration
            if new_page == 1:
                return self.respond(f"Back to the first page, {self.honorific}.")
            return self.respond(f"Page {new_page}, {self.honorific}.")
        return self.respond(self._pick_response("error"))

    def minimize_window(self) -> str:
        """Minimize the active browser window."""
        wm_env = os.environ.copy()
        wm_env.setdefault("DISPLAY", ":0")

        try:
            result = subprocess.run(
                ["wmctrl", "-l"], capture_output=True, text=True, timeout=3,
                env=wm_env,
            )
            if result.returncode != 0:
                return self.respond(self._pick_response("resize_no_window"))

            browser_keywords = ["Brave", "brave", "Chrome", "chrome", "Firefox", "firefox"]
            window_id = None
            for line in reversed(result.stdout.strip().split("\n")):
                if any(kw in line for kw in browser_keywords):
                    window_id = line.split()[0]
                    break

            if not window_id:
                return self.respond(self._pick_response("resize_no_window"))

            # Remove fullscreen first, then minimize via xdotool or wmctrl
            subprocess.run(
                ["wmctrl", "-i", "-r", window_id, "-b", "remove,fullscreen"],
                capture_output=True, timeout=3, env=wm_env,
            )
            # Try xdotool first (most reliable), fall back to wmctrl hidden
            try:
                subprocess.run(
                    ["xdotool", "windowminimize", window_id],
                    capture_output=True, timeout=3, env=wm_env,
                )
            except FileNotFoundError:
                subprocess.run(
                    ["wmctrl", "-i", "-r", window_id, "-b", "add,hidden"],
                    capture_output=True, timeout=3, env=wm_env,
                )
            return self.respond(self._pick_response("minimize"))

        except FileNotFoundError:
            self.logger.error("wmctrl not installed")
            return self.respond(f"I need wmctrl installed to manage windows, {self.honorific}.")
        except Exception as e:
            self.logger.error(f"Window minimize failed: {e}")
            return self.respond(self._pick_response("resize_no_window"))

    def _number_to_ordinal(self, n: int) -> str:
        """Convert number to spoken ordinal."""
        ordinals = {
            1: "the first one", 2: "the second one", 3: "the third one",
            4: "the fourth one", 5: "the fifth one", 6: "the sixth one",
            7: "the seventh one", 8: "the eighth one", 9: "the ninth one",
            10: "the tenth one",
        }
        return ordinals.get(n, f"number {n}")
