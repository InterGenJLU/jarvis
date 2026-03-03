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
os.environ['TQDM_DISABLE'] = '1'  # Suppress sentence-transformer progress bars

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
os.environ['JARVIS_LOG_TARGET'] = 'web'

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
from core.conversation_router import ConversationRouter
from core.context_window import get_context_window
from core.document_buffer import DocumentBuffer, BINARY_EXTENSIONS
from core.speech_chunker import SpeechChunker
from core.metrics_tracker import get_metrics_tracker
from core.self_awareness import SelfAwareness
from core.task_planner import TaskPlanner
from core.interaction_cache import get_interaction_cache, Artifact


def _make_artifact_id() -> str:
    """Generate a short unique artifact ID."""
    import uuid
    return uuid.uuid4().hex[:16]

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
        # Wire reminder manager for tool-calling dispatch
        from core.tool_executor import (set_reminder_manager, set_config as set_tool_config,
                                        set_current_user_fn)
        set_reminder_manager(rm)
        set_current_user_fn(
            lambda: getattr(conversation, 'current_user', None) or 'christopher'
        )
        set_tool_config(config)

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

    # Interaction artifact cache
    from core.interaction_cache import get_interaction_cache
    components['interaction_cache'] = get_interaction_cache(config=config)

    # LLM metrics tracking
    components['metrics'] = get_metrics_tracker(config)

    # Self-awareness layer (Phase 1 of task planner)
    components['self_awareness'] = SelfAwareness(
        skill_manager=components['skill_manager'],
        metrics=components['metrics'],
        memory_manager=components['memory_manager'],
        context_window=components['context_window'],
        config=config,
    )

    # Task planner (Phase 2-3 of task planner)
    components['task_planner'] = TaskPlanner(
        llm=components['llm'],
        skill_manager=components['skill_manager'],
        self_awareness=components['self_awareness'],
        conversation=conversation,
        config=config,
        event_queue=None,  # Web: no voice interrupt queue
        context_window=components.get('context_window'),
        web_researcher=components['web_researcher'],
    )

    # Centralized conversation state (Phase 2 of conversational flow refactor)
    components['conv_state'] = ConversationState()

    # Unified awareness assembler
    components['awareness'] = None
    try:
        from core.awareness import AwarenessAssembler
        cal_mgr = None
        rm = components.get('reminder_manager')
        if rm and hasattr(rm, '_calendar_manager'):
            cal_mgr = rm._calendar_manager
        components['awareness'] = AwarenessAssembler(
            memory_manager=components['memory_manager'],
            people_manager=None,  # Web frontend doesn't have people_manager
            self_awareness=components['self_awareness'],
            calendar_manager=cal_mgr,
            news_manager=components['news_manager'],
            context_window=components['context_window'],
            config=config,
        )
    except Exception as e:
        logger.warning(f"Awareness assembler init failed (non-fatal): {e}")

    # Shared command router (Phase 3 of conversational flow refactor)
    components['router'] = ConversationRouter(
        skill_manager=components['skill_manager'],
        conversation=conversation,
        llm=components['llm'],
        reminder_manager=components['reminder_manager'],
        memory_manager=components['memory_manager'],
        news_manager=components['news_manager'],
        context_window=components['context_window'],
        conv_state=components['conv_state'],
        config=config,
        web_researcher=components['web_researcher'],
        self_awareness=components['self_awareness'],
        task_planner=components['task_planner'],
        awareness=components['awareness'],
    )

    return components


# ---------------------------------------------------------------------------
# Readback offer follow-up (recipes, instructions, how-to, steps)
# ---------------------------------------------------------------------------

_AFFIRM_WORDS = {"yes", "yeah", "yep", "yup", "sure", "please", "go ahead",
                 "yes please", "go for it", "read it", "absolutely", "ok",
                 "okay", "definitely"}


def _is_readback_affirm(command: str, last_response: str) -> bool:
    """Check if user is affirming an offer to read (recipe, instructions, steps, etc.)."""
    cmd = command.strip().lower().rstrip(".,!?")
    if cmd not in _AFFIRM_WORDS:
        return False
    # Verify last response contained a recipe offer
    offer_phrases = ["would you like me to read", "want me to read",
                     "shall i read", "like me to go through"]
    lower_resp = last_response.lower()
    return any(p in lower_resp for p in offer_phrases)


