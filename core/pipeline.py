"""
Event pipeline for JARVIS — Phase 4 of the latency refactor.

Provides queue-connected worker threads and a coordinator that replaces
the ad-hoc callback architecture with a centralized event dispatch loop.

Components:
    Coordinator   — main-thread event loop, routes commands, manages state
    STTWorker     — persistent transcription thread (replaces per-utterance daemons)
    TTSWorker     — persistent playback thread (serializes all audio output)
    EventBridge   — adapter that translates callback-based APIs into events
    EventTTSProxy — drop-in TTS replacement for background services
"""

import queue
import re
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Optional

from core.events import Event, EventType, PipelineState
from core.speech_chunker import SpeechChunker
from core.logger import get_logger
from core.honorific import set_honorific
from core import persona
from core.conversation_state import ConversationState
from core.conversation_router import ConversationRouter, RouteResult


# ---------------------------------------------------------------------------
# "Show me" display hook for developer_tools (lazy-loaded)
# ---------------------------------------------------------------------------

_display_router_cache = None

_DEVTOOLS_DISPLAY_MAP = {
    "git_status": ("git_status", "Git Status"),
    "git_log": ("git_log", "Git Log"),
    "git_diff": ("git_diff", "Git Diff"),
    "git_branch": ("git_branch", "Git Branches"),
    "codebase_search": ("codebase_search", "Codebase Search"),
    "process_info": ("process_list", "Top Processes"),
    "service_status": ("service_status", "Service Status"),
    "network_info": ("network_info", "Network Info"),
    "package_info": ("package_list", "Package Info"),
    "system_health": ("health_check", "System Health"),
    "check_logs": ("log_output", "Service Logs"),
    "run_command": ("general", "Shell Output"),
    "confirm_pending": ("general", "Confirmed Output"),
}


def _get_display_router(config):
    """Lazy-load DisplayRouter from developer_tools skill."""
    global _display_router_cache
    if _display_router_cache is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_display',
            '/mnt/storage/jarvis/skills/system/developer_tools/_display.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _display_router_cache = mod.DisplayRouter(config)
    return _display_router_cache


def _detect_show_me(text):
    """Check for 'show me' trigger — replicates DisplayRouter.detect_show_me() logic."""
    lower = text.lower().strip()
    if 'in terminal' in lower or 'in the terminal' in lower:
        return 'terminal'
    if 'in code' in lower or 'in vs code' in lower or 'in vscode' in lower:
        return 'vscode'
    for prefix in ['show me', 'let me see', 'display', 'pull up', 'open up']:
        if lower.startswith(prefix):
            return 'auto'
    return None
from core.llm_router import ToolCallRequest
from core.web_research import WebResearcher, format_search_results
from core.self_awareness import SelfAwareness
from core.task_planner import TaskPlanner


# ---------------------------------------------------------------------------
# STT Worker
# ---------------------------------------------------------------------------

class STTWorker(threading.Thread):
    """Persistent STT worker. Reads audio from a queue, transcribes,
    and emits TRANSCRIPTION_READY events to the coordinator.

    When a SpeakerIdentifier is provided, speaker identification runs
    in parallel with Whisper transcription via a ThreadPoolExecutor.
    """

    def __init__(self, stt, event_queue: queue.Queue, audio_queue: queue.Queue,
                 config=None, speaker_id=None):
        super().__init__(daemon=True, name="stt-worker")
        self.stt = stt
        self.event_queue = event_queue
        self.audio_queue = audio_queue
        self.speaker_id = speaker_id
        self.logger = get_logger("pipeline.stt", config)

    def run(self):
        self.logger.info("STT worker started")
        while True:
            audio = self.audio_queue.get()
            if audio is None:  # shutdown sentinel
                self.logger.info("STT worker shutting down")
                break
            try:
                sample_rate = 16000  # audio is always resampled to 16 kHz

                if self.speaker_id is not None:
                    # Run Whisper + speaker ID in parallel
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        stt_future = pool.submit(self.stt.transcribe, audio, sample_rate)
                        sid_future = pool.submit(self.speaker_id.identify, audio, sample_rate)
                        text = stt_future.result()
                        speaker_user_id, speaker_confidence = sid_future.result()
                else:
                    text = self.stt.transcribe(audio, sample_rate)
                    speaker_user_id, speaker_confidence = None, 0.0

                if text and text.strip():
                    # Enriched event data when speaker ID is available
                    if self.speaker_id is not None:
                        data = {
                            "text": text.strip(),
                            "speaker_id": speaker_user_id,
                            "speaker_confidence": speaker_confidence,
                        }
                    else:
                        data = text.strip()

                    self.event_queue.put(Event(
                        EventType.TRANSCRIPTION_READY,
                        data=data,
                        source="stt_worker",
                    ))
                else:
                    self.logger.info("Blank transcription")
                    print("⚠️  (no speech detected)")
            except Exception as e:
                self.logger.error(f"STT worker error: {e}", exc_info=True)
                self.event_queue.put(Event(
                    EventType.ERROR,
                    data={"source": "stt", "error": str(e)},
                    source="stt_worker",
                ))


# ---------------------------------------------------------------------------
# TTS Worker
# ---------------------------------------------------------------------------

class TTSWorker(threading.Thread):
    """Persistent TTS worker. Reads speak requests from a queue and
    plays them sequentially, emitting lifecycle events."""

    def __init__(self, tts, event_queue: queue.Queue, tts_queue: queue.Queue,
                 config=None):
        super().__init__(daemon=True, name="tts-worker")
        self.tts = tts
        self.event_queue = event_queue
        self.tts_queue = tts_queue
        self.logger = get_logger("pipeline.tts", config)

    def run(self):
        self.logger.info("TTS worker started")
        while True:
            item = self.tts_queue.get()
            if item is None:  # shutdown sentinel
                self.logger.info("TTS worker shutting down")
                break

            event = item
            done_event = None  # threading.Event for synchronous callers

            # Emit pause + started
            self.event_queue.put(Event(EventType.PAUSE_LISTENING, source="tts_worker"))
            self.event_queue.put(Event(EventType.SPEECH_STARTED, source="tts_worker"))

            try:
                if event.type == EventType.SPEAK_ACK:
                    self.tts.speak_ack()
                elif event.type == EventType.SPEAK_REQUEST:
                    data = event.data
                    if isinstance(data, dict):
                        done_event = data.get("done_event")
                        text = data.get("text", "")
                    else:
                        text = str(data)
                    if text:
                        self.tts.speak(text)
            except Exception as e:
                self.logger.error(f"TTS worker error: {e}", exc_info=True)
                self.event_queue.put(Event(
                    EventType.ERROR,
                    data={"source": "tts", "error": str(e)},
                    source="tts_worker",
                ))

            # Emit finished
            self.event_queue.put(Event(EventType.SPEECH_FINISHED, source="tts_worker"))

            # Signal synchronous callers (EventTTSProxy)
            if done_event is not None:
                done_event.set()


# ---------------------------------------------------------------------------
# EventBridge — adapter for background services' listener callbacks
# ---------------------------------------------------------------------------

