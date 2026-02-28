# Extending JARVIS — Tools & Skills Development Guide

> **For:** Anyone adding new functionality to JARVIS
> **Updated:** February 27, 2026

---

## Overview: Two Extension Mechanisms

JARVIS has two ways to add new capabilities. Choosing the right one depends on the **nature** of your feature, not on preference — each serves a fundamentally different purpose.

| | **Tool** | **Skill** |
|---|---|---|
| **Purpose** | Give the LLM access to external data or actions | Self-contained module with its own control flow |
| **Who drives** | The LLM decides when to call it | The skill drives the interaction |
| **State** | Stateless (query → response) | Can be stateful (multi-turn, confirmations, state machines) |
| **Complexity** | One Python file in `core/tools/` | Directory with `skill.py`, `metadata.yaml`, `__init__.py` |
| **Routing** | LLM selects via tool-calling (P4-LLM in priority chain) | Semantic intent matching (P4 in priority chain) |
| **Examples** | Time, weather, system info, file search, news headlines | App launcher, document generation, social introductions |
| **Adding one** | Create one `.py` file — auto-discovered | Create a skill directory with 3 files |

### When to Use Which

**Create a Tool when:**
- The feature answers a question using external data ("What's the weather?", "What time is it?")
- The interaction is stateless — one request, one response
- The LLM should decide when to use it based on natural language
- The response data needs LLM formatting (the tool returns raw data, the LLM makes it conversational)

**Create a Skill when:**
- The feature needs its own control flow (multi-turn conversations, confirmation dialogs)
- It manages complex state (state machines, pending actions, timeouts)
- It interacts with the desktop environment (window management, app launching, clipboard)
- It runs nested LLM calls internally (document generation pipelines)
- The interaction pattern doesn't fit a simple function call

**Hybrid approach** — some features benefit from both:
- A **tool** for LLM-driven queries (e.g., "git status" → developer_tools tool)
- A **skill** for complex interactions (e.g., safety-classified command execution with confirmation)
- The tool handles the common path; the skill handles the stateful edge cases

---

## Creating a Tool

Tools are the preferred way to add most new functionality. Each tool is a single Python file in `core/tools/` that the registry auto-discovers at startup.

### How It Works

1. You create a `.py` file in `core/tools/`
2. The registry (`core/tool_registry.py`) auto-discovers it at import time
3. The tool's schema is presented to the LLM when relevant queries come in
4. The LLM decides whether to call your tool based on the user's request
5. Your handler runs and returns data
6. The LLM formats the data into a natural response

### Step 1: Create the Tool File

Create `core/tools/your_tool.py` with these required attributes:

```python
"""Tool definition: your_tool — brief description."""

TOOL_NAME = "your_tool"        # OpenAI function name (used in tool calls)
SKILL_NAME = "your_skill"      # Semantic matcher skill name (for pruning), or None

SCHEMA = {
    "type": "function",
    "function": {
        "name": "your_tool",
        "description": (
            "Clear description of what this tool does. "
            "Be specific about WHEN to call it — the LLM uses this "
            "to decide whether your tool is relevant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "param_name": {
                    "type": "string",
                    "description": "What this parameter controls"
                }
            },
            "required": ["param_name"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For ANY question about [topic], call your_tool. "
    "NEVER answer [topic] questions from your own knowledge."
)


def handler(args: dict) -> str:
    """Execute the tool and return plain-text data for the LLM."""
    param = args.get("param_name", "default")
    # Your logic here
    return f"Result: {param}"
```

### Real Example: get_time.py

The simplest tool — a good starting point:

```python
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
                    "description": "Set true ONLY when the user explicitly asks for the date."
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
    hour = now.hour % 12 or 12
    minute = now.minute
    period = "AM" if now.hour < 12 else "PM"
    time_str = f"{hour}:{minute:02d} {period}"

    if args.get("include_date", False):
        day_name = now.strftime("%A")
        month_name = now.strftime("%B")
        return f"Current time: {time_str}. Date: {day_name}, {month_name} {now.day}, {now.year}."
    return f"Current time: {time_str}."
```

