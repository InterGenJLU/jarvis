"""
Continuous Listener

Always-listening mode with VAD and wake word detection in transcriptions.
Buffers audio and transcribes when speech is detected.
"""

import os
import re
import sounddevice as sd
import numpy as np
from scipy.signal import resample_poly
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


def is_garbage_transcription(text: str) -> bool:
    """Return True if *text* looks like repetitive-char garbage (TTS bleed, single-char noise)."""
    unique_chars = set(text.replace(' ', '').replace('.', ''))
    return len(unique_chars) <= 3 and len(text) > 5


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

        # High-frequency diagnostic logging (config toggle)
        self._diag_audio = config.get("diagnostics.audio_pipeline", False)

        # Audio callback heartbeat — always on (not gated by _diag_audio).
        # Tracks whether the sounddevice callback thread is alive.
        self._callback_count = 0
        self._callback_last_log = 0  # monotonic timestamp
        self._callback_heartbeat_interval = 60  # seconds between heartbeat logs
        
        # Device sample rate (will be determined when stream starts)
        self.device_sample_rate = None
        
        # Initialize VAD (determines frame size)
        self.vad = VoiceActivityDetector(config, on_speech_detected=self._on_speech_start)
        self.frame_duration_ms = self.vad.frame_duration_ms
        self.frame_size = self.vad.frame_size

        # Wake word configuration
        self.wake_word = config.get("wake_word.keyword", "jarvis").lower()
        
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
        self._using_fallback_device = False  # True when preferred mic wasn't found at start

        # Speech collection
        self.collecting_speech = False
        self.speech_buffer = []
        self._buffer_lock = threading.Lock()  # protects speech_buffer access across threads
        self._vad_timestamps = []  # rate-limit VAD triggers (noise burst detection)
        self._last_vad_activity_ts = 0.0  # monotonic timestamp of last VAD speech detection
        
        # Conversation window - allow responses without wake word during conversation
        self.conversation_window_active = False
        self._conversation_lock = threading.Lock()
        self._conversation_timer = None
        self._conversation_epoch = 0  # guards against stale timer callbacks

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
            # Short question/command words (3 chars) that the noise filter
            # would otherwise reject during conversation window
            "why", "how", "who", "what", "when", "where",
            "run", "set", "get", "add", "all", "any", "new",
            "off", "end", "try", "use", "yet", "now",
            "six", "ten", "two", "one",
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
        self._last_vad_activity_ts = now
        self._vad_timestamps.append(now)
        # Keep only last 3 seconds of timestamps
        self._vad_timestamps = [t for t in self._vad_timestamps if now - t <= 3.0]
        if len(self._vad_timestamps) > 8:
            self.logger.debug(f"🔇 Noise burst detected ({len(self._vad_timestamps)} VAD triggers in 3s) — skipping")
            return

        # Pause conversation timeout while speech is being collected —
        # prevents the timer from firing during speaker ID + transcription
        if self.conversation_window_active:
            with self._conversation_lock:
                self._cancel_conversation_timer()

        self.logger.debug("🗣️  Speech detected (VAD triggers=%d), starting collection",
                          len(self._vad_timestamps))
        print("🗣️  Speech detected...")
        self.collecting_speech = True
        with self._buffer_lock:
            self.speech_buffer = []
        # Snapshot the pre-speech ring buffer NOW, before more speech frames
        # are added to it.  If we wait until _process_speech(), the ring
        # buffer will contain the speech itself (it never stops recording),
        # causing the utterance to appear twice in the audio sent to Whisper.
        self._pre_speech_audio = self.vad.get_buffered_audio()
    
    def _audio_callback(self, indata, frames, time_info, status):
        """Audio stream callback"""
        # Heartbeat: always-on proof-of-life for the callback thread.
        # Logs every 60s regardless of _diag_audio setting.
        self._callback_count += 1
        now = time.monotonic()
        if now - self._callback_last_log >= self._callback_heartbeat_interval:
            self._callback_last_log = now
            speaking_event = self._speaking_event.is_set()
            self.logger.info(
                "🫀 Audio callback heartbeat: %d calls, speaking_event=%s, "
                "speaking_flag=%s, collecting=%s, conv_window=%s, paused=%s",
                self._callback_count, speaking_event, self.speaking,
                self.collecting_speech, self.conversation_window_active,
                getattr(self, '_paused', False),
            )

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
        
        # Skip ALL processing if we're speaking (don't feed TTS audio to VAD)
        # Use Event for thread-safe check (set = speaking/paused)
        speaking_event_set = self._speaking_event.is_set()
        speaking_flag = self.speaking
        if speaking_event_set or speaking_flag:
            # Diagnostic: log once per second when blocked
            if self._diag_audio:
                if not hasattr(self, '_diag_blocked_count'):
                    self._diag_blocked_count = 0
                self._diag_blocked_count += 1
                if self._diag_blocked_count % 31 == 1:  # ~1/sec at 32ms frames
                    self.logger.info(
                        f"🔇 DIAG audio blocked: speaking_event={speaking_event_set} "
                        f"speaking_flag={speaking_flag} "
                        f"collecting={self.collecting_speech} "
                        f"conv_window={self.conversation_window_active}"
                    )
            return

        if hasattr(self, '_diag_blocked_count') and self._diag_blocked_count > 0:
            if self._diag_audio:
                self.logger.info(f"🔊 DIAG audio unblocked after {self._diag_blocked_count} blocked frames")
            self._diag_blocked_count = 0

        # Process through VAD (only when not speaking)
        in_speech, state_changed = self.vad.process_frame(audio_int16)

        # Diagnostic: log VAD state every ~1 second
        if self._diag_audio:
            if not hasattr(self, '_diag_vad_count'):
                self._diag_vad_count = 0
            self._diag_vad_count += 1
            if self._diag_vad_count % 31 == 0:  # ~1/sec at 32ms frames
                rms = float(np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2)))
                self.logger.info(
                    f"🎙️ DIAG VAD: in_speech={in_speech} "
                    f"speech_frames={self.vad.speech_frames} "
                    f"silence_frames={self.vad.silence_frames} "
                    f"rms={rms:.0f} collecting={self.collecting_speech} "
                    f"conv_window={self.conversation_window_active}"
                )
        
        # If collecting speech, add raw device-rate audio to buffer.
        # RATE CONTRACT: speech_buffer stores audio at device_sample_rate.
        # _process_speech() batch-resamples to self.sample_rate (VAD rate)
        # before concatenating with pre_buffer (which is already at VAD rate
        # from the VAD ring buffer).  Do NOT change one without the other.
        if self.collecting_speech:
            with self._buffer_lock:
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
        with self._buffer_lock:
            speech_audio_raw = np.concatenate(self.speech_buffer)
            # Reset collection
            self.collecting_speech = False
            self.speech_buffer = []

        # Batch resample device-rate audio → VAD rate using bandlimited resampling.
        # CRITICAL: must use resample_poly (same as enrollment path in speaker_id.py)
        # to produce spectrally identical audio. np.interp (linear interpolation)
        # causes spectral differences that destroy speaker ID scores.
        # RATE CONTRACT: pre_buffer is at self.sample_rate (from VAD ring buffer).
        # speech_audio must be resampled to match before concatenation.
        if self.device_sample_rate and self.device_sample_rate != self.sample_rate:
            from math import gcd
            _up = self.sample_rate
            _down = self.device_sample_rate
            _g = gcd(_up, _down)
            speech_audio = resample_poly(speech_audio_raw, _up // _g, _down // _g).astype(np.float32)
        else:
            speech_audio = speech_audio_raw

        # Safety check: both arrays must be at the same sample rate (self.sample_rate)
        # before concatenation.  A mismatch here means one of the two collection
        # paths changed without updating the other — catch it early.
        if len(pre_buffer) > 0 and len(speech_audio) > 0:
            # Heuristic: if device_rate != vad_rate and the ratio of samples
            # doesn't roughly match expected durations, something is wrong.
            pre_duration = len(pre_buffer) / self.sample_rate
            speech_duration = len(speech_audio) / self.sample_rate
            total_frames = len(self.vad.audio_buffer) if hasattr(self.vad, 'audio_buffer') else 0
            if pre_duration > self.vad.buffer_duration + 1.0:
                self.logger.warning(
                    f"⚠️ Pre-buffer duration ({pre_duration:.2f}s) exceeds ring buffer "
                    f"capacity ({self.vad.buffer_duration}s) — possible sample rate mismatch"
                )

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

            # Filter obvious garbage (repetitive chars from TTS bleed, etc.)
            if is_garbage_transcription(text):
                self.logger.info(f"⚠️  Ignoring garbage transcription: {text[:30]}...")
                return

            # Apply brand-name corrections before any routing decisions
            corrected = self._apply_transcription_corrections(text)
            if corrected != text:
                self.logger.info(f"🔧 Transcription correction: '{text}' → '{corrected}'")
                text = corrected

            self.logger.info(f"📝 Transcribed: {text}")
            print(f"📝 Heard: \"{text}\"")

            # Check if conversation window is active.
            # Must hold _conversation_lock to prevent race with _conversation_timeout:
            # without the lock, the timer could fire between our check and cancel,
            # causing the utterance to be dropped or processed after cleanup.
            in_conversation = False
            with self._conversation_lock:
                if self.conversation_window_active:
                    in_conversation = True
                    # Pause the timeout while we process this utterance
                    self._cancel_conversation_timer()

            if in_conversation:
                # Filter out likely noise during conversation window
                if self._is_conversation_noise(text):
                    self.logger.info(f"🔇 Filtered noise during conversation: '{text}'")
                    # Restart the conversation timer (was paused when speech started)
                    self.open_conversation_window(self._default_duration)
                    return

                # Apply corrections for common mishearings
                corrected_text = self._apply_command_corrections(text)
                if corrected_text != text:
                    self.logger.info(f"🔧 Corrected in conversation: '{text}' → '{corrected_text}'")
                    text = corrected_text

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
        """Find microphone device index, routing through PipeWire.

        Prefers the 'pulse' or 'pipewire' virtual device so that capture
        goes through PipeWire's gain staging and processing pipeline.
        Falls back to direct ALSA only if PipeWire isn't available.
        """
        devices = sd.query_devices()

        # First, verify the configured mic exists in the system at all
        if self.device:
            hw_found = False
            max_retries = 10  # 10 × 3s = 30s — USB mic can take 20-30s to enumerate
            for attempt in range(max_retries):
                devices = sd.query_devices()
                for dev in devices:
                    if (self.device in dev['name'] and
                            dev.get('max_input_channels', 0) > 0):
                        hw_found = True
                        break
                if hw_found:
                    if attempt > 0:
                        self.logger.info(f"Found mic '{self.device}' on retry {attempt + 1}/{max_retries}")
                    break
                if attempt < max_retries - 1:
                    self.logger.debug(f"Mic '{self.device}' not yet available, retrying in 3s ({attempt + 1}/{max_retries})")
                    time.sleep(3)

            if not hw_found:
                self.logger.warning(f"Configured mic '{self.device}' not found after retries")

        # Prefer PipeWire/PulseAudio virtual device for gain staging
        for i, dev in enumerate(devices):
            if dev['name'] in ('pulse', 'pipewire') and dev.get('max_input_channels', 0) > 0:
                self._using_fallback_device = False
                self.logger.info(f"Using PipeWire capture device: {dev['name']} (index {i})")
                return i

        # Fall back to system default input
        try:
            default_idx = sd.default.device[0]
            if default_idx is not None and default_idx >= 0:
                dev = sd.query_devices(default_idx)
                if dev.get('max_input_channels', 0) > 0:
                    self.logger.info(f"Using default input device: {dev['name']}")
                    self._using_fallback_device = True
                    return default_idx
        except Exception:
            pass

        # Last resort: direct ALSA match by name
        if self.device:
            for i, dev in enumerate(devices):
                if (self.device in dev['name'] and
                        dev.get('max_input_channels', 0) > 0):
                    self.logger.warning(f"Using direct ALSA device: {dev['name']} (no PipeWire)")
                    self._using_fallback_device = True
                    return i

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

            # Force 48000 Hz to match PipeWire's native clock rate.
            # sounddevice reports 44100 for the "pipewire" virtual device,
            # but PipeWire actually runs at 48000.  Opening at 44100 forces
            # PipeWire's SPA resampler (non-integer 160:147 ratio) whose
            # filter state is non-deterministic across restarts — this broke
            # speaker ID embeddings.  48000 bypasses the resampler entirely.
            device_info = sd.query_devices(device_index)
            device_sr = 48000
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
            self._callback_count = 0
            self._callback_last_log = time.monotonic()
            self.logger.info(
                "🎤 Continuous listening active... "
                "(stream.active=%s, device=%s, sr=%d, blocksize=%d)",
                self.stream.active, device_info['name'], device_sr, device_blocksize,
            )
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
        When running on fallback device, checks if preferred mic appeared.
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

                # Case 1b: Verify PipeWire still has the right mic as default source
                if self.device and self.stream is not None:
                    self._check_pipewire_source()

                # Case 1c: Stream alive but on fallback — check if preferred path appeared
                if self._using_fallback_device and self.stream is not None:
                    try:
                        devices = sd.query_devices()
                        for dev in devices:
                            if dev['name'] in ('pulse', 'pipewire') and dev.get('max_input_channels', 0) > 0:
                                self.logger.info("🎤 PipeWire device appeared — switching from fallback...")
                                self._handle_stream_lost()
                                if self.start():
                                    self.logger.info("🎤 Switched to PipeWire capture!")
                                    print("🎤 PipeWire audio restored!")
                                break
                    except Exception as e:
                        self.logger.debug(f"PipeWire device check failed: {e}")

            # Case 2: No stream — try to reconnect
            else:
                self.logger.debug("Device monitor: no stream — attempting reconnect")
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

    def _check_pipewire_source(self):
        """Verify PipeWire's default source is still the configured mic.

        If PipeWire switched the default source away from our mic (e.g.
        after a system settings change), fix it with wpctl.
        """
        try:
            import subprocess
            result = subprocess.run(
                ["pactl", "get-default-source"],
                capture_output=True, text=True, timeout=5,
            )
            default_source = result.stdout.strip()
            # Check if our configured mic name appears in the default source
            if self.device and self.device.lower().replace(' ', '_') not in default_source.lower().replace(' ', '_'):
                # Also accept partial matches (e.g. "usb_pnp" in source name)
                mic_key = self.device.lower().split()[0]  # e.g. "usb" from "USB PnP Audio Device"
                if mic_key not in default_source.lower():
                    self.logger.warning(
                        f"PipeWire default source changed to '{default_source}' "
                        f"(expected '{self.device}') — resetting"
                    )
                    # Find and set the correct source
                    list_result = subprocess.run(
                        ["pactl", "list", "sources", "short"],
                        capture_output=True, text=True, timeout=5,
                    )
                    for line in list_result.stdout.splitlines():
                        if self.device.lower().replace(' ', '_') in line.lower().replace(' ', '_'):
                            source_name = line.split('\t')[1]
                            subprocess.run(
                                ["pactl", "set-default-source", source_name],
                                timeout=5,
                            )
                            self.logger.info(f"🎤 Reset PipeWire default source to: {source_name}")
                            break

            # Enforce 100% source volume — prevents drift from GNOME, USB re-enum, etc.
            vol_result = subprocess.run(
                ["pactl", "get-source-volume", "@DEFAULT_SOURCE@"],
                capture_output=True, text=True, timeout=5,
            )
            if "100%" not in vol_result.stdout:
                subprocess.run(
                    ["pactl", "set-source-volume", "@DEFAULT_SOURCE@", "100%"],
                    timeout=5,
                )
                self.logger.info(f"🎤 Source volume drifted ({vol_result.stdout.strip()}), reset to 100%")
        except Exception as e:
            self.logger.debug(f"PipeWire source check failed: {e}")

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
        with self._buffer_lock:
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

        # Reset Silero VAD's internal hidden state after TTS playback.
        # Silero is stateful (carries context across chunks) — stale state
        # from before TTS can suppress speech detection after resuming.
        # The speech_frames counter also resets, requiring ~300ms of speech
        # before triggering, but this is acceptable since the user is
        # waiting for TTS to finish anyway.
        self.vad.reset()

        # Acoustic settling delay — let room echo/reverb dissipate before
        # re-enabling the audio callback.  _speaking_event is still set
        # during this window, so incoming frames are discarded.
        time.sleep(0.35)

        self.speaking = False
        self._speaking_event.clear()  # Allow audio callback to resume processing
        self.logger.info(
            f"🔊 Listening resumed (vad_speech_frames={self.vad.speech_frames}, "
            f"vad_silence_frames={self.vad.silence_frames}, "
            f"collecting={self.collecting_speech}, "
            f"conv_window={self.conversation_window_active})"
        )
    
    def get_stream_health(self) -> dict:
        """Return stream health info for diagnostics/watchdog."""
        return {
            "stream_active": self.stream.active if self.stream else False,
            "callback_count": self._callback_count,
            "speaking": self.speaking,
            "speaking_event": self._speaking_event.is_set(),
            "collecting": self.collecting_speech,
            "conv_window": self.conversation_window_active,
            "running": self.running,
        }

    # Post-transcription word corrections for known Whisper mishearings.
    # Applied early (before routing) so all downstream logic sees clean text.
    # Keyed by lowercased phrase → replacement.
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
        for wrong, right in self._TRANSCRIPTION_CORRECTIONS.items():
            if wrong in text:
                text = text.replace(wrong, right)
        return text

    def _apply_command_corrections(self, text: str) -> str:
        """Apply corrections for common command mishearings"""
        # "i was/analyzed [command]" -> "analyze [command]"
        if re.match(r'^i (was|analyzed)\s+', text, re.IGNORECASE):
            corrected = re.sub(r'^i (was|analyzed)\s+', 'analyze ', text, flags=re.IGNORECASE)
            return corrected

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

            # Bump epoch so any stale timer callback (already past cancel
            # but blocked on the lock) will see a mismatch and no-op.
            self._conversation_epoch += 1
            epoch = self._conversation_epoch

            # Start new auto-close timer, capturing current epoch
            self._conversation_timer = threading.Timer(
                duration, self._conversation_timeout, args=(epoch,)
            )
            self._conversation_timer.daemon = True
            self._conversation_timer.start()

        if not was_active:
            self.logger.info(f"🔓 Conversation window opened ({duration:.0f}s)")
            self.logger.debug("Window open: duration=%.1fs turn_count=%d",
                              duration, getattr(self, '_turn_count', 0))
            print(f"🔓 Conversation window open ({duration:.0f}s)")
        else:
            self.logger.debug(f"🔓 Conversation window extended ({duration:.0f}s)")
        self._play_tone("tone_path", "tone")

    def close_conversation_window(self):
        """Close conversation window and cancel timer."""
        with self._conversation_lock:
            self._cancel_conversation_timer()
            if self.conversation_window_active:
                self.conversation_window_active = False
                self.logger.info("🔒 Conversation window closed")
                print("🔒 Conversation window closed")
                self._play_tone("close_tone_path", "close tone")

    def _cancel_conversation_timer(self):
        """Cancel the conversation timeout timer (must hold lock or be called from locked context)."""
        if self._conversation_timer is not None:
            self._conversation_timer.cancel()
            self._conversation_timer = None

    def _conversation_timeout(self, epoch: int = None):
        """Called by timer when conversation window expires due to silence."""
        timed_out = False
        with self._conversation_lock:
            # If epoch doesn't match, a newer window superseded this timer
            if epoch is not None and epoch != self._conversation_epoch:
                self.logger.debug("Stale conversation timer (epoch %d != %d) — ignoring",
                                  epoch, self._conversation_epoch)
                return
            if self.conversation_window_active:
                self.conversation_window_active = False
                self._conversation_timer = None
                timed_out = True
                self.logger.info("🔒 Conversation window timed out (silence)")
                print("🔒 Conversation ended (silence)")
                self._play_tone("close_tone_path", "close tone")
        # Invoke cleanup callback AFTER releasing the lock
        if timed_out and self.on_window_close:
            try:
                self.on_window_close()
            except Exception as e:
                self.logger.error(f"on_window_close callback error: {e}")

    def _play_tone(self, config_key, label="tone"):
        """Play a conversation tone if configured."""
        if not self.config.get("conversation.follow_up_window.play_tone", True):
            return
        tone_path = self.config.get(f"conversation.follow_up_window.{config_key}")
        if not tone_path:
            return
        tone_path = os.path.expanduser(tone_path)
        if not os.path.exists(tone_path):
            self.logger.warning(f"Conversation {label} file not found: {tone_path}")
            return
        try:
            import subprocess
            from core.tts import resolve_output_device
            output_device = resolve_output_device(
                self.config.get("audio.output_device", "default")
            )
            subprocess.Popen(
                ["aplay", "-D", output_device, tone_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.logger.error(f"Failed to play conversation {label}: {e}")

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
