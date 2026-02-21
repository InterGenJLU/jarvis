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
from core import persona
from core.conversation_state import ConversationState
from core.context_window import get_context_window
from core.document_buffer import DocumentBuffer, BINARY_EXTENSIONS
from core.speech_chunker import SpeechChunker

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

        # Don't start RM background polling in web mode — the voice pipeline
        # handles proactive reminders/rundowns.  The RM is still available for
        # explicit commands ("daily rundown", "remind me...").
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

    # Centralized conversation state (Phase 2 of conversational flow refactor)
    components['conv_state'] = ConversationState()

    return components


# ---------------------------------------------------------------------------
# Command processing — 6-priority pipeline (mirrors jarvis_console.py)
# ---------------------------------------------------------------------------

async def process_command(command: str, components: dict, tts_proxy: WebTTSProxy,
                          config: dict, ws=None) -> dict:
    """Process a user command through the full pipeline.

    Returns dict with 'response', 'stats', 'used_llm', 'streamed', etc.
    When ws is provided, LLM responses are streamed token-by-token over WebSocket.
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
    conv_state = components['conv_state']

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
    # NOTE: Only intercept when the rundown was explicitly triggered via web UI
    # (not background polling, which duplicates the voice pipeline's offers).
    if reminder_manager and reminder_manager.is_rundown_pending():
        text_lower = command.strip().lower()
        words = set(re.findall(r'\b\w+\b', text_lower))
        negative = bool(
            words & {"no", "later", "hold", "skip"}
            or "not now" in text_lower
            or "not yet" in text_lower
        )
        if negative:
            reminder_manager.defer_rundown()
            response = persona.rundown_defer()
            skill_handled = True
        elif words & {"yes", "yeah", "yep", "sure", "go", "ready", "proceed"}:
            await asyncio.to_thread(reminder_manager.deliver_rundown)
            response = ""
            skill_handled = True
        # If neither affirmative nor negative, fall through to normal routing
        # (user typed an unrelated command while rundown was pending)

    # Priority 2: Reminder acknowledgment
    if not skill_handled and reminder_manager and reminder_manager.is_awaiting_ack():
        reminder_manager.acknowledge_last()
        response = persona.pick("reminder_ack")
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
            response = persona.pick("fact_stored")
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
            response = persona.pick("news_pullup")
            skill_handled = True

    # Priority 6: News continue
    if not skill_handled and news_manager:
        continue_words = ["continue", "keep going", "more headlines", "go on", "read more"]
        if any(w in command.strip().lower() for w in continue_words):
            remaining = news_manager.get_unread_count()
            if sum(remaining.values()) > 0:
                response = await asyncio.to_thread(news_manager.read_headlines, limit=5)
                skill_handled = True

    # LLM fallback (streaming over WebSocket when ws is available)
    streamed = False
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

        # Streaming LLM over WebSocket
        if ws:
            response, streamed = await _stream_llm_ws(
                ws, llm, llm_command, history, web_researcher,
                memory_context=memory_context,
                conversation_messages=context_messages,
                max_tokens=600 if doc_buffer.active else None,
            )
        else:
            response = await _llm_fallback(
                llm, llm_command, history, web_researcher,
                memory_context=memory_context,
                conversation_messages=context_messages,
                max_tokens=600 if doc_buffer.active else None,
            )

        if not response:
            response = "I'm sorry, I'm having trouble processing that right now."
            streamed = False  # Force non-streamed so error message gets sent
        elif not streamed:
            # Only strip filler for non-streamed responses;
            # _stream_llm_ws already strips before stream_end
            response = llm.strip_filler(response)

    t_end = time.perf_counter()

    conversation.add_message("assistant", response)

    # Update centralized conversation state
    conv_state.update(
        command=command,
        response_text=response or "",
        response_type="llm" if not skill_handled else "skill",
    )

    # Build stats
    stats = _build_stats(match_info, llm, used_llm, t_start, t_match, t_end)

    return {
        'response': response,
        'stats': stats,
        'used_llm': used_llm,
        'streamed': streamed,
    }


async def _stream_llm_ws(ws, llm, command, history, web_researcher,
                          memory_context=None, conversation_messages=None,
                          max_tokens=None) -> tuple:
    """Stream LLM response over WebSocket with quality gate and tool calling.

    Returns (response_text, streamed_bool).
    When streamed_bool is True, stream_start/stream_end were sent over ws.
    When False, caller should send a normal 'response' message.
    """
    use_tools = llm.tool_calling and web_researcher
    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _producer():
        """Sync thread: run LLM streaming, push items to async queue."""
        try:
            source = (
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
            for item in source:
                asyncio.run_coroutine_threadsafe(queue.put(('item', item)), loop)
        except Exception as e:
            logger.error("LLM streaming producer error: %s", e)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(('done', None)), loop)

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    chunker = SpeechChunker()
    full_response = ""
    buffered_tokens = ""
    stream_started = False
    first_chunk_checked = False
    tool_call_request = None

    # --- Phase 1: Consume tokens, buffer until quality gate passes ---
    while True:
        try:
            tag, value = await asyncio.wait_for(queue.get(), timeout=60)
        except asyncio.TimeoutError:
            break

        if tag == 'done':
            break

        if tag == 'item':
            if isinstance(value, ToolCallRequest):
                tool_call_request = value
                break

            token = value
            full_response += token

            if not stream_started:
                # Buffer tokens until first sentence for quality gate
                buffered_tokens += token
                chunk = chunker.feed(token)
                if chunk and not first_chunk_checked:
                    first_chunk_checked = True
                    quality_issue = llm._check_response_quality(chunk, command)
                    if quality_issue:
                        # Quality retry — fall back to non-streaming
                        await ws.send_json({
                            'type': 'info',
                            'content': f'Quality retry: {quality_issue}',
                        })
                        retry = await asyncio.to_thread(
                            llm.chat,
                            user_message=command,
                            conversation_history=history,
                        )
                        # Drain remaining producer output
                        thread.join(timeout=5)
                        return (retry or "", False)
                    # Quality OK — start streaming, flush buffer
                    await ws.send_json({'type': 'stream_start'})
                    await ws.send_json({
                        'type': 'stream_token',
                        'token': buffered_tokens,
                    })
                    stream_started = True
            else:
                await ws.send_json({'type': 'stream_token', 'token': token})

    # --- Handle tool call (web search) ---
    if tool_call_request:
        query = tool_call_request.arguments.get('query', command)
        await ws.send_json({
            'type': 'info',
            'content': f'Searching: {query}',
        })

        if tool_call_request.name == 'web_search':
            results = await asyncio.to_thread(web_researcher.search, query)
            page_sections = await asyncio.to_thread(
                web_researcher.fetch_pages_parallel, results
            )
            page_content = ""
            if page_sections:
                page_content = ("\n\nFull article content:\n\n"
                                + "\n\n---\n\n".join(page_sections))
            tool_result = format_search_results(results) + page_content
            await ws.send_json({
                'type': 'info',
                'content': f'Found {len(results)} results',
            })
        else:
            tool_result = f"Unknown tool: {tool_call_request.name}"

        # Stream synthesis response
        synthesis_queue = asyncio.Queue()

        def _synthesis_producer():
            try:
                for token in llm.continue_after_tool_call(
                    tool_call_request, tool_result
                ):
                    asyncio.run_coroutine_threadsafe(
                        synthesis_queue.put(('item', token)), loop
                    )
            except Exception as e:
                logger.error("Synthesis streaming error: %s", e)
            finally:
                asyncio.run_coroutine_threadsafe(
                    synthesis_queue.put(('done', None)), loop
                )

        syn_thread = threading.Thread(target=_synthesis_producer, daemon=True)
        syn_thread.start()

        await ws.send_json({'type': 'stream_start'})
        synthesis = ""
        while True:
            try:
                tag, value = await asyncio.wait_for(
                    synthesis_queue.get(), timeout=60
                )
            except asyncio.TimeoutError:
                break
            if tag == 'done':
                break
            if tag == 'item':
                synthesis += value
                await ws.send_json({'type': 'stream_token', 'token': value})

        cleaned = llm.strip_filler(synthesis) if synthesis else ""
        await ws.send_json({
            'type': 'stream_end',
            'full_response': cleaned,
        })
        return (cleaned, True)

    # --- Handle short response (no sentence boundary hit) ---
    if not stream_started:
        remaining = chunker.flush()
        if remaining and not first_chunk_checked:
            quality_issue = llm._check_response_quality(remaining, command)
            if quality_issue:
                await ws.send_json({
                    'type': 'info',
                    'content': f'Quality retry: {quality_issue}',
                })
                retry = await asyncio.to_thread(
                    llm.chat,
                    user_message=command,
                    conversation_history=history,
                )
                return (retry or "", False)
        # Short enough to send as non-streaming response
        return (full_response, False)

    # --- Deflection safety net ---
    if full_response and web_researcher and _is_deflection(full_response):
        await ws.send_json({
            'type': 'info',
            'content': 'Searching for current information...',
        })
        fallback = await _do_web_search(command, web_researcher, llm)
        await ws.send_json({
            'type': 'stream_end',
            'full_response': fallback or "",
        })
        return (fallback or "", True)

    # --- Normal end ---
    cleaned = llm.strip_filler(full_response) if full_response else ""
    await ws.send_json({
        'type': 'stream_end',
        'full_response': cleaned,
    })
    return (cleaned, True)


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


def _extract_health_data(skill_manager) -> dict | None:
    """Check if developer_tools just ran a health check and extract the data.

    Returns dict with 'layers' (filtered check data) and 'brief' (corrected
    voice summary matching filtered data) or None.
    """
    dt_skill = skill_manager.skills.get('developer_tools')
    if dt_skill:
        data = getattr(dt_skill, '_last_health_data', None)
        if data:
            dt_skill._last_health_data = None  # consume it
            # Filter out checks not applicable in web mode
            # (no mic, no Coordinator/pipeline)
            skip_names = {'Audio Input'}
            skip_phrases = {'Coordinator not available'}
            filtered = {}
            for layer, checks in data.items():
                kept = [
                    c for c in checks
                    if c['name'] not in skip_names
                    and not any(p in c.get('summary', '') for p in skip_phrases)
                ]
                if kept:
                    filtered[layer] = kept
            # Generate corrected brief from filtered data
            from core.health_check import format_voice_brief
            brief = format_voice_brief(filtered)
            return {'layers': filtered, 'brief': brief}
    return None


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

    # Send current session messages + session list on connect
    try:
        conversation = components['conversation']
        all_messages = await asyncio.to_thread(conversation.load_full_history)
        sessions = _detect_sessions(all_messages)
        meta = _load_sessions_meta(config)

        # Apply custom names
        for s in sessions:
            s['custom_name'] = meta.get(s['id'], None)

        # Send session list (first 10)
        await ws.send_json({
            'type': 'session_list',
            'sessions': sessions[:10],
            'has_more': len(sessions) > 10,
            'total': len(sessions),
        })

        # Send current (most recent) session's messages
        if sessions:
            current = sessions[0]
            current_msgs = [
                m for m in all_messages
                if current['start_ts'] <= m.get('timestamp', 0) <= current['end_ts']
            ]
            await ws.send_json({
                'type': 'history',
                'messages': current_msgs,
                'session_id': current['id'],
            })
    except Exception:
        logger.exception("Failed to send history on connect")

    # Send system stats on connect for header readout
    try:
        sys_stats = _gather_system_stats(components)
        await ws.send_json({'type': 'system_stats', 'data': sys_stats})
    except Exception:
        logger.exception("Failed to send system stats on connect")

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
                                content, components, tts_proxy, config,
                                ws=ws,
                            )
                            # Drain announcements queued during command processing
                            # (skills call tts_proxy.speak() which would duplicate
                            # the response as a gold announcement banner)
                            tts_proxy.get_pending_announcements()

                            # Check for structured health data from developer_tools
                            health_data = _extract_health_data(components['skill_manager'])

                            # Use corrected brief when health data is present
                            # (raw brief counts web-irrelevant warnings)
                            response_text = result['response']
                            if health_data and health_data.get('brief'):
                                response_text = health_data['brief']

                            # Only send response message if not already streamed
                            if not result.get('streamed') and response_text:
                                await ws.send_json({
                                    'type': 'response',
                                    'content': response_text,
                                })
                            if result['stats']:
                                await ws.send_json({
                                    'type': 'stats',
                                    'data': result['stats'],
                                })
                            # Send structured health report for rich rendering
                            if health_data:
                                await ws.send_json({
                                    'type': 'health_report',
                                    'data': health_data['layers'],
                                })
                            # Send doc buffer status
                            await ws.send_json({
                                'type': 'doc_status',
                                'active': doc_buffer.active,
                                'tokens': doc_buffer.token_estimate,
                                'source': doc_buffer.source,
                            })
                            # Send updated system stats for header readout
                            await ws.send_json({
                                'type': 'system_stats',
                                'data': _gather_system_stats(components),
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

                elif msg_type == 'file_drop':
                    filename = data.get('filename', 'unknown')
                    content = data.get('content', '')
                    ext = Path(filename).suffix.lower()
                    if ext in BINARY_EXTENSIONS:
                        await ws.send_json({
                            'type': 'info',
                            'content': f"Cannot load binary file ({ext}): {filename}",
                        })
                    elif content:
                        doc_buffer.load(content, f"file:{filename}")
                        await _send_doc_loaded(ws, doc_buffer, f"file:{filename}", content)
                    else:
                        await ws.send_json({
                            'type': 'info',
                            'content': f"File is empty: {filename}",
                        })

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

                elif msg_type == 'restart':
                    logger.info("Restart requested via web UI")
                    await ws.send_json({'type': 'info', 'content': 'Restarting...'})
                    # Schedule restart after WebSocket closes cleanly
                    asyncio.get_event_loop().call_later(0.5, _restart_server)

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        pump_task.cancel()

    return ws


def _restart_server():
    """Re-exec the server process. Client auto-reconnects."""
    logger.info("Re-executing server process...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def _handle_ws_slash(ws, cmd: str, data: dict, doc_buffer: DocumentBuffer):
    """Handle slash commands received via WebSocket."""
    if cmd == '/paste':
        content = data.get('content', '').strip()
        if content:
            doc_buffer.load(content, "paste")
            await _send_doc_loaded(ws, doc_buffer, "paste", content)
        else:
            await ws.send_json({'type': 'info', 'content': "Nothing pasted."})

    elif cmd == '/append':
        content = data.get('content', '').strip()
        if content:
            doc_buffer.append(content, "paste")
            lines = content.count('\n') + 1
            await ws.send_json({
                'type': 'doc_status',
                'active': True,
                'tokens': doc_buffer.token_estimate,
                'source': doc_buffer.source,
            })
            await ws.send_json({
                'type': 'info',
                'content': f"Appended {lines} lines (~{doc_buffer.token_estimate} tokens total)",
            })
        else:
            await ws.send_json({'type': 'info', 'content': "Nothing to append."})

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

    elif cmd == '/file':
        file_path = data.get('path', '').strip()
        if not file_path:
            await ws.send_json({'type': 'info', 'content': "Usage: /file <path>"})
            return
        await _load_file_into_buffer(ws, doc_buffer, file_path)

    elif cmd == '/clipboard':
        try:
            import subprocess
            result = subprocess.run(
                ['wl-paste'], capture_output=True, text=True, timeout=5,
                env={**os.environ, 'DISPLAY': ':0'},
            )
            content = result.stdout.strip()
            if content:
                doc_buffer.load(content, "clipboard")
                await _send_doc_loaded(ws, doc_buffer, "clipboard", content)
            else:
                await ws.send_json({
                    'type': 'info',
                    'content': "Clipboard is empty.",
                })
        except Exception as e:
            await ws.send_json({
                'type': 'info',
                'content': f"Failed to read clipboard: {e}",
            })

    elif cmd == '/context':
        if doc_buffer.active:
            preview = doc_buffer.content[:300]
            if len(doc_buffer.content) > 300:
                preview += "..."
            await ws.send_json({
                'type': 'info',
                'content': (
                    f"Document buffer active: ~{doc_buffer.token_estimate} tokens, "
                    f"source: {doc_buffer.source}\n"
                    f"Preview: {preview}"
                ),
            })
        else:
            await ws.send_json({
                'type': 'info',
                'content': "Document buffer is empty.",
            })

    elif cmd == '/help':
        await ws.send_json({
            'type': 'info',
            'content': "JARVIS Web UI — type naturally to interact. "
                       "Use the toolbar buttons for paste, clear, file, clipboard, and help.",
        })


async def _send_doc_loaded(ws, doc_buffer, source_label, content):
    """Send doc_status + info after loading content into the buffer."""
    await ws.send_json({
        'type': 'doc_status',
        'active': True,
        'tokens': doc_buffer.token_estimate,
        'source': doc_buffer.source,
    })
    lines = content.count('\n') + 1
    await ws.send_json({
        'type': 'info',
        'content': f"Document loaded: ~{doc_buffer.token_estimate} tokens, {lines} lines ({source_label})",
    })


async def _load_file_into_buffer(ws, doc_buffer, file_path):
    """Load a file from the server filesystem into the document buffer."""
    p = Path(file_path).expanduser().resolve()
    if not p.exists():
        await ws.send_json({'type': 'info', 'content': f"File not found: {file_path}"})
        return
    if not p.is_file():
        await ws.send_json({'type': 'info', 'content': f"Not a file: {file_path}"})
        return
    ext = p.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        await ws.send_json({
            'type': 'info',
            'content': f"Cannot load binary file ({ext}): {p.name}",
        })
        return
    try:
        size = p.stat().st_size
        if size > 500_000:
            await ws.send_json({
                'type': 'info',
                'content': f"File too large ({size:,} bytes, max 500KB): {p.name}",
            })
            return
        content = p.read_text(errors='replace')
        doc_buffer.load(content, f"file:{p.name}")
        await _send_doc_loaded(ws, doc_buffer, f"file:{p.name}", content)
    except Exception as e:
        await ws.send_json({'type': 'info', 'content': f"Failed to read file: {e}"})


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

SESSION_GAP_SECONDS = 1800  # 30 minutes

def _detect_sessions(messages: list[dict], gap_seconds: int = SESSION_GAP_SECONDS) -> list[dict]:
    """Detect session boundaries from timestamp gaps in message history.

    Returns list of sessions (most recent first), each with:
        id, start_ts, end_ts, message_count, preview
    """
    if not messages:
        return []

    sessions = []
    current_start = 0  # index into messages

    for i in range(1, len(messages)):
        prev_ts = messages[i - 1].get('timestamp', 0)
        curr_ts = messages[i].get('timestamp', 0)
        if curr_ts - prev_ts > gap_seconds:
            # Close current session
            session_msgs = messages[current_start:i]
            sessions.append(_build_session(session_msgs))
            current_start = i

    # Final session (always exists if messages is non-empty)
    session_msgs = messages[current_start:]
    sessions.append(_build_session(session_msgs))

    sessions.reverse()  # Most recent first
    return sessions


def _build_session(msgs: list[dict]) -> dict:
    """Build a session dict from a slice of messages."""
    start_ts = msgs[0].get('timestamp', 0)
    end_ts = msgs[-1].get('timestamp', 0)
    # Preview = first user message, truncated
    preview = ''
    for m in msgs:
        if m.get('role') == 'user':
            preview = m.get('content', '')[:80]
            break
    return {
        'id': str(start_ts),
        'start_ts': start_ts,
        'end_ts': end_ts,
        'message_count': len(msgs),
        'preview': preview,
    }


def _sessions_meta_path(config) -> Path:
    storage = Path(config.get("system.storage_path"))
    return storage / "data" / "conversations" / "sessions_meta.json"


def _load_sessions_meta(config) -> dict:
    """Load custom session names from disk."""
    p = _sessions_meta_path(config)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_sessions_meta(config, meta: dict):
    """Persist custom session names to disk."""
    p = _sessions_meta_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2), encoding='utf-8')


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

async def sessions_handler(request):
    """GET /api/sessions — Return session list for sidebar.

    Query params:
        offset: Number of sessions to skip (default 0)
        limit: Max sessions to return (default 10, max 50)
    """
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    config = request.app['config']
    conversation = components['conversation']
    offset = int(request.query.get('offset', 0))
    limit = min(int(request.query.get('limit', 10)), 50)

    all_messages = await asyncio.to_thread(conversation.load_full_history)
    sessions = _detect_sessions(all_messages)
    meta = _load_sessions_meta(config)

    # Apply custom names
    for s in sessions:
        s['custom_name'] = meta.get(s['id'], None)

    total = len(sessions)
    page = sessions[offset:offset + limit]

    return web.json_response({
        'sessions': page,
        'has_more': (offset + limit) < total,
        'total': total,
    })


async def session_messages_handler(request):
    """GET /api/session/{session_id} — Return messages for a specific session."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    session_id = request.match_info['session_id']
    conversation = components['conversation']

    all_messages = await asyncio.to_thread(conversation.load_full_history)
    sessions = _detect_sessions(all_messages)

    # Find matching session — session_id is the start_ts as string
    for s in sessions:
        if s['id'] == session_id:
            # Extract messages in this time range from the original (chronological) list
            msgs = [
                m for m in all_messages
                if s['start_ts'] <= m.get('timestamp', 0) <= s['end_ts']
            ]
            return web.json_response({'messages': msgs, 'session': s})

    return web.json_response({'error': 'Session not found'}, status=404)