### Required Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `TOOL_NAME` | `str` | OpenAI function name. Must match `SCHEMA.function.name`. |
| `SKILL_NAME` | `str` or `None` | Maps to a skill name for the semantic pruner. Set `None` if the tool isn't associated with a skill. |
| `SCHEMA` | `dict` | OpenAI-compatible tool schema. The `description` field is critical — the LLM uses it to decide when to call your tool. |
| `SYSTEM_PROMPT_RULE` | `str` | Injected into the LLM system prompt when your tool is active. Use prescriptive language ("ALWAYS call X", "NEVER answer from knowledge"). |
| `handler` | `function` or `None` | `handler(args: dict) -> str`. Returns plain-text data. Set to `None` for tools dispatched by the frontend (e.g., `web_search`). |

### Optional Attributes

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `ALWAYS_INCLUDED` | `bool` | `False` | If `True`, this tool is always in the LLM's tool list regardless of semantic pruning. Used for `web_search`. |
| `DEPENDENCIES` | `dict` | `{}` | Maps dependency names to module-level variable names for runtime injection. |

### Dependency Injection

Tools that need runtime objects (database connections, manager instances, config) declare dependencies:

```python
DEPENDENCIES = {"reminder_manager": "_reminder_manager"}
_reminder_manager = None  # Injected at runtime by the registry

def handler(args: dict) -> str:
    if not _reminder_manager:
        return "Error: reminder system not available"
    return _reminder_manager.list_reminders()
```

The registry's `inject_dependencies()` is called during startup by each frontend (pipeline, console, web) with the actual objects.

### Schema Design Tips

The schema description is the most important part of a tool — it determines whether the LLM calls your tool at the right time.

- **Be specific about when to call it.** "Get current weather, forecast, or rain check" is better than "Weather tool."
- **List the trigger phrases.** "Call this for any question about time, date, day of the week, or year."
- **Use enum parameters** for action types — they constrain the LLM's choices and reduce errors.
- **Make descriptions prescriptive** in `SYSTEM_PROMPT_RULE` — "MUST call X" and "NEVER answer from knowledge" work better than "consider calling X."
- **Keep parameter descriptions clear** — the LLM reads these to decide what arguments to pass.

### Testing Your Tool

1. **Edge case tests** — Add test queries to `scripts/test_edge_cases.py` to verify routing
2. **Tool-calling harness** — Add queries to `scripts/test_tool_calling.py` with your tool name as the expected category
3. **Voice test** — Speak to JARVIS and verify the tool is called correctly

```bash
# Run edge case tests
python3 -u scripts/test_edge_cases.py --verbose > /tmp/test_output.txt 2>&1

# Run tool-calling tests
python3 -u scripts/test_tool_calling.py --runs 1 --verbose > /tmp/test_output.txt 2>&1
```

### Tool File Location

```
core/tools/
├── __init__.py          # Package docstring
├── web_search.py        # Web search (ALWAYS_INCLUDED, handler=None)
├── get_time.py          # Time and date queries
├── get_system_info.py   # CPU, memory, disk, hardware info
├── find_files.py        # File search, line counting, directory listing
├── get_weather.py       # Current weather, forecast, rain check
├── manage_reminders.py  # Reminder CRUD (add, list, cancel, acknowledge, snooze)
├── developer_tools.py   # Git, codebase search, system admin, shell execution
└── get_news.py          # News headline reading and counting
```

---

## Creating a Skill

Skills are self-contained modules for features that need their own control flow — multi-turn conversations, confirmation dialogs, desktop integration, or complex state management.

### When to Create a Skill

- **Multi-turn state machines** — Social introductions walks through a conversation flow (ask name → ask relationship → confirm → store)
- **Confirmation flows** — File editor asks "overwrite existing file?" and waits for yes/no
- **Desktop interaction** — App launcher manages windows, volumes, workspaces via D-Bus
- **Nested LLM pipelines** — Document generation runs a two-stage LLM pipeline (parse request → generate content)