class EventBridge:
    """Translates callback-based interactions into events.

    Background services (reminder_manager, news_manager) were designed to
    call listener.pause_listening() / resume_listening() directly.  This
    adapter provides the same API but emits events instead, so the
    coordinator can manage all listener state centrally.
    """

    def __init__(self, event_queue: queue.Queue):
        self.event_queue = event_queue

    def pause_listening(self):
        self.event_queue.put(Event(EventType.PAUSE_LISTENING, source="bridge"))

    def resume_listening(self):
        self.event_queue.put(Event(EventType.RESUME_LISTENING, source="bridge"))

    def open_conversation_window(self, duration: float = None):
        self.event_queue.put(Event(
            EventType.OPEN_CONVERSATION_WINDOW,
            data=duration,
            source="bridge",
        ))


# ---------------------------------------------------------------------------
# EventTTSProxy — drop-in TTS for background services
# ---------------------------------------------------------------------------

class EventTTSProxy:
    """Drop-in TTS replacement that routes through the TTS worker queue.

    Background services (reminder_manager, news_manager) hold a reference
    to this instead of the real TTS.  speak() blocks until playback
    finishes, preserving the synchronous contract these services expect.
    """

    def __init__(self, tts_queue: queue.Queue, event_queue: queue.Queue):
        self.tts_queue = tts_queue
        self.event_queue = event_queue
        self._spoke = False

    # --- public API matching core.tts.TextToSpeech ---

    def speak(self, text: str):
        """Speak text via the TTS worker.  Blocks until playback finishes."""
        self._spoke = True
        done = threading.Event()
        self.tts_queue.put(Event(
            EventType.SPEAK_REQUEST,
            data={"text": text, "done_event": done},
            source="bg_service",
        ))
        done.wait(timeout=60)

    def speak_ack(self):
        """Play a pre-cached acknowledgment phrase (non-blocking)."""
        self.tts_queue.put(Event(EventType.SPEAK_ACK, source="bg_service"))


# ---------------------------------------------------------------------------
# StreamingAudioPipeline — gapless multi-sentence TTS
# ---------------------------------------------------------------------------

class StreamingAudioPipeline:
    """Background audio pipeline for gapless multi-sentence TTS.

    Accepts sentence text via put(), generates audio via Kokoro,
    and streams PCM to a single persistent aplay process.
    Eliminates inter-sentence gaps by overlapping generation with playback.
    """

    def __init__(self, tts, logger):
        self.tts = tts
        self.logger = logger
        self._text_queue = queue.Queue()
        self._done = threading.Event()
        self._error = None
        self._total_chunks = 0
        self._thread = None

    def start(self):
        """Start the background audio pipeline thread."""
        self._done.clear()
        self._error = None
        self._total_chunks = 0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="streaming-audio"
        )
        self._thread.start()

    def put(self, text: str):
        """Submit a sentence for audio generation and playback."""
        self._text_queue.put(text)
        self._total_chunks += 1

    def finish(self):
        """Signal no more sentences. Blocks until all audio finishes."""
        self._text_queue.put(None)  # sentinel
        self._done.wait(timeout=120)
        if self._error:
            self.logger.error(f"Streaming audio pipeline error: {self._error}")

    def _run(self):
        """Background thread: generate audio and stream to persistent aplay."""
        import subprocess
        import numpy as np

        tts = self.tts
        aplay = None
        total_samples = 0
        t0 = time.time()
        first_chunk_logged = False

        try:
            with tts._tts_lock:
                while True:
                    text = self._text_queue.get()
                    if text is None:
                        break

                    # Normalize
                    if tts.normalization_enabled and tts.normalizer:
                        text = tts.normalizer.normalize(text)

                    if not text or not text.strip():
                        continue

                    # Stream Kokoro sub-chunks directly to aplay
                    for gs, ps, audio in tts._kokoro_pipeline(
                        text, voice=tts._kokoro_voice,
                        speed=tts._kokoro_speed
                    ):
                        audio_np = np.asarray(audio)
                        pcm = (audio_np * 32767).astype(
                            np.int16
                        ).tobytes()

                        # Lazy-spawn aplay on first audio data
                        # (not first sentence — gives PipeWire
                        # Kokoro-generation time to release device)
                        if aplay is None:
                            aplay = tts._open_aplay()
                            if aplay is None:
                                self._error = "Failed to open audio device"
                                break
                            tts._track_proc(aplay)

                        aplay.stdin.write(pcm)
                        total_samples += len(audio_np)

                        if not first_chunk_logged:
                            first_chunk_logged = True
                            self.logger.info(
                                f"Kokoro first chunk in "
                                f"{time.time() - t0:.3f}s"
                            )

                    if self._error:
                        break

                # All sentences done — close aplay
                if aplay is not None:
                    aplay.stdin.close()
                    duration = total_samples / tts.sample_rate
                    gen_time = time.time() - t0

                    try:
                        aplay_return = aplay.wait(
                            timeout=max(15, duration + 5)
                        )
                    except subprocess.TimeoutExpired:
                        self.logger.error("aplay timed out — killing")
                        aplay.kill()
                        aplay.wait()
                        return

                    if aplay_return != 0:
                        aplay_err = aplay.stderr.read().decode().strip()
                        self.logger.error(
                            f"aplay error (code {aplay_return}): "
                            f"{aplay_err}"
                        )
                    else:
                        self.logger.info(
                            f"Kokoro streamed {duration:.1f}s audio in "
                            f"{gen_time:.3f}s across "
                            f"{self._total_chunks} chunks "
                            f"(RTF: {duration/gen_time:.1f}x)"
                        )

        except BrokenPipeError:
            if aplay:
                aplay_err = aplay.stderr.read().decode().strip()
                self.logger.error(f"aplay broken pipe: {aplay_err}")
                aplay.wait()
            self._error = "aplay broken pipe"

        except Exception as e:
            self.logger.error(f"Streaming audio pipeline error: {e}")
            import traceback
            traceback.print_exc()
            self._error = str(e)
            if aplay and aplay.poll() is None:
                try:
                    aplay.stdin.close()
                except Exception:
                    pass
                aplay.kill()
                aplay.wait()

        finally:
            if aplay is not None:
                tts._untrack_proc(aplay)
            self._done.set()


# ---------------------------------------------------------------------------
# Coordinator — main-thread event loop
# ---------------------------------------------------------------------------