async def session_rename_handler(request):
    """PUT /api/session/{session_id}/rename — Rename a session."""
    config = request.app['config']

    session_id = request.match_info['session_id']
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    name = body.get('name', '').strip()
    if not name:
        return web.json_response({'error': 'Name required'}, status=400)
    if len(name) > 200:
        return web.json_response({'error': 'Name too long (max 200 chars)'}, status=400)

    meta = _load_sessions_meta(config)
    meta[session_id] = name
    _save_sessions_meta(config, meta)

    return web.json_response({'ok': True})


async def history_handler(request):
    """GET /api/history — Return recent chat messages for scroll-back.

    Query params:
        before: Unix timestamp — return messages before this time (for pagination)
        limit: Max messages to return (default 50, max 200)
    """
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    conversation = components['conversation']
    before = request.query.get('before')
    limit = min(int(request.query.get('limit', 50)), 200)

    # Load all messages from disk (personal assistant — file is manageable)
    all_messages = await asyncio.to_thread(conversation.load_full_history)

    # Filter by timestamp if paginating
    if before:
        before_ts = float(before)
        all_messages = [m for m in all_messages if m.get('timestamp', 0) < before_ts]

    # Return the most recent `limit` messages
    page = all_messages[-limit:] if len(all_messages) > limit else all_messages

    return web.json_response({
        'messages': page,
        'has_more': len(all_messages) > limit,
    })


