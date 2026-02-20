#!/usr/bin/env python3
"""
JARVIS Web UI — Browser-based chat interface.

Serves a static frontend via aiohttp and bridges commands to the
full JARVIS skill pipeline over WebSocket.

Usage:
    python3 jarvis_web.py
    python3 jarvis_web.py --port 8088
    python3 jarvis_web.py --voice   # Start with voice output enabled
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'

import sys
import re
import time
import json
import random
import asyncio
import logging
import argparse
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Suppress noisy library warnings
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

from aiohttp import web

from core.config import load_config
from core.conversation import ConversationManager
from core.responses import get_response_library
from core.llm_router import LLMRouter, ToolCallRequest
from core.web_research import WebResearcher, format_search_results
from core.skill_manager import SkillManager
from core.reminder_manager import get_reminder_manager
from core.news_manager import get_news_manager
from core.honorific import get_honorific
from core.context_window import get_context_window
from core.document_buffer import DocumentBuffer, BINARY_EXTENSIONS

logger = logging.getLogger("jarvis.web")


# ---------------------------------------------------------------------------
# WebTTSProxy — Routes TTS calls to WebSocket + optional real TTS
# ---------------------------------------------------------------------------

class WebTTSProxy:
    """TTS proxy that routes speech to WebSocket announcements + optional audio."""

    def __init__(self, real_tts=None):
        self.real_tts = real_tts
        self.hybrid = False  # Toggled by voice switch
        self._announcement_queue: list[str] = []
        self._lock = threading.Lock()

    def speak(self, text, normalize=True):
        """Queue announcement for WebSocket delivery + optional TTS."""
        with self._lock:
            self._announcement_queue.append(text)
        if self.hybrid and self.real_tts:
            threading.Thread(
                target=self.real_tts.speak, args=(text, normalize), daemon=True
            ).start()
        return True

    def get_pending_announcements(self) -> list[str]:
        with self._lock:
            announcements = self._announcement_queue[:]
            self._announcement_queue.clear()
        return announcements

    def __getattr__(self, name):
        if self.real_tts:
            return getattr(self.real_tts, name)
        raise AttributeError(f"WebTTSProxy has no real TTS and no attribute '{name}'")


# ---------------------------------------------------------------------------
# Component initialization (mirrors jarvis_console.py)
# ---------------------------------------------------------------------------

def init_components(config, tts_proxy):
    """Initialize all JARVIS core components. Returns dict of components."""
    components = {}

    # Core
    conversation = ConversationManager(config)
    conversation.current_user = "user"
    components['conversation'] = conversation
    components['responses'] = get_response_library()
    components['llm'] = LLMRouter(config)
    components['skill_manager'] = SkillManager(
        config, conversation, tts_proxy, components['responses'], components['llm']
    )
    components['skill_manager'].load_all_skills()

    # Web research
    if config.get("llm.local.tool_calling", False):
        components['web_researcher'] = WebResearcher(config)
    else:
        components['web_researcher'] = None

    # Reminder system
    components['reminder_manager'] = None
    components['calendar_manager'] = None
    if config.get("reminders.enabled", True):
        rm = get_reminder_manager(config, tts_proxy, conversation)
        rm.set_ack_window_callback(lambda rid: None)
        rm.set_window_callback(lambda d: None)
        rm.set_listener_callbacks(pause=lambda: None, resume=lambda: None)

        if config.get("google_calendar.enabled", False):
            try:
                from core.google_calendar import get_calendar_manager
                cm = get_calendar_manager(config)
                rm.set_calendar_manager(cm)
                cm.start()
                components['calendar_manager'] = cm
            except Exception as e:
                logger.warning("Calendar init failed: %s", e)

        rm.start()
        components['reminder_manager'] = rm

    # News
    components['news_manager'] = None
    if config.get("news.enabled", False):
        nm = get_news_manager(config, tts_proxy, conversation, components['llm'])
        nm.set_listener_callbacks(pause=lambda: None, resume=lambda: None)
        nm.set_window_callback(lambda d: None)
        nm.start()
        components['news_manager'] = nm

    # Conversational memory
    components['memory_manager'] = None
    if config.get("conversational_memory.enabled", False):
        from core.memory_manager import get_memory_manager
        mm = get_memory_manager(
            config=config,
            conversation=conversation,
            embedding_model=components['skill_manager']._embedding_model,
        )
        conversation.set_memory_manager(mm)
        components['memory_manager'] = mm

    # Context window
    components['context_window'] = None
    if config.get("context_window.enabled", False):
        cw = get_context_window(
            config=config,
            embedding_model=components['skill_manager']._embedding_model,
            llm=components['llm'],
        )
        conversation.set_context_window(cw)
        cw.load_prior_segments(fallback_messages=conversation.session_history)
        components['context_window'] = cw

    # Document buffer
    components['doc_buffer'] = DocumentBuffer()

    return components


# ---------------------------------------------------------------------------
# Command processing — 6-priority pipeline (mirrors jarvis_console.py)
# ---------------------------------------------------------------------------

async def process_command(command: str, components: dict, tts_proxy: WebTTSProxy,
                          config: dict) -> dict:
    """Process a user command through the full pipeline.

    Returns dict with 'response', 'stats', 'used_llm', etc.
    """
    conversation = components['conversation']
    llm = components['llm']
    skill_manager = components['skill_manager']
    reminder_manager = components['reminder_manager']
    news_manager = components['news_manager']
    memory_manager = components['memory_manager']
    context_window = components['context_window']
    doc_buffer = components['doc_buffer']
    web_researcher = components['web_researcher']

    # Strip wake word prefixes
    command = re.sub(r'^(?:hey\s+)?jarvis[\s,.:!]*', '', command, flags=re.IGNORECASE).strip()
    command = re.sub(r'[\s,.:!]*jarvis[\s,.:!]*$', '', command, flags=re.IGNORECASE).strip()
    if not command:
        command = "jarvis_only"

    conversation.add_message("user", command)

    t_start = time.perf_counter()
    skill_handled = False
    response = ""
    used_llm = False
    match_info = None

    # Priority 1: Rundown acceptance
    if reminder_manager and reminder_manager.is_rundown_pending():
        text_lower = command.strip().lower()
        negative = any(w in text_lower for w in [
            "no", "not now", "later", "not yet", "hold", "skip",
        ])
        if negative:
            reminder_manager.defer_rundown()
            response = f"Very well, {get_honorific()}. Just say 'daily rundown' whenever you're ready."
        else:
            await asyncio.to_thread(reminder_manager.deliver_rundown)
            response = ""
        skill_handled = True

    # Priority 2: Reminder acknowledgment
    if not skill_handled and reminder_manager and reminder_manager.is_awaiting_ack():
        reminder_manager.acknowledge_last()
        h = get_honorific()
        response = random.choice([
            f"Very good, {h}.", f"Noted, {h}.",
            f"Of course, {h}.", f"Absolutely, {h}.",
        ])
        skill_handled = True

    # Priority 2.5: Memory forget confirmation
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

    # Priority 3: Memory operations
    if not skill_handled and memory_manager:
        mm = memory_manager
        user_id = "primary_user"

        if mm.is_forget_request(command):
            response = await asyncio.to_thread(mm.handle_forget, command, user_id)
            skill_handled = True
        elif mm.is_transparency_request(command):
            response = await asyncio.to_thread(mm.handle_transparency, command, user_id)
            skill_handled = True
        elif mm.is_fact_request(command):
            response = random.choice([
                "Noted, sir.", "Very good, sir.", "Understood, sir.",
                "I'll remember that, sir.", "Committed to memory, sir.",
            ])
            skill_handled = True
        elif mm.is_recall_query(command):
            recall_context = await asyncio.to_thread(mm.handle_recall, command, user_id)
            if recall_context:
                history = conversation.format_history_for_llm(include_system_prompt=False)
                response = await asyncio.to_thread(
                    llm.chat,
                    user_message=(
                        f"The user is asking you to recall something. Here is what you found "
                        f"in your memory:\n\n{recall_context}\n\n"
                        f"Now answer naturally: {command}"
                    ),
                    conversation_history=history,
                )
                skill_handled = True
                used_llm = True

    # Priority 4: Skill routing (skip when document buffer is active)
    if not skill_handled and not doc_buffer.active:
        skill_response = await asyncio.to_thread(skill_manager.execute_intent, command)
        match_info = skill_manager._last_match_info
        if skill_response:
            response = skill_response
            skill_handled = True

    t_match = time.perf_counter()

    # Priority 5: News pull-up
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

    # Priority 6: News continue
    if not skill_handled and news_manager:
        continue_words = ["continue", "keep going", "more headlines", "go on", "read more"]
        if any(w in command.strip().lower() for w in continue_words):
            remaining = news_manager.get_unread_count()
            if sum(remaining.values()) > 0:
                response = await asyncio.to_thread(news_manager.read_headlines, limit=5)
                skill_handled = True

    # LLM fallback (non-streaming for Phase 1)
    if not skill_handled:
        used_llm = True
        history = conversation.format_history_for_llm(include_system_prompt=False)

        # Context assembly
        context_messages = None
        if context_window and context_window.enabled:
            context_messages = context_window.assemble_context(command)

        # Proactive memory
        memory_context = None
        if memory_manager:
            memory_context = memory_manager.get_proactive_context(command, "primary_user")

        # Document-aware hint
        if doc_buffer.active:
            doc_hint = ("The user has loaded a document into the context buffer. "
                        "Refer to the <document> tags in their message. "
                        "Be analytical and specific in your response.")
            memory_context = f"{doc_hint}\n\n{memory_context}" if memory_context else doc_hint

        # Fact-extraction acknowledgment
        llm_command = command
        if memory_manager and memory_manager.last_extracted:
            subjects = ", ".join(f.get("subject", "") for f in memory_manager.last_extracted)
            llm_command = (
                f"{command}\n\n[System: you just stored these facts from the user's message: "
                f"{subjects}. Briefly acknowledge you'll remember this.]"
            )

        # Document buffer injection
        if doc_buffer.active:
            llm_command = doc_buffer.build_augmented_message(llm_command)

        # Non-streaming LLM call (web research tool calling supported)
        response = await _llm_fallback(
            llm, llm_command, history, web_researcher,
            memory_context=memory_context,
            conversation_messages=context_messages,
            max_tokens=600 if doc_buffer.active else None,
        )

        if not response:
            response = "I'm sorry, I'm having trouble processing that right now."
        else:
            response = llm.strip_filler(response)

    t_end = time.perf_counter()

    conversation.add_message("assistant", response)

    # Build stats
    stats = _build_stats(match_info, llm, used_llm, t_start, t_match, t_end)

    return {
        'response': response,
        'stats': stats,
        'used_llm': used_llm,
    }


async def _llm_fallback(llm, command, history, web_researcher,
                         memory_context=None, conversation_messages=None,
                         max_tokens=None) -> str:
    """Non-streaming LLM with web research tool calling support."""
    use_tools = web_researcher is not None

    if use_tools:
        # Use tool-calling stream, collect full response
        full_response = ""
        tool_call_request = None

        def _run_stream():
            nonlocal full_response, tool_call_request
            for item in llm.stream_with_tools(
                user_message=command,
                conversation_history=history,
                memory_context=memory_context,
                conversation_messages=conversation_messages,
            ):
                if isinstance(item, ToolCallRequest):
                    tool_call_request = item
                    break
                full_response += item

        await asyncio.to_thread(_run_stream)

        # Handle tool call (web search)
        if tool_call_request:
            query = tool_call_request.arguments.get("query", command)

            if tool_call_request.name == "web_search":
                results = await asyncio.to_thread(web_researcher.search, query)
                page_sections = await asyncio.to_thread(
                    web_researcher.fetch_pages_parallel, results
                )
                page_content = ""
                if page_sections:
                    page_content = "\n\nFull article content:\n\n" + \
                        "\n\n---\n\n".join(page_sections)
                tool_result = format_search_results(results) + page_content
            else:
                tool_result = f"Unknown tool: {tool_call_request.name}"

            # Collect synthesis response
            synthesis = ""
            def _run_synthesis():
                nonlocal synthesis
                for token in llm.continue_after_tool_call(
                    tool_call_request, tool_result
                ):
                    synthesis += token

            await asyncio.to_thread(_run_synthesis)
            return synthesis

        # Check for deflection
        if full_response and web_researcher and _is_deflection(full_response):
            return await _do_web_search(command, web_researcher, llm)

        return full_response

    else:
        # Simple non-streaming chat
        return await asyncio.to_thread(
            llm.chat,
            user_message=command,
            conversation_history=history,
            memory_context=memory_context,
            conversation_messages=conversation_messages,
            max_tokens=max_tokens,
        )


def _is_deflection(response: str) -> bool:
    """Detect when Qwen deflects instead of answering."""
    deflection_phrases = [
        "check official", "official channels", "official website",
        "check the latest", "latest information",
        "i don't have real-time", "i don't have access to real-time",
        "as of my last update", "as of my knowledge cutoff",
        "i cannot browse", "i'm unable to browse",
        "i recommend checking", "please check",
    ]
    lower = response.lower()
    return any(p in lower for p in deflection_phrases)


async def _do_web_search(command: str, web_researcher, llm) -> str:
    """Fallback web search when deflection detected."""
    results = await asyncio.to_thread(web_researcher.search, command)
    if not results:
        return await asyncio.to_thread(llm.chat, user_message=command, conversation_history=[])

    page_sections = await asyncio.to_thread(web_researcher.fetch_pages_parallel, results)
    page_content = ""
    if page_sections:
        page_content = "\n\nFull article content:\n\n" + "\n\n---\n\n".join(page_sections)

    search_context = format_search_results(results) + page_content

    return await asyncio.to_thread(
        llm.chat,
        user_message=f"Based on these search results:\n\n{search_context}\n\nAnswer: {command}",
        conversation_history=[],
    )


def _build_stats(match_info, llm, used_llm, t_start, t_match, t_end) -> dict:
    """Build stats dict for WebSocket delivery."""
    stats = {}
    total_ms = int((t_end - t_start) * 1000)
    stats['total_ms'] = total_ms

    if match_info:
        stats['layer'] = match_info.get('layer', '')
        stats['skill_name'] = match_info.get('skill_name', '')
        stats['handler'] = match_info.get('handler', '')
        conf = match_info.get('confidence')
        if conf is not None:
            stats['confidence'] = round(conf, 3)

    if used_llm:
        info = llm.last_call_info or {}
        stats['llm_model'] = info.get('model', '')
        tokens = info.get('tokens_used')
        if tokens:
            stats['llm_tokens'] = tokens

    return stats


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def websocket_handler(request):
    """Handle a single WebSocket client connection."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    app = request.app
    components = app['components']
    tts_proxy = app['tts_proxy']
    config = app['config']
    cmd_lock = app['cmd_lock']
    doc_buffer = components['doc_buffer']

    # Announcement pump task
    async def announcement_pump():
        """Periodically check for background announcements (reminders, news)."""
        while not ws.closed:
            announcements = tts_proxy.get_pending_announcements()
            for ann in announcements:
                try:
                    await ws.send_json({'type': 'announcement', 'content': ann})
                except Exception:
                    return
            await asyncio.sleep(1)

    pump_task = asyncio.create_task(announcement_pump())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get('type', '')

                if msg_type == 'message':
                    content = data.get('content', '').strip()
                    if not content:
                        continue

                    async with cmd_lock:
                        try:
                            result = await process_command(
                                content, components, tts_proxy, config
                            )
                            if result['response']:
                                await ws.send_json({
                                    'type': 'response',
                                    'content': result['response'],
                                })
                            if result['stats']:
                                await ws.send_json({
                                    'type': 'stats',
                                    'data': result['stats'],
                                })
                            # Send doc buffer status
                            await ws.send_json({
                                'type': 'doc_status',
                                'active': doc_buffer.active,
                                'tokens': doc_buffer.token_estimate,
                                'source': doc_buffer.source,
                            })
                        except Exception:
                            logger.exception("Error processing command")
                            await ws.send_json({
                                'type': 'error',
                                'content': "An error occurred processing your request.",
                            })

                elif msg_type == 'slash_command':
                    cmd = data.get('command', '')
                    await _handle_ws_slash(ws, cmd, data, doc_buffer)

                elif msg_type == 'toggle_voice':
                    enabled = data.get('enabled', False)
                    tts_proxy.hybrid = enabled
                    # Lazy-init real TTS if needed
                    if enabled and tts_proxy.real_tts is None:
                        try:
                            from core.tts import TextToSpeech
                            tts_proxy.real_tts = TextToSpeech(config)
                            logger.info("TTS initialized for voice mode")
                        except Exception:
                            logger.exception("Failed to initialize TTS")
                    await ws.send_json({
                        'type': 'voice_status',
                        'enabled': tts_proxy.hybrid,
                    })

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        pump_task.cancel()

    return ws