class Coordinator:
    """Central event dispatcher running on the main thread.

    Replaces the old ``while running: sleep(0.1)`` loop.  Receives typed
    events from all workers and background services, makes routing
    decisions, and manages conversation state.
    """

    def __init__(self, *, config, event_queue: queue.Queue,
                 tts_queue: queue.Queue, listener, tts, llm,
                 skill_manager, conversation, reminder_manager=None,
                 news_manager=None, calendar_manager=None,
                 profile_manager=None, memory_manager=None,
                 context_window=None, desktop_manager=None,
                 metrics=None):
        self.config = config
        self.logger = get_logger("pipeline.coordinator", config)
        self.event_queue = event_queue
        self.tts_queue = tts_queue
        self.listener = listener
        self.tts = tts
        self.llm = llm
        self.skill_manager = skill_manager
        self.conversation = conversation
        self.reminder_manager = reminder_manager
        # Wire reminder manager and config for tool-calling dispatch
        if reminder_manager:
            from core.tool_executor import set_reminder_manager
            set_reminder_manager(reminder_manager)
        from core.tool_executor import set_config as set_tool_config
        set_tool_config(config)
        self.news_manager = news_manager
        self.calendar_manager = calendar_manager
        self.profile_manager = profile_manager
        self.memory_manager = memory_manager
        self.context_window = context_window
        self.desktop_manager = desktop_manager
        self.metrics = metrics

        # Web research (tool calling)
        self.web_researcher = WebResearcher(config) if config.get("llm.local.tool_calling", False) else None

        # Session stats for health reporting (must be before SelfAwareness which reads it)
        self.stats = {
            'start_time': time.time(),
            'commands_processed': 0,
            'errors': 0,
            'last_error_time': None,
            'last_error_msg': None,
        }

        # Self-awareness layer (Phase 1 of task planner)
        self.self_awareness = SelfAwareness(
            skill_manager=skill_manager,
            metrics=metrics,
            memory_manager=memory_manager,
            context_window=context_window,
            coordinator_stats=self.stats,
            config=config,
        )

        # Task planner (Phase 2-3 of task planner)
        self.task_planner = TaskPlanner(
            llm=llm,
            skill_manager=skill_manager,
            self_awareness=self.self_awareness,
            conversation=conversation,
            config=config,
            event_queue=event_queue,
            context_window=context_window,
        )

        # Centralized conversation state (Phase 2 of conversational flow refactor)
        self.conv_state = ConversationState()

        self.running = True
        self.state = PipelineState.IDLE
        self.wake_word = config.get("system.wake_word", "jarvis").lower()

        # Streaming LLM state
        self._streaming_active = False
        self._llm_responded = False

        # Beep
        from pathlib import Path
        self.beep_path = Path(__file__).parent.parent / "assets" / "wake_word_detect.wav"

        # Valid short replies (copied from continuous_listener for conversation noise filter)
        self._valid_short_replies = {
            "yes", "no", "yeah", "yep", "nah", "nope",
            "thanks", "thank you", "okay", "ok", "please",
            "stop", "cancel", "nevermind", "never mind",
            "sure", "right", "correct", "wrong", "good", "great",
            "hello", "hey", "hi", "bye", "goodbye",
        }

        # People manager (social introductions + pronunciation)
        self.people_manager = None
        if config.get("people.enabled", False):
            from core.people_manager import get_people_manager
            self.people_manager = get_people_manager(config)

        # Shared command router (Phase 3 of conversational flow refactor)
        self.router = ConversationRouter(
            skill_manager=skill_manager,
            conversation=conversation,
            llm=llm,
            reminder_manager=reminder_manager,
            memory_manager=memory_manager,
            news_manager=news_manager,
            context_window=context_window,
            conv_state=self.conv_state,
            config=config,
            web_researcher=self.web_researcher,
            self_awareness=self.self_awareness,
            task_planner=self.task_planner,
            people_manager=self.people_manager,
        )

        # Wire timeout cleanup so silence-expired windows get same cleanup as dismissals
        self.listener.on_window_close = self._on_conversation_timeout

    # ----- main loop -----

    def run(self):
        """Block on the event queue, dispatching events until shutdown."""
        self.logger.info("Coordinator event loop started")
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._dispatch(event)
            except Exception as e:
                self.logger.error(f"Dispatch error for {event}: {e}", exc_info=True)
                self.stats['errors'] += 1
                self.stats['last_error_time'] = time.time()
                self.stats['last_error_msg'] = f"dispatch: {e}"

        self.logger.info("Coordinator event loop exited")

    def shutdown(self):
        self.running = False

    def get_health(self) -> dict:
        """Collect JARVIS internal state for health reporting."""
        # Listener state
        listener_health = {}
        if self.listener:
            listener_health = {
                'running': getattr(self.listener, 'running', False),
                'stream_active': getattr(self.listener, 'stream', None) is not None,
                'device': getattr(self.listener, 'device', 'unknown'),
                'conversation_active': getattr(self.listener, 'conversation_window_active', False),
                'rnnoise': getattr(self.listener, 'use_rnnoise', False),
            }

        # Skills count
        skills_loaded = 0
        intent_count = 0
        if self.skill_manager:
            skills_loaded = len(getattr(self.skill_manager, 'skills', {}))
            matcher = getattr(self.skill_manager, 'matcher', None)
            if matcher and hasattr(matcher, 'get_intent_count'):
                intent_count = matcher.get_intent_count()

        # LLM state
        llm_health = {}
        if self.llm:
            llm_health = {
                'api_call_count': getattr(self.llm, 'api_call_count', 0),
                'fallback_enabled': getattr(self.llm, 'fallback_enabled', False),
                'local_model': getattr(self.llm, 'local_model_path', None) is not None,
            }

        return {
            'running': self.running,
            'state': self.state.name,
            'stats': dict(self.stats),
            'event_queue_size': self.event_queue.qsize(),
            'tts_queue_size': self.tts_queue.qsize(),
            'managers': {
                'reminders': self.reminder_manager is not None,
                'news': self.news_manager is not None,
                'calendar': self.calendar_manager is not None,
                'profiles': self.profile_manager is not None,
                'memory': self.memory_manager is not None,
                'context_window': self.context_window is not None,
                'desktop': self.desktop_manager is not None,
            },
            'listener': listener_health,
            'tts_engine': getattr(self.tts, 'engine', 'unknown'),
            'llm': llm_health,
            'skills_loaded': skills_loaded,
            'semantic_intents': intent_count,
        }

    # ----- dispatch -----

    def _dispatch(self, event: Event):
        handlers = {
            EventType.TRANSCRIPTION_READY: self._handle_transcription,
            EventType.COMMAND_DETECTED: self._handle_command,
            EventType.PAUSE_LISTENING: lambda e: self.listener.pause_listening(),
            EventType.RESUME_LISTENING: self._handle_resume,
            EventType.OPEN_CONVERSATION_WINDOW: lambda e: self.listener.open_conversation_window(e.data),
            EventType.CLOSE_CONVERSATION_WINDOW: self._handle_close_conversation,
            EventType.SPEECH_STARTED: self._handle_speech_started,
            EventType.SPEECH_FINISHED: self._handle_speech_finished,
            EventType.LLM_COMPLETE: self._handle_llm_complete,
            EventType.SHUTDOWN: lambda e: self.shutdown(),
            EventType.ERROR: self._handle_error,
        }
        handler = handlers.get(event.type)
        if handler:
            handler(event)
        else:
            self.logger.debug(f"Unhandled event: {event.type.name}")

    # ----- transcription handling (extracted from _transcribe_and_check) -----

    def _handle_transcription(self, event: Event):
        """Process raw transcription text: validate, check wake word or
        conversation window, and emit COMMAND_DETECTED if appropriate."""
        # Extract text and speaker context from enriched or plain event data
        if isinstance(event.data, dict):
            raw_text = event.data["text"]
            speaker_id = event.data.get("speaker_id")
            speaker_confidence = event.data.get("speaker_confidence", 0.0)
            self._apply_speaker_context(speaker_id, speaker_confidence)
        else:
            raw_text = event.data

        text = raw_text.lower()

        # Filter noise annotations
        if (text.startswith('(') and text.endswith(')')) or \
           (text.startswith('[') and text.endswith(']')):
            self.logger.info(f"Ignoring noise annotation: {text}")
            print("⚠️  Ignoring background noise")
            return

        # Filter garbage (repetitive chars)
        unique_chars = set(text.replace(' ', '').replace('.', ''))
        if len(unique_chars) <= 3 and len(text) > 5:
            self.logger.info(f"Ignoring garbage transcription: {text[:30]}...")
            return

        # Apply brand-name corrections (quinn→qwen, etc.) before any routing
        text = self._apply_transcription_corrections(text)

        self.logger.info(f"Transcribed: {text}")
        print(f"📝 Heard: \"{text}\"")

        # Conversation window — accept without wake word
        if self.listener.conversation_window_active:
            if self._is_conversation_noise(text):
                self.logger.info(f"Filtered noise during conversation: '{text}'")
                return
            text = self._apply_command_corrections(text)
            self.listener._cancel_conversation_timer()
            self.logger.info(f"Response during conversation window: {text}")
            self.event_queue.put(Event(
                EventType.COMMAND_DETECTED,
                data=text,
                source="coordinator",
            ))
            return

        # Wake word fuzzy match (threshold 0.80 — eliminates "paris" 0.73, etc.)
        words = text.split()
        wake_word_found = False
        matched_word = ""
        for word in words:
            word_clean = word.strip('.,!?;:')
            similarity = SequenceMatcher(None, self.wake_word, word_clean).ratio()
            if similarity >= 0.80:
                self.logger.info(f"Wake word detected (similarity: {similarity:.2f}): {word_clean} in {text}")
                wake_word_found = True
                matched_word = word_clean
                break

        if wake_word_found:
            # Check if this is ambient conversation rather than a command
            if self._is_ambient_wake_word(text, matched_word):
                print("🔇 Ambient mention (ignored)")
                return

            corrected_text = text.replace(matched_word, self.wake_word)
            self.logger.info(f"Corrected: '{text}' → '{corrected_text}'")
            self.event_queue.put(Event(
                EventType.COMMAND_DETECTED,
                data=corrected_text,
                source="coordinator",
            ))
        else:
            self.logger.info(f"No wake word in: {text}")
            print("❌ No wake word (ignored)")

    # ----- command processing (extracted from on_command_detected) -----

    def _handle_command(self, event: Event):
        """Route a detected command through the priority chain."""
        full_text = event.data
        in_conversation = self.listener.conversation_window_active
        self.state = PipelineState.PROCESSING_COMMAND
        self.stats['commands_processed'] += 1

        # Parse input
        if in_conversation:
            self.logger.info(f"Conversation continues: {full_text}")
            print(f"\n💬 You said: {full_text}")
            if self.wake_word in full_text.lower():
                command = self._extract_command(full_text)
            else:
                command = full_text
        else:
            self.logger.info(f"Command detected: {full_text}")
            print(f"\n🟡 Command detected: {full_text}")
            command = self._extract_command(full_text)
            # Fresh wake-word activation — reset memory surfacing window
            # (covers conversation timeout path where no explicit close event fires)
            if self.memory_manager:
                self.memory_manager.reset_surfacing_window()

        # Pause listening while we process
        self.listener.pause_listening()

        # Beep to acknowledge: wake-word activation OR conversation-window follow-up
        if not in_conversation and self.wake_word in full_text.lower():
            self._play_beep()
        elif in_conversation:
            self._play_beep()

        if not command:
            self.logger.warning("No command extracted")
            self.listener.resume_listening()
            self.state = PipelineState.IDLE
            return

        self.logger.info(f"Command: {repr(command.strip())}")

        # --- Minimal greeting (voice-specific: adds "jarvis" to history) ---
        if command.strip() == "jarvis_only" or len(command.strip()) <= 2:
            self._handle_minimal_greeting(command, in_conversation)
            return

        # --- Process real command ---
        print(f"📝 Processing: {command}")
        self.logger.info(f"Processing command: {command}")
        self.conversation.add_message("user", command)
        self.tts._spoke = False

        # --- Pre-route ack decision (must happen BEFORE routing, which blocks) ---
        ack_style, suppress_ack = self._classify_ack(
            command,
            in_conversation=in_conversation,
            jarvis_asked_question=self.conv_state.jarvis_asked_question,
        )

        # --- Route through shared priority chain ---
        # For skill-handled commands, play an ack if warranted (the skill
        # handler may take tens of seconds, e.g. document generation).
        # Timer at 1.5s: routing + fast skills complete in <1s, so the ack
        # only fires for genuinely slow operations (doc gen, web research).
        self._llm_responded = False
        ack_timer = None
        if not suppress_ack:
            ack_timer = threading.Timer(
                1.5, self._play_ack_if_still_thinking, args=(ack_style,)
            )
            ack_timer.daemon = True
            ack_timer.start()

        result = self.router.route(command, in_conversation=in_conversation)

        # Cancel ack timer if skill returned before it fired
        if ack_timer:
            ack_timer.cancel()
            self._llm_responded = True

        # Skip: bare acknowledgment noise
        if result.skip:
            self.logger.info("Router: skip (bare ack noise)")
            self.listener.resume_listening()
            self.state = PipelineState.IDLE
            return

        if result.handled:
            response = result.text

            # Task plan: speak announcement, then execute the plan
            if result.intent == "task_plan" and self.task_planner.active_plan:
                if response:
                    self._speak_and_wait(response)
                response = self._execute_task_plan()
            elif response and not self.tts._spoke:
                # Speak response (unless handler already spoke via TTS proxy,
                # e.g. deliver_rundown or a skill that calls tts.speak directly)
                self._speak_and_wait(response)

            # Conversation window side effects
            if result.close_window:
                self._handle_close_conversation(None)
            elif result.open_window is not None:
                self.listener.open_conversation_window(result.open_window)
        else:
            # LLM fallback (streaming)
            print("🤖 Thinking...")
            response = self._stream_llm_response(
                result.llm_command, result.llm_history,
                memory_context=result.memory_context,
                conversation_messages=result.context_messages,
                raw_command=command,
                in_conversation=in_conversation,
                use_tools=result.use_tools,
                tool_temperature=result.tool_temperature,
                tool_presence_penalty=result.tool_presence_penalty,
            )
            if not response:
                response = "I'm sorry, I'm having trouble processing that right now."

        # Post-process: strip metric conversions Qwen sneaks in, then filler for history
        response = self.llm.strip_metric(response, command) if response else response
        stored_response = self.llm.strip_filler(response) if response else response
        self.conversation.add_message("assistant", stored_response)
        print(f"💬 Jarvis: {response}")
        if not self.tts._spoke:
            self._speak_and_wait(response)

        # Update centralized conversation state
        self.conv_state.update(
            command=command,
            response_text=response or "",
            response_type="llm" if not result.handled else "skill",
        )

        # Record LLM metrics (only when LLM was actually used)
        used_llm = not result.handled or result.used_llm
        self._record_metrics(result, used_llm)

        # Follow-up window — handled results with explicit window instructions
        # skip the default window management
        if result.handled and (result.close_window or result.open_window is not None):
            pass  # Already handled above
        elif self.conversation.request_follow_up:
            duration = self.conversation.request_follow_up
            self.conversation.request_follow_up = None
            self.listener.open_conversation_window(duration)
        else:
            self._manage_conversation_window(response, in_conversation)

        # Stats and resume
        stats = self.conversation.get_conversation_stats()
        print(f"\n📊 Session: {stats['session_user_messages']} user, "
              f"{stats['session_assistant_messages']} assistant messages\n")
        self.listener.resume_listening()
        self.state = PipelineState.IDLE

    # ----- minimal greeting -----

    def _handle_minimal_greeting(self, command: str, in_conversation: bool):
        self.logger.info("Minimal greeting - just wake word")
        if self.reminder_manager and self.reminder_manager.has_rundown_mention():
            self.reminder_manager.clear_rundown_mention()
            response = persona.rundown_mention()
        else:
            response = persona.pick("greeting")

        self.conversation.add_message("user", "jarvis")
        if response:
            self.conversation.add_message("assistant", response)
            print(f"💬 Jarvis: {response}")
            self._speak_and_wait(response)

        self.listener.open_conversation_window(self.listener._extended_duration)
        self.listener.resume_listening()
        self.state = PipelineState.IDLE

    # ----- task plan execution -----

    def _execute_task_plan(self) -> str:
        """Execute the active task plan with TTS progress callbacks.

        Phase 3: Resumes listener during plan execution so voice interrupts
        can be captured. Handles cancellation and partial completion outcomes.
        """
        from core.task_planner import PlanStatus

        plan = self.task_planner.active_plan
        if not plan:
            return "I'm sorry, the plan was lost before I could execute it."

        def progress_callback(description: str):
            """Speak progress between steps."""
            msg = persona.task_progress(description)
            print(f"📋 {msg}")
            self._speak_and_wait(msg)

        # Resume listening so voice interrupts ("stop", "cancel") are captured
        self.listener.resume_listening()

        result = self.task_planner.execute_plan(
            plan, progress_callback=progress_callback,
        )

        # Pause listening before speaking outcome
        self.listener.pause_listening()

        # Speak outcome based on plan status
        completed_count = sum(
            1 for s in plan.steps
            if s.status.value == "completed"
        )
        total = len(plan.steps)

        if plan.status == PlanStatus.CANCELLED:
            if completed_count > 0:
                # Partial completion — speak partial message + result
                msg = persona.task_partial(completed_count, total)
                print(f"📋 {msg}")
                self._speak_and_wait(msg)
            else:
                msg = persona.task_cancelled()
                print(f"📋 {msg}")
                self._speak_and_wait(msg)
        elif result:
            completion = persona.task_complete()
            print(f"📋 {completion}")
            self._speak_and_wait(completion)

        return result or ""

    # ----- streaming LLM -----

    def _stream_llm_response(self, command: str, history: str,
                              memory_context: str = None,
                              conversation_messages: list = None,
                              raw_command: str = None,
                              in_conversation: bool = False,
                              use_tools: list = None,
                              tool_temperature: float = None,
                              tool_presence_penalty: float = None) -> str:
        """Stream LLM response with first-chunk quality gating and tool calling.

        Streams tokens from Qwen, accumulates into sentence chunks,
        and speaks each chunk via a persistent aplay process for
        gapless multi-sentence playback.

        When tool calling is enabled, the LLM may request a tool call
        instead of generating text. The pipeline will execute the tool,
        feed results back, and stream the synthesized answer.

        Args:
            command: The (possibly augmented) text to send to the LLM.
            raw_command: The original user query before context augmentation.
                         Used for tool_choice regex and research exchange storage.
                         Falls back to command if not provided.
            use_tools: When set by the router's P4-LLM path, list of tool
                       schema dicts to pass to stream_with_tools().
            tool_temperature: Override temperature for tool selection phase.
            tool_presence_penalty: Presence penalty for tool-calling requests.
        """
        if raw_command is None:
            raw_command = command
        chunker = SpeechChunker()
        full_response = ""
        chunks_spoken = 0
        first_chunk_checked = False

        # Decide whether to play an ack while LLM streams
        ack_style, suppress_ack = self._classify_ack(
            raw_command,
            in_conversation=in_conversation,
            jarvis_asked_question=self.conv_state.jarvis_asked_question,
        )
        self._llm_responded = False
        ack_timer = None

        if suppress_ack:
            # Skip ack entirely — mark as already responded so callback never fires
            self._llm_responded = True
            self.logger.debug(f"Ack suppressed for: {raw_command!r} (style={ack_style})")
        else:
            ack_timer = threading.Timer(
                0.3, self._play_ack_if_still_thinking, args=(ack_style,)
            )
            ack_timer.daemon = True
            ack_timer.start()

        # Gapless audio pipeline (Kokoro only; Piper falls back to blocking)
        use_pipeline = (self.tts.engine == "kokoro")
        audio_pipeline = None

        # Choose tool-aware or plain streaming
        _enable_tools = self.llm.tool_calling and (self.web_researcher or use_tools)

        try:
            pending_chunk = None
            tool_call_request = None

            # --- Phase A: stream from LLM (may yield ToolCallRequest) ---
            token_source = (
                self.llm.stream_with_tools(
                    user_message=command,
                    conversation_history=history,
                    memory_context=memory_context,
                    conversation_messages=conversation_messages,
                    raw_command=raw_command,
                    tools=use_tools,
                    tool_temperature=tool_temperature,
                    tool_presence_penalty=tool_presence_penalty,
                ) if _enable_tools else
                self.llm.stream(
                    user_message=command,
                    conversation_history=history,
                    memory_context=memory_context,
                    conversation_messages=conversation_messages,
                )
            )

            for item in token_source:
                # Tool call sentinel — break to Phase B
                if isinstance(item, ToolCallRequest):
                    tool_call_request = item
                    if not self._llm_responded:
                        self._llm_responded = True
                        if ack_timer:
                            ack_timer.cancel()
                    break

                # Regular token
                token = item
                if not self._llm_responded:
                    self._llm_responded = True
                    if ack_timer:
                        ack_timer.cancel()

                full_response += token
                chunk = chunker.feed(token)

                if chunk:
                    chunks_spoken, first_chunk_checked, pending_chunk, audio_pipeline = \
                        self._process_speech_chunk(
                            chunk, command, history, memory_context,
                            conversation_messages, chunks_spoken,
                            first_chunk_checked, pending_chunk,
                            audio_pipeline, use_pipeline,
                        )
                    if chunks_spoken == -1:  # quality gate failed
                        return pending_chunk  # contains fallback response

            # --- Phase B: handle tool call if requested ---
            # Multi-tool loop: execute tool, synthesize, repeat if LLM
            # requests another tool (e.g. "time and weather" → get_time then get_weather).
            # Cap at 3 to prevent runaway loops.
            _MAX_TOOL_CHAIN = 3
            tool_chain_count = 0

            while tool_call_request and tool_chain_count < _MAX_TOOL_CHAIN:
                tool_chain_count += 1
                self.logger.info(
                    f"🔧 Tool call: {tool_call_request.name}({tool_call_request.arguments})"
                )

                # Execute the tool
                if tool_call_request.name == "web_search":
                    query = tool_call_request.arguments.get("query", command)
                    print(f"🔍 Searching: {query}")
                    results = self.web_researcher.search(query)
                    self.conv_state.research_results = results

                    page_sections = self.web_researcher.fetch_pages_parallel(results)
                    page_content = ""
                    if page_sections:
                        page_content = "\n\nFull article content:\n\n" + \
                            "\n\n---\n\n".join(page_sections)

                    tool_result = format_search_results(results) + page_content
                    print(f"📋 Found {len(results)} results")
                else:
                    from core.tool_executor import execute_tool
                    print(f"🔧 Running: {tool_call_request.name}")
                    tool_result = execute_tool(
                        tool_call_request.name, tool_call_request.arguments
                    )
                    self.logger.info(f"Tool result: {tool_result[:100]}")

                    # "Show me" display hook for developer_tools
                    if tool_call_request.name == "developer_tools":
                        show_me = _detect_show_me(command)
                        if (show_me and tool_result
                                and not tool_result.startswith(("BLOCKED", "CONFIRMATION REQUIRED", "Error"))):
                            action = tool_call_request.arguments.get("action", "")
                            ct, title = _DEVTOOLS_DISPLAY_MAP.get(action, ("general", "Output"))
                            backend = show_me if show_me in ("terminal", "vscode") else None
                            try:
                                _get_display_router(self.config).show(
                                    tool_result, content_type=ct, title=title,
                                    force_backend=backend,
                                )
                            except Exception as e:
                                self.logger.warning(f"Display hook error: {e}")

                # Stream synthesis — may yield text tokens or another ToolCallRequest
                next_tool_call = None
                for item in self.llm.continue_after_tool_call(
                    tool_call_request, tool_result,
                    tools=use_tools,
                ):
                    if isinstance(item, ToolCallRequest):
                        next_tool_call = item
                        break

                    token = item
                    if not self._llm_responded:
                        self._llm_responded = True

                    full_response += token
                    chunk = chunker.feed(token)

                    if chunk:
                        chunks_spoken, first_chunk_checked, pending_chunk, audio_pipeline = \
                            self._process_speech_chunk(
                                chunk, command, history, memory_context,
                                conversation_messages, chunks_spoken,
                                first_chunk_checked, pending_chunk,
                                audio_pipeline, use_pipeline,
                            )
                        if chunks_spoken == -1:
                            return pending_chunk

                tool_call_request = next_tool_call

            # Combine buffered last chunk + flush remnant, strip filler, then speak
            remaining = chunker.flush()
            final_text = (pending_chunk or "") + (" " + remaining if remaining else "")

            if final_text.strip():
                if not first_chunk_checked:
                    quality_issue = self.llm._check_response_quality(final_text, command)
                    if quality_issue:
                        self.logger.warning(
                            f"Streaming quality gate failed ({quality_issue}): "
                            f"'{final_text[:60]}' — falling back to sync chat()"
                        )
                        if audio_pipeline:
                            audio_pipeline.finish()
                        return self.llm.chat(
                            user_message=command,
                            conversation_history=history,
                            memory_context=memory_context,
                            conversation_messages=conversation_messages,
                        )
                final_text = self.llm.strip_filler(self.llm.strip_metric(final_text, command))
                if final_text.strip():
                    if audio_pipeline:
                        audio_pipeline.put(final_text)
                    else:
                        self._speak_and_wait(final_text)
                    chunks_spoken += 1

            # Wait for all audio to finish playing
            if audio_pipeline:
                audio_pipeline.finish()

        except Exception as e:
            self.logger.error(f"Streaming LLM error: {e}")
            self._llm_responded = True
            if ack_timer:
                ack_timer.cancel()
            if audio_pipeline:
                audio_pipeline.finish()
            if not full_response:
                return self.llm.chat(
                    user_message=command,
                    conversation_history=history,
                    memory_context=memory_context,
                    conversation_messages=conversation_messages,
                )

        # Cancel ack timer if stream was empty
        if not self._llm_responded:
            self._llm_responded = True
            if ack_timer:
                ack_timer.cancel()

        if chunks_spoken > 0:
            self.logger.info(f"Streamed LLM response in {chunks_spoken} chunks")
            self.tts._spoke = True

        # Extended conversation window after research answers
        if tool_call_request:
            self.conversation.request_follow_up = 15.0
            # Store the exchange so follow-ups have context.
            # Use raw_command (not the augmented command) to prevent nested
            # context wrapping on successive follow-ups.
            self.conv_state.set_research_context(
                results=self.conv_state.research_results or [],
                exchange={"query": raw_command, "answer": full_response},
            )

        return full_response

    def _process_speech_chunk(self, chunk, command, history, memory_context,
                              conversation_messages, chunks_spoken,
                              first_chunk_checked, pending_chunk,
                              audio_pipeline, use_pipeline):
        """Process a completed sentence chunk for speech.

        Returns updated (chunks_spoken, first_chunk_checked, pending_chunk, audio_pipeline).
        On quality gate failure, returns (-1, ..., fallback_response, ...).
        """
        if not first_chunk_checked:
            first_chunk_checked = True
            quality_issue = self.llm._check_response_quality(chunk, command)
            if quality_issue:
                self.logger.warning(
                    f"Streaming quality gate failed ({quality_issue}): "
                    f"'{chunk[:60]}' — falling back to sync chat()"
                )
                fallback = self.llm.chat(
                    user_message=command,
                    conversation_history=history,
                    memory_context=memory_context,
                    conversation_messages=conversation_messages,
                )
                return -1, first_chunk_checked, fallback, audio_pipeline

        if chunks_spoken == 0 and pending_chunk is None:
            # First chunk — strip redundant opener if ack already played
            if self.tts.ack_played:
                chunk = self._strip_ack_opener(chunk)
                self.tts.clear_ack_played()
                if not chunk:
                    return chunks_spoken, first_chunk_checked, pending_chunk, audio_pipeline
            processed = self.llm.strip_metric(chunk, command)
            self.listener.speaking = True
            if use_pipeline:
                audio_pipeline = StreamingAudioPipeline(
                    self.tts, self.logger
                )
                audio_pipeline.start()
                audio_pipeline.put(processed)
            else:
                self._speak_and_wait(processed)
            chunks_spoken += 1
        else:
            # Buffer subsequent chunks; submit the previous one
            if pending_chunk:
                processed = self.llm.strip_metric(pending_chunk, command)
                if audio_pipeline:
                    audio_pipeline.put(processed)
                else:
                    self._speak_and_wait(processed)
                chunks_spoken += 1
            pending_chunk = chunk

        return chunks_spoken, first_chunk_checked, pending_chunk, audio_pipeline

    # ----- TTS helpers -----

    def _speak_and_wait(self, text: str):
        """Speak text synchronously (blocks until playback finishes).

        For the coordinator thread this is fine — we don't need to process
        other events while speaking a response to the current command.
        """
        self.listener.speaking = True
        self.tts.speak(text)

    # Regex to strip redundant LLM opening phrases when ack already played
    _ACK_OPENER_RE = re.compile(
        r'^(Certainly|Of course|Very well|Right away|One moment|Just a moment|'
        r'Give me (?:just )?a moment|Absolutely|Sure thing|One second)'
        r'[,.]?\s*(?:sir|ma\'am|miss)?\.?\s*',
        re.IGNORECASE,
    )

    @staticmethod
    def _classify_ack_style(command: str) -> str:
        """Classify query into an ack style for contextual acknowledgments.

        Legacy wrapper — use _classify_ack() for new code.
        """
        style, _ = Coordinator._classify_ack(command)
        return style

    @staticmethod
    def _classify_ack(command: str, in_conversation: bool = False,
                      jarvis_asked_question: bool = False) -> tuple:
        """Classify query style and decide whether to suppress the ack.

        Returns:
            (style: str, suppress: bool)
            style — one of "research", "checking", "working", "neutral"
            suppress — True if the ack should be skipped entirely
        """
        cl = command.lower().strip()
        word_count = len(cl.split())

        # --- Classify style ---
        # Research / current events
        if any(w in cl for w in ("search", "look up", "find out", "latest", "current", "news about")):
            style = "research"
        # Factual lookup
        elif any(cl.startswith(w) for w in (
            "what ", "who ", "when ", "where ", "how many ", "how much ", "is ", "are ", "was ", "does ",
        )):
            style = "checking"
        # Complex / explanatory
        elif any(cl.startswith(w) for w in (
            "explain ", "tell me about ", "describe ", "compare ", "why ", "how do ", "how does ",
        )):
            style = "working"
        else:
            style = "neutral"

        # --- Suppression rules ---
        # NEVER suppress research or working/complex queries — they benefit from acks
        if style in ("research", "working"):
            return style, False

        # Very short query (<=5 words) — likely conversational, Qwen responds fast
        if word_count <= 5:
            return style, True

        # JARVIS just asked a question and user is answering — no ack needed
        if jarvis_asked_question:
            return style, True

        # In-conversation + short query (<=12 words) — fast follow-up
        if in_conversation and word_count <= 12:
            return style, True

        return style, False

    def _play_ack_if_still_thinking(self, style_hint: str = None):
        """Timer callback — plays ack if LLM hasn't responded yet."""
        if not self._llm_responded:
            # Pause mic BEFORE playback to prevent speaker-to-mic bleed
            # (ack phrase picked up as a new user command).  The main response
            # flow calls listener.resume_listening() when fully done.
            self.listener.pause_listening()
            # Pass cancel_check so speak_ack rechecks after acquiring the
            # TTS lock — prevents stale acks when the response arrived
            # while this thread was blocked waiting for the lock.
            self.tts.speak_ack(
                style_hint=style_hint,
                cancel_check=lambda: self._llm_responded,
            )

    def _strip_ack_opener(self, text: str) -> str:
        """Strip leading ack phrase from LLM text if ack was already spoken."""
        stripped = self._ACK_OPENER_RE.sub('', text)
        if stripped != text:
            # Capitalize the new leading character
            stripped = stripped.lstrip()
            if stripped:
                stripped = stripped[0].upper() + stripped[1:]
            self.logger.info(f"Stripped ack opener: '{text[:40]}' → '{stripped[:40]}'")
        return stripped

    def _play_beep(self):
        """Play wake-word acknowledgment beep."""
        try:
            if not self.beep_path.exists():
                return
            import subprocess
            audio_device = self.config.get("audio.output_device", "plughw:0,0")
            subprocess.run(
                ["aplay", "-D", audio_device, str(self.beep_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception as e:
            self.logger.error(f"Failed to play beep: {e}")

    # ----- conversation helpers -----

    def _handle_close_conversation(self, event: Event):
        """Handle explicit conversation window close + reset memory surfacing."""
        self.listener.close_conversation_window()
        if self.memory_manager:
            self.memory_manager.reset_surfacing_window()
        if self.context_window:
            self.context_window.reset()
        # Reset centralized conversation state (clears research cache,
        # jarvis_asked_question, last intent/response tracking)
        self.conv_state.close_window()
        if self.web_researcher:
            self.web_researcher.clear_cache()

    def _on_conversation_timeout(self):
        """Cleanup when conversation window expires due to silence.

        Called by ContinuousListener.on_window_close callback.
        Same cleanup as _handle_close_conversation, but the listener already
        closed itself (flipped flag + played tone), so we skip that call.
        """
        self.logger.info("Timeout cleanup: resetting state, context, caches")
        if self.memory_manager:
            self.memory_manager.reset_surfacing_window()
        if self.context_window:
            self.context_window.reset()
        self.conv_state.close_window()
        if self.web_researcher:
            self.web_researcher.clear_cache()

    def _manage_conversation_window(self, response: str, was_in_conversation: bool):
        """Decide whether to open/extend the conversation window.

        Window duration adapts to conversation context:
        - Base: extended (7s) if follow-up invited, default (4s) otherwise
        - Active conversation bonus: 3+ turns → 50% extension
        - JARVIS asked a question → at least 10s for user to think
        """
        if self.conversation.should_open_follow_up_window(response):
            base = self.listener._extended_duration
        else:
            base = self.listener._default_duration

        # Active conversation bonus: 3+ turns → 50% extension
        if self.conv_state.turn_count >= 3:
            base *= 1.5

        # JARVIS asked a question → give user time to think
        if response and response.rstrip().endswith("?"):
            base = max(base, 10.0)

        self.listener.open_conversation_window(base)

    def _extract_command(self, full_text: str) -> str:
        """Extract command text from transcription (remove wake word)."""
        import string
        text_lower = full_text.lower()
        wake_pos = text_lower.find(self.wake_word)

        if wake_pos == -1:
            return full_text.strip()

        before = full_text[:wake_pos].strip()
        after = full_text[wake_pos + len(self.wake_word):].strip()

        before = before.strip(string.punctuation + ' ')
        after = after.strip(string.punctuation + ' ')

        if after:
            return after
        elif before:
            return before
        else:
            return "jarvis_only"

    # ----- noise / correction helpers (from continuous_listener) -----

    def _is_conversation_noise(self, text: str) -> bool:
        """Check if text during conversation window is likely noise."""
        if len(text) < 2:
            return True
        unique_chars = set(text.replace(' ', ''))
        if len(unique_chars) <= 3 and len(text) > 5:
            return True
        words = text.strip().split()
        if len(words) == 1 and words[0] not in self._valid_short_replies:
            if len(words[0]) < 4:
                return True
        return False

    # Post-transcription word corrections for known Whisper mishearings.
    # Mirrors continuous_listener._TRANSCRIPTION_CORRECTIONS — keep in sync.
    _TRANSCRIPTION_CORRECTIONS = {
        "and videos": "amd's",
        "and video": "amd",
        "in video": "nvidia",
        "in vidya": "nvidia",
        "and vidya": "nvidia",
        "quinn": "qwen",
    }

    def _apply_transcription_corrections(self, text: str) -> str:
        """Fix known Whisper brand-name mishearings (AMD, NVIDIA, etc.)."""
        corrected = text
        for wrong, right in self._TRANSCRIPTION_CORRECTIONS.items():
            if wrong in corrected:
                corrected = corrected.replace(wrong, right)
        if corrected != text:
            self.logger.info(f"Corrected: '{text}' → '{corrected}'")
        return corrected

    def _apply_command_corrections(self, text: str) -> str:
        """Apply corrections for common command mishearings."""
        import re
        if re.match(r'^i (was|analyzed)\s+', text, re.IGNORECASE):
            return re.sub(r'^i (was|analyzed)\s+', 'analyze ', text, flags=re.IGNORECASE)
        if text.lower().startswith("i'm "):
            return "analyze " + text[4:]
        return text

    # ----- ambient wake word filter -----

    # Words that follow "jarvis" in ambient speech (talking ABOUT jarvis)
    # but never follow "jarvis," in a command.
    _AMBIENT_FOLLOWERS = frozenset({
        'is', 'was', 'has', 'had', 'will', 'would', 'can', 'could',
        'does', 'did', 'should', 'might', 'may', 'of',
    })

    # Prefixes that legitimately precede the wake word (e.g. "hey jarvis")
    _WAKE_PREFIXES = frozenset({
        'hey', 'hi', 'yo', 'morning', 'good', 'okay', 'ok',
    })

    def _is_ambient_wake_word(self, text: str, matched_word: str) -> bool:
        """Determine if a wake word detection is ambient conversation, not a command.

        Uses position, post-wake-word analysis, and utterance length to
        distinguish "Jarvis, what time is it?" from "he was talking about Jarvis".

        Returns True if the wake word should be IGNORED (ambient).
        """
        words = text.split()

        # Find word index of the matched wake word
        word_idx = None
        for i, w in enumerate(words):
            if w.strip('.,!?;:\'"') == matched_word:
                word_idx = i
                break

        if word_idx is None:
            return False  # Can't determine — let it through

        # --- Signal 1: Position ---
        # Real commands have "jarvis" at the start (first 2-3 words with
        # prefixes like "hey"/"good morning") OR at the end (trailing
        # address like "what time is it, jarvis?").
        # Ambient mentions are in the MIDDLE of longer sentences.
        effective_pos = word_idx
        if word_idx <= 2:
            # Check if earlier words are known prefixes
            prefix_words = [w.strip('.,!?;:') for w in words[:word_idx]]
            if all(pw in self._WAKE_PREFIXES for pw in prefix_words):
                effective_pos = 0  # Treat as position 0

        # Trailing wake word = command ("how are you, jarvis?")
        is_trailing = word_idx >= len(words) - 2  # last or second-to-last word

        if effective_pos >= 3 and not is_trailing:
            self.logger.info(
                f"🔇 Ambient rejected (position {word_idx}): {text[:80]}"
            )
            return True

        # --- Signal 2: Post-wake-word copula/auxiliary ---
        # "jarvis is listening" = ambient.  "jarvis, is it raining?" = command.
        # The comma after "jarvis" is the key differentiator.
        if word_idx < len(words):
            wake_token = words[word_idx]  # e.g. "jarvis," or "jarvis" or "jarvis's"

            # Possessive = always ambient ("jarvis's brain")
            if wake_token.endswith("'s") or wake_token.endswith("\u2019s"):
                self.logger.info(
                    f"🔇 Ambient rejected (possessive): {text[:80]}"
                )
                return True

            has_comma = wake_token.endswith(',')
            if not has_comma and word_idx + 1 < len(words):
                next_word = words[word_idx + 1].strip('.,!?;:').lower()
                if next_word in self._AMBIENT_FOLLOWERS:
                    self.logger.info(
                        f"🔇 Ambient rejected ('{matched_word} {next_word}' "
                        f"without comma): {text[:80]}"
                    )
                    return True

        # --- Signal 5: Length heuristic ---
        # Very long utterances where wake word isn't the opener are
        # almost certainly ambient conversation, not commands.
        if len(words) > 15 and word_idx > 0:
            self.logger.info(
                f"🔇 Ambient rejected (long utterance {len(words)} words, "
                f"wake word at position {word_idx}): {text[:80]}"
            )
            return True

        return False

    # ----- speaker context -----

    def _apply_speaker_context(self, speaker_id: Optional[str], confidence: float):
        """Set honorific and conversation user based on speaker identification."""
        if speaker_id and self.profile_manager:
            honorific = self.profile_manager.get_honorific_for(speaker_id)
            set_honorific(honorific)
            self.conversation.current_user = speaker_id
            self.logger.info(
                f"Speaker identified: {speaker_id} (confidence={confidence:.3f}, "
                f"honorific={honorific})"
            )
        elif speaker_id is None and self.profile_manager:
            # Unknown speaker — keep current honorific (default "sir")
            self.logger.debug(f"Speaker unknown (confidence={confidence:.3f})")

    # ----- resume handler -----

    def _handle_resume(self, event: Event):
        """Handle RESUME_LISTENING — suppressed while streaming is active."""
        if self._streaming_active:
            self.logger.debug("Suppressing resume — streaming active")
            return
        self.listener.resume_listening()

    # ----- speech lifecycle -----

    def _handle_speech_started(self, event: Event):
        self.logger.debug("Speech started")

    def _handle_speech_finished(self, event: Event):
        self.logger.debug("Speech finished")

    # ----- LLM complete -----

    def _handle_llm_complete(self, event: Event):
        """Handle LLM streaming completion."""
        self._streaming_active = False
        self.logger.info("LLM streaming complete")

    # ----- error handling -----

    def _handle_error(self, event: Event):
        data = event.data or {}
        source = data.get("source", "unknown")
        error = data.get("error", "unknown error")
        self.logger.error(f"Pipeline error from {source}: {error}")

        self.stats['errors'] += 1
        self.stats['last_error_time'] = time.time()
        self.stats['last_error_msg'] = f"{source}: {error}"

        if source == "tts":
            # TTS failure — ensure listening resumes
            self.listener.resume_listening()

    # ----- metrics recording -----

    def _record_metrics(self, result: RouteResult, used_llm: bool):
        """Record LLM interaction metrics if the LLM was used."""
        if not self.metrics or not used_llm:
            return
        try:
            info = self.llm.last_call_info or {}
            self.metrics.record(
                provider=info.get('provider', 'unknown'),
                method=info.get('method', 'unknown'),
                prompt_tokens=info.get('input_tokens'),
                completion_tokens=info.get('output_tokens'),
                estimated_tokens=info.get('estimated_tokens'),
                model=info.get('model'),
                latency_ms=info.get('latency_ms'),
                ttft_ms=info.get('ttft_ms'),
                skill=result.match_info.get('skill_name') if result.match_info else None,
                intent=result.match_info.get('handler') if result.match_info else None,
                input_method='voice',
                quality_gate=info.get('quality_gate', False),
                is_fallback=info.get('is_fallback', False),
                error=info.get('error'),
            )
        except Exception as e:
            self.logger.error(f"Metrics recording failed: {e}")