async def _stream_readback(ws, llm, cached_tool_result: str,
                                   conv_state=None,
                                   prior_synthesis: str = None) -> tuple:
    """Stream full content (recipe, instructions, steps) from cached search results."""
    import requests as _requests
    from core.honorific import get_honorific, get_formal_address

    h = get_honorific()
    formal = get_formal_address()
    if formal:
        honorific_rule = f"YOU MUST address the user as '{h}' or '{formal}'."
    else:
        honorific_rule = f"YOU MUST address the user as '{h}'."

    # Tell readback which result was picked — prefer cache artifact over conv_state
    prior_pick = ""
    _pick_source = prior_synthesis or (conv_state.last_response_text if conv_state else "")
    if _pick_source:
        prior_pick = (
            f"\nIn your previous response you recommended this:\n"
            f'"{_pick_source}"\n'
            "YOU MUST read from the SAME source you recommended above.\n"
        )

    # Build a simple messages list: system + tool result + read instruction
    messages = [
        {"role": "system", "content": "You are JARVIS, a personal AI assistant."},
        {"role": "user", "content": (
            f"Here are search results:\n\n{cached_tool_result}\n\n"
            f"{prior_pick}"
            "The user has asked you to read the full content (recipe, instructions, steps, etc.).\n"
            "RULES — follow these EXACTLY:\n"
            "1. YOU MUST read from the SAME source you recommended in your previous response. "
            "DO NOT switch to a different source. DO NOT combine, consolidate, or merge "
            "content from multiple sources.\n"
            "2. Read ALL of the content from that single result in full. If it is a recipe, "
            "list ALL ingredients with exact quantities, then ALL steps in order. If it is "
            "instructions or a how-to, read every step. DO NOT summarize or skip anything.\n"
            f"3. {honorific_rule}\n"
            "4. DO NOT start with filler. Jump straight into the content.\n"
            "5. Present it clearly and in logical order.\n"
            "6. DO NOT explain your reasoning about which rules you are following. "
            "Just deliver the content naturally."
        )},
    ]

    # --- Debug logging (temporary — remove after readback tuning) ---
    logger.info("READBACK: cached_tool_result length = %d chars", len(cached_tool_result))
    logger.info("READBACK: cached_tool_result (first 500):\n%s", cached_tool_result[:500])
    logger.info("READBACK: prior_pick context:\n%s", prior_pick[:300] if prior_pick else "(none)")
    logger.info("READBACK: max_tokens = 4096")

    await ws.send_json({'type': 'stream_start'})
    full_response = ""
    loop = asyncio.get_event_loop()
    queue = asyncio.Queue()

    def _producer():
        try:
            resp = _requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "messages": messages,
                    "temperature": llm.temperature,
                    "top_p": llm.top_p,
                    "top_k": llm.top_k,
                    "max_tokens": 4096,
                    "stream": True,
                },
                timeout=120,
                stream=True,
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    import json as _json
                    chunk = _json.loads(data)
                    token = chunk["choices"][0].get("delta", {}).get("content", "")
                    if token:
                        asyncio.run_coroutine_threadsafe(
                            queue.put(('item', token)), loop
                        )
                except (KeyError, _json.JSONDecodeError):
                    continue
        except Exception as e:
            logger.error("Recipe readback streaming error: %s", e)
        finally:
            asyncio.run_coroutine_threadsafe(
                queue.put(('done', None)), loop
            )

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    while True:
        try:
            tag, value = await asyncio.wait_for(queue.get(), timeout=60)
        except asyncio.TimeoutError:
            break
        if tag == 'done':
            break
        if tag == 'item':
            full_response += value
            await ws.send_json({'type': 'stream_token', 'token': value})

    cleaned = llm.strip_filler(full_response) if full_response else ""
    # --- Debug logging (temporary — remove after readback tuning) ---
    logger.info("READBACK: raw response length = %d chars", len(full_response))
    logger.info("READBACK: full response:\n%s", full_response)
    await ws.send_json({'type': 'stream_end', 'full_response': cleaned})
    return (cleaned, True)


# ---------------------------------------------------------------------------
# Command processing — shared router (Phase 3 of conversational flow refactor)
# ---------------------------------------------------------------------------

