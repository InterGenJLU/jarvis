# Document Ingestion for JARVIS Console

## Context

the user uses Claude Code daily to paste logs, code, errors, and articles for AI analysis. JARVIS console currently only accepts single-line `input()` via readline — no way to paste multi-line text, read files, or ingest clipboard contents. This feature bridges that gap, turning JARVIS from "voice assistant you talk at" into "AI analyst you can feed data to."

## Approach: prompt_toolkit + DocumentBuffer + slash commands

**Terminal library:** `prompt_toolkit` 3.0.52 (the library behind IPython/Jupyter). Gets us multi-line editing, paste support, file path completion, and a persistent toolbar — all out of the box.

**Document buffer:** Simple in-memory string buffer that persists until cleared. When active, document content is prepended to the user's query in `<document>` tags before sending to the LLM. No changes needed to `llm_router.py`.

**Slash commands:** `/paste`, `/file`, `/clipboard`, `/context`, `/clear`, `/append`, `/help`

---

## Phase 1: Terminal Library Migration + /paste + Document Buffer

**Files:** `jarvis_console.py` only

### 1.1 Install dependency
```bash
pip install prompt_toolkit  # or alternative TBD
```

### 1.2 Add DocumentBuffer class (after SessionStats, ~line 106)
```python
@dataclass
class DocumentBuffer:
    content: str = ""
    source: str = ""        # "paste", "file:name.py", "clipboard"
    token_estimate: int = 0
    max_tokens: int = 4000  # Leaves room for system prompt + history + response in 8K ctx

    def load(self, text: str, source: str = "paste"):
        self.content = text
        self.source = source
        self.token_estimate = estimate_tokens(text)

    def clear(self):
        self.content = ""
        self.source = ""
        self.token_estimate = 0

    @property
    def active(self) -> bool:
        return bool(self.content)

    def build_augmented_message(self, user_query: str) -> str:
        if not self.content:
            return user_query
        return f"<document>\n{self.content}\n</document>\n\n{user_query}"

    def truncate_to_budget(self) -> bool:
        if self.token_estimate <= self.max_tokens:
            return False
        words = self.content.split()
        target_words = int(self.max_tokens / TOKEN_RATIO)
        self.content = " ".join(words[:target_words])
        self.token_estimate = estimate_tokens(self.content)
        self.source += " (truncated)"
        return True
```

Import: `from core.context_window import estimate_tokens, TOKEN_RATIO`

### 1.3 Replace readline with new terminal library

**Current state (line 22, 317-322, 339, 361, 566):**
- `import readline` + `readline.set_history_length(1000)` + `readline.read_history_file()`
- `command = input("You > ").strip()`
- `readline.write_history_file()` on exit

**Replace with:** `prompt_toolkit.PromptSession` + `FileHistory` + `KeyBindings`

**What we get:**
- Persistent command history via `FileHistory` (auto-saves, cross-session)
- Multi-line input mode (for /paste — Enter = newline, Esc+Enter = submit)
- Bottom toolbar (dynamic — shows buffer status or command hints)
- Ctrl+R reverse history search
- Tab completion (for /file paths and slash commands — Phase 3)
- Coexists with Rich library (used for output formatting + stats panel)

### 1.4 Add slash command dispatcher (before wake-word stripping, after line 343)

```python
if command.startswith("/"):
    handled = _handle_slash_command(command, doc_buffer, console, session)
    if handled:
        continue
```

Dispatcher routes to: `/paste`, `/file`, `/clipboard`, `/context`, `/clear`, `/append`, `/help`

### 1.5 Implement /paste

Multi-line input mode — Enter = newline, Ctrl+D = submit, Ctrl+C = cancel. Loads into `doc_buffer.load(text, "paste")`. Shows Rich Panel with token count and preview.

### 1.6 Inject document context into LLM queries

**Single change at line 528** (before `_stream_llm_console` call):
```python
llm_command = command
# ... existing fact-extraction logic ...

# Document buffer injection
if doc_buffer.active:
    llm_command = doc_buffer.build_augmented_message(llm_command)
```

Document content is NEVER persisted to chat_history.jsonl — `conversation.add_message("user", command)` at line 374 receives the raw command, not the augmented one.

### 1.7 Add DocCtx to stats panel

