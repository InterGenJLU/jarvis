"""
Continuous Listener

Always-listening mode with VAD and wake word detection in transcriptions.
Buffers audio and transcribes when speech is detected.
"""

import sounddevice as sd
import numpy as np
import threading
import time
from typing import Optional, Callable

from core.logger import get_logger
from core.vad import VoiceActivityDetector
from core.stt import SpeechToText

# Try to import RNNoise for noise suppression
try:
    from core.rnnoise_wrapper import RNNoise
    RNNOISE_AVAILABLE = True
except (ImportError, OSError) as e:
    RNNOISE_AVAILABLE = False
    RNNoise = None


class ContinuousListener:
    """Continuous audio listener with VAD"""
    
    def __init__(self, config, stt: SpeechToText, on_command: Callable,
                 audio_queue=None):
        """
        Initialize continuous listener

        Args:
            config: Configuration object
            stt: Speech-to-text engine
            on_command: Callback when command detected (receives full text)
            audio_queue: Optional queue.Queue for event pipeline mode.
                         When set, audio is put on the queue instead of
                         spawning per-utterance transcription threads.
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        self.stt = stt
        self.on_command = on_command
        self.audio_queue = audio_queue
        self.on_interrupt = None  # Callback for interruption detection
        
        # Audio configuration
        self.sample_rate = config.get("audio.sample_rate", 16000)
        self.device = config.get("audio.mic_device")
        
        # Device sample rate (will be determined when stream starts)
        self.device_sample_rate = None
        
        # VAD configuration
        self.frame_duration_ms = 30
        self.frame_size = int(self.sample_rate * self.frame_duration_ms / 1000)
        
        # Wake word configuration
        self.wake_word = config.get("wake_word.keyword", "jarvis").lower()
        
        # Initialize VAD
        self.vad = VoiceActivityDetector(config, on_speech_detected=self._on_speech_start)
        
        # State
        self.running = False
        self.listening_thread = None
        self.stream = None
        self.speaking = False  # Flag to pause listening while speaking
        self._speaking_event = threading.Event()  # Thread-safe pause signal
        
        # Device monitor (hot-plug recovery)
        self._monitor_thread = None
        self._monitor_interval = config.get("audio.device_monitor_interval", 5.0)
        self._mic_lost_announced = False
        self._on_mic_state_change = None  # Callback: (available: bool) -> None

        # Speech collection
        self.collecting_speech = False
        self.speech_buffer = []
        
        # Conversation window - allow responses without wake word during conversation
        self.conversation_window_active = False
        self._conversation_lock = threading.Lock()
        self._conversation_timer = None

        # Conversation window durations (from config)
        self._default_duration = config.get("conversation.follow_up_window.default_duration", 5.0)
        self._extended_duration = config.get("conversation.follow_up_window.extended_duration", 8.0)

        # Optional callback when conversation window closes due to silence timeout.
        # Set by pipeline/coordinator to clean up state (conv_state, context_window, etc.)
        self.on_window_close = None

        # Known valid short replies (don't filter these as noise)
        self._valid_short_replies = {
            "yes", "no", "yeah", "yep", "nah", "nope",
            "thanks", "thank you", "okay", "ok", "please",
            "stop", "cancel", "nevermind", "never mind",
            "sure", "right", "correct", "wrong", "good", "great",
            "hello", "hey", "hi", "bye", "goodbye",
        }
        
        # Initialize RNNoise for audio denoising
        self.use_rnnoise = config.get("audio.use_rnnoise", True) and RNNOISE_AVAILABLE
        if self.use_rnnoise:
            try:
                # RNNoise works on 48kHz audio, processes 480 samples (10ms) at a time
                self.denoiser = RNNoise()
                self.logger.info("RNNoise audio denoising enabled")
            except Exception as e:
                self.logger.warning(f"Failed to initialize RNNoise: {e}")
                self.use_rnnoise = False
        elif not RNNOISE_AVAILABLE:
            self.logger.info("RNNoise not available - install with: pip install rnnoise-python")
        
        self.logger.info("Continuous listener initialized")
    
    def _on_speech_start(self):
        """Callback when VAD detects speech start"""
        # Don't start collecting if we're paused for TTS playback
        if self._speaking_event.is_set() or self.speaking:
            return

        # Rate-limit VAD triggers to avoid wasting CPU on ambient noise floods
        now = time.monotonic()
        if not hasattr(self, '_vad_timestamps'):
            self._vad_timestamps = []
        self._vad_timestamps.append(now)
        # Keep only last 3 seconds of timestamps
        self._vad_timestamps = [t for t in self._vad_timestamps if now - t <= 3.0]
        if len(self._vad_timestamps) > 8:
            self.logger.debug(f"🔇 Noise burst detected ({len(self._vad_timestamps)} VAD triggers in 3s) — skipping")
            return

        self.logger.debug("🗣️  Speech detected, starting collection")
        print("🗣️  Speech detected...")
        self.collecting_speech = True
        self.speech_buffer = []
        # Snapshot the pre-speech ring buffer NOW, before more speech frames
        # are added to it.  If we wait until _process_speech(), the ring
        # buffer will contain the speech itself (it never stops recording),
        # causing the utterance to appear twice in the audio sent to Whisper.
        self._pre_speech_audio = self.vad.get_buffered_audio()
    
    def _audio_callback(self, indata, frames, time_info, status):
        """Audio stream callback"""
        if status:
            self.logger.warning(f"Audio callback status: {status}")
        
        # Handle stereo/mono input
        if indata.ndim > 1 and indata.shape[1] > 1:
            # Stereo: mix to mono (average both channels)
            audio = np.mean(indata, axis=1)
        else:
            # Already mono
            audio = indata[:, 0] if indata.ndim > 1 else indata
        
        # Apply RNNoise denoising if enabled
        if self.use_rnnoise and hasattr(self, 'denoiser'):
            try:
                # RNNoise expects float32 samples in range [-1, 1]
                # Process in 480-sample chunks (10ms at 48kHz)
                if self.device_sample_rate == 48000:
                    # Convert to float32 if needed
                    if audio.dtype != np.float32:
                        audio_f32 = audio.astype(np.float32)
                    else:
                        audio_f32 = audio
                    
                    # Denoise
                    audio_denoised = self.denoiser.process_frame(audio_f32)
                    audio = audio_denoised
                # For other sample rates, skip denoising (would need resampling)
            except Exception as e:
                self.logger.debug(f"RNNoise processing failed: {e}")
        
        # Resample if needed (device rate -> VAD rate)
        if self.device_sample_rate != self.sample_rate:
            # Simple linear resampling
            num_samples = int(len(audio) * self.sample_rate / self.device_sample_rate)
            indices = np.linspace(0, len(audio) - 1, num_samples)
            audio_resampled = np.interp(indices, np.arange(len(audio)), audio)
        else:
            audio_resampled = audio
        
        # Convert to int16 for VAD
        audio_int16 = (audio_resampled * 32767).astype(np.int16)
        
        # Ensure correct frame size
        if len(audio_int16) >= self.frame_size:
            audio_int16 = audio_int16[:self.frame_size]
        else:
            # Pad if too short
            audio_int16 = np.pad(audio_int16, (0, self.frame_size - len(audio_int16)))
        
        # Process through VAD
        in_speech, state_changed = self.vad.process_frame(audio_int16)
        
        # Skip further processing if we're speaking (don't transcribe our own voice)
        # Use Event for thread-safe check (set = speaking/paused)
        if self._speaking_event.is_set() or self.speaking:
            return
        
        # If collecting speech, add raw device-rate audio to buffer
        # (batch resampling in _process_speech is cheaper than per-frame np.interp)
        if self.collecting_speech:
            self.speech_buffer.append(audio.copy())

            # If speech ended, process the collected audio
            if not in_speech and len(self.speech_buffer) > 10:  # At least 10 frames (~300ms)
                self._process_speech()
    
    def _process_speech(self):
        """Process collected speech"""
        self.logger.info(f"💬 Processing speech ({len(self.speech_buffer)} frames)")
        print(f"💬 Processing speech...")

        # Use the pre-speech snapshot taken in _on_speech_start().
        # Calling get_buffered_audio() HERE would return the ring buffer
        # which now contains the speech itself (the ring buffer never stops
        # recording), causing the utterance to be doubled.
        pre_buffer = getattr(self, '_pre_speech_audio', np.array([], dtype=np.float32))

        # Combine speech frames (at device sample rate)
        speech_audio_raw = np.concatenate(self.speech_buffer)

        # Reset collection
        self.collecting_speech = False
        self.speech_buffer = []

        # Batch resample device-rate audio → VAD rate (single np.interp on full buffer)
        if self.device_sample_rate and self.device_sample_rate != self.sample_rate:
            num_samples = int(len(speech_audio_raw) * self.sample_rate / self.device_sample_rate)
            indices = np.linspace(0, len(speech_audio_raw) - 1, num_samples)
            speech_audio = np.interp(indices, np.arange(len(speech_audio_raw)), speech_audio_raw).astype(np.float32)
        else:
            speech_audio = speech_audio_raw

        full_audio = np.concatenate([pre_buffer, speech_audio])

        self.logger.info(f"Audio length: {len(full_audio)} samples ({len(full_audio)/self.sample_rate:.2f}s)")

        # Event pipeline mode: put audio on queue for STT worker
        if self.audio_queue is not None:
            self.audio_queue.put(full_audio)
            return

        # Legacy mode: transcribe in background thread
        threading.Thread(
            target=self._transcribe_and_check,
            args=(full_audio,),
            daemon=True
        ).start()
    
    def _transcribe_and_check(self, audio: np.ndarray):
        """
        Transcribe audio and check for wake word (or accept if conversation window open)
        
        Args:
            audio: Audio data to transcribe
        """
        try:
            self.logger.info("🎤 Transcribing...")
            print("🎤 Transcribing...")
            
            # Transcribe
            text = self.stt.transcribe(audio, self.sample_rate)
            
            if not text or not text.strip():
                self.logger.info("⚠️  Blank transcription")
                print("⚠️  (no speech detected)")
                return
            
            text = text.strip().lower()
            
            # Filter out Whisper noise annotations like (music), (laughter), [blank_audio], etc.
            if text.startswith('(') and text.endswith(')'):
                self.logger.info(f"⚠️  Ignoring noise annotation: {text}")
                print(f"⚠️  Ignoring background noise")
                return
            
            if text.startswith('[') and text.endswith(']'):
                self.logger.info(f"⚠️  Ignoring Whisper annotation: {text}")
                print(f"⚠️  Ignoring background noise")
                return

            # Filter obvious garbage before any further processing
            # (repetitive chars from TTS bleed, single-char noise, etc.)
            unique_chars = set(text.replace(' ', '').replace('.', ''))
            if len(unique_chars) <= 3 and len(text) > 5:
                self.logger.info(f"⚠️  Ignoring garbage transcription: {text[:30]}...")
                return

            self.logger.info(f"📝 Transcribed: {text}")
            print(f"📝 Heard: \"{text}\"")
            
            # Check if conversation window is active
            if self.conversation_window_active:
                # Filter out likely noise during conversation window
                if self._is_conversation_noise(text):
                    self.logger.info(f"🔇 Filtered noise during conversation: '{text}'")
                    return

                # Apply corrections for common mishearings
                corrected_text = self._apply_command_corrections(text)
                if corrected_text != text:
                    self.logger.info(f"🔧 Corrected in conversation: '{text}' → '{corrected_text}'")
                    text = corrected_text

                # Pause the timeout while we process this utterance
                self._cancel_conversation_timer()

                self.logger.info(f"✅ Response during conversation window: {text}")
                self.on_command(text)
                return
            
            # Otherwise, check for wake word using fuzzy matching
            from difflib import SequenceMatcher

            # Split text into words and check each
            words = text.split()
            wake_word_found = False

            for word in words:
                # Remove punctuation
                word_clean = word.strip('.,!?;:')

                # Check similarity to "jarvis"
                similarity = SequenceMatcher(None, self.wake_word, word_clean).ratio()

                if similarity >= 0.80:  # Raised from 0.7 to eliminate "paris" (0.73) etc.
                    self.logger.info(f"✅ Wake word detected (similarity: {similarity:.2f}): {word_clean} in {text}")
                    wake_word_found = True
                    matched_word = word_clean
                    break

            if wake_word_found:
                # Check if this is ambient conversation rather than a command
                if self._is_ambient_wake_word(text, matched_word):
                    print("🔇 Ambient mention (ignored)")
                    return

                # Correct the wake word before passing to command handler
                corrected_text = text.replace(matched_word, self.wake_word)
                self.logger.info(f"🔧 Corrected: '{text}' → '{corrected_text}'")
                self.on_command(corrected_text)
            else:
                self.logger.info(f"❌ No wake word in: {text}")
                print(f"❌ No wake word (ignored)")
        
        except Exception as e:
            self.logger.error(f"Transcription error: {e}")
            import traceback
            traceback.print_exc()
    
    def _find_mic_device(self) -> Optional[int]:
        """Find microphone device index by name, with default fallback.

        Mirrors the pattern in wake_word.py:_find_mic_device().
        Returns device index or None if no input device exists at all.
        """
        devices = sd.query_devices()

        # Try configured device name first
        if self.device:
            for i, dev in enumerate(devices):
                if (self.device in dev['name'] and
                        dev.get('max_input_channels', 0) > 0):
                    return i
            self.logger.warning(f"Configured mic '{self.device}' not found, trying default")

        # Fall back to system default input
        try:
            default_idx = sd.default.device[0]
            if default_idx is not None and default_idx >= 0:
                dev = sd.query_devices(default_idx)
                if dev.get('max_input_channels', 0) > 0:
                    self.logger.info(f"Using default input device: {dev['name']}")
                    return default_idx
        except Exception:
            pass

        return None

    def start(self) -> bool:
        """Start continuous listening.

        Returns:
            True if audio stream started successfully, False otherwise.
        """
        if self.running and self.stream is not None:
            self.logger.warning("Already running")
            return True

        self.logger.info("Starting continuous listener...")

        try:
            device_index = self._find_mic_device()

            if device_index is None:
                self.logger.error("No input audio device available")
                self.running = False
                return False

            # Get actual device sample rate
            device_info = sd.query_devices(device_index)
            device_sr = int(device_info['default_samplerate'])
            self.device_sample_rate = device_sr

            self.logger.info(f"Using device: {device_info['name']}")
            self.logger.info(f"Device sample rate: {device_sr} Hz, VAD rate: {self.sample_rate} Hz")

            # Get channel count from config
            channels = self.config.get("audio.channels", 2)

            # Calculate blocksize in device sample rate
            # self.frame_size is for VAD rate (16kHz), but stream runs at device_sr (e.g. 48kHz)
            device_blocksize = int(device_sr * self.frame_duration_ms / 1000)

            # Open audio stream
            self.stream = sd.InputStream(
                device=device_index,
                channels=channels,
                samplerate=device_sr,
                blocksize=device_blocksize,
                callback=self._audio_callback
            )

            self.stream.start()
            self.running = True
            self.logger.info("🎤 Continuous listening active...")
            return True

        except Exception as e:
            self.logger.error(f"Failed to start listener: {e}")
            self.running = False
            self.stream = None
            return False

    def start_with_retry(self, max_retries: int = None,
                         base_delay: float = None) -> bool:
        """Start listening with exponential backoff retry.

        USB devices can enumerate slowly after boot. This retries with
        exponential delays (default 2, 4, 8, 16, 32s = ~62s total).

        Returns:
            True if eventually started, False if all retries exhausted.
        """
        if max_retries is None:
            max_retries = int(self.config.get("audio.startup_retry_count", 5))
        if base_delay is None:
            base_delay = float(self.config.get("audio.startup_retry_base_delay", 2.0))

        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = base_delay * (2 ** (attempt - 1))
                self.logger.warning(
                    f"Mic retry {attempt}/{max_retries} in {delay:.0f}s..."
                )
                print(f"⏳ Mic not found, retrying in {delay:.0f}s "
                      f"(attempt {attempt}/{max_retries})...")
                time.sleep(delay)

            if self.start():
                if attempt > 0:
                    self.logger.info(f"Mic connected after {attempt} retries")
                return True

        self.logger.error(
            f"Mic unavailable after {max_retries} retries — "
            "starting in degraded mode (no voice input)"
        )
        return False

    @property
    def mic_available(self) -> bool:
        """Whether the microphone stream is active and receiving audio."""
        return self.running and self.stream is not None

    # --- Device monitor (hot-plug recovery) ---

    def start_device_monitor(self):
        """Start the background device monitor thread."""
        if self._monitor_thread is not None:
            return
        self._monitor_thread = threading.Thread(
            target=self._device_monitor_loop,
            daemon=True,
            name="mic-monitor",
        )
        self._monitor_thread.start()

    def stop_device_monitor(self):
        """Stop the device monitor thread."""
        thread = self._monitor_thread
        self._monitor_thread = None  # Signal the loop to exit
        if thread and thread.is_alive():
            thread.join(timeout=self._monitor_interval + 1)

    def _device_monitor_loop(self):
        """Background thread: detect mic disconnection/reconnection.

        When stream is alive, checks stream.active (cheap).
        When stream is dead, calls start() to try reconnection.
        """
        self.logger.info("Device monitor started")

        while self._monitor_thread is not None:
            try:
                time.sleep(self._monitor_interval)
            except Exception:
                break

            # Bail if we've been told to stop
            if self._monitor_thread is None:
                break

            # Case 1: Stream exists — check if it's still alive
            if self.stream is not None:
                try:
                    if not self.stream.active:
                        self.logger.warning("Audio stream died (device disconnected?)")
                        self._handle_stream_lost()
                except Exception as e:
                    self.logger.warning(f"Stream health check failed: {e}")
                    self._handle_stream_lost()

            # Case 2: No stream — try to reconnect
            else:
                if self.start():
                    self.logger.info("🎤 Microphone reconnected!")
                    print("🎤 Microphone reconnected!")
                    self._mic_lost_announced = False
                    if self._on_mic_state_change:
                        try:
                            self._on_mic_state_change(True)
                        except Exception as e:
                            self.logger.error(f"Mic state callback error: {e}")

        self.logger.info("Device monitor stopped")

    def _handle_stream_lost(self):
        """Clean up after detecting the audio stream has died."""
        try:
            if self.stream:
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        self.running = False
        self.collecting_speech = False
        self.speech_buffer = []

        if not self._mic_lost_announced:
            self._mic_lost_announced = True
            self.logger.warning("🔇 Microphone lost — voice input suspended")
            print("🔇 Microphone lost — voice input suspended")
            if self._on_mic_state_change:
                try:
                    self._on_mic_state_change(False)
                except Exception as e:
                    self.logger.error(f"Mic state callback error: {e}")

    def pause_listening(self):
        """Temporarily pause speech collection (for TTS playback)"""
        # Set Event FIRST — audio callback checks this immediately (thread-safe)
        self._speaking_event.set()
        self.speaking = True

        # Discard any in-progress speech collection — do NOT process/transcribe it,
        # because that would spawn a background thread that races with TTS playback
        self.collecting_speech = False
        self.speech_buffer = []

        self.logger.info("🔇 Listening paused (TTS playback)")
    
    def resume_listening(self):
        """Resume speech collection after TTS playback.

        Includes a brief cooldown and buffer clear to prevent the mic
        from immediately picking up TTS echo/reverb as speech.
        """
        # Clear any audio that was buffered during TTS playback
        self.vad.clear_buffer()
        self.collecting_speech = False
        self.speech_buffer = []

        # Reset VAD state so residual TTS energy doesn't count as speech
        self.vad.reset()

        self.speaking = False
        self._speaking_event.clear()  # Allow audio callback to resume processing
        self.logger.info("🔊 Listening resumed")
    
    def _apply_command_corrections(self, text: str) -> str:
        """Apply corrections for common command mishearings"""
        import re
        
        # "i was/analyzed [command]" -> "analyze [command]"
        if re.match(r'^i (was|analyzed)\s+', text, re.IGNORECASE):
            corrected = re.sub(r'^i (was|analyzed)\s+', 'analyze ', text, flags=re.IGNORECASE)
            return corrected
        
        # "i'm" at start -> "analyze" 
        if text.lower().startswith("i'm "):
            return "analyze " + text[4:]
        
        return text
    
    # Words that follow "jarvis" in ambient speech (talking ABOUT jarvis)
    _AMBIENT_FOLLOWERS = frozenset({
        'is', 'was', 'has', 'had', 'will', 'would', 'can', 'could',
        'does', 'did', 'should', 'might', 'may', 'of',
    })
    _WAKE_PREFIXES = frozenset({
        'hey', 'hi', 'yo', 'morning', 'good', 'okay', 'ok',
    })

    def _is_ambient_wake_word(self, text: str, matched_word: str) -> bool:
        """Determine if a wake word detection is ambient conversation, not a command.

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
            return False

        # Signal 1: Position — wake word should be in first 2 words OR trailing
        effective_pos = word_idx
        if word_idx <= 2:
            prefix_words = [w.strip('.,!?;:') for w in words[:word_idx]]
            if all(pw in self._WAKE_PREFIXES for pw in prefix_words):
                effective_pos = 0
        # Trailing wake word = command ("how are you, jarvis?")
        is_trailing = word_idx >= len(words) - 2
        if effective_pos >= 3 and not is_trailing:
            self.logger.info(f"🔇 Ambient rejected (position {word_idx}): {text[:80]}")
            return True

        # Signal 2: Post-wake-word copula/auxiliary without comma
        if word_idx < len(words):
            wake_token = words[word_idx]
            if wake_token.endswith("'s") or wake_token.endswith("\u2019s"):
                self.logger.info(f"🔇 Ambient rejected (possessive): {text[:80]}")
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

        # Signal 5: Long utterance with wake word not at position 0
        if len(words) > 15 and word_idx > 0:
            self.logger.info(
                f"🔇 Ambient rejected (long utterance {len(words)} words, "
                f"position {word_idx}): {text[:80]}"
            )
            return True

        return False

    def _is_conversation_noise(self, text: str) -> bool:
        """Check if transcribed text during conversation window is likely noise."""
        # Very short non-word sounds
        if len(text) < 2:
            return True

        # Repetitive characters (e.g. "wrwwwwww" from TTS feedback)
        unique_chars = set(text.replace(' ', ''))
        if len(unique_chars) <= 3 and len(text) > 5:
            return True

        # Single word that isn't a known valid short reply
        words = text.strip().split()
        if len(words) == 1 and words[0] not in self._valid_short_replies:
            # Allow single words 4+ chars (likely real words)
            if len(words[0]) < 4:
                return True

        return False

    def open_conversation_window(self, duration: float = None):
        """Open or extend conversation window with auto-close timer.

        Args:
            duration: Seconds before auto-close. None uses default.
        """
        if duration is None:
            duration = self._default_duration

        with self._conversation_lock:
            # Cancel existing timer
            self._cancel_conversation_timer()

            was_active = self.conversation_window_active
            self.conversation_window_active = True

            # Start new auto-close timer
            self._conversation_timer = threading.Timer(duration, self._conversation_timeout)
            self._conversation_timer.daemon = True
            self._conversation_timer.start()

        if not was_active:
            self.logger.info(f"🔓 Conversation window opened ({duration:.0f}s)")
            print(f"🔓 Conversation window open ({duration:.0f}s)")
        else:
            self.logger.debug(f"🔓 Conversation window extended ({duration:.0f}s)")
        self._play_conversation_tone()

    def close_conversation_window(self):
        """Close conversation window and cancel timer."""
        with self._conversation_lock:
            self._cancel_conversation_timer()
            if self.conversation_window_active:
                self.conversation_window_active = False
                self.logger.info("🔒 Conversation window closed")
                print("🔒 Conversation window closed")
                self._play_conversation_close_tone()

    def _cancel_conversation_timer(self):
        """Cancel the conversation timeout timer (must hold lock or be called from locked context)."""
        if self._conversation_timer is not None:
            self._conversation_timer.cancel()
            self._conversation_timer = None

    def _conversation_timeout(self):
        """Called by timer when conversation window expires due to silence."""
        timed_out = False
        with self._conversation_lock:
            if self.conversation_window_active:
                self.conversation_window_active = False
                self._conversation_timer = None
                timed_out = True
                self.logger.info("🔒 Conversation window timed out (silence)")
                print("🔒 Conversation ended (silence)")
                self._play_conversation_close_tone()
        # Invoke cleanup callback AFTER releasing the lock
        if timed_out and self.on_window_close:
            try:
                self.on_window_close()
            except Exception as e:
                self.logger.error(f"on_window_close callback error: {e}")

    def _play_conversation_tone(self):
        """Play the conversation window tone if configured."""
        if not self.config.get("conversation.follow_up_window.play_tone", True):
            return
        tone_path = self.config.get("conversation.follow_up_window.tone_path")
        if not tone_path:
            return
        import os
        tone_path = os.path.expanduser(tone_path)
        if not os.path.exists(tone_path):
            self.logger.warning(f"Conversation tone file not found: {tone_path}")
            return
        try:
            import subprocess
            output_device = self.config.get("audio.output_device", "default")
            subprocess.Popen(
                ["aplay", "-D", output_device, tone_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.logger.error(f"Failed to play conversation tone: {e}")

    def _play_conversation_close_tone(self):
        """Play the conversation window close tone if configured."""
        if not self.config.get("conversation.follow_up_window.play_tone", True):
            return
        tone_path = self.config.get("conversation.follow_up_window.close_tone_path")
        if not tone_path:
            return
        import os
        tone_path = os.path.expanduser(tone_path)
        if not os.path.exists(tone_path):
            self.logger.warning(f"Conversation close tone file not found: {tone_path}")
            return
        try:
            import subprocess
            output_device = self.config.get("audio.output_device", "default")
            subprocess.Popen(
                ["aplay", "-D", output_device, tone_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.logger.error(f"Failed to play conversation close tone: {e}")

    def stop(self):
        """Stop continuous listening"""
        self.logger.info("Stopping continuous listener...")
        self.running = False

        self.stop_device_monitor()

        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        self.logger.info("Continuous listener stopped")
    
    def is_running(self) -> bool:
        """Check if listener is running"""
        return self.running


def get_continuous_listener(config, stt: SpeechToText, on_command: Callable) -> ContinuousListener:
    """Get continuous listener instance"""
    return ContinuousListener(config, stt, on_command)