async def process_command(command: str, components: dict, tts_proxy: WebTTSProxy,
                          config: dict, ws=None) -> dict:
    """Process a user command through the shared ConversationRouter.

    Returns dict with 'response', 'stats', 'used_llm', 'streamed', etc.
    When ws is provided, LLM responses are streamed token-by-token over WebSocket.
    """
    conversation = components['conversation']
    llm = components['llm']
    doc_buffer = components['doc_buffer']
    web_researcher = components['web_researcher']
    conv_state = components['conv_state']

    # Strip wake word prefix (leading only — preserve trailing
    # "Jarvis" so greetings like "Good afternoon, Jarvis" stay intact)
    command = re.sub(r'^(?:hey\s+)?jarvis[\s,.:!]*', '', command, flags=re.IGNORECASE).strip()
    if not command:
        command = "jarvis_only"

    conversation.add_message("user", command)

    t_start = time.perf_counter()
    skill_handled = False
    response = ""
    used_llm = False
    match_info = None

    # --- Route through shared priority chain ---
    # Web UI is always in-conversation: every typed message is intentional,
    # and unlike voice mode there's no silence-based window close.  This
    # enables P3.5 research follow-ups and context augmentation for
    # follow-up queries like "please elaborate".
    router = components['router']
    result = await asyncio.to_thread(
        router.route, command, in_conversation=True, doc_buffer=doc_buffer,
    )
    t_match = time.perf_counter()

    streamed = False
    if result.skip:
        # Bare ack noise — return empty response
        t_end = time.perf_counter()
        return {'response': '', 'stats': {}, 'used_llm': False, 'streamed': False}

    if result.handled:
        response = result.text
        skill_handled = True
        used_llm = result.used_llm
        match_info = result.match_info

        # Task plan: stream announcement + progress + final result
        task_planner = components.get('task_planner')
        if result.intent == "task_plan" and task_planner and task_planner.active_plan:
            if ws:
                # Start streaming so the frontend creates a message bubble
                await ws.send_json({"type": "stream_start"})
                await ws.send_json({"type": "stream_token", "token": response + "\n\n"})

            # Capture event loop for sync→async bridge in progress callback
            loop = asyncio.get_event_loop()

            def _web_progress(desc):
                from core import persona as _persona
                msg = _persona.task_progress(desc)
                logger.info(f"Plan progress: {msg}")
                if ws:
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            ws.send_json({"type": "stream_token", "token": msg + "\n\n"}),
                            loop,
                        )
                        future.result(timeout=2)
                    except Exception:
                        pass

            plan_result = await asyncio.to_thread(
                task_planner.execute_plan,
                task_planner.active_plan,
                progress_callback=_web_progress,
            )
            response = plan_result or "I wasn't able to complete the requested steps."
            used_llm = True
            streamed = True

            if ws:
                # End streaming with the final synthesized response
                await ws.send_json({"type": "stream_end", "full_response": response})

            # Speak the plan result if voice is enabled
            if tts_proxy.hybrid and tts_proxy.real_tts:
                threading.Thread(
                    target=tts_proxy.real_tts.speak,
                    args=(response,),
                    daemon=True,
                ).start()
    # --- Readback follow-up: user said "yes" to "Would you like me to read?" ---
    elif (not result.handled
          and conv_state.jarvis_asked_question
          and _is_readback_affirm(command, conv_state.last_response_text)):

        # Cache-first retrieval, fall back to conv_state
        _cache = get_interaction_cache()
        _cached_text = None
        _prior_synthesis = None

        if _cache and conv_state.window_id:
            _art = _cache.get_latest(
                conv_state.window_id, artifact_type="search_result_set",
            )
            if _art:
                _cached_text = _art.content
            _synth = _cache.get_latest(
                conv_state.window_id, artifact_type="synthesis",
            )
            if _synth:
                _prior_synthesis = _synth.content

        # Fallback to old conv_state path
        if not _cached_text:
            _cached_text = conv_state.last_tool_result_text

        if _cached_text:
            used_llm = True
            response, streamed = await _stream_readback(
                ws, llm, _cached_text, conv_state=conv_state,
                prior_synthesis=_prior_synthesis,
            )
            conv_state.last_tool_result_text = ""

    else:
        # LLM fallback (streaming over WebSocket when ws is available)
        used_llm = True
        if ws:
            response, streamed = await _stream_llm_ws(
                ws, llm, result.llm_command, result.llm_history, web_researcher,
                memory_context=result.memory_context,
                conversation_messages=result.context_messages,
                max_tokens=result.llm_max_tokens,
                use_tools_list=result.use_tools,
                tool_temperature=result.tool_temperature,
                tool_presence_penalty=result.tool_presence_penalty,
                memory_manager=components.get('memory_manager'),
                raw_command=command,
                user_id=conversation.current_user,
                conv_state=conv_state,
            )
        else:
            response = await _llm_fallback(
                llm, result.llm_command, result.llm_history, web_researcher,
                memory_context=result.memory_context,
                conversation_messages=result.context_messages,
                max_tokens=result.llm_max_tokens,
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

    # Record LLM metrics
    metrics = components.get('metrics')
    if metrics and used_llm:
        try:
            info = llm.last_call_info or {}
            metrics.record(
                provider=info.get('provider', 'unknown'),
                method=info.get('method', 'unknown'),
                prompt_tokens=info.get('input_tokens'),
                completion_tokens=info.get('output_tokens'),
                estimated_tokens=info.get('estimated_tokens'),
                model=info.get('model'),
                latency_ms=info.get('latency_ms'),
                ttft_ms=info.get('ttft_ms'),
                skill=match_info.get('skill_name') if match_info else None,
                intent=match_info.get('handler') if match_info else None,
                input_method='web',
                quality_gate=info.get('quality_gate', False),
                is_fallback=info.get('is_fallback', False),
                error=info.get('error'),
            )
        except Exception as e:
            logger.error("Metrics recording failed: %s", e)

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
                          max_tokens=None, use_tools_list=None,
                          tool_temperature=None,
                          tool_presence_penalty=None,
                          memory_manager=None, raw_command=None,
                          user_id=None, conv_state=None) -> tuple:
    """Stream LLM response over WebSocket with quality gate and tool calling.

    Returns (response_text, streamed_bool).
    When streamed_bool is True, stream_start/stream_end were sent over ws.
    When False, caller should send a normal 'response' message.
    """
    if raw_command is None:
        raw_command = command
    _enable_tools = llm.tool_calling and (web_researcher or use_tools_list)
    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _producer():
        """Sync thread: run LLM streaming, push items to async queue."""
        try:
            logger.info(f"LLM input (first 200): {command[:200]}")
            logger.info(f"Tools: {[t['function']['name'] for t in use_tools_list] if use_tools_list else 'none'}")
            source = (
                llm.stream_with_tools(
                    user_message=command,
                    conversation_history=history,
                    memory_context=memory_context,
                    conversation_messages=conversation_messages,
                    tools=use_tools_list,
                    tool_temperature=tool_temperature,
                    tool_presence_penalty=tool_presence_penalty,
                ) if _enable_tools else
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

    # --- Handle tool call (multi-tool loop) ---
    _MAX_TOOL_CHAIN = 3
    tool_chain_count = 0

    if tool_call_request:
        await ws.send_json({'type': 'stream_start'})
        synthesis = ""

        while tool_call_request and tool_chain_count < _MAX_TOOL_CHAIN:
            tool_chain_count += 1

            logger.info(f"Tool call: {tool_call_request.name}({tool_call_request.arguments})")
            if tool_call_request.name == 'web_search':
                query = tool_call_request.arguments.get('query', command)
                await ws.send_json({
                    'type': 'info',
                    'content': f'Searching: {query}',
                })
                results = await asyncio.to_thread(web_researcher.search, query)
                page_sections = await asyncio.to_thread(
                    web_researcher.fetch_pages_parallel, results
                )
                page_content = ""
                if page_sections:
                    page_content = ("\n\nFull article content:\n\n"
                                    + "\n\n---\n\n".join(page_sections))
                tool_result = format_search_results(results) + page_content
                # Cache for recipe-offer follow-ups
                if conv_state is not None:
                    conv_state.last_tool_result_text = tool_result
                    conv_state.research_results = results

                # Artifact cache (dual-write alongside conv_state)
                _cache = get_interaction_cache()
                if _cache is not None and conv_state is not None:
                    _wid = _cache.ensure_window_id(conv_state)
                    _uid = user_id or 'christopher'
                    _cache.store(Artifact(
                        artifact_id=_make_artifact_id(),
                        turn_id=conv_state.turn_count,
                        item_index=0,
                        artifact_type="search_result_set",
                        content=tool_result,
                        summary=f"Web search: {query} ({len(results)} results)",
                        source="web_search",
                        provenance={"query": query, "result_urls": [
                            {"title": r.get("title", ""), "url": r.get("url", "")}
                            for r in results
                        ]},
                        metadata={"result_count": len(results)},
                        parent_id=None,
                        user_id=_uid,
                        window_id=_wid,
                        tier="hot",
                        created_at=time.time(),
                    ))

                await ws.send_json({
                    'type': 'info',
                    'content': f'Found {len(results)} results',
                })
            else:
                # Skill tool — dispatch via tool_executor
                from core.tool_executor import execute_tool
                await ws.send_json({
                    'type': 'info',
                    'content': f'Running: {tool_call_request.name}',
                })
                tool_result = await asyncio.to_thread(
                    execute_tool, tool_call_request.name,
                    tool_call_request.arguments,
                )

            # Stream synthesis — may yield text tokens or another ToolCallRequest
            synthesis_queue = asyncio.Queue()
            _current_tcr = tool_call_request
            _current_tr = tool_result

            def _synthesis_producer(tcr=_current_tcr, tr=_current_tr):
                try:
                    for token in llm.continue_after_tool_call(
                        tcr, tr,
                        tools=use_tools_list,
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

            syn_thread = threading.Thread(
                target=_synthesis_producer, daemon=True,
            )
            syn_thread.start()

            next_tool_call = None
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
                    if isinstance(value, ToolCallRequest):
                        next_tool_call = value
                        # Drain remaining queue items
                        syn_thread.join(timeout=5)
                        break
                    synthesis += value
                    await ws.send_json({
                        'type': 'stream_token', 'token': value,
                    })

            tool_call_request = next_tool_call

        cleaned = llm.strip_filler(synthesis) if synthesis else ""
        await ws.send_json({
            'type': 'stream_end',
            'full_response': cleaned,
        })

        # Persist interaction for cross-session awareness
        if memory_manager and cleaned and tool_chain_count > 0:
            # Check if web_search was involved (results variable from loop)
            try:
                if results is not None:
                    search_query = query if 'query' in dir() else raw_command
                    result_urls = [
                        {"title": r.get("title", ""), "url": r.get("url", "")}
                        for r in (results or [])
                    ]
                    memory_manager.persist_interaction(
                        "research", raw_command, cleaned,
                        detail=search_query,
                        metadata={"result_urls": result_urls},
                        user_id=user_id or 'christopher',
                    )
            except NameError:
                pass  # No web search results — non-research tool call

        # Artifact cache: store synthesis
        _cache = get_interaction_cache()
        if _cache is not None and conv_state is not None and cleaned:
            _wid = _cache.ensure_window_id(conv_state)
            _uid = user_id or 'christopher'
            _cache.store(Artifact(
                artifact_id=_make_artifact_id(),
                turn_id=conv_state.turn_count,
                item_index=1,
                artifact_type="synthesis",
                content=cleaned,
                summary=cleaned[:100] + ("..." if len(cleaned) > 100 else ""),
                source="llm_synthesis",
                provenance={"tool_chain_count": tool_chain_count},
                metadata={},
                parent_id=None,
                user_id=_uid,
                window_id=_wid,
                tier="hot",
                created_at=time.time(),
            ))

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
        # Persist pure LLM conversation for cross-session awareness
        if memory_manager and full_response:
            memory_manager.persist_interaction(
                "conversation", raw_command, full_response,
                user_id=user_id or 'christopher',
            )
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

    # Persist pure LLM conversation for cross-session awareness
    if memory_manager and cleaned:
        memory_manager.persist_interaction(
            "conversation", raw_command, cleaned,
            user_id=user_id or 'christopher',
        )
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

        # Handle tool call
        if tool_call_request:
            if tool_call_request.name == "web_search":
                query = tool_call_request.arguments.get("query", command)
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
                # Skill tool — dispatch via tool_executor
                from core.tool_executor import execute_tool
                tool_result = await asyncio.to_thread(
                    execute_tool, tool_call_request.name, tool_call_request.arguments
                )

            # Collect synthesis response
            synthesis = ""
            def _run_synthesis():
                nonlocal synthesis
                for token in llm.continue_after_tool_call(
                    tool_call_request, tool_result,
                    tools=None,  # fallback path — web search only, no chaining
                ):
                    if isinstance(token, ToolCallRequest):
                        break
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

                            # Speak LLM responses via TTS when voice is enabled
                            # (skills already speak internally; this covers LLM fallback)
                            if result.get('used_llm') and result['response'] and tts_proxy.hybrid and tts_proxy.real_tts:
                                threading.Thread(
                                    target=tts_proxy.real_tts.speak,
                                    args=(result['response'],),
                                    daemon=True,
                                ).start()

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

                elif msg_type == 'set_user':
                    uid = data.get('user_id', 'christopher')
                    conversation.current_user = uid
                    # Update honorific + formal address for the switched user
                    from core.honorific import set_honorific
                    try:
                        from core.user_profile import ProfileManager
                        pm = ProfileManager(config)
                        set_honorific(pm.get_honorific_for(uid), pm.get_formal_address_for(uid))
                    except Exception:
                        formal = "Ms. Guest" if uid == "secondary_user" else None
                        set_honorific("ma'am" if uid == "secondary_user" else "sir", formal)
                    logger.info(f"User switched to: {uid}")
                    await ws.send_json({'type': 'user_changed', 'user_id': uid})

                    # Reload sessions + history filtered for this user
                    try:
                        all_messages = await asyncio.to_thread(conversation.load_full_history)
                        user_msgs = [m for m in all_messages if m.get('user_id', 'christopher') == uid]
                        user_sessions = _detect_sessions(user_msgs)
                        meta = _load_sessions_meta(config)
                        for s in user_sessions:
                            s['custom_name'] = meta.get(s['id'], None)
                        await ws.send_json({
                            'type': 'session_list',
                            'sessions': user_sessions[:10],
                            'has_more': len(user_sessions) > 10,
                            'total': len(user_sessions),
                        })
                        if user_sessions:
                            current = user_sessions[0]
                            current_msgs = [
                                m for m in user_msgs
                                if current['start_ts'] <= m.get('timestamp', 0) <= current['end_ts']
                            ]
                            await ws.send_json({
                                'type': 'history',
                                'messages': current_msgs,
                                'session_id': current['id'],
                            })
                    except Exception:
                        logger.exception("Failed to reload history after user switch")

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
            'content': "J.A.R.V.I.S. Web UI — type naturally to interact. "
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
    user_filter = request.query.get('user', conversation.current_user or 'christopher')
    filtered = [m for m in all_messages if m.get('user_id', 'christopher') == user_filter]
    sessions = _detect_sessions(filtered)
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
    user_filter = request.query.get('user', conversation.current_user or 'christopher')
    filtered = [m for m in all_messages if m.get('user_id', 'christopher') == user_filter]
    sessions = _detect_sessions(filtered)

    # Find matching session — session_id is the start_ts as string
    for s in sessions:
        if s['id'] == session_id:
            # Extract messages in this time range from the filtered list
            msgs = [
                m for m in filtered
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


# ---------------------------------------------------------------------------
# Metrics Dashboard — REST endpoints + WebSocket
# ---------------------------------------------------------------------------

async def dashboard_handler(request):
    """Serve dashboard.html for the metrics dashboard."""
    return web.FileResponse(Path(__file__).parent / 'web' / 'dashboard.html')


async def metrics_summary_handler(request):
    """GET /api/metrics/summary?hours=24 — Aggregated dashboard cards."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    metrics = components.get('metrics')
    if not metrics:
        return web.json_response({'error': 'Metrics not enabled'}, status=503)

    hours = int(request.query.get('hours', 24))
    data = await asyncio.to_thread(metrics.get_summary, hours)
    return web.json_response(data)


async def metrics_timeseries_handler(request):
    """GET /api/metrics/timeseries?hours=24&bucket=hour — Chart data."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    metrics = components.get('metrics')
    if not metrics:
        return web.json_response({'error': 'Metrics not enabled'}, status=503)

    hours = int(request.query.get('hours', 24))
    bucket = request.query.get('bucket', 'hour')
    if bucket not in ('hour', 'day'):
        bucket = 'hour'

    data = await asyncio.to_thread(metrics.get_timeseries, hours, bucket)
    return web.json_response(data)


async def metrics_skills_handler(request):
    """GET /api/metrics/skills?hours=24 — Skill breakdown."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    metrics = components.get('metrics')
    if not metrics:
        return web.json_response({'error': 'Metrics not enabled'}, status=503)

    hours = int(request.query.get('hours', 24))
    data = await asyncio.to_thread(metrics.get_skill_breakdown, hours)
    return web.json_response(data)


async def metrics_interactions_handler(request):
    """GET /api/metrics/interactions?offset=0&limit=50&provider=&skill= — Paginated raw data."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    metrics = components.get('metrics')
    if not metrics:
        return web.json_response({'error': 'Metrics not enabled'}, status=503)

    offset = int(request.query.get('offset', 0))
    limit = min(int(request.query.get('limit', 50)), 200)

    filters = {}
    for key in ('provider', 'skill', 'method', 'input_method'):
        val = request.query.get(key, '')
        if val:
            filters[key] = val
    if request.query.get('error_only', '').lower() in ('1', 'true'):
        filters['error_only'] = True
    if request.query.get('fallback_only', '').lower() in ('1', 'true'):
        filters['fallback_only'] = True
    if request.query.get('start'):
        filters['start'] = request.query['start']
    if request.query.get('end'):
        filters['end'] = request.query['end']

    data = await asyncio.to_thread(metrics.get_interactions, offset, limit, filters)
    return web.json_response(data)


async def metrics_filters_handler(request):
    """GET /api/metrics/filters — Distinct values for filter dropdowns."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    metrics = components.get('metrics')
    if not metrics:
        return web.json_response({'error': 'Metrics not enabled'}, status=503)

    data = await asyncio.to_thread(metrics.get_filter_options)
    return web.json_response(data)


async def metrics_export_handler(request):
    """GET /api/metrics/export?format=csv — CSV download."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    metrics = components.get('metrics')
    if not metrics:
        return web.json_response({'error': 'Metrics not enabled'}, status=503)

    filters = {}
    for key in ('provider', 'skill', 'method', 'input_method', 'start', 'end'):
        val = request.query.get(key, '')
        if val:
            filters[key] = val
    if request.query.get('error_only', '').lower() in ('1', 'true'):
        filters['error_only'] = True

    csv_data = await asyncio.to_thread(metrics.export_csv, filters)
    return web.Response(
        text=csv_data,
        content_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="jarvis_metrics.csv"'},
    )


async def memory_page_handler(request):
    """Serve memory.html for /memory."""
    return web.FileResponse(Path(__file__).parent / 'web' / 'memory.html')


async def memory_summary_handler(request):
    """GET /api/memory/summary — Fact counts, context stats, FAISS size (all users combined)."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    memory_manager = components.get('memory_manager')
    context_window = components.get('context_window')

    result = {}

    # Facts by category, split by user
    if memory_manager:
        try:
            def _all_user_facts():
                import sqlite3
                with memory_manager._db_lock:
                    conn = sqlite3.connect(str(memory_manager.db_path))
                    conn.row_factory = sqlite3.Row
                    try:
                        rows = conn.execute("""
                            SELECT category, user_id, COUNT(*) as cnt FROM facts
                            WHERE deleted = 0 AND superseded_by IS NULL
                            GROUP BY category, user_id
                            ORDER BY category, user_id
                        """).fetchall()
                        return [{'category': r['category'], 'user_id': r['user_id'], 'count': r['cnt']} for r in rows]
                    finally:
                        conn.close()

            by_cat_user = await asyncio.to_thread(_all_user_facts)
            result['facts'] = {
                'total': sum(r['count'] for r in by_cat_user),
                'by_category_user': by_cat_user,
            }
        except Exception as e:
            result['facts'] = {'total': 0, 'by_category_user': [], 'error': str(e)}

        # Recent interaction log stats (7 days), split by user
        try:
            def _all_user_interactions():
                import sqlite3, time
                cutoff = time.time() - 7 * 86400
                with memory_manager._db_lock:
                    conn = sqlite3.connect(str(memory_manager.db_path))
                    conn.row_factory = sqlite3.Row
                    try:
                        rows = conn.execute("""
                            SELECT type, user_id, COUNT(*) as cnt FROM interaction_log
                            WHERE created_at > ? GROUP BY type, user_id ORDER BY type, user_id
                        """, (cutoff,)).fetchall()
                        return [{'type': r['type'], 'user_id': r['user_id'], 'count': r['cnt']} for r in rows]
                    finally:
                        conn.close()

            by_type_user = await asyncio.to_thread(_all_user_interactions)
            result['interactions'] = {
                'total_7d': sum(r['count'] for r in by_type_user),
                'by_type_user': by_type_user,
            }
        except Exception as e:
            result['interactions'] = {'total_7d': 0, 'by_type_user': [], 'error': str(e)}

        # FAISS index size
        try:
            faiss_vectors = memory_manager.faiss_index.ntotal if memory_manager.faiss_index else 0
            faiss_dir = memory_manager.faiss_index_path
            faiss_size = sum(f.stat().st_size for f in faiss_dir.iterdir() if f.is_file()) if faiss_dir.is_dir() else 0
            result['faiss'] = {'vectors': faiss_vectors, 'size_bytes': faiss_size}
        except Exception as e:
            result['faiss'] = {'vectors': 0, 'size_bytes': 0, 'error': str(e)}
    else:
        result['facts'] = {'total': 0, 'by_category_user': []}
        result['interactions'] = {'total_7d': 0, 'by_type_user': []}
        result['faiss'] = {'vectors': 0, 'size_bytes': 0}

    # Context window stats
    if context_window:
        try:
            ctx_stats = await asyncio.to_thread(context_window.get_stats)
            ctx_pct = await asyncio.to_thread(context_window.get_usage_percentage)
            result['context'] = {
                'usage_pct': round(ctx_pct, 1),
                'segments': ctx_stats.get('segments', 0),
                'estimated_tokens': ctx_stats.get('estimated_tokens', 0),
            }
        except Exception as e:
            result['context'] = {'usage_pct': 0.0, 'segments': 0, 'estimated_tokens': 0, 'error': str(e)}
    else:
        result['context'] = {'usage_pct': 0.0, 'segments': 0, 'estimated_tokens': 0}

    return web.json_response(result)


async def memory_facts_handler(request):
    """GET /api/memory/facts?category=&user_id=&sort=&offset=0&limit=50 — Paginated facts."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    memory_manager = components.get('memory_manager')
    if not memory_manager:
        return web.json_response({'error': 'Memory not enabled'}, status=503)

    category = request.query.get('category', '') or None
    user_id = request.query.get('user_id', '') or None  # None = all users
    offset = max(0, int(request.query.get('offset', 0)))
    limit = min(int(request.query.get('limit', 50)), 200)
    sort = request.query.get('sort', 'last_referenced')

    def _fetch_facts():
        allowed_sorts = {'last_referenced', 'created_at', 'confidence', 'times_referenced'}
        sort_col = sort if sort in allowed_sorts else 'last_referenced'
        import sqlite3
        with memory_manager._db_lock:
            conn = sqlite3.connect(str(memory_manager.db_path))
            conn.row_factory = sqlite3.Row
            try:
                base = "FROM facts WHERE deleted = 0 AND superseded_by IS NULL"
                args: list = []
                if user_id:
                    base += " AND user_id = ?"
                    args.append(user_id)
                if category:
                    base += " AND category = ?"
                    args.append(category)
                total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
                rows = conn.execute(
                    f"SELECT * {base} ORDER BY {sort_col} DESC LIMIT ? OFFSET ?",
                    args + [limit, offset]
                ).fetchall()
                facts = []
                for r in rows:
                    facts.append({
                        'fact_id': r['fact_id'],
                        'category': r['category'],
                        'subject': r['subject'],
                        'content': r['content'],
                        'source': r['source'],
                        'confidence': r['confidence'],
                        'times_referenced': r['times_referenced'],
                        'created_at': r['created_at'],
                        'last_referenced': r['last_referenced'],
                    })
                return {'facts': facts, 'total': total, 'offset': offset, 'limit': limit}
            finally:
                conn.close()

    data = await asyncio.to_thread(_fetch_facts)
    return web.json_response(data)


async def memory_fact_delete_handler(request):
    """DELETE /api/memory/facts/{fact_id} — Soft-delete a fact."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    memory_manager = components.get('memory_manager')
    if not memory_manager:
        return web.json_response({'error': 'Memory not enabled'}, status=503)

    fact_id = request.match_info['fact_id']
    ok = await asyncio.to_thread(memory_manager.delete_fact, fact_id, True)
    if ok:
        return web.json_response({'deleted': fact_id})
    return web.json_response({'error': 'Not found'}, status=404)


async def memory_interactions_handler(request):
    """GET /api/memory/interactions?type=&days=7&offset=0&limit=50 — Paginated interaction log."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    memory_manager = components.get('memory_manager')
    if not memory_manager:
        return web.json_response({'error': 'Memory not enabled'}, status=503)

    type_filter = request.query.get('type', '') or None
    days = int(request.query.get('days', 30))
    offset = max(0, int(request.query.get('offset', 0)))
    limit = min(int(request.query.get('limit', 50)), 200)
    user_id = request.query.get('user_id', '') or None  # None = all users

    def _fetch_interactions():
        import sqlite3, time
        cutoff = time.time() - (days * 86400)
        with memory_manager._db_lock:
            conn = sqlite3.connect(str(memory_manager.db_path))
            conn.row_factory = sqlite3.Row
            try:
                base = "FROM interaction_log WHERE created_at > ?"
                args: list = [cutoff]
                if user_id:
                    base += " AND user_id = ?"
                    args.append(user_id)
                if type_filter:
                    base += " AND type = ?"
                    args.append(type_filter)
                total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
                rows = conn.execute(
                    f"SELECT interaction_id, user_id, type, query, detail, answer_summary, created_at "
                    f"{base} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    args + [limit, offset]
                ).fetchall()
                interactions = [dict(r) for r in rows]
                return {'interactions': interactions, 'total': total, 'offset': offset, 'limit': limit}
            finally:
                conn.close()

    data = await asyncio.to_thread(_fetch_interactions)
    return web.json_response(data)


async def memory_timeseries_handler(request):
    """GET /api/memory/timeseries?days=30 — Interaction counts over time by type."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)

    memory_manager = components.get('memory_manager')
    if not memory_manager:
        return web.json_response({'error': 'Memory not enabled'}, status=503)

    days = min(int(request.query.get('days', 30)), 365)

    def _fetch_timeseries():
        import sqlite3, time
        cutoff = time.time() - (days * 86400)
        with memory_manager._db_lock:
            conn = sqlite3.connect(str(memory_manager.db_path))
            conn.row_factory = sqlite3.Row
            try:
                # Per user, per type, per day
                rows = conn.execute(
                    """SELECT date(created_at, 'unixepoch', 'localtime') AS day,
                              user_id, type, COUNT(*) AS cnt
                       FROM interaction_log
                       WHERE created_at > ?
                       GROUP BY day, user_id, type ORDER BY day, user_id""",
                    (cutoff,)
                ).fetchall()
                # Pivot: series_by_user[user_id][day] = {date, research, tool_call, ...}
                users_map: dict = {}
                for r in rows:
                    uid = r['user_id'] or 'unknown'
                    d = r['day']
                    if uid not in users_map:
                        users_map[uid] = {}
                    if d not in users_map[uid]:
                        users_map[uid][d] = {'date': d, 'research': 0, 'tool_call': 0, 'conversation': 0, 'document': 0, 'skill': 0, 'total': 0}
                    users_map[uid][d][r['type']] = users_map[uid][d].get(r['type'], 0) + r['cnt']
                    users_map[uid][d]['total'] += r['cnt']
                series_by_user = {uid: sorted(dm.values(), key=lambda x: x['date'])
                                  for uid, dm in users_map.items()}
                return {'series_by_user': series_by_user}
            finally:
                conn.close()

    data = await asyncio.to_thread(_fetch_timeseries)
    return web.json_response(data)


async def memory_db_health_handler(request):
    """GET /api/memory/db-health — All data store sizes, row counts, and status."""
    import sqlite3, time as _time, datetime

    data_dir = Path('/mnt/storage/jarvis/data')
    conversations_dir = data_dir / 'conversations'

    stores_config = [
        {
            'name': 'memory.db',
            'path': str(data_dir / 'memory.db'),
            'tables': {'facts': 'SELECT COUNT(*) FROM facts WHERE deleted = 0',
                       'interaction_log': 'SELECT COUNT(*) FROM interaction_log'},
            'timeline_sql': "SELECT date(created_at, 'unixepoch', 'localtime') as d, COUNT(*) as c FROM interaction_log WHERE created_at > ? GROUP BY d ORDER BY d",
        },
        {
            'name': 'reminders.db',
            'path': str(data_dir / 'reminders.db'),
            'tables': {'reminders': 'SELECT COUNT(*) FROM reminders WHERE status = "pending"',
                       'reminders_total': 'SELECT COUNT(*) FROM reminders'},
            'timeline_sql': "SELECT date(created_at) as d, COUNT(*) as c FROM reminders WHERE created_at > datetime(?, 'unixepoch') GROUP BY d ORDER BY d",
        },
        {
            'name': 'metrics.db',
            'path': str(data_dir / 'metrics.db'),
            'tables': {'llm_interactions': 'SELECT COUNT(*) FROM llm_interactions'},
            'timeline_sql': "SELECT date(timestamp, 'unixepoch', 'localtime') as d, COUNT(*) as c FROM llm_interactions WHERE timestamp > ? GROUP BY d ORDER BY d",
        },
        {
            'name': 'news_headlines.db',
            'path': str(data_dir / 'news_headlines.db'),
            'tables': {'news_headlines': 'SELECT COUNT(*) FROM news_headlines'},
        },
        {
            'name': 'people.db',
            'path': str(data_dir / 'people.db'),
            'tables': {'people': 'SELECT COUNT(*) FROM people'},
        },
        {
            'name': 'web_queries.db',
            'path': str(data_dir / 'web_queries.db'),
            'tables': {'web_queries': 'SELECT COUNT(*) FROM web_queries'},
        },
        {
            'name': 'profiles.db',
            'path': str(data_dir / 'profiles' / 'profiles.db'),
            'tables': {'profiles': 'SELECT COUNT(*) FROM profiles'},
        },
    ]

    def _check_stores():
        results = []
        now = _time.time()

        for cfg in stores_config:
            p = Path(cfg['path'])
            entry = {
                'name': cfg['name'],
                'path': cfg['path'],
                'size_bytes': 0,
                'row_counts': {},
                'last_modified': None,
                'status': 'missing',
            }
            if p.exists():
                stat = p.stat()
                entry['size_bytes'] = stat.st_size
                entry['last_modified'] = datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
                stale = (now - stat.st_mtime) > 86400 * 2  # >2 days old = warn
                try:
                    conn = sqlite3.connect(str(p))
                    row_counts = {}
                    for label, sql in cfg.get('tables', {}).items():
                        try:
                            row_counts[label] = conn.execute(sql).fetchone()[0]
                        except Exception:
                            row_counts[label] = None
                    # Timeline sparkline data: 14-day window ending at most recent entry
                    timeline = []
                    tl_sql = cfg.get('timeline_sql')
                    if tl_sql:
                        try:
                            # Fetch last 14 days of activity
                            cutoff_14d = now - 14 * 86400
                            trows = conn.execute(tl_sql, (cutoff_14d,)).fetchall()
                            raw = {t[0]: t[1] for t in trows}
                            if raw:
                                # Pad all 14 days from (most_recent - 13d) to most_recent
                                most_recent = max(raw.keys())
                                from datetime import date as _date, timedelta as _td
                                end = _date.fromisoformat(most_recent)
                                start = end - _td(days=13)
                                timeline = []
                                d = start
                                while d <= end:
                                    ds = d.isoformat()
                                    timeline.append({'date': ds, 'count': raw.get(ds, 0)})
                                    d += _td(days=1)
                        except Exception:
                            timeline = []
                    conn.close()
                    entry['row_counts'] = row_counts
                    entry['timeline'] = timeline
                    entry['status'] = 'warning' if stale else 'ok'
                except Exception as e:
                    entry['status'] = 'error'
                    entry['error'] = str(e)
                    entry['timeline'] = []
            results.append(entry)

        # chat_history.jsonl
        jsonl = conversations_dir / 'chat_history.jsonl'
        jsonl_entry = {
            'name': 'chat_history.jsonl',
            'path': str(jsonl),
            'size_bytes': 0,
            'row_counts': {'messages': 0},
            'last_modified': None,
            'status': 'missing',
            'timeline': [],
        }
        if jsonl.exists():
            stat = jsonl.stat()
            jsonl_entry['size_bytes'] = stat.st_size
            jsonl_entry['last_modified'] = datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
            try:
                with open(jsonl, 'r', encoding='utf-8') as f:
                    jsonl_entry['row_counts']['messages'] = sum(1 for line in f if line.strip())
                jsonl_entry['status'] = 'ok'
            except Exception as e:
                jsonl_entry['status'] = 'error'
                jsonl_entry['error'] = str(e)
        results.append(jsonl_entry)

        # FAISS index directory
        faiss_dir = data_dir / 'memory_faiss'
        faiss_entry = {
            'name': 'FAISS index',
            'path': str(faiss_dir),
            'size_bytes': 0,
            'row_counts': {'vectors': 0},
            'last_modified': None,
            'status': 'missing',
            'timeline': [],
        }
        if faiss_dir.is_dir():
            files = list(faiss_dir.iterdir())
            total_size = sum(f.stat().st_size for f in files if f.is_file())
            faiss_entry['size_bytes'] = total_size
            meta = faiss_dir / 'default_meta.jsonl'
            if meta.exists():
                faiss_entry['last_modified'] = datetime.datetime.fromtimestamp(meta.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
                try:
                    with open(meta, 'r', encoding='utf-8') as f:
                        faiss_entry['row_counts']['vectors'] = sum(1 for line in f if line.strip())
                    faiss_entry['status'] = 'ok'
                except Exception as e:
                    faiss_entry['status'] = 'error'
                    faiss_entry['error'] = str(e)
            else:
                faiss_entry['status'] = 'ok'
        results.append(faiss_entry)

        # Total size
        total_bytes = sum(s['size_bytes'] for s in results)
        return {'stores': results, 'total_bytes': total_bytes}

    data = await asyncio.to_thread(_check_stores)
    return web.json_response(data)


async def memory_fact_update_handler(request):
    """PATCH /api/memory/facts/{fact_id} — Update editable fields of a fact."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)
    memory_manager = components.get('memory_manager')
    if not memory_manager:
        return web.json_response({'error': 'Memory not enabled'}, status=503)

    fact_id = request.match_info['fact_id']
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    ALLOWED = {'category', 'subject', 'content', 'source'}
    updates = {k: str(v) for k, v in body.items() if k in ALLOWED and v is not None}
    if not updates:
        return web.json_response({'error': 'No valid fields to update'}, status=400)

    def _update():
        import sqlite3
        with memory_manager._db_lock:
            conn = sqlite3.connect(str(memory_manager.db_path))
            try:
                set_clause = ', '.join(f'{k} = ?' for k in updates)
                vals = list(updates.values()) + [fact_id]
                rowcount = conn.execute(
                    f"UPDATE facts SET {set_clause} WHERE fact_id = ? AND deleted = 0",
                    vals
                ).rowcount
                conn.commit()
                return rowcount
            finally:
                conn.close()

    rowcount = await asyncio.to_thread(_update)
    if rowcount == 0:
        return web.json_response({'error': 'Not found'}, status=404)
    return web.json_response({'updated': fact_id})


async def memory_interaction_delete_handler(request):
    """DELETE /api/memory/interactions/{interaction_id} — Delete an interaction log entry."""
    components = request.app.get('components')
    if not components:
        return web.json_response({'error': 'Not initialized'}, status=503)
    memory_manager = components.get('memory_manager')
    if not memory_manager:
        return web.json_response({'error': 'Memory not enabled'}, status=503)

    interaction_id = request.match_info['interaction_id']

    def _delete():
        import sqlite3
        with memory_manager._db_lock:
            conn = sqlite3.connect(str(memory_manager.db_path))
            try:
                rowcount = conn.execute(
                    "DELETE FROM interaction_log WHERE interaction_id = ?",
                    (interaction_id,)
                ).rowcount
                conn.commit()
                return rowcount
            finally:
                conn.close()

    rowcount = await asyncio.to_thread(_delete)
    if rowcount == 0:
        return web.json_response({'error': 'Not found'}, status=404)
    return web.json_response({'deleted': interaction_id})


async def dashboard_ws_handler(request):
    """WebSocket endpoint for live dashboard metric updates."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    app = request.app
    dashboard_clients = app.setdefault('dashboard_clients', set())
    dashboard_clients.add(ws)

    try:
        async for msg in ws:
            if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
            # Dashboard WS is push-only; ignore client messages
    finally:
        dashboard_clients.discard(ws)

    return ws


async def index_handler(request):
    """Serve index.html for the root path."""
    return web.FileResponse(Path(__file__).parent / 'web' / 'index.html')


def create_app(config) -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()

    web_dir = Path(__file__).parent / 'web'

    # Routes: WebSocket, API, root index, then static assets
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/ws/dashboard', dashboard_ws_handler)
    app.router.add_get('/api/history', history_handler)
    app.router.add_get('/api/sessions', sessions_handler)
    app.router.add_get('/api/session/{session_id}', session_messages_handler)
    app.router.add_put('/api/session/{session_id}/rename', session_rename_handler)
    app.router.add_post('/api/upload', upload_handler)
    app.router.add_get('/api/browse', browse_handler)
    app.router.add_get('/api/stats', stats_overview_handler)
    app.router.add_get('/dashboard', dashboard_handler)
    app.router.add_get('/api/metrics/summary', metrics_summary_handler)
    app.router.add_get('/api/metrics/timeseries', metrics_timeseries_handler)
    app.router.add_get('/api/metrics/skills', metrics_skills_handler)
    app.router.add_get('/api/metrics/interactions', metrics_interactions_handler)
    app.router.add_get('/api/metrics/filters', metrics_filters_handler)
    app.router.add_get('/api/metrics/export', metrics_export_handler)
    app.router.add_get('/memory', memory_page_handler)
    app.router.add_get('/api/memory/summary', memory_summary_handler)
    app.router.add_get('/api/memory/facts', memory_facts_handler)
    app.router.add_patch('/api/memory/facts/{fact_id}', memory_fact_update_handler)
    app.router.add_delete('/api/memory/facts/{fact_id}', memory_fact_delete_handler)
    app.router.add_get('/api/memory/interactions', memory_interactions_handler)
    app.router.add_delete('/api/memory/interactions/{interaction_id}', memory_interaction_delete_handler)
    app.router.add_get('/api/memory/timeseries', memory_timeseries_handler)
    app.router.add_get('/api/memory/db-health', memory_db_health_handler)
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
    app['dashboard_clients'] = set()

    # Wire live dashboard push — MetricsTracker calls this after each record()
    metrics = components.get('metrics')
    if metrics:
        loop = asyncio.get_event_loop()

        def _push_to_dashboard(row: dict):
            """Non-blocking push of new metric to all connected dashboard clients."""
            clients = app.get('dashboard_clients', set())
            if not clients:
                return
            payload = json.dumps({'type': 'new_metric', 'data': row})
            for ws in list(clients):
                if not ws.closed:
                    asyncio.run_coroutine_threadsafe(ws.send_str(payload), loop)

        metrics.set_on_record(_push_to_dashboard)

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
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. Web UI")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--voice", action="store_true", help="Start with voice enabled")
    args = parser.parse_args()

    # Setup logging — route to web.log file + console
    log_file = Path(__file__).parent / "logs" / "web.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(log_file)),
        ],
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

    print(f"\n  J.A.R.V.I.S. Web UI → http://{host}:{port}\n")
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