async def upload_handler(request):
    """Handle file upload via POST /api/upload."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    doc_buffer = components['doc_buffer']

    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != 'file':
            return web.json_response({'error': 'No file field'}, status=400)

        filename = field.filename or 'upload'
        ext = Path(filename).suffix.lower()
        if ext in BINARY_EXTENSIONS:
            return web.json_response({
                'error': f'Binary file type not supported: {ext}',
            }, status=400)

        # Read content with size limit
        content = b''
        while True:
            chunk = await field.read_chunk(8192)
            if not chunk:
                break
            content += chunk
            if len(content) > 500_000:
                return web.json_response({
                    'error': 'File too large (max 500KB)',
                }, status=400)

        text = content.decode('utf-8', errors='replace')
        doc_buffer.load(text, f"file:{filename}")

        return web.json_response({
            'active': True,
            'tokens': doc_buffer.token_estimate,
            'source': doc_buffer.source,
            'lines': text.count('\n') + 1,
        })
    except Exception as e:
        logger.exception("Upload error")
        return web.json_response({'error': str(e)}, status=500)


def _gather_system_stats(components: dict) -> dict:
    """Collect system-level stats from all components for header readout."""
    data = {}

    # LLM
    llm = components.get('llm')
    if llm:
        model_name = Path(llm.local_model_path).stem if llm.local_model_path else None
        data['llm'] = {
            'model': model_name,
            'api_fallback': llm.api_model if llm.api_key_env else None,
        }
    else:
        data['llm'] = None

    # Web research
    data['web_research'] = components.get('web_researcher') is not None

    # Memory
    mm = components.get('memory_manager')
    if mm:
        data['memory'] = {
            'vectors': mm.faiss_index.ntotal if mm.faiss_index else 0,
            'proactive': mm.proactive_enabled,
        }
    else:
        data['memory'] = None

    # Context window
    cw = components.get('context_window')
    if cw and cw.enabled:
        cw_stats = cw.get_stats()
        data['context_window'] = {
            'segments': cw_stats['segments'],
            'tokens': cw_stats['estimated_tokens'],
            'open': cw_stats['open_segment'],
        }
    else:
        data['context_window'] = None

    # Skills
    sm = components.get('skill_manager')
    data['skills_loaded'] = len(sm.skills) if sm else 0

    # Reminders
    rm = components.get('reminder_manager')
    if rm:
        pending = rm.list_reminders(status="pending")
        data['reminders'] = {'active': len(pending)}
    else:
        data['reminders'] = None

    # News
    nm = components.get('news_manager')
    if nm:
        data['news'] = {'feeds': len(nm.feeds)}
    else:
        data['news'] = None

    # Calendar
    data['calendar'] = components.get('calendar_manager') is not None

    return data


async def browse_handler(request):
    """GET /api/browse — List directory contents for file browser.

    Query params:
        path: Directory path to list (default: /home/user)
    """
    raw_path = request.query.get('path', '/home/user')
    p = Path(raw_path).expanduser().resolve()

    if not p.is_dir():
        return web.json_response({'error': f'Not a directory: {raw_path}'}, status=400)

    entries = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            entry = {'name': item.name, 'type': 'dir' if item.is_dir() else 'file'}
            if item.is_file():
                try:
                    entry['size'] = item.stat().st_size
                except OSError:
                    entry['size'] = 0
                entry['ext'] = item.suffix.lower()
                entry['binary'] = item.suffix.lower() in BINARY_EXTENSIONS
            entries.append(entry)
            if len(entries) >= 200:
                break
    except PermissionError:
        return web.json_response({'error': f'Permission denied: {p}'}, status=403)

    parent = str(p.parent) if p != p.parent else None

    return web.json_response({
        'path': str(p),
        'parent': parent,
        'entries': entries,
    })


async def stats_overview_handler(request):
    """GET /api/stats — Return system overview stats for header readout."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    data = _gather_system_stats(components)
    return web.json_response(data)


async def index_handler(request):
    """Serve index.html for the root path."""
    return web.FileResponse(Path(__file__).parent / 'web' / 'index.html')


def create_app(config) -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()

    web_dir = Path(__file__).parent / 'web'

    # Routes: WebSocket, API, root index, then static assets
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/api/history', history_handler)
    app.router.add_get('/api/sessions', sessions_handler)
    app.router.add_get('/api/session/{session_id}', session_messages_handler)
    app.router.add_put('/api/session/{session_id}/rename', session_rename_handler)
    app.router.add_post('/api/upload', upload_handler)
    app.router.add_get('/api/browse', browse_handler)
    app.router.add_get('/api/stats', stats_overview_handler)
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
