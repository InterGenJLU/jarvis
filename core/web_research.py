"""
Web Research Module

Provides web search (DuckDuckGo) and page content extraction (trafilatura)
for LLM-driven web research via tool calling.

Thread-safe, error-resilient — never crashes the pipeline.
"""

import time
import threading
from typing import Optional

from core.logger import get_logger


class _TTLCache:
    """Simple thread-safe TTL cache."""

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
            if entry and (time.time() - entry[1]) < self._ttl:
                return entry[0]
            # Expired or missing
            self._data.pop(key, None)
            return None

    def put(self, key: str, value):
        with self._lock:
            self._data[key] = (value, time.time())

    def clear(self):
        with self._lock:
            self._data.clear()


class WebResearcher:
    """Web search and page content extraction for LLM tool calling."""

    def __init__(self, config=None):
        self.logger = get_logger(__name__, config)
        self._search_cache = _TTLCache(ttl_seconds=300)   # 5 min
        self._page_cache = _TTLCache(ttl_seconds=600)      # 10 min
        self._last_search_time = 0.0
        self._rate_limit_gap = 2.0  # seconds between searches
        self.logger.info("WebResearcher initialized")

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search the web via DuckDuckGo.

        Args:
            query: Search query string
            max_results: Maximum number of results (default 5)

        Returns:
            List of dicts with keys: title, url, snippet
            Empty list on error.
        """
        # Check cache
        cache_key = f"{query}:{max_results}"
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            self.logger.debug(f"Search cache hit: {query!r}")
            return cached

        # Rate limiting
        elapsed = time.time() - self._last_search_time
        if elapsed < self._rate_limit_gap:
            time.sleep(self._rate_limit_gap - elapsed)

        try:
            from ddgs import DDGS

            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    })

            self._last_search_time = time.time()
            self._search_cache.put(cache_key, results)
            self.logger.info(f"Web search: {query!r} → {len(results)} results")
            return results

        except Exception as e:
            self.logger.error(f"Web search failed: {e}")
            self._last_search_time = time.time()
            return []

    def fetch_page(self, url: str, max_chars: int = 4000) -> Optional[str]:
        """Fetch and extract main content from a web page.

        Uses trafilatura for content extraction with requests fallback.

        Args:
            url: Page URL to fetch
            max_chars: Maximum characters to return (default 4000)

        Returns:
            Extracted text content, or None on failure.
        """
        # Check cache
        cached = self._page_cache.get(url)
        if cached is not None:
            self.logger.debug(f"Page cache hit: {url}")
            return cached[:max_chars]

        try:
            import trafilatura

            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                self.logger.warning(f"Page fetch returned nothing: {url}")
                return None

            text = trafilatura.extract(downloaded, include_links=False,
                                       include_tables=True,
                                       include_comments=False)
            if not text:
                self.logger.warning(f"Content extraction empty: {url}")
                return None

            # Cache full text, return truncated
            self._page_cache.put(url, text)
            self.logger.info(f"Page fetched: {url} ({len(text)} chars)")
            return text[:max_chars]

        except Exception as e:
            self.logger.error(f"Page fetch failed ({url}): {e}")
            return None

    def clear_cache(self):
        """Clear all caches (e.g. when conversation window closes)."""
        self._search_cache.clear()
        self._page_cache.clear()


def format_search_results(results: list[dict]) -> str:
    """Format search results as numbered text for LLM context.

    Args:
        results: List of search result dicts (title, url, snippet)

    Returns:
        Formatted string like:
        [1] Title - url
        Snippet text...

        [2] Title - url
        ...
    """
    if not results:
        return ("No search results found. "
                "Tell the user you were unable to find current information on this topic. "
                "Do NOT guess or make up an answer.")

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']} - {r['url']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()
