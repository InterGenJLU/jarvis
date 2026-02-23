#!/usr/bin/env python3

# CRITICAL: Set multiprocessing to spawn BEFORE any imports
import multiprocessing as mp
mp.set_start_method("spawn", force=True)


# CRITICAL: Set ROCm environment BEFORE any imports
import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'

"""
Jarvis Continuous Listening Mode

Always-listening mode with Voice Activity Detection (VAD).
Detects speech, transcribes it, and responds when wake word is found.

Flow:
1. Continuously listen for speech (VAD)
2. Buffer last 3 seconds of audio
3. When speech detected, transcribe including buffer
4. If "Jarvis" found in transcription, process as command
5. Respond naturally

This allows natural phrases like "Good morning Jarvis" to work!
"""

import re
import sys
import time
import signal
import subprocess
import threading
import numpy as np
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

import queue

from core.config import load_config
from core.logger import get_logger
from core.stt import SpeechToText
from core.tts import TextToSpeech
from core.conversation import ConversationManager
from core.responses import get_response_library
from core.llm_router import LLMRouter
from core.skill_manager import SkillManager
from core.continuous_listener import ContinuousListener
from core.reminder_manager import get_reminder_manager
from core.news_manager import get_news_manager
from core.honorific import get_honorific
from core.speech_chunker import SpeechChunker
from core.events import Event, EventType
from core.pipeline import (
    Coordinator, STTWorker, TTSWorker,
    EventBridge, EventTTSProxy,
)
from core.user_profile import get_profile_manager
from core.speaker_id import SpeakerIdentifier
from core.context_window import get_context_window
from core.desktop_manager import get_desktop_manager
from core.metrics_tracker import get_metrics_tracker