async def _handle_ws_slash(ws, cmd: str, data: dict, doc_buffer: DocumentBuffer):
    """Handle slash commands received via WebSocket."""
    if cmd == '/paste':
        content = data.get('content', '').strip()
        if content:
            doc_buffer.load(content, "paste")
            await ws.send_json({
                'type': 'doc_status',
                'active': True,
                'tokens': doc_buffer.token_estimate,
                'source': doc_buffer.source,
            })
            lines = content.count('\n') + 1
            await ws.send_json({
                'type': 'info',
                'content': f"Document loaded: ~{doc_buffer.token_estimate} tokens, {lines} lines (paste)",
            })
        else:
            await ws.send_json({
                'type': 'info',
                'content': "Nothing pasted.",
            })

    elif cmd == '/clear':
        old_source, old_tokens = doc_buffer.clear()
        await ws.send_json({
            'type': 'doc_status',
            'active': False,
            'tokens': 0,
            'source': '',
        })
        if old_source:
            await ws.send_json({
                'type': 'info',
                'content': f"Document buffer cleared ({old_source}, ~{old_tokens} tokens).",
            })
        else:
            await ws.send_json({
                'type': 'info',
                'content': "Document buffer is already empty.",
            })

    elif cmd == '/help':
        await ws.send_json({
            'type': 'info',
            'content': "JARVIS Web UI — type naturally to interact. "
                       "Use the toolbar buttons for paste, clear, and help.",
        })


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

