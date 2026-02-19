# JARVIS Console Guide

## Starting the Console

```bash
python3 jarvis_console.py              # Text mode (default)
python3 jarvis_console.py --hybrid     # Text input, spoken + printed output
python3 jarvis_console.py --speech     # Launches full voice mode
```

## Slash Commands

| Command | What It Does |
|---------|-------------|
| `/paste` | Enter multi-line paste mode — paste or type text, then press **Esc → Enter** to submit |
| `/file <path>` | Load a file into the document buffer (`--tail` for end of file) |
| `/clipboard` | Load clipboard contents into the buffer (via `wl-paste`) |
| `/append` | Append text to the existing buffer (multi-line mode) |
| `/context` | Show what's currently loaded — source, token count, preview |
| `/clear` | Clear the document buffer |
| `/help` | List all available commands |

## Document Ingestion

The document buffer lets you feed text into JARVIS for analysis — logs, code, articles, error output, whatever you need examined.

### Loading a document

There are four ways to load content:

**Paste mode** — `/paste` opens multi-line editing:

```
You > /paste
Paste mode — type or paste text. Press Esc then Enter to submit, Ctrl+C to cancel.
paste> [paste your content here]
```

- **Enter** inserts a newline (so you can paste multi-line content)
- **Esc then Enter** submits the text
- **Ctrl+C** cancels

**Load a file** — `/file <path>`:

```
You > /file ~/jarvis/core/stt.py
╭─ Document Buffer ──────────────────────────╮
│ Loaded ~1,200 tokens, 85 lines, 4,521 bytes│
│ (file:stt.py)                              │
╰────────────────────────────────────────────╯
```

- Paths expand `~` and resolve to absolute
- Binary files (images, archives, models, etc.) are rejected
- Files over 500KB load with a warning (auto-truncated to token budget)
- Use `--tail` to load the end of a file instead of the beginning (useful for logs)

**Load from clipboard** — `/clipboard`:

```
You > /clipboard
╭─ Document Buffer ─────────────────╮
│ Loaded ~340 tokens, 12 lines      │
│ (clipboard)                       │
╰───────────────────────────────────╯
```

Reads from Wayland clipboard via `wl-paste`. Requires `wl-clipboard` (`sudo apt install wl-clipboard`).

**Drag and drop** — drag a file from Nautilus into the terminal:

The terminal pastes the file path as text. JARVIS auto-detects absolute file paths and loads them as if you typed `/file`.

### Appending to the buffer

Use `/append` to add more text to an existing buffer without replacing it:

```
You > /append
Append mode — type or paste text. Press Esc then Enter to submit, Ctrl+C to cancel.
append> [additional content]
```

The new text is added after the existing content with a blank line separator.

### Asking questions about it

Once loaded, just ask naturally. The document stays attached to every query until you clear it:

```
You > what does this code do?
You > are there any bugs in this?
You > summarize the key points
You > rewrite this in Python
```

### Checking what's loaded

```
You > /context
╭─ Document Context ─╮
│ Source: paste       │
│ Tokens: ~820 / 4000│
│ Size: 2,341 chars   │
│ ...preview...       │
╰─────────────────────╯
```

### Clearing the buffer

```
You > /clear
Cleared document buffer (paste, ~820 tokens)
```

The buffer also clears automatically when you exit the console.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **Up/Down** | Navigate command history |
| **Ctrl+R** | Reverse search through history |
| **Tab** | Auto-complete slash commands and file paths |
| **Ctrl+C** | Cancel current input |
| **Ctrl+D** | Exit console |

Command history persists across sessions automatically.

Tab completion works for:
- Slash commands: type `/` then Tab to see all commands, or `/fi` Tab to complete `/file`
- File paths after `/file`: type `/file ~/` then Tab to browse directories

## The Bottom Toolbar

The toolbar at the bottom of the terminal updates dynamically:

- **No document loaded:** Shows available commands (`/paste`, `/file`, `/help`)
- **Document loaded:** Shows buffer status (`DocCtx: ~820 tok (paste) — /context to view, /clear to remove`)

## The Stats Panel

After every command, a stats panel shows routing info, timing, and LLM token usage. When a document is loaded, it also shows:

```
DocCtx  ~820 tok (paste)
```

## Tips

- The document is **never saved** to chat history — it only lives in memory for the current session
- Large documents are automatically truncated to ~4000 tokens (roughly 3000 words) to leave room for the LLM's context window
- You can load a new document at any time with `/paste` or `/file` — it replaces the previous one
- Use `/append` to add to the buffer instead of replacing it
- When a document is loaded, all queries go to the LLM (skill routing is bypassed) so JARVIS focuses on your document. Use `/clear` to return to normal skill routing.
