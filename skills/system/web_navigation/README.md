# Web Navigation Skill

Intelligent web browsing with search capabilities, query tracking, and personality.

## Features

### 🔍 Search Engines
- **Google** (default)
- **DuckDuckGo**
- **Bing**

### 🎯 Site-Specific Searches
- **YouTube** - Video searches
- **Amazon** - Product searches
- **Wikipedia** - Encyclopedia lookups
- **Reddit** - Community searches
- **GitHub** - Code searches

### 🕵️ Incognito Mode
Automatically uses incognito/private browsing for non-authenticated sites:
- **Incognito:** Shopping, searches, general browsing
- **Normal mode:** Gmail, Calendar, Keep, Drive, authenticated sites

### 📊 Query Tracking
- Stores last 100 queries in SQLite database
- Tracks query text, timestamps, URLs clicked
- "Show me that again" repeats last search

### 🎭 Personality Touches
Witty responses for silly queries:
- "Right away sir, since I wasn't doing anything else"
- "I hope this is work-related"

## Commands

### General Search
```
"Jarvis, search for quantum computing"
"Jarvis, google best pizza near me"
"Jarvis, look up weather forecast"
```

### YouTube
```
"Jarvis, search YouTube for 2017 jeep alignment"
"Jarvis, show me YouTube results for cooking tutorials"
"Jarvis, find cat videos on YouTube"
```

### Amazon
```
"Jarvis, look up 2TB SSDs on Amazon"
"Jarvis, search Amazon for standing desk"
"Jarvis, find noise cancelling headphones on Amazon"
```

### Wikipedia
```
"Jarvis, search Wikipedia for quantum mechanics"
"Jarvis, look up Abraham Lincoln on Wikipedia"
```

### Reddit
```
"Jarvis, search Reddit for home automation"
"Jarvis, find programming tips on Reddit"
```

### Direct URLs
```
"Jarvis, open github.com"
"Jarvis, go to reddit.com"
"Jarvis, navigate to youtube.com"
```

### Repeat Last Search
```
"Jarvis, show me that again"
"Jarvis, open that again"
```

## User Preferences

Located in `/mnt/storage/jarvis/data/user_preferences.yaml`:

```yaml
web_navigation:
  browser: "chrome"              # chrome, firefox, brave, edge
  search_engine: "google"        # google, duckduckgo, bing
  monitor: "primary"             # primary, secondary
  incognito_mode: "non_authenticated"  # always, never, non_authenticated
  analysis_level: "basic"        # none, basic, full
  shopping_site: "amazon"
  video_site: "youtube"
  auto_fullscreen: true
  result_announcement: "brief"   # brief, detailed
```

## Query Database

**Location:** `/mnt/storage/jarvis/data/web_queries.db`

**Schema:**
```sql
CREATE TABLE web_queries (
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
);
```

**Retention:** Last 100 queries (rolling window)

## Browser Support

### Chrome (Default)
- Incognito: `--incognito`
- Fullscreen: `--start-fullscreen`

### Firefox
- Private: `--private-window`
- Fullscreen: `--start-fullscreen`

### Brave
- Incognito: `--incognito`
- Fullscreen: `--start-fullscreen`

### Edge
- Private: `--inprivate`
- Fullscreen: `--start-fullscreen`

## Authenticated Sites (No Incognito)

These sites always open in normal mode:
- Gmail / Google Mail
- Google Calendar
- Google Keep
- Google Drive
- Apple Music
- Facebook
- Twitter

## Future Enhancements

### Phase 2 Features (Not Yet Implemented)
- **Result scraping:** Basic analysis of search results
- **Price comparison:** For shopping queries
- **Monitor selection:** Multi-monitor support
- **Session management:** Remember open tabs
- **Smart recommendations:** Learn preferences over time

## Examples

### Simple Search
```
User: "Jarvis, search for best coffee makers"
Jarvis: "Searching now, sir."
[Opens Google search in Chrome incognito fullscreen]
```

### Silly Query (With Personality)
```
User: "Jarvis, show me kitten videos on YouTube"
Jarvis: "Right away sir, since I wasn't doing anything else."
[Opens YouTube search for kitten videos]
```

### Product Search
```
User: "Jarvis, look up 2TB SSDs on Amazon"
Jarvis: "Searching Amazon, sir."
[Opens Amazon search for 2TB SSDs]
```

### Repeat Last Search
```
User: "Jarvis, show me that again"
Jarvis: "Opening that again, sir."
[Reopens last URL]
```

## Design Philosophy

**Speed First:**
- Instant browser opening (no delays)
- Pre-configured preferences (no questions)
- Minimal conversation

**Smart Defaults:**
- Incognito for privacy
- Fullscreen for immersion
- Site-specific searches

**Conversational:**
- Natural language patterns
- Personality for engagement
- Brief confirmations

## Technical Notes

### Browser Detection
Skill checks for browser binary existence:
- `google-chrome`
- `firefox`
- `brave-browser`
- `microsoft-edge`

Falls back to `google-chrome` if preferred browser not found.

### URL Parsing
- Detects if input is URL or search query
- Adds `https://` to bare domains
- Treats multi-word input as search query

### Process Management
Browsers launched as detached processes:
```python
subprocess.Popen(
    cmd,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True
)
```

This prevents Jarvis from blocking on browser execution.

## Troubleshooting

**Browser doesn't open:**
- Check browser is installed
- Verify binary path in `self.browsers` dict
- Check system PATH

**Incognito not working:**
- Verify browser supports incognito flag
- Check preference setting in user_preferences.yaml

**Query history not saving:**
- Check database permissions
- Verify `/mnt/storage/jarvis/data/` exists
- Check disk space

## Integration Points

**Works With:**
- Email skill (opens Gmail)
- Music skill (opens Apple Music)
- News skill (opens news articles)
- Threat hunting (opens analysis sites)

**Database Shared:**
- Query history available to other skills
- Pattern learning for recommendations