async def index_handler(request):
    """Serve index.html for the root path."""
    return web.FileResponse(Path(__file__).parent / 'web' / 'index.html')


def create_app(config) -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()

    web_dir = Path(__file__).parent / 'web'

    # Routes: WebSocket, root index, then static assets
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/', index_handler)
    app.router.add_static('/', web_dir)

    return app


async def on_startup(app):
    """Initialize JARVIS components on server startup."""
    config = app['config']

    tts_proxy = WebTTSProxy()
    app['tts_proxy'] = tts_proxy

    logger.info("Initializing JARVIS components...")
    components = await asyncio.to_thread(init_components, config, tts_proxy)
    app['components'] = components
    app['cmd_lock'] = asyncio.Lock()

    skill_count = len(components['skill_manager'].skills)
    logger.info("JARVIS Web UI ready — %d skills loaded", skill_count)


async def on_shutdown(app):
    """Clean shutdown of components."""
    components = app.get('components', {})

    mm = components.get('memory_manager')
    if mm:
        mm.save()

    nm = components.get('news_manager')
    if nm:
        nm.stop()

    cm = components.get('calendar_manager')
    if cm:
        cm.stop()

    rm = components.get('reminder_manager')
    if rm:
        rm.stop()

    logger.info("JARVIS Web UI shut down")


def main():
    parser = argparse.ArgumentParser(description="JARVIS Web UI")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--voice", action="store_true", help="Start with voice enabled")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()

    host = args.host or config.get("web.host", "127.0.0.1")
    port = args.port or config.get("web.port", 8088)

    app = create_app(config)
    app['config'] = config

    if args.voice:
        # Will be set once TTS proxy is created in on_startup
        app['start_with_voice'] = True

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    print(f"\n  JARVIS Web UI → http://{host}:{port}\n")
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