In `render_stats()`, add before the Session line:
```python
if doc_buffer and doc_buffer.active:
    pairs.append(("DocCtx", f"~{doc_buffer.token_estimate} tok ({doc_buffer.source})"))
```

---

## Phase 2: /file and /clipboard Commands

**Files:** `jarvis_console.py` only

### 2.1 /file command
- Accepts path, expands `~`, resolves to absolute
- Rejects binary files (`.exe`, `.png`, `.gguf`, `.npy`, etc. — blocklist)
- 500KB sanity limit (warn but still read)
- `--tail` flag: keeps end of file instead of beginning (useful for logs)
- Loads into `doc_buffer.load(text, f"file:{name}")`
- Shows Rich Panel: token count, line count, file size
- **Drag-and-drop auto-detect:** If raw input is a valid file path (and nothing else), treat as implicit `/file`. Enables dragging files from Nautilus into the terminal.

### 2.2 /clipboard command
- Calls `wl-paste --no-newline` (subprocess, 3s timeout)
- Loads into `doc_buffer.load(text, "clipboard")`
- Shows Rich Panel with preview

### 2.3 /append command
- Like /paste but appends to existing buffer: `doc_buffer.content + "\n\n" + new_text`

### 2.4 /context and /clear commands
- `/context`: Shows source, token count, chars, lines, 500-char preview in Rich Panel
- `/clear`: Clears buffer, shows confirmation with what was removed

### 2.5 /help command
- Lists all slash commands with descriptions

---

## Phase 3: Polish — Token Budget, LLM Hints, Tab Completion

**Files:** `jarvis_console.py` only

### 3.1 Document-aware system prompt hint
When buffer is active, inject via `memory_context`:
```python
if doc_buffer.active:
    doc_hint = "The user has loaded a document. Refer to <document> tags in their message. Be specific."
    memory_context = f"{doc_hint}\n\n{memory_context}" if memory_context else doc_hint
```

### 3.2 Tab completion for /file paths
- Slash command names autocomplete after `/`
- File paths autocomplete after `/file ` or `/f `

### 3.3 Dynamic max_tokens
When document buffer is active, pass explicit `max_tokens=600` to allow longer analytical responses. Add parameter to `_stream_llm_console()`.

---

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Terminal lib | **prompt_toolkit** 3.0.52 | Multi-line, completion, toolbar, FileHistory, Rich-compatible |
| Context injection | Prepend to user_message | No LLM router changes, survives overflow trimming, works with Qwen + Claude |
| Document format | `<document>` XML tags | Both Qwen and Claude handle XML tags well |
| Buffer persistence | Until `/clear` | Enables follow-up questions about same document |
| History storage | Raw command only | Document content never enters chat_history.jsonl |
| Token counting | `estimate_tokens()` from context_window | Already battle-tested, no new dependency |
| Max document size | 4000 tokens (~3000 words) | Conservative: leaves ~4K for system + history + response |

## Critical Files

| File | Action |
|------|--------|
| `jarvis_console.py` | **PRIMARY** — all changes go here |
| `core/context_window.py` | Import `estimate_tokens`, `TOKEN_RATIO` (read-only) |
| `core/llm_router.py` | **No changes** — understand `stream()` message assembly |
| `core/conversation.py` | **No changes** — verify raw command goes to `add_message()` |

## Testing

**Phase 1:**
1. `python3 jarvis_console.py` — prompt works, history persists
2. `/paste` → paste multi-line text → Ctrl+D → verify token count
3. Ask "what is this?" → verify LLM receives document
4. Ask follow-up → verify document persists
5. `/clear` → verify buffer clears
6. Normal commands still work: "what time is it?", "weather"

**Phase 2:**
1. `/file core/speech_chunker.py` → verify loads with line/token count
2. "Explain this code" → verify LLM answers about the file
3. `/file /nonexistent` → error message
4. `/file model.gguf` → binary rejection
5. `/clipboard` → verify clipboard read
6. `/context` → shows preview

**Phase 3:**
1. Load large file → verify truncation warning
2. Tab complete `/fi` → `/file`, then path completion
3. Verify chat_history.jsonl has NO document content

---

## Status: Phase 1 COMPLETE — prompt_toolkit chosen and implemented