### Skill Directory Structure

```
/mnt/storage/jarvis/skills/{category}/{skill_name}/
├── __init__.py      # from .skill import YourSkill
├── skill.py         # Class extending BaseSkill
└── metadata.yaml    # Name, version, category, keywords
```

Categories: `system/` (system-level utilities) or `personal/` (user-specific features).

### Step 1: Create metadata.yaml

```yaml
name: my_new_skill
version: 1.0.0
description: Brief description of what this skill does
author: Your Name
category: system  # or 'personal'
dependencies: []  # Python packages needed
```

### Step 2: Create __init__.py

```python
from .skill import MyNewSkill
```

### Step 3: Create skill.py

```python
from core.base_skill import BaseSkill
from typing import Dict, Any

class MyNewSkill(BaseSkill):
    """Brief description of your skill."""

    def initialize(self) -> bool:
        """Register intents and initialize. Called once at startup."""

        self.register_semantic_intent(
            examples=[
                "example phrase 1",
                "example phrase 2",
                "example phrase 3",
                "example phrase 4"
            ],
            handler=self.my_handler,
            threshold=0.75
        )

        self.logger.info("MyNewSkill initialized")
        return True  # CRITICAL: Must return True

    # NOTE: handle_intent() has a default implementation that dispatches
    # to the handler registered above via semantic_intents dict.
    # Override only if you need custom dispatch logic.

    def my_handler(self, entities: dict) -> str:
        """Handle the matched intent."""
        user_text = entities.get('original_text', '')
        try:
            result = "Processing..."
            return f"Done, {self.honorific}. {result}"
        except Exception as e:
            self.logger.error(f"Error: {e}")
            return f"I encountered an error, {self.honorific}."
```

### Semantic Intent Registration

Skills use sentence-transformer embeddings for flexible natural language matching:

```python
self.register_semantic_intent(
    examples=[
        "how many lines of code in the codebase",
        "count lines in jarvis",
        "how big is the project",
        "show me code statistics"
    ],
    handler=self.count_code_lines,
    threshold=0.75  # Similarity score (0.0-1.0)
)
```

**Threshold guidelines:**
- `0.85+`: Very strict, near-exact phrasing
- `0.75-0.84`: Standard, similar phrasing
- `0.70-0.74`: Flexible, related concepts
- `<0.70`: Too loose, may false-match

**Tips:**
- Provide 3-5 diverse example phrases per intent
- Focus on how real users would phrase the request
- Test with actual voice commands — spoken language differs from written

### Default handle_intent Dispatch

`BaseSkill.handle_intent()` has a concrete default implementation that routes to the handler registered via `register_semantic_intent()`. You only need to override it if your skill has custom dispatch logic (like checking confirmation state before routing).

### Skill Deployment

```bash
# Restart JARVIS to load new skill
systemctl --user restart jarvis

# Watch for loading confirmation
journalctl --user -u jarvis -f | grep "my_new_skill"

# Look for:
# ✅ Loaded skill: my_new_skill (system)
```

### Existing Skills

```
system/
├── app_launcher/       # Desktop control (16 intents: apps, windows, volume, workspaces, clipboard)
├── developer_tools/    # Codebase search, git, shell — has companion tool for LLM queries
├── file_editor/        # File ops + document generation (PPTX/DOCX/PDF), confirmation flows
├── filesystem/         # Find files, count lines — has companion tool for LLM queries
├── system_info/        # CPU, memory, system info — has companion tool for LLM queries
├── time_info/          # Time and date — has companion tool for LLM queries
├── weather/            # Current weather, forecasts — has companion tool for LLM queries
└── web_navigation/     # Web search & browsing (result selection, page nav, scroll pagination)

personal/
├── news/               # Voice headline delivery — has companion tool for LLM queries
├── reminders/          # Voice reminders, rundowns, Google Calendar — has companion tool for LLM queries
└── social_introductions/  # Butler-style introductions, people DB, multi-turn state machine
```