class JarvisContinuous:
    """Jarvis with continuous listening"""
    
    def __init__(self, config):
        """
        Initialize Jarvis Continuous
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        
        # Wake word
        self.wake_word = config.get("system.wake_word", "jarvis").lower()
        
        # Initialize components
        self.logger.info("Initializing Jarvis components...")
        
        self.tts = TextToSpeech(config)
        self.stt = SpeechToText(config)
        self.conversation = ConversationManager(config)
        self.responses = get_response_library()
        self.llm = LLMRouter(config)
        
        # Initialize skill system
        self.skill_manager = SkillManager(config, self.conversation, self.tts, self.responses, self.llm)
        self.logger.info("Loading skills...")
        skills_loaded = self.skill_manager.load_all_skills()
        self.logger.info(f"Loaded {skills_loaded} skills")

        # --- User profile + speaker identification ---
        self.profile_manager = None
        self.speaker_id = None
        if config.get("user_profiles.enabled", False):
            self.profile_manager = get_profile_manager(config)
            if self.profile_manager and config.get("user_profiles.voice_recognition", False):
                self.speaker_id = SpeakerIdentifier(config, self.profile_manager)
                self.speaker_id.load_embeddings()
                self.logger.info(f"Speaker ID ready ({len(self.speaker_id._cache)} enrolled voices)")

        # --- Conversational memory system ---
        self.memory_manager = None
        if config.get("conversational_memory.enabled", False):
            from core.memory_manager import get_memory_manager
            self.memory_manager = get_memory_manager(
                config=config,
                conversation=self.conversation,
                embedding_model=self.skill_manager._embedding_model,
            )
            self.conversation.set_memory_manager(self.memory_manager)
            self.logger.info("Conversational memory system enabled")

        # --- Context window (working memory) ---
        self.context_window = None
        if config.get("context_window.enabled", False):
            self.context_window = get_context_window(
                config=config,
                embedding_model=self.skill_manager._embedding_model,
                llm=self.llm,
            )
            self.conversation.set_context_window(self.context_window)

            # Load prior segments from SQLite (falls back to JSONL replay if empty)
            self.context_window.load_prior_segments(
                fallback_messages=self.conversation.session_history
            )

            self.logger.info("Context window (working memory) enabled")

        # --- Desktop manager (GNOME integration) ---
        self.desktop_manager = None
        if config.get("desktop.enabled", True):
            self.desktop_manager = get_desktop_manager(config)
            self.logger.info("Desktop manager initialized")

        # --- LLM Metrics tracking ---
        self.metrics = get_metrics_tracker(config)
        if self.metrics:
            self.logger.info("LLM metrics tracking enabled")

        # --- Event pipeline mode (Phase 4) ---
        self.event_mode = config.get("pipeline.event_mode", False)

        if self.event_mode:
            self.event_queue = queue.Queue()
            self.tts_queue = queue.Queue()
            self.audio_queue = queue.Queue()
            self.bridge = EventBridge(self.event_queue)
            self.bg_tts = EventTTSProxy(self.tts_queue, self.event_queue)
        else:
            self.event_queue = None
            self.tts_queue = None
            self.audio_queue = None
            self.bridge = None
            self.bg_tts = None

        # Initialize continuous listener
        self.listener = ContinuousListener(
            config,
            self.stt,
            on_command=self.on_command_detected,
            audio_queue=self.audio_queue,
        )

        # Register interruption handler
        self.listener.on_interrupt = self.on_interrupt_detected

        # Cleanup on conversation timeout (silence-expired windows)
        if not self.event_mode:
            self.listener.on_window_close = self._on_conversation_timeout

        # TTS subprocess tracking for interruption
        self.current_tts_process = None
        self.tts_interrupted = False  # Track if TTS was interrupted

        # Initialize reminder system
        if config.get("reminders.enabled", True):
            if self.event_mode:
                self.reminder_manager = get_reminder_manager(config, self.bg_tts, self.conversation)
                self.reminder_manager.set_ack_window_callback(
                    lambda rid: self.bridge.open_conversation_window(30)
                )
                self.reminder_manager.set_window_callback(
                    lambda duration: self.bridge.open_conversation_window(duration)
                )
                self.reminder_manager.set_listener_callbacks(
                    pause=self.bridge.pause_listening,
                    resume=self.bridge.resume_listening,
                )
            else:
                self.reminder_manager = get_reminder_manager(config, self.tts, self.conversation)
                self.reminder_manager.set_ack_window_callback(
                    lambda rid: self.listener.open_conversation_window(30)
                )
                self.reminder_manager.set_window_callback(
                    lambda duration: self.listener.open_conversation_window(duration)
                )
                self.reminder_manager.set_listener_callbacks(
                    pause=self.listener.pause_listening,
                    resume=self.listener.resume_listening,
                )

            # Google Calendar integration
            self.calendar_manager = None
            if config.get("google_calendar.enabled", False):
                try:
                    from core.google_calendar import get_calendar_manager
                    self.calendar_manager = get_calendar_manager(config)
                    self.reminder_manager.set_calendar_manager(self.calendar_manager)
                    self.calendar_manager.start()
                    self.logger.info("Google Calendar sync started")
                except Exception as e:
                    self.logger.warning(f"Google Calendar init failed: {e}")

            self.reminder_manager.start()
            self.logger.info("Reminder system started")
        else:
            self.reminder_manager = None
            self.calendar_manager = None

        # Initialize news system
        if config.get("news.enabled", False):
            if self.event_mode:
                self.news_manager = get_news_manager(config, self.bg_tts, self.conversation, self.llm)
                self.news_manager.set_listener_callbacks(
                    pause=self.bridge.pause_listening,
                    resume=self.bridge.resume_listening,
                )
                self.news_manager.set_window_callback(
                    lambda duration: self.bridge.open_conversation_window(duration)
                )
            else:
                self.news_manager = get_news_manager(config, self.tts, self.conversation, self.llm)
                self.news_manager.set_listener_callbacks(
                    pause=self.listener.pause_listening,
                    resume=self.listener.resume_listening,
                )
                self.news_manager.set_window_callback(
                    lambda duration: self.listener.open_conversation_window(duration)
                )
            self.news_manager.start()
            self.logger.info("News monitor started")
        else:
            self.news_manager = None

        # --- Create pipeline workers and coordinator (event mode) ---
        if self.event_mode:
            self.stt_worker = STTWorker(
                self.stt, self.event_queue, self.audio_queue,
                config, speaker_id=self.speaker_id,
            )
            self.coordinator = Coordinator(
                config=config,
                event_queue=self.event_queue,
                tts_queue=self.tts_queue,
                listener=self.listener,
                tts=self.tts,
                llm=self.llm,
                skill_manager=self.skill_manager,
                conversation=self.conversation,
                reminder_manager=self.reminder_manager,
                news_manager=self.news_manager,
                calendar_manager=self.calendar_manager,
                profile_manager=self.profile_manager,
                memory_manager=self.memory_manager,
                context_window=self.context_window,
                desktop_manager=self.desktop_manager,
                metrics=self.metrics,
            )
            self.logger.info("Event pipeline mode enabled")

        # Beep
        self.beep_path = Path(__file__).parent / "assets" / "wake_word_detect.wav"

        self.logger.info("✅ Jarvis initialized")
    
    def play_beep(self):
        """Play acknowledgment beep"""
        try:
            if not self.beep_path.exists():
                return
            
            audio_device = self.config.get("audio.output_device", "plughw:0,0")
            
            subprocess.run(
                ["aplay", "-D", audio_device, str(self.beep_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2
            )
        except Exception as e:
            self.logger.error(f"Failed to play beep: {e}")
    
    def on_interrupt_detected(self):
        """Handle interruption during TTS playback"""
        self.logger.info("Interruption: Stopping TTS")

        # Set interrupted flag
        self.tts_interrupted = True

        # Kill only our tracked audio subprocesses (scoped, not global pkill)
        try:
            self.tts.kill_active()
            self.logger.info("TTS processes killed")
        except Exception as e:
            self.logger.error(f"Failed to kill TTS: {e}")

        # Wait a moment for audio buffers to drain
        import time
        time.sleep(0.5)
        
        # Mark speaking as false so VAD can collect the new command
        self.listener.speaking = False
        self.listener.collecting_speech = True
    
    def speak_interruptible(self, text: str):
        """Speak text with interruption support"""
        self.tts_interrupted = False  # Reset flag
        try:
            self.tts.speak(text)
        except Exception as e:
            self.logger.error(f"TTS error: {e}")
        
        # Only clear speaking flag if not interrupted
        if not self.tts_interrupted:
            self.listener.speaking = False
    
    def on_command_detected(self, full_text: str):
        """
        Callback when wake word is detected or speech arrives during conversation window.

        Supports multi-turn conversation: the window stays open across exchanges
        and only closes on silence timeout.
        """
        import random

        # Track whether we were already mid-conversation
        in_conversation = self.listener.conversation_window_active

        # --- Parse the input ---
        if in_conversation:
            self.logger.info(f"💬 Conversation continues: {full_text}")
            print(f"\n💬 You said: {full_text}")
            # If wake word appears mid-conversation, extract command from it
            if self.wake_word in full_text.lower():
                command = self._extract_command(full_text)
            else:
                command = full_text
        else:
            self.logger.info(f"🟡 Command detected: {full_text}")
            print(f"\n🟡 Command detected: {full_text}")
            command = self._extract_command(full_text)

        # Pause listening while we process and respond
        self.listener.pause_listening()

        # Play beep only for fresh wake word activation (not mid-conversation)
        if not in_conversation and self.wake_word in full_text.lower():
            self.play_beep()

        if not command:
            self.logger.warning("No command extracted")
            self.listener.resume_listening()
            return

        self.logger.info(f"Command: {repr(command.strip())}")

        # --- Minimal greeting: just the wake word ---
        if command.strip() == "jarvis_only" or len(command.strip()) <= 2:
            self.logger.info("Minimal greeting - just wake word")

            # Check if there's a pending rundown mention
            if self.reminder_manager and self.reminder_manager.has_rundown_mention():
                self.reminder_manager.clear_rundown_mention()
                response = f"Good morning, {get_honorific()}. I have your daily rundown whenever you're ready."
            else:
                h = get_honorific()
                responses = [
                    f"At your service, {h}.",
                    f"How may I assist you, {h}?",
                    f"You rang, {h}?",
                    f"I'm listening, {h}.",
                    f"Ready when you are, {h}.",
                    f"Standing by, {h}.",
                ]
                response = random.choice(responses)

            # Record in history
            self.conversation.add_message("user", "jarvis")
            if response:
                self.conversation.add_message("assistant", response)
                print(f"💬 Jarvis: {response}")
                self._speak_response(response)

            # User just summoned JARVIS — they want to talk, use extended duration
            self.listener.open_conversation_window(self.listener._extended_duration)
            self.listener.resume_listening()
            return

        # --- Process real command ---
        print(f"📝 Processing: {command}")
        self.logger.info(f"Processing command: {command}")

        self.conversation.add_message("user", command)

        skill_handled = False
        response = ""
        self.tts._spoke = False

        # Priority 1: Rundown acceptance (must intercept before skill routing)
        if self.reminder_manager and self.reminder_manager.is_rundown_pending():
            text_lower = command.strip().lower()
            words = set(re.findall(r'\b\w+\b', text_lower))
            negative = bool(
                words & {"no", "later", "hold", "skip"}
                or "not now" in text_lower
                or "not yet" in text_lower
            )
            if negative:
                self.reminder_manager.defer_rundown()
                response = f"Very well, {get_honorific()}. Just say 'daily rundown' whenever you're ready."
                skill_handled = True
                self._speak_response(response)
            else:
                # Affirmative or ambiguous — user is present, deliver it
                self.reminder_manager.deliver_rundown()
                response = ""
                skill_handled = True

        # Priority 2: Reminder acknowledgment
        if not skill_handled and self.reminder_manager and self.reminder_manager.is_awaiting_ack():
            # Any speech after a fired reminder counts as acknowledgment
            self.logger.info("Treating response as reminder acknowledgment")
            self.reminder_manager.acknowledge_last()
            h = get_honorific()
            response = random.choice([
                f"Very good, {h}.",
                f"Noted, {h}.",
                f"Of course, {h}.",
                f"Absolutely, {h}.",
            ])
            skill_handled = True
            self._speak_response(response)

        # Priority 3: Skill routing
        if not skill_handled:
            print("🔍 Checking skills...")
            skill_response = self.skill_manager.execute_intent(command)
            if skill_response:
                response = skill_response
                skill_handled = True
                self.logger.info("Handled by skill")

        # Priority 4: News article pull-up
        if not skill_handled and self.news_manager and self.news_manager.get_last_read_url():
            pull_phrases = ["pull that up", "show me that", "open that",
                            "let me see", "show me the article", "open the article"]
            if any(p in command.strip().lower() for p in pull_phrases):
                url = self.news_manager.get_last_read_url()
                browser = self.config.get("web_navigation.default_browser", "brave")
                browser_cmd = f"{browser}-browser" if browser != "brave" else "brave-browser"
                import subprocess as _sp
                _sp.Popen([browser_cmd, url])
                self.news_manager.clear_last_read()
                h = get_honorific()
                response = random.choice([
                    f"Right away, {h}.",
                    f"Pulling that up now, {h}.",
                    f"Opening that article for you, {h}.",
                ])
                skill_handled = True
                self._speak_response(response)

        # Priority 5: News continuation
        if not skill_handled and self.news_manager and in_conversation:
            continue_words = ["continue", "keep going", "more headlines",
                              "go on", "read more"]
            if any(w in command.strip().lower() for w in continue_words):
                remaining = self.news_manager.get_unread_count()
                if sum(remaining.values()) > 0:
                    response = self.news_manager.read_headlines(limit=5)
                    skill_handled = True
                    self._speak_response(response)
                    self.listener.open_conversation_window(
                        self.listener._extended_duration)

        # Priority 6: LLM fallback (streaming)
        if not skill_handled:
            print("🤖 Thinking...")
            history = self.conversation.format_history_for_llm(include_system_prompt=False)
            response = self._stream_llm_response(command, history)
            if not response:
                response = "I'm sorry, I'm having trouble processing that right now."

        # Record and speak
        self.conversation.add_message("assistant", response)
        print(f"💬 Jarvis: {response}")
        if not self.tts._spoke:
            self._speak_response(response)

        # --- Check for skill-requested follow-up window ---
        if self.conversation.request_follow_up:
            duration = self.conversation.request_follow_up
            self.conversation.request_follow_up = None
            self.listener.open_conversation_window(duration)
        else:
            self._manage_conversation_window(response, in_conversation)

        # Stats and resume
        stats = self.conversation.get_conversation_stats()
        print(f"\n📊 Session: {stats['session_user_messages']} user, {stats['session_assistant_messages']} assistant messages\n")
        self.listener.resume_listening()

    def _speak_response(self, response: str):
        """Speak a response with interruption support.

        Note: Does NOT reset self.listener.speaking — the caller must call
        resume_listening() after all post-speech logic is done. This prevents
        the mic from picking up tail-end reverb/echo between speech ending
        and resume_listening() clearing the audio buffers.
        """
        self.listener.speaking = True
        self.tts_interrupted = False
        self.tts.speak(response)

    # Regex to strip redundant LLM opening phrases when ack already played
    _ACK_OPENER_RE = re.compile(
        r'^(Certainly|Of course|Very well|Right away|One moment|Absolutely|Sure thing)'
        r',?\s*(?:sir|ma\'am|miss)\.?\s*',
        re.IGNORECASE,
    )

    @staticmethod
    def _classify_ack_style(command: str) -> str:
        """Classify query into an ack style for contextual acknowledgments."""
        cl = command.lower().strip()
        if any(w in cl for w in ("search", "look up", "find out", "latest", "current", "news about")):
            return "research"
        if any(cl.startswith(w) for w in (
            "what ", "who ", "when ", "where ", "how many ", "how much ", "is ", "are ", "was ", "does ",
        )):
            return "checking"
        if any(cl.startswith(w) for w in (
            "explain ", "tell me about ", "describe ", "compare ", "why ", "how do ", "how does ",
        )):
            return "working"
        return "neutral"

    def _play_ack_if_still_thinking(self, style_hint: str = None):
        """Called by timer — plays a quick ack phrase if LLM hasn't responded yet."""
        if not self._llm_responded:
            self.tts.speak_ack(style_hint=style_hint)

    def _strip_ack_opener(self, text: str) -> str:
        """Strip leading ack phrase from LLM text if ack was already spoken."""
        stripped = self._ACK_OPENER_RE.sub('', text)
        if stripped != text:
            stripped = stripped.lstrip()
            if stripped:
                stripped = stripped[0].upper() + stripped[1:]
            self.logger.info(f"Stripped ack opener: '{text[:40]}' → '{stripped[:40]}'")
        return stripped

    def _stream_llm_response(self, command: str, history: str) -> str:
        """Stream LLM response with first-chunk quality gating.

        Streams tokens from Qwen, accumulates into sentence chunks via
        SpeechChunker, and speaks each chunk as it becomes ready.

        First chunk is quality-checked — if bad, falls back to the
        non-streaming chat() path with its full retry logic.

        Returns:
            Full accumulated response text.
        """
        chunker = SpeechChunker()
        full_response = ""
        chunks_spoken = 0
        first_chunk_checked = False

        # Fire ack timer in case streaming itself is slow to start
        ack_style = self._classify_ack_style(command)
        ack_timer = threading.Timer(
            0.3, self._play_ack_if_still_thinking, args=(ack_style,)
        )
        self._llm_responded = False
        ack_timer.daemon = True
        ack_timer.start()

        try:
            for token in self.llm.stream(
                user_message=command,
                conversation_history=history,
            ):
                # Cancel ack timer once tokens start flowing
                if not self._llm_responded:
                    self._llm_responded = True
                    ack_timer.cancel()

                full_response += token
                chunk = chunker.feed(token)

                if chunk:
                    # First chunk: quality gate
                    if not first_chunk_checked:
                        first_chunk_checked = True
                        quality_issue = self.llm._check_response_quality(chunk, command)
                        if quality_issue:
                            self.logger.warning(
                                f"Streaming quality gate failed ({quality_issue}): "
                                f"'{chunk[:60]}' — falling back to sync chat()"
                            )
                            # Abort streaming, use non-streaming path with retry
                            return self.llm.chat(
                                user_message=command,
                                conversation_history=history,
                            )

                    # Strip redundant opener if ack already played
                    if chunks_spoken == 0 and self.tts.ack_played:
                        chunk = self._strip_ack_opener(chunk)
                        self.tts.clear_ack_played()
                        if not chunk:
                            continue  # Opener was entire chunk; wait for next

                    self._speak_response(chunk)
                    chunks_spoken += 1

            # Flush remaining buffer
            remaining = chunker.flush()
            if remaining:
                if not first_chunk_checked:
                    # Entire response was shorter than one chunk
                    quality_issue = self.llm._check_response_quality(remaining, command)
                    if quality_issue:
                        self.logger.warning(
                            f"Streaming quality gate failed ({quality_issue}): "
                            f"'{remaining[:60]}' — falling back to sync chat()"
                        )
                        return self.llm.chat(
                            user_message=command,
                            conversation_history=history,
                        )
                self._speak_response(remaining)
                chunks_spoken += 1

        except Exception as e:
            self.logger.error(f"Streaming LLM error: {e}")
            # Cancel ack timer if still pending
            self._llm_responded = True
            ack_timer.cancel()
            # Fall back to sync path
            if not full_response:
                return self.llm.chat(
                    user_message=command,
                    conversation_history=history,
                )

        # Cancel ack timer if stream was empty
        if not self._llm_responded:
            self._llm_responded = True
            ack_timer.cancel()

        if chunks_spoken > 0:
            self.logger.info(f"Streamed LLM response in {chunks_spoken} chunks")
            # Mark that TTS already spoke (so caller doesn't re-speak)
            self.tts._spoke = True

        return full_response

    def _on_conversation_timeout(self):
        """Cleanup when conversation window expires due to silence (legacy mode)."""
        self.logger.info("Timeout cleanup: resetting memory surfacing + context window")
        if self.memory_manager:
            self.memory_manager.reset_surfacing_window()
        if self.context_window:
            self.context_window.reset()

    def _manage_conversation_window(self, response: str, was_in_conversation: bool):
        """Decide whether to open/extend the conversation window after a response.

        Rules:
        - If already in conversation: always extend (keep the conversation going)
        - If response invites follow-up (question, "would you like"): open with extended duration
        - Otherwise: open with short default duration (brief window for "thanks" etc.)
        """
        if was_in_conversation:
            # Mid-conversation: always keep it open
            if self.conversation.should_open_follow_up_window(response):
                duration = self.listener._extended_duration
            else:
                duration = self.listener._default_duration
            self.listener.open_conversation_window(duration)
        elif self.conversation.should_open_follow_up_window(response):
            # New interaction with follow-up invitation
            self.listener.open_conversation_window(self.listener._extended_duration)
        else:
            # Terminal response — short window for "thanks" etc.
            self.listener.open_conversation_window(self.listener._default_duration)
    
    def _run_startup_health_check(self):
        """Run health check at startup; speak advisory if issues found."""
        try:
            from core.health_check import register_coordinator, get_full_health, format_visual_report
            from core.honorific import get_honorific

            register_coordinator(self.coordinator)
            health = get_full_health(self.config)

            # Count issues across all layers
            issues = []
            for layer_results in health.values():
                for check in layer_results:
                    if check['status'] in ('yellow', 'red'):
                        issues.append(check)

            if issues:
                h = get_honorific()
                if len(issues) == 1:
                    msg = f"{h.capitalize()}, there was an issue found during initialization. I'm putting it on screen for you."
                else:
                    msg = f"{h.capitalize()}, there were {len(issues)} issues found during initialization. I'm putting them on screen for you."
                self.tts.speak(msg)

                # Display visual report
                report = format_visual_report(health)
                try:
                    _skill_dir = Path("/mnt/storage/jarvis/skills/system/developer_tools")
                    import importlib.util
                    _spec = importlib.util.spec_from_file_location('_display', _skill_dir / '_display.py')
                    _display_mod = importlib.util.module_from_spec(_spec)
                    _spec.loader.exec_module(_display_mod)
                    display = _display_mod.DisplayRouter(self.config)
                    display.show(report, content_type='health_check', title='Startup Health Report')
                except Exception as e:
                    self.logger.warning(f"Could not open visual report: {e}")
            else:
                self.logger.info("Startup health check: all systems nominal")

        except Exception as e:
            self.logger.error(f"Startup health check failed: {e}", exc_info=True)

    def _extract_command(self, full_text: str) -> str:
        """
        Extract command from full transcription
        
        Args:
            full_text: Full transcribed text
            
        Returns:
            Command text (everything except wake word)
        """
        text_lower = full_text.lower()
        
        # Find wake word position
        wake_pos = text_lower.find(self.wake_word)
        
        if wake_pos == -1:
            # Wake word not found (shouldn't happen)
            return full_text.strip()
        
        # Get everything before and after wake word
        before = full_text[:wake_pos].strip()
        after = full_text[wake_pos + len(self.wake_word):].strip()
        
        # Remove leading/trailing punctuation
        import string
        before = before.strip(string.punctuation + ' ')
        after = after.strip(string.punctuation + ' ')
        
        # Combine (prefer after, but include before if meaningful)
        if after:
            # Command comes after wake word: "Jarvis, what time is it?"
            return after
        elif before:
            # Command comes before wake word: "What time is it, Jarvis?"
            return before
        else:
            # Only wake word - use special minimal greeting intent
            return "jarvis_only"
    
    def run(self):
        """Run Jarvis"""
        print("\n" + "="*60)
        if self.event_mode:
            print("🟢 JARVIS - EVENT PIPELINE MODE")
        else:
            print("🟢 JARVIS - CONTINUOUS LISTENING MODE")
        print("="*60)
        print(f"\nWake word: '{self.wake_word}'")
        print("\nSay the wake word anywhere in your sentence:")
        print("  - 'Good morning Jarvis'")
        print("  - 'Jarvis, what time is it?'")
        print("  - 'Tell me the weather, Jarvis'")
        print("\nPress Ctrl+C to stop.\n")

        self.logger.info("🟢 Jarvis starting in continuous mode...")

        # Start continuous listener with retry for slow USB enumeration
        mic_ok = self.listener.start_with_retry()

        if not mic_ok:
            h = get_honorific()
            self.tts.speak(
                f"Microphone not detected, {h}. "
                "I'll continue running without voice input. "
                "I'll let you know when the microphone becomes available."
            )
            self.logger.warning("DEGRADED MODE: No microphone — voice input disabled")
            print("⚠️  DEGRADED MODE: No microphone detected")
            print("    JARVIS is running (TTS, skills, reminders active)")
            print("    Voice input will resume when mic is reconnected")

        # Set up mic state change announcements and start device monitor
        def _on_mic_state_change(available: bool):
            h = get_honorific()
            if available:
                self.tts.speak(f"Microphone reconnected, {h}. Voice input is active.")
            else:
                self.tts.speak(f"Microphone disconnected, {h}. Voice input is suspended.")

        self.listener._on_mic_state_change = _on_mic_state_change
        self.listener.start_device_monitor()

        if self.event_mode:
            # Start pipeline workers
            self.stt_worker.start()
            self.tts_worker = TTSWorker(self.tts, self.event_queue, self.tts_queue, self.config)
            self.tts_worker.start()
            self.logger.info("Pipeline workers started (STT + TTS)")

            # Startup health check
            if self.config.get("health_check.run_on_startup", True):
                self._run_startup_health_check()

            try:
                # Coordinator event loop runs on main thread
                self.coordinator.run()
            except KeyboardInterrupt:
                print("\n\nShutdown signal received...")
                self.logger.info("Shutdown requested")
            finally:
                self.coordinator.shutdown()
                if self.context_window:
                    self.context_window.flush()
                self.audio_queue.put(None)   # STT worker shutdown sentinel
                self.tts_queue.put(None)     # TTS worker shutdown sentinel
                if self.news_manager:
                    self.news_manager.stop()
                if self.calendar_manager:
                    self.calendar_manager.stop()
                if self.reminder_manager:
                    self.reminder_manager.stop()
                self.listener.stop_device_monitor()
                self.listener.stop()
                self.logger.info("Jarvis stopped")
        else:
            # Legacy mode — sleep loop
            try:
                # Stay alive even in degraded mode (no mic) — device
                # monitor will reconnect when mic appears
                while True:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print("\n\nShutdown signal received...")
                self.logger.info("Shutdown requested")
            finally:
                if self.news_manager:
                    self.news_manager.stop()
                if self.calendar_manager:
                    self.calendar_manager.stop()
                if self.reminder_manager:
                    self.reminder_manager.stop()
                self.listener.stop_device_monitor()
                self.listener.stop()
                self.logger.info("Jarvis stopped")


def main():
    """Main entry point"""
    # Load configuration
    config_path = Path(__file__).parent / "config.yaml"
    config = load_config(config_path)
    
    # Create and run Jarvis
    jarvis = JarvisContinuous(config)
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        print("\n\nShutdown signal received...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run
    jarvis.run()


if __name__ == "__main__":
    main()
