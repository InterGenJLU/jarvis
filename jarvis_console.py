#!/usr/bin/env python3
"""
JARVIS Console — Keyboard interaction mode.

Bypasses STT/TTS and feeds typed text directly into the skill pipeline.
Stats panel shows match layer, skill, confidence, timing, and token counts.

Usage:
    python jarvis_console.py              # Text mode (default)
    python jarvis_console.py --text       # Text mode (explicit)
    python jarvis_console.py --speech     # Launches voice mode
    python jarvis_console.py --hybrid     # Text input, spoken + printed output
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'

import sys
import time
import random
import argparse
import threading
import warnings
from pathlib import Path
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document as PTDocument
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

sys.path.insert(0, str(Path(__file__).parent))

# Suppress torch/transformers/huggingface warnings and redirect all logging to file only
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from core.config import load_config
from core.conversation import ConversationManager
from core.responses import get_response_library
from core.llm_router import LLMRouter, ToolCallRequest
from core.web_research import WebResearcher, format_search_results
from core.skill_manager import SkillManager
from core.reminder_manager import get_reminder_manager
from core.news_manager import get_news_manager
from core.honorific import get_honorific
from core.speech_chunker import SpeechChunker
from core.context_window import get_context_window, estimate_tokens, TOKEN_RATIO
from core.document_buffer import DocumentBuffer, BINARY_EXTENSIONS


class TTSProxy:
    """Drop-in TTS replacement that queues announcements for console display.

    In hybrid mode, speech runs in a background thread so the console can
    print the response text while JARVIS is still speaking.
    """

    def __init__(self, real_tts, console, mode):
        self.real_tts = real_tts
        self.console = console
        self.mode = mode
        self._announcement_queue = []
        self._speech_thread = None

    def speak(self, text, normalize=True):
        self._announcement_queue.append(text)
        if self.mode == "hybrid" and self.real_tts:
            # Non-blocking: speak in background so console prints during speech
            self._wait_for_speech()  # Wait for any previous speech to finish
            self._speech_thread = threading.Thread(
                target=self.real_tts.speak, args=(text, normalize), daemon=True
            )
            self._speech_thread.start()
        return True

    def _wait_for_speech(self):
        """Block until any in-progress speech finishes."""
        if self._speech_thread and self._speech_thread.is_alive():
            self._speech_thread.join()

    def get_pending_announcements(self):
        announcements = self._announcement_queue[:]
        self._announcement_queue.clear()
        return announcements

    def __getattr__(self, name):
        if self.real_tts:
            return getattr(self.real_tts, name)
        raise AttributeError(f"TTSProxy has no real TTS and no attribute '{name}'")


@dataclass
class SessionStats:
    total: int = 0
    skill_hits: int = 0
    llm_hits: int = 0

    def update(self, skill_handled, used_llm):
        self.total += 1
        if skill_handled:
            self.skill_hits += 1
        if used_llm:
            self.llm_hits += 1


class SlashCompleter(Completer):
    """Tab completion for slash commands and /file paths."""

    _commands = ["/paste", "/file", "/clipboard", "/append", "/context", "/clear", "/help"]
    _path_completer = PathCompleter(expanduser=True)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # After "/file " — complete file paths
        if text.startswith("/file "):
            path_text = text[6:]
            path_doc = PTDocument(path_text, len(path_text))
            yield from self._path_completer.get_completions(path_doc, complete_event)
            return

        # After "/" — complete slash command names
        if text.startswith("/") and " " not in text:
            for cmd in self._commands:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))



# DocumentBuffer + BINARY_EXTENSIONS imported from core.document_buffer


def render_stats(console, match_info, llm, used_llm, t_start, t_match, t_end, session,
                  doc_buffer=None):
    """Render a compact stats panel with adaptive column layout."""
    total_ms = (t_end - t_start) * 1000
    match_ms = (t_match - t_start) * 1000
    exec_ms = (t_end - t_match) * 1000

    # Collect all stat pairs
    pairs = []
    if match_info:
        pairs.append(("Layer", match_info.get("layer", "?")))
        pairs.append(("Skill", match_info.get("skill_name", "-")))
        pairs.append(("Handler", match_info.get("handler_name", "-")))
        conf = match_info.get("confidence")
        pairs.append(("Confidence", f"{conf:.3f}" if conf else "-"))
    elif used_llm:
        pairs.append(("Layer", "llm_fallback"))

    pairs.append(("Time", f"{total_ms:.0f}ms (match {match_ms:.0f} + exec {exec_ms:.0f})"))

    if used_llm and llm.last_call_info:
        info = llm.last_call_info
        tokens = f"{info.get('input_tokens', '?')}+{info.get('output_tokens', '?')}"
        pairs.append(("LLM", f"{info['provider']} ({tokens} tok)"))

    if doc_buffer and doc_buffer.active:
        pairs.append(("DocCtx", f"~{doc_buffer.token_estimate} tok ({doc_buffer.source})"))

    pairs.append(("Session", f"{session.total} cmd | Skill {session.skill_hits} | LLM {session.llm_hits}"))

    # Adaptive columns: 3 pairs/row when wide, 2 when medium
    width = console.width
    cols = 3 if width >= 120 else 2 if width >= 80 else 1

    table = Table(show_header=False, box=None, padding=(0, 0), expand=True)
    for c in range(cols):
        table.add_column(style="cyan dim", no_wrap=True, ratio=1)  # key
        table.add_column(style="white", no_wrap=True, ratio=3)     # value
        if c < cols - 1:
            table.add_column(width=3, style="dim")                 # separator

    # Fill rows with pairs, padding incomplete rows with blanks
    for i in range(0, len(pairs), cols):
        row_data = []
        for j in range(cols):
            if i + j < len(pairs):
                row_data.extend(pairs[i + j])
            else:
                row_data.extend(("", ""))
            if j < cols - 1:
                row_data.append("│" if i + j < len(pairs) else "")
        table.add_row(*row_data)

    console.print(Panel(table, title="[dim]Stats[/dim]", border_style="dim"))


_DEFLECTION_PHRASES = [
    "check official", "check the official", "you might want to check",
    "i recommend checking", "i suggest checking", "check recent",
    "i don't have access to", "i don't have real-time",
    "i don't have information on", "i cannot access",
    "beyond my knowledge", "outside my knowledge",
    "check online", "search for", "look up",
]


def _is_deflection(response: str) -> bool:
    """Detect responses that tell the user to go look it up themselves."""
    lower = response.lower()
    return any(phrase in lower for phrase in _DEFLECTION_PHRASES)


def _do_web_search(query, web_researcher, llm, console):
    """Execute a web search and stream the synthesized answer. Returns response text."""
    console.print(f"\n[dim]Searching: {query}[/dim]")

    results = web_researcher.search(query)

    page_sections = web_researcher.fetch_pages_parallel(results)

    page_content = ""
    if page_sections:
        page_content = "\n\nFull article content:\n\n" + \
            "\n\n---\n\n".join(page_sections)

    tool_result = format_search_results(results) + page_content
    console.print(f"[dim]Found {len(results)} results[/dim]")

    # Build a fake ToolCallRequest for continue_after_tool_call
    forced_call = ToolCallRequest(
        name="web_search",
        arguments={"query": query},
        call_id="forced_search",
    )
    # Prime the LLM's tool call message history
    from datetime import date
    today = date.today().strftime("%B %d, %Y")
    system_prompt = llm._build_system_prompt()
    system_prompt += (
        f"\n\nToday's date is {today}. "
        "Your training data is OUTDATED. "
        "Answer the user's question using ONLY the search results provided."
    )
    llm._tool_call_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    full_response = ""
    console.print("[bold cyan]JARVIS:[/bold cyan] ", end="")
    for token in llm.continue_after_tool_call(forced_call, tool_result):
        full_response += token
        sys.stdout.write(token)
        sys.stdout.flush()

    sys.stdout.write("\n")
    return full_response


def _stream_llm_console(llm, command, history, console, mode, real_tts,
                        memory_context=None, conversation_messages=None,
                        max_tokens=None, web_researcher=None):
    """Stream LLM response with typewriter output, web research, and quality gate.

    Returns the full accumulated response text, or empty string on failure.
    """
    chunker = SpeechChunker()
    full_response = ""
    first_chunk_checked = False
    use_tools = llm.tool_calling and web_researcher

    # Print prefix for typewriter output
    console.print("[bold cyan]JARVIS:[/bold cyan] ", end="")

    try:
        # Choose tool-aware or plain streaming
        token_source = (
            llm.stream_with_tools(
                user_message=command,
                conversation_history=history,
                memory_context=memory_context,
                conversation_messages=conversation_messages,
            ) if use_tools else
            llm.stream(
                user_message=command,
                conversation_history=history,
                memory_context=memory_context,
                conversation_messages=conversation_messages,
                max_tokens=max_tokens,
            )
        )

        tool_call_request = None

        for item in token_source:
            # Tool call sentinel — break to handle web search
            if isinstance(item, ToolCallRequest):
                tool_call_request = item
                break

            token = item
            full_response += token

            # Typewriter: print tokens as they arrive
            sys.stdout.write(token)
            sys.stdout.flush()

            chunk = chunker.feed(token)
            if chunk and not first_chunk_checked:
                first_chunk_checked = True
                quality_issue = llm._check_response_quality(chunk, command)
                if quality_issue:
                    # Clear the partial typewriter output
                    sys.stdout.write("\n")
                    console.print(f"[dim](quality retry: {quality_issue})[/dim]")
                    response = llm.chat(
                        user_message=command,
                        conversation_history=history,
                    )
                    return response

        # --- Web search phase (tool call) ---
        if tool_call_request:
            query = tool_call_request.arguments.get("query", command)
            console.print(f"\n[dim]Searching: {query}[/dim]")

            if tool_call_request.name == "web_search":
                results = web_researcher.search(query)

                # Fetch top 3 page contents concurrently for richer synthesis
                page_sections = web_researcher.fetch_pages_parallel(results)

                page_content = ""
                if page_sections:
                    page_content = "\n\nFull article content:\n\n" + \
                        "\n\n---\n\n".join(page_sections)

                tool_result = format_search_results(results) + page_content
                console.print(f"[dim]Found {len(results)} results[/dim]")
            else:
                tool_result = f"Unknown tool: {tool_call_request.name}"

            # Stream synthesized answer
            console.print("[bold cyan]JARVIS:[/bold cyan] ", end="")
            for token in llm.continue_after_tool_call(
                tool_call_request, tool_result
            ):
                full_response += token
                sys.stdout.write(token)
                sys.stdout.flush()

            sys.stdout.write("\n")
            return full_response

        # --- Deflection safety net ---
        # If Qwen answered but deflected ("check official channels", etc.),
        # discard the response and do a web search instead.
        if full_response and web_researcher and _is_deflection(full_response):
            sys.stdout.write("\n")
            console.print("[dim](deflection detected — searching instead)[/dim]")
            return _do_web_search(command, web_researcher, llm, console)

        # Flush remaining
        remaining = chunker.flush()
        if remaining and not first_chunk_checked:
            quality_issue = llm._check_response_quality(remaining, command)
            if quality_issue:
                sys.stdout.write("\n")
                console.print(f"[dim](quality retry: {quality_issue})[/dim]")
                return llm.chat(
                    user_message=command,
                    conversation_history=history,
                )

    except Exception:
        sys.stdout.write("\n")
        if not full_response:
            return llm.chat(
                user_message=command,
                conversation_history=history,
            )

    # End the typewriter line
    sys.stdout.write("\n\n")
    sys.stdout.flush()

    # Speak full response in hybrid mode
    if mode == "hybrid" and real_tts and full_response:
        real_tts.speak(full_response)

    return full_response


def _handle_slash_command(command, doc_buffer, console, pt_history):
    """Handle slash commands. Returns True if handled, False if not a slash command."""
    parts = command.split(None, 1)
    cmd = parts[0].lower()
    # arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/paste":
        console.print("[cyan]Paste mode — type or paste text. Press [bold]Esc then Enter[/bold] to submit, [bold]Ctrl+C[/bold] to cancel.[/cyan]")

        # Multi-line key bindings: Enter = newline, Esc+Enter = submit
        paste_bindings = KeyBindings()

        @paste_bindings.add('escape', 'enter')
        def _submit(event):
            event.current_buffer.validate_and_handle()

        @paste_bindings.add('enter')
        def _newline(event):
            event.current_buffer.insert_text('\n')

        try:
            # Separate session avoids contaminating the main prompt with
            # sticky multiline/key_bindings params. Shares FileHistory
            # so up-arrow still recalls previous pastes.
            paste_session = PromptSession(history=pt_history)
            text = paste_session.prompt(
                "paste> ",
                multiline=True,
                key_bindings=paste_bindings,
                bottom_toolbar=HTML('<b>Esc+Enter</b> to submit | <b>Ctrl+C</b> to cancel'),
            )
        except KeyboardInterrupt:
            console.print("[dim]Paste cancelled.[/dim]")
            return True

        text = text.strip()
        if not text:
            console.print("[yellow]Nothing pasted.[/yellow]")
            return True

        doc_buffer.load(text, "paste")
        lines = text.count('\n') + 1
        preview = text[:200] + ("..." if len(text) > 200 else "")
        console.print(Panel(
            f"[bold green]Loaded[/bold green] ~{doc_buffer.token_estimate} tokens, "
            f"{lines} lines ({doc_buffer.source})\n\n"
            f"[dim]{preview}[/dim]",
            title="[cyan]Document Buffer[/cyan]",
            border_style="cyan",
        ))
        return True

    elif cmd == "/file":
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            console.print("[yellow]Usage: /file <path> [--tail][/yellow]")
            return True

        # Parse --tail flag
        tail_mode = "--tail" in arg
        if tail_mode:
            arg = arg.replace("--tail", "").strip()

        # Strip quotes (drag-and-drop or copy-paste may include them)
        arg = arg.strip("'\"")

        filepath = Path(arg).expanduser().resolve()

        if not filepath.exists():
            console.print(f"[red]File not found:[/red] {filepath}")
            return True
        if not filepath.is_file():
            console.print(f"[red]Not a file:[/red] {filepath}")
            return True

        # Binary rejection
        if filepath.suffix.lower() in BINARY_EXTENSIONS:
            console.print(f"[red]Binary file rejected:[/red] {filepath.suffix} files are not supported")
            return True

        # Size check
        size = filepath.stat().st_size
        if size > 500_000:
            console.print(f"[yellow]Warning:[/yellow] File is {size:,} bytes — loading anyway (will truncate to token budget)")

        try:
            text = filepath.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            console.print(f"[red]Error reading file:[/red] {e}")
            return True

        if not text.strip():
            console.print(f"[yellow]File is empty:[/yellow] {filepath.name}")
            return True

        # --tail: keep end of file instead of beginning
        if tail_mode:
            lines = text.splitlines()
            target_words = int(doc_buffer.max_tokens / TOKEN_RATIO)
            kept = []
            word_count = 0
            for line in reversed(lines):
                word_count += len(line.split())
                if word_count > target_words:
                    break
                kept.append(line)
            text = "\n".join(reversed(kept))

        doc_buffer.load(text, f"file:{filepath.name}")
        line_count = text.count('\n') + 1
        preview = text[:200] + ("..." if len(text) > 200 else "")
        tail_tag = " [dim](tail)[/dim]" if tail_mode else ""
        console.print(Panel(
            f"[bold green]Loaded[/bold green] ~{doc_buffer.token_estimate} tokens, "
            f"{line_count} lines, {size:,} bytes ({doc_buffer.source}){tail_tag}\n\n"
            f"[dim]{preview}[/dim]",
            title="[cyan]Document Buffer[/cyan]",
            border_style="cyan",
        ))
        return True

    elif cmd == "/clipboard":
        import subprocess as _sp
        try:
            result = _sp.run(
                ["wl-paste", "--no-newline"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode != 0:
                console.print(f"[red]Clipboard error:[/red] {result.stderr.strip() or 'wl-paste failed'}")
                return True
            text = result.stdout
        except FileNotFoundError:
            console.print("[red]wl-paste not found.[/red] Install with: [bold]sudo apt install wl-clipboard[/bold]")
            return True
        except _sp.TimeoutExpired:
            console.print("[red]Clipboard read timed out.[/red]")
            return True

        if not text.strip():
            console.print("[yellow]Clipboard is empty.[/yellow]")
            return True

        doc_buffer.load(text, "clipboard")
        lines = text.count('\n') + 1
        preview = text[:200] + ("..." if len(text) > 200 else "")
        console.print(Panel(
            f"[bold green]Loaded[/bold green] ~{doc_buffer.token_estimate} tokens, "
            f"{lines} lines ({doc_buffer.source})\n\n"
            f"[dim]{preview}[/dim]",
            title="[cyan]Document Buffer[/cyan]",
            border_style="cyan",
        ))
        return True

    elif cmd == "/append":
        console.print("[cyan]Append mode — type or paste text. Press [bold]Esc then Enter[/bold] to submit, [bold]Ctrl+C[/bold] to cancel.[/cyan]")

        append_bindings = KeyBindings()

        @append_bindings.add('escape', 'enter')
        def _submit_append(event):
            event.current_buffer.validate_and_handle()

        @append_bindings.add('enter')
        def _newline_append(event):
            event.current_buffer.insert_text('\n')

        try:
            append_session = PromptSession(history=pt_history)
            text = append_session.prompt(
                "append> ",
                multiline=True,
                key_bindings=append_bindings,
                bottom_toolbar=HTML('<b>Esc+Enter</b> to submit | <b>Ctrl+C</b> to cancel'),
            )
        except KeyboardInterrupt:
            console.print("[dim]Append cancelled.[/dim]")
            return True

        text = text.strip()
        if not text:
            console.print("[yellow]Nothing to append.[/yellow]")
            return True

        was_empty = not doc_buffer.active
        doc_buffer.append(text, "append")
        verb = "Loaded" if was_empty else "Appended"
        console.print(Panel(
            f"[bold green]{verb}[/bold green] — buffer now ~{doc_buffer.token_estimate} tokens ({doc_buffer.source})",
            title="[cyan]Document Buffer[/cyan]",
            border_style="cyan",
        ))
        return True

    elif cmd == "/context":
        if not doc_buffer.active:
            console.print("[dim]No document loaded. Use /paste or /file to load one.[/dim]")
            return True
        lines = doc_buffer.content.count('\n') + 1
        chars = len(doc_buffer.content)
        preview = doc_buffer.content[:500] + ("..." if chars > 500 else "")
        console.print(Panel(
            f"[bold]Source:[/bold] {doc_buffer.source}\n"
            f"[bold]Tokens:[/bold] ~{doc_buffer.token_estimate} / {doc_buffer.max_tokens} max\n"
            f"[bold]Size:[/bold] {chars:,} chars, {lines} lines\n\n"
            f"[dim]{preview}[/dim]",
            title="[cyan]Document Context[/cyan]",
            border_style="cyan",
        ))
        return True

    elif cmd == "/clear":
        if not doc_buffer.active:
            console.print("[dim]Nothing to clear — buffer is empty.[/dim]")
            return True
        old_source, old_tokens = doc_buffer.clear()
        console.print(f"[green]Cleared[/green] document buffer ({old_source}, ~{old_tokens} tokens)")
        return True

    elif cmd in ("/help", "/?"):
        help_table = Table(title="Slash Commands", box=box.SIMPLE, show_edge=False)
        help_table.add_column("Command", style="cyan bold", no_wrap=True)
        help_table.add_column("Description")
        help_table.add_row("/paste", "Multi-line paste mode (Esc+Enter to submit)")
        help_table.add_row("/file <path>", "Load a file into document buffer (--tail for end of file)")
        help_table.add_row("/clipboard", "Load clipboard contents via wl-paste")
        help_table.add_row("/append", "Append text to existing buffer (multi-line mode)")
        help_table.add_row("/context", "Show current document buffer info")
        help_table.add_row("/clear", "Clear document buffer")
        help_table.add_row("/help", "Show this help")
        help_table.add_row("", "")
        help_table.add_row("[dim]Tip[/dim]", "[dim]Drag a file from Nautilus to auto-load it[/dim]")
        console.print(help_table)
        return True

    else:
        console.print(f"[yellow]Unknown command: {cmd}[/yellow] — type /help for available commands")
        return True


def run_console(config, mode):
    """Main REPL loop."""
    console = Console()

    # TTS: real for hybrid, None for text-only
    real_tts = None
    if mode == "hybrid":
        from core.tts import TextToSpeech
        real_tts = TextToSpeech(config)
    tts_proxy = TTSProxy(real_tts, console, mode)

    # Core components
    conversation = ConversationManager(config)
    conversation.current_user = "user"  # Console defaults to primary user
    responses = get_response_library()
    llm = LLMRouter(config)
    skill_manager = SkillManager(config, conversation, tts_proxy, responses, llm)
    skill_manager.load_all_skills()

    # Web research (for tool-calling LLM queries)
    web_researcher = WebResearcher(config) if config.get("llm.local.tool_calling", False) else None
    if web_researcher:
        console.print("[cyan]Web research:[/cyan] enabled (DuckDuckGo + trafilatura)")

    # Reminder system
    reminder_manager = None
    calendar_manager = None
    if config.get("reminders.enabled", True):
        reminder_manager = get_reminder_manager(config, tts_proxy, conversation)
        reminder_manager.set_ack_window_callback(lambda rid: None)
        reminder_manager.set_window_callback(lambda d: None)
        reminder_manager.set_listener_callbacks(pause=lambda: None, resume=lambda: None)

        if config.get("google_calendar.enabled", False):
            try:
                from core.google_calendar import get_calendar_manager
                calendar_manager = get_calendar_manager(config)
                reminder_manager.set_calendar_manager(calendar_manager)
                calendar_manager.start()
            except Exception as e:
                console.print(f"[yellow]Calendar init failed: {e}[/yellow]")

        reminder_manager.start()

    # News system
    news_manager = None
    if config.get("news.enabled", False):
        news_manager = get_news_manager(config, tts_proxy, conversation, llm)
        news_manager.set_listener_callbacks(pause=lambda: None, resume=lambda: None)
        news_manager.set_window_callback(lambda d: None)
        news_manager.start()

    # Conversational memory system
    memory_manager = None
    if config.get("conversational_memory.enabled", False):
        from core.memory_manager import get_memory_manager
        memory_manager = get_memory_manager(
            config=config,
            conversation=conversation,
            embedding_model=skill_manager._embedding_model,
        )
        conversation.set_memory_manager(memory_manager)
        vec_count = memory_manager.faiss_index.ntotal if memory_manager.faiss_index else 0
        pro_status = "on" if memory_manager.proactive_enabled else "off"
        pro_color = "green" if memory_manager.proactive_enabled else "red"
        console.print(f"[cyan]Memory system:[/cyan] {vec_count} vectors, "
                       f"[cyan]proactive[/cyan] [{pro_color}]{pro_status}[/{pro_color}] "
                       f"([cyan]threshold[/cyan] {memory_manager.proactive_threshold})")

    # Context window (working memory)
    context_window = None
    if config.get("context_window.enabled", False):
        context_window = get_context_window(
            config=config,
            embedding_model=skill_manager._embedding_model,
            llm=llm,
        )
        conversation.set_context_window(context_window)

        # Load prior segments from SQLite (falls back to JSONL replay if empty)
        context_window.load_prior_segments(
            fallback_messages=conversation.session_history
        )

        cw_stats = context_window.get_stats()
        console.print(f"[cyan]Context window:[/cyan] enabled "
                       f"(budget={context_window.token_budget} tokens, "
                       f"threshold={context_window.topic_shift_threshold}, "
                       f"prior={cw_stats['segments']} seg(s))")

    # Command history (persists across sessions) + document buffer
    history_file = Path(__file__).parent / ".console_history"
    pt_history = FileHistory(str(history_file))
    doc_buffer = DocumentBuffer()

    def _bottom_toolbar():
        if doc_buffer.active:
            return HTML(
                f'<b>DocCtx:</b> ~{doc_buffer.token_estimate} tok '
                f'({doc_buffer.source}) — /context to view, /clear to remove'
            )
        return HTML('<b>/paste</b> load text  <b>/file</b> load file  <b>/help</b> commands')

    pt_session = PromptSession(
        history=pt_history,
        bottom_toolbar=_bottom_toolbar,
        enable_history_search=True,
        completer=SlashCompleter(),
        complete_while_typing=False,
    )

    console.print(Panel(
        f"[bold]JARVIS Console[/bold] — {mode} mode\n"
        f"Type commands directly. Type [bold]quit[/bold] to exit.\n"
        f"Slash commands: /paste /file /clipboard /context /clear /help",
        border_style="cyan"
    ))

    session_stats = SessionStats()

    try:
        while True:
            # Show queued announcements from background threads
            for ann in tts_proxy.get_pending_announcements():
                console.print(Panel(ann, title="[yellow]Announcement[/yellow]", border_style="yellow"))

            try:
                command = pt_session.prompt("You > ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not command:
                continue
            if command.lower() in ("quit", "exit", "q"):
                break
            if command.lower() in ("clear", "cls"):
                os.system("clear")
                continue
            if command.lower() in ("reload", "restart", "console_reload"):
                console.print("[cyan]Reloading console...[/cyan]\n")
                # Clean shutdown before exec
                if memory_manager:
                    memory_manager.save()
                if news_manager:
                    news_manager.stop()
                if calendar_manager:
                    calendar_manager.stop()
                if reminder_manager:
                    reminder_manager.stop()
                os.execv(sys.executable, [sys.executable] + sys.argv)

            # Drag-and-drop auto-detect: bare file path → implicit /file
            # Nautilus drops absolute paths; also handle ~/... paths
            _stripped = command.strip().strip("'\"")
            if ((_stripped.startswith("/") or _stripped.startswith("~/")) and
                    os.path.isfile(os.path.expanduser(_stripped))):
                _handle_slash_command(f"/file {_stripped}", doc_buffer, console, pt_history)
                continue

            # Slash commands — handle before any skill routing
            if command.startswith("/"):
                _handle_slash_command(command, doc_buffer, console, pt_history)
                continue

            # --- Process command ---
            # Strip wake word prefixes (voice mode does this in continuous_listener)
            import re
            command = re.sub(r'^(?:hey\s+)?jarvis[\s,.:!]*', '', command, flags=re.IGNORECASE).strip()
            # Also strip trailing wake word: "what time is it, jarvis"
            command = re.sub(r'[\s,.:!]*jarvis[\s,.:!]*$', '', command, flags=re.IGNORECASE).strip()
            if not command:
                command = "jarvis_only"

            conversation.add_message("user", command)

            # Show context window events inline
            if context_window and context_window.enabled:
                stats = context_window.get_stats()
                seg_count = stats["segments"]
                open_seg = "open" if stats["open_segment"] else "closed"
                summarized = stats.get("summarized", 0)
                summary_tag = f", {summarized} summarized" if summarized else ""
                console.print(
                    f"[dim]  ctx: {seg_count} segment(s), current={open_seg}, "
                    f"~{stats['estimated_tokens']} tokens{summary_tag}[/dim]"
                )

            t_start = time.perf_counter()
            skill_handled = False
            response = ""
            used_llm = False
            llm_streamed = False  # True only when _stream_llm_console already printed
            skill_already_spoke = False
            match_info = None

            # Priority 1: Rundown acceptance (must intercept before skill routing)
            if reminder_manager and reminder_manager.is_rundown_pending():
                text_lower = command.strip().lower()
                negative = any(w in text_lower for w in [
                    "no", "not now", "later", "not yet", "hold", "skip",
                ])
                if negative:
                    reminder_manager.defer_rundown()
                    response = f"Very well, {get_honorific()}. Just say 'daily rundown' whenever you're ready."
                    skill_handled = True
                else:
                    reminder_manager.deliver_rundown()
                    response = ""
                    skill_handled = True

            # Priority 2: Reminder acknowledgment
            if not skill_handled and reminder_manager and reminder_manager.is_awaiting_ack():
                reminder_manager.acknowledge_last()
                h = get_honorific()
                response = random.choice([
                    f"Very good, {h}.",
                    f"Noted, {h}.",
                    f"Of course, {h}.",
                    f"Absolutely, {h}.",
                ])
                skill_handled = True

            # Priority 2.5: Memory forget confirmation/cancellation (must intercept before skill routing)
            if not skill_handled and memory_manager and memory_manager._pending_forget:
                cmd_lower = command.lower().strip()
                affirm = ("yes", "yeah", "yep", "go ahead", "do it", "proceed", "confirm", "sure", "remove", "delete")
                deny = ("no", "nope", "nah", "cancel", "nevermind", "never mind", "keep", "don't")
                if any(w in cmd_lower for w in affirm):
                    response = memory_manager.confirm_forget()
                    skill_handled = True
                elif any(w in cmd_lower for w in deny):
                    response = memory_manager.cancel_forget()
                    skill_handled = True

            # Priority 3: Memory operations (recall, forget, transparency)
            # Must run before skill routing — "forget my server ip" was matching network_info
            if not skill_handled and memory_manager:
                mm = memory_manager
                user_id = "primary_user"

                if mm.is_forget_request(command):
                    response = mm.handle_forget(command, user_id)
                    skill_handled = True
                elif mm.is_transparency_request(command):
                    response = mm.handle_transparency(command, user_id)
                    skill_handled = True
                elif mm.is_fact_request(command):
                    # Fact already extracted by on_message() hook — just confirm
                    import random
                    response = random.choice([
                        "Noted, sir.", "Very good, sir.", "Understood, sir.",
                        "I'll remember that, sir.", "Committed to memory, sir.",
                        "Duly noted, sir.", "Of course, sir.",
                    ])
                    skill_handled = True

                elif mm.is_recall_query(command):
                    recall_context = mm.handle_recall(command, user_id)
                    if recall_context:
                        history = conversation.format_history_for_llm(include_system_prompt=False)
                        response = llm.chat(
                            user_message=(
                                f"The user is asking you to recall something. Here is what you found "
                                f"in your memory:\n\n{recall_context}\n\n"
                                f"Now answer naturally: {command}"
                            ),
                            conversation_history=history,
                        )
                        skill_handled = True
                        used_llm = True

            # Priority 4: Skill routing
            # When document buffer is active, skip skill routing — the user
            # is asking the LLM about their document, not invoking a skill.
            # Priorities 1-3 (rundowns, reminders, memory) still work normally.
            if not skill_handled and not doc_buffer.active:
                skill_response = skill_manager.execute_intent(command)
                skill_already_spoke = len(tts_proxy.get_pending_announcements()) > 0
                match_info = skill_manager._last_match_info
                if skill_response:
                    response = skill_response
                    skill_handled = True

            t_match = time.perf_counter()

            # Priority 5: News pull-up handler
            if not skill_handled and news_manager and news_manager.get_last_read_url():
                pull_phrases = ["pull that up", "show me that", "open that",
                                "let me see", "show me the article", "open the article"]
                if any(p in command.strip().lower() for p in pull_phrases):
                    url = news_manager.get_last_read_url()
                    browser = config.get("web_navigation.default_browser", "brave")
                    browser_cmd = f"{browser}-browser" if browser != "brave" else "brave-browser"
                    import subprocess as _sp
                    _sp.Popen([browser_cmd, url])
                    news_manager.clear_last_read()
                    response = f"Pulling that up now, {get_honorific()}."
                    skill_handled = True

            # Priority 6: News continue handler
            if not skill_handled and news_manager:
                continue_words = ["continue", "keep going", "more headlines", "go on", "read more"]
                if any(w in command.strip().lower() for w in continue_words):
                    remaining = news_manager.get_unread_count()
                    if sum(remaining.values()) > 0:
                        response = news_manager.read_headlines(limit=5)
                        skill_handled = True

            # LLM fallback (streaming with typewriter output)
            if not skill_handled:
                used_llm = True
                llm_streamed = True
                history = conversation.format_history_for_llm(include_system_prompt=False)

                # Context assembly — use context window if enabled, else flat history
                context_messages = None
                if context_window and context_window.enabled:
                    context_messages = context_window.assemble_context(command)
                    if context_messages:
                        console.print(
                            f"[dim]  ctx assembled: {len(context_messages)} messages "
                            f"for LLM (max_tokens={llm._estimate_max_tokens(command)})[/dim]"
                        )

                # Proactive memory surfacing — inject relevant facts into LLM context
                memory_context = None
                if memory_manager:
                    memory_context = memory_manager.get_proactive_context(command, "primary_user")

                # Document-aware LLM hint — tell the LLM a document is loaded
                if doc_buffer.active:
                    doc_hint = ("The user has loaded a document into the context buffer. "
                                "Refer to the <document> tags in their message. "
                                "Be analytical and specific in your response.")
                    memory_context = f"{doc_hint}\n\n{memory_context}" if memory_context else doc_hint

                # Fact-extraction acknowledgment — let LLM know it just stored facts
                llm_command = command
                if memory_manager and memory_manager.last_extracted:
                    subjects = ", ".join(f.get("subject", "") for f in memory_manager.last_extracted)
                    llm_command = (
                        f"{command}\n\n[System: you just stored these facts from the user's message: "
                        f"{subjects}. Briefly acknowledge you'll remember this.]"
                    )

                # Document buffer injection — prepend document in XML tags
                if doc_buffer.active:
                    llm_command = doc_buffer.build_augmented_message(llm_command)

                response = _stream_llm_console(
                    llm, llm_command, history, console, mode, real_tts,
                    memory_context=memory_context,
                    conversation_messages=context_messages,
                    max_tokens=600 if doc_buffer.active else None,
                    web_researcher=web_researcher,
                )
                if not response:
                    response = "I'm sorry, I'm having trouble processing that right now."
                else:
                    response = llm.strip_filler(response)

            t_end = time.perf_counter()

            conversation.add_message("assistant", response)

            # Display response (skip if LLM streaming already printed it)
            if response and not llm_streamed:
                console.print(f"\n[bold cyan]JARVIS:[/bold cyan] {response}\n")

            # Speak in hybrid mode — skip if skill already spoke or LLM streaming handled it
            if mode == "hybrid" and real_tts and response and not skill_already_spoke and not llm_streamed:
                real_tts.speak(response)

            # Stats panel
            session_stats.update(skill_handled, used_llm)
            render_stats(console, match_info, llm, used_llm, t_start, t_match, t_end, session_stats,
                         doc_buffer=doc_buffer)

            # Wait for any background speech to finish before next prompt
            tts_proxy._wait_for_speech()

    finally:
        console.print("\n[dim]Shutting down...[/dim]")
        if memory_manager:
            memory_manager.save()
        if news_manager:
            news_manager.stop()
        if calendar_manager:
            calendar_manager.stop()
        if reminder_manager:
            reminder_manager.stop()


def main():
    parser = argparse.ArgumentParser(description="JARVIS Console — Keyboard interaction mode")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text", action="store_true", default=True, help="Text mode (default)")
    group.add_argument("--speech", action="store_true", help="Launch voice mode")
    group.add_argument("--hybrid", action="store_true", help="Text input, spoken + printed output")
    args = parser.parse_args()

    if args.speech:
        # Delegate to the existing voice mode
        from jarvis_continuous import JarvisContinuous
        config = load_config()
        jarvis = JarvisContinuous(config)
        jarvis.run()
        return

    mode = "hybrid" if args.hybrid else "text"
    config = load_config()
    run_console(config, mode)


if __name__ == "__main__":
    main()