Skills marked "has companion tool" have their query-handling logic in `core/tools/` while the skill directory provides initialization, semantic intents, and any complex interaction logic.

---

## Hybrid: Skills with Tools

Some features naturally span both mechanisms. The pattern:

- **Tool** handles the common path — the LLM routes "what's the weather?" to `get_weather` tool, gets data, formats a response
- **Skill** provides the infrastructure — initialization, semantic intent registration for the pruner, and any complex logic the tool can't handle

**Example: developer_tools**
- The `core/tools/developer_tools.py` tool handles 13 actions (git status, codebase search, system health, etc.)
- The skill directory (`skills/system/developer_tools/`) provides the safety classification module (`_safety.py`) and "show me" visual output
- The tool's `confirm_pending` action ties into the skill's confirmation flow

When you migrate a skill to a tool, you don't delete the skill — you move the query-handling logic to a tool file and let the skill handle everything the tool can't.

---

## Architecture Reference

### How Requests Route to Tools and Skills

The conversation router (`core/conversation_router.py`) processes requests through a priority chain. The relevant layers for tools and skills:

```
...
P4-LLM:  Tool calling — semantic pruner selects relevant tools,
         LLM decides which to call via stream_with_tools()
P4:      Skill routing — semantic intent matching for non-tool skills
         (app_launcher, file_editor, social_introductions)
...
P7:      LLM fallback — no tool or skill matched
```

**Tool selection flow:**
1. Semantic pruner scores all skills against the user's query
2. Top-scoring skills that have companion tools get their tools included
3. Always-included tools (web_search) are added
4. LLM receives the pruned tool list + system prompt rules
5. LLM decides: call a tool, or answer directly
6. If a tool is called, the handler runs, LLM formats the result

**Skill selection flow (for non-tool skills):**
1. Skill manager tries exact/fuzzy regex, keyword matching, then semantic matching
2. Best-matching skill's `handle_intent()` is called
3. Skill returns response text directly

### Tool Registry

`core/tool_registry.py` auto-discovers tool files at import time and builds:

- `TOOL_HANDLERS` — tool_name → handler function
- `SKILL_TOOLS` — tool_name → schema (skill-gated tools only)
- `ALL_TOOLS` — all tool schemas including always-included
- `TOOL_SKILL_MAP` — skill_name → tool_name (for the semantic pruner)
- `build_tool_prompt_rules(active_tool_names)` — assembles numbered LLM rules for the active set
- `execute_tool(name, args)` — dispatches to handler
- `inject_dependencies(deps)` — wires runtime objects into tool modules

### Testing

| Test Suite | Command | What It Tests |
|-----------|---------|---------------|
| Edge cases | `python3 -u scripts/test_edge_cases.py --verbose` | 266 routing + unit tests across 9 phases |
| Tool calling | `python3 -u scripts/test_tool_calling.py --runs 1 --verbose` | 175 queries, verifies LLM selects correct tool |
| Voice pipeline | `python3 scripts/test_voice_pipeline.py --verbose` | 25 TTS→STT round-trip pronunciation tests |

**Important:** Always use `--verbose`, always redirect to a temp file (`> /tmp/test_output.txt 2>&1`), and never run test suites in parallel.

---

## Feature Ideas (Not Yet Implemented)

- **Email (Gmail)** — voice-composed email via Gmail API + OAuth
- **Google Keep** — shared grocery/todo lists
- **Audio Recording** — voice memos, meeting notes
- **Music Control** — playlist management via Apple Music
- **Network Awareness** — device discovery, threat detection
- **Vision/OCR** — screenshot reading via Tesseract or Qwen3.5 mmproj

See `docs/PRIORITY_ROADMAP.md` for the full prioritized backlog.

---

**Remember:** Tools make JARVIS smarter by giving the LLM access to live data. Skills make JARVIS capable by handling complex interactions the LLM can't drive alone. Choose the right mechanism for your feature, test thoroughly, and keep it focused.
