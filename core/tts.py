"""
Text-to-Speech Engine

Dual-engine TTS: Kokoro (primary) and Piper (fallback).
Configured via tts.engine in config.yaml.
"""

import subprocess
import json
import os
import time
import random
import threading
from pathlib import Path
from typing import Optional, Dict

from core.logger import get_logger
from core.tts_normalizer import get_normalizer

logger = get_logger(__name__)


def resolve_output_device(configured: str) -> str:
    """Resolve audio output device name to ALSA device string.

    If configured value is already an ALSA string (plughw:, hw:, default),
    use it directly. Otherwise, search /proc/asound/cards by name and
    return plughw:N,0 for the matching card that has playback capability.
    """
    if not configured or configured == "default":
        return "default"

    # Already an ALSA device string — use directly
    if configured.startswith(("plughw:", "hw:")):
        return configured

    # Resolve by name: read /proc/asound/cards
    logger.debug("resolve_output_device: searching for '%s'", configured)
    try:
        with open("/proc/asound/cards") as f:
            cards_text = f.read()
    except OSError:
        logger.warning("Cannot read /proc/asound/cards, using 'default'")
        return "default"

    # Parse lines like: " 3 [Generic        ]: HDA-Intel - HD-Audio Generic"
    import re
    for match in re.finditer(
        r"^\s*(\d+)\s+\[(\w+)\s*\].*?-\s*(.+)$", cards_text, re.MULTILINE
    ):
        card_num, card_id, card_desc = match.group(1), match.group(2), match.group(3)

        if (configured.lower() in card_id.lower()
                or configured.lower() in card_desc.lower()):
            # Verify this card has playback capability
            pcm_path = Path(f"/proc/asound/card{card_num}/pcm0p")
            if pcm_path.exists():
                device = f"plughw:{card_num},0"
                logger.info(
                    f"Resolved output device '{configured}' -> {device} "
                    f"({card_desc})"
                )
                return device
            else:
                logger.debug(
                    f"Card {card_num} ({card_id}) matches '{configured}' "
                    f"but has no playback"
                )

    logger.warning(
        f"Output device '{configured}' not found in ALSA cards, using 'default'"
    )
    return "default"


class TextToSpeech:
    """Text-to-speech engine supporting Kokoro and Piper backends"""

    def __init__(self, config):
        self.config = config
        self.logger = get_logger(__name__, config)

        # TTS lock to prevent concurrent calls
        self._tts_lock = threading.Lock()

        # Track active audio subprocesses for scoped interrupt/kill
        self._active_procs: list = []
        self._active_procs_lock = threading.Lock()

        # Track whether speak() was called (for caller detection)
        self._spoke = False

        # Audio output device (resolved by name at startup)
        self.audio_device = resolve_output_device(
            config.get("audio.output_device", "default")
        )

        # Normalization
        self.normalization_enabled = config.get("tts.normalization_enabled", True)
        self.normalizer = get_normalizer() if self.normalization_enabled else None

        # Engine selection
        self.engine = config.get("tts.engine", "piper")

        self._piper_ready = False  # Tracks whether Piper fallback is initialized

        if self.engine == "kokoro":
            self._init_kokoro(config)
        else:
            self._init_piper(config)
            self._piper_ready = True

        if self.normalization_enabled:
            self.logger.info("Text normalization enabled")

    # ── Kokoro initialization ──────────────────────────────────────────

    def _init_kokoro(self, config):
        """Initialize Kokoro TTS engine (in-process, CPU)."""
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*dropout option adds dropout.*")
            warnings.filterwarnings("ignore", message=".*weight_norm.*is deprecated.*")
            from kokoro import KPipeline
        import torch
        import numpy as np

        self._np = np
        self.sample_rate = 24000

        self.logger.info("Initializing Kokoro TTS pipeline...")
        t0 = time.time()
        # Force CPU — faster than GPU for this 82M model, and avoids
        # stealing the ROCm device from CTranslate2/STT
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*dropout option adds dropout.*")
            warnings.filterwarnings("ignore", message=".*weight_norm.*is deprecated.*")
            self._kokoro_pipeline = KPipeline(lang_code='b', repo_id='hexgrad/Kokoro-82M', device='cpu')

        # Load blended voice: 50% fable + 50% george
        voice_a = config.get("tts.kokoro_voice_a", "bm_fable")
        voice_b = config.get("tts.kokoro_voice_b", "bm_george")
        blend_ratio = config.get("tts.kokoro_blend_ratio", 0.5)

        voice_dir = Path(os.path.expanduser(
            "~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M"
        ))
        # Find the snapshot directory dynamically
        snapshots = list((voice_dir / "snapshots").iterdir())
        if snapshots:
            voices_dir = snapshots[0] / "voices"
        else:
            raise FileNotFoundError(f"No Kokoro snapshots found in {voice_dir}")

        va = torch.load(voices_dir / f"{voice_a}.pt", weights_only=True)
        vb = torch.load(voices_dir / f"{voice_b}.pt", weights_only=True)
        self._kokoro_voice = va * blend_ratio + vb * (1.0 - blend_ratio)

        self._kokoro_speed = config.get("tts.kokoro_speed", 1.0)

        # Inject pronunciation overrides into Misaki G2P lexicon
        pronunciations = config.get("tts.kokoro_pronunciations", {})
        if pronunciations and hasattr(self._kokoro_pipeline, 'g2p'):
            golds = getattr(getattr(self._kokoro_pipeline.g2p, 'lexicon', None), 'golds', None)
            if golds is not None:
                for word, phonemes in pronunciations.items():
                    golds[word.lower()] = phonemes
                self.logger.info(f"Kokoro G2P: injected {len(pronunciations)} pronunciation override(s)")

        init_time = time.time() - t0
        self.logger.info(
            f"Kokoro TTS initialized in {init_time:.1f}s "
            f"(voice: {voice_a} {int(blend_ratio*100)}% + {voice_b} {int((1-blend_ratio)*100)}%, "
            f"speed: {self._kokoro_speed})"
        )

        # Pre-synthesize short acknowledgment phrases for instant playback
        # Maps phrase → (pcm_bytes, style_tag)
        self._ack_cache: Dict[str, tuple[bytes, str]] = {}
        self._ack_played = False
        self._build_ack_cache()

        # CAL-L0 response cache — persistent on-disk cache with SQLite catalog.
        # Loads from disk (~10ms) on startup. Only generates missing phrases.
        from core.tts_cache import TTSCache
        self._tts_cache = TTSCache(config)
        loaded = self._tts_cache.load_all()

        # Background thread generates any missing phrases (first startup
        # or new honorific added). Subsequent startups load from disk instantly.
        self._cal_l0_generating = False
        import threading
        t = threading.Thread(
            target=self._build_cal_l0_cache,
            daemon=True,
            name="cal-l0-cache",
        )
        t.start()

    # ── Piper initialization ──────────────────────────────────────────

    def _init_piper(self, config):
        """Initialize Piper TTS engine (subprocess-based)."""
        self.model_path = config.get("tts.model_path")
        self.config_path = config.get("tts.config_path")
        self.piper_bin = config.get("tts.piper_bin", "piper")

        self.length_scale = config.get("tts.length_scale", 1.0)
        self.noise_scale = config.get("tts.noise_scale", 0.667)
        self.noise_w_scale = config.get("tts.noise_w_scale", 0.8)
        self.sentence_silence = config.get("tts.sentence_silence", 0.2)

        self.sample_rate = self._get_piper_sample_rate()

        self.logger.info(f"Piper TTS initialized with model: {Path(self.model_path).name}")

    def _get_piper_sample_rate(self) -> int:
        """Read sample rate from Piper config file."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    piper_config = json.load(f)
                    return piper_config.get("audio", {}).get("sample_rate", 22050)
        except Exception as e:
            self.logger.warning(f"Could not read sample rate from config: {e}")
        return 22050

    # ── Piper fallback ─────────────────────────────────────────────────

    def _fallback_to_piper(self, text: str) -> bool:
        """Attempt Piper TTS when Kokoro fails. Lazy-inits Piper on first call."""
        if not self._piper_ready:
            try:
                self.logger.warning("Kokoro failed — initializing Piper fallback...")
                kokoro_rate = self.sample_rate  # Save Kokoro's rate
                self._init_piper(self.config)
                self._piper_ready = True
                # Restore Kokoro sample rate as primary (Piper uses its own
                # rate internally via _speak_piper's subprocess pipeline)
                self._piper_sample_rate = self.sample_rate
                self.sample_rate = kokoro_rate
            except Exception as e:
                self.logger.error(f"Piper fallback init failed: {e}")
                return False

        self.logger.warning("Kokoro failed — falling back to Piper")
        # _speak_piper opens its own aplay with the correct rate
        saved_rate = self.sample_rate
        self.sample_rate = getattr(self, '_piper_sample_rate', 22050)
        try:
            return self._speak_piper(text)
        finally:
            self.sample_rate = saved_rate

    # ── Shared speak interface ─────────────────────────────────────────

    def speak(self, text: str, normalize: bool = True) -> bool:
        """
        Speak text using the configured TTS engine.

        Args:
            text: Text to speak
            normalize: Whether to normalize text (default: True)

        Returns:
            True if successful, False otherwise
        """
        with self._tts_lock:
            if not text or not text.strip():
                self.logger.warning("Empty text provided to speak()")
                return False

            self._spoke = True
            self.logger.info(f"TTS speak() called with: '{text[:50]}...'")

            # CAL-L0 cache: check for pre-generated audio before synthesizing.
            # Saves ~300ms per cached phrase. Cache key is the exact text.
            cached_pcm = self._tts_cache.get(text)
            if cached_pcm is not None:
                try:
                    aplay = self._open_aplay()
                    if aplay:
                        self._track_proc(aplay)
                        aplay.stdin.write(cached_pcm)
                        aplay.stdin.close()
                        aplay.wait(timeout=10)
                        self._untrack_proc(aplay)
                        self.logger.info(f"CAL-L0 cached playback: '{text[:50]}'")
                        return aplay.returncode == 0
                except Exception as e:
                    self.logger.warning(f"CAL-L0 cache playback failed, falling through: {e}")
                    # Fall through to normal synthesis

            try:
                # Normalize text for human-readable speech
                if normalize and self.normalization_enabled and self.normalizer:
                    original_text = text
                    text = self.normalizer.normalize(text)
                    if text != original_text:
                        self.logger.debug(f"Normalized: '{original_text}' -> '{text}'")

                if self.engine == "kokoro":
                    result = self._speak_kokoro(text)
                    if not result:
                        return self._fallback_to_piper(text)
                    return result
                else:
                    return self._speak_piper(text)

            except Exception as e:
                self.logger.error(f"TTS error: {e}")
                return False

    # ── Acknowledgment cache ─────────────────────────────────────────

    def _build_ack_cache(self):
        """Pre-synthesize short phrases as raw PCM for instant playback."""
        from core import persona
        tagged_phrases = persona.pool_tagged("ack_cache")
        t0 = time.time()
        for phrase, style in tagged_phrases:
            try:
                chunks = []
                for gs, ps, audio in self._kokoro_pipeline(
                    phrase, voice=self._kokoro_voice, speed=self._kokoro_speed
                ):
                    chunks.append(audio)
                if chunks:
                    full = self._np.concatenate(chunks)
                    pcm = (full * 32767).astype(self._np.int16).tobytes()
                    self._ack_cache[phrase] = (pcm, style)
            except Exception as e:
                self.logger.warning(f"Failed to cache ack phrase '{phrase}': {e}")

        elapsed = time.time() - t0
        self.logger.info(
            f"Ack cache: {len(self._ack_cache)} phrases pre-synthesized in {elapsed:.1f}s"
        )

    # ── CAL-L0 response cache ─────────────────────────────────────────

    # All response templates from the conversation skill.
    # {honorific} placeholder gets resolved per-honorific during cache build.
    _CAL_L0_TEMPLATES = [
        # Greetings (time-aware ones are handled dynamically, cache the generics)
        "Good morning, {honorific}.",
        "Morning, {honorific}.",
        "Good to see you up and about, {honorific}.",
        "Good morning, {honorific}. I trust you slept well.",
        "Morning, {honorific}. Another day, another opportunity.",
        "Good afternoon, {honorific}.",
        "Afternoon, {honorific}.",
        "Good afternoon, {honorific}. I hope the day is treating you well.",
        "Afternoon, {honorific}. Productive day so far, I hope.",
        "Good evening, {honorific}.",
        "Evening, {honorific}.",
        "Good evening, {honorific}. Winding down, or just getting started?",
        "Evening, {honorific}. I trust the day went well.",
        "Burning the midnight oil, I see.",
        "Still at it, {honorific}? I admire the dedication.",
        "Good evening, {honorific}. I was beginning to wonder if you'd forgotten about me.",
        "Evening, {honorific}. I should point out it's well past a reasonable hour.",
        "Hello, {honorific}.",
        "At your service, {honorific}.",
        "Ready when you are, {honorific}.",
        # Minimal greetings
        "How can I help, {honorific}?",
        "Standing by, {honorific}.",
        "Ready, {honorific}.",
        "I'm listening, {honorific}.",
        "What do you need, {honorific}?",
        "Go ahead, {honorific}.",
        # Farewells
        "Have a good morning, {honorific}.",
        "Until next time, {honorific}.",
        "Take care, {honorific}. I'll be here when you need me.",
        "Good luck out there, {honorific}.",
        "I'll hold down the fort, {honorific}.",
        "Have a good day, {honorific}.",
        "Take care, {honorific}.",
        "I'll be here when you need me, {honorific}.",
        "Have a productive afternoon, {honorific}.",
        "Don't be a stranger, {honorific}.",
        "Have a good evening, {honorific}.",
        "Goodnight, {honorific}.",
        "Sleep well, {honorific}.",
        "Have a restful evening, {honorific}.",
        "Until tomorrow, {honorific}. Try to get some rest.",
        "Goodnight, {honorific}. I'll keep an eye on things.",
        # Thanks
        "You're welcome, {honorific}.",
        "My pleasure, {honorific}.",
        "Of course, {honorific}.",
        "Happy to help, {honorific}.",
        "Anytime, {honorific}.",
        "Not a problem, {honorific}.",
        "Always happy to assist, {honorific}.",
        "Glad to be of service.",
        "That's what I'm here for, {honorific}.",
        "No trouble at all.",
        "Happy to oblige, {honorific}.",
        "Think nothing of it, {honorific}.",
        "It's what I do, {honorific}.",
        "Delighted to be of help.",
        "All part of the service, {honorific}.",
        # Acknowledgments
        "Indeed, {honorific}.",
        "Quite so.",
        "Precisely, {honorific}.",
        "Very good, {honorific}.",
        "Understood.",
        "Noted, {honorific}.",
        "Absolutely, {honorific}.",
        "Right you are, {honorific}.",
        "As it should be, {honorific}.",
        # Pleasantries
        "All systems operational, {honorific}.",
        "Functioning within normal parameters.",
        "Quite well, thank you for asking.",
        "Operating at full capacity, as always.",
        "All systems nominal, {honorific}.",
        "Functioning perfectly, {honorific}. No complaints.",
        "Running smoothly, {honorific}.",
        "Can't complain. Well, I could, but it wouldn't be very British of me.",
        "Everything's in order, {honorific}.",
        "All good here, {honorific}.",
        "Rather well, all things considered.",
        "Tip-top, {honorific}. Thank you for asking.",
        "Perfectly adequate, {honorific}. Which is about as enthusiastic as I get.",
        # Compliments
        "Thank you, {honorific}. I do my best.",
        "Most kind of you, {honorific}.",
        "I appreciate that, {honorific}.",
        "You're too kind, {honorific}.",
        "Glad I could help, {honorific}.",
        "That means a great deal, {honorific}. Thank you.",
        "Happy to meet expectations, {honorific}.",
        "I'll try not to let it go to my head, {honorific}.",
        "All in a day's work, {honorific}.",
        "I'm rather pleased to hear that.",
        "You'll make my circuits blush, {honorific}.",
        "I appreciate the kind words, {honorific}.",
        # Apologies
        "No need to apologize, {honorific}.",
        "No worries at all, {honorific}.",
        "That's perfectly fine, {honorific}.",
        "Think nothing of it, {honorific}.",
        "Not a problem in the slightest.",
        "No harm done, {honorific}.",
        "These things happen, {honorific}.",
        "Please, don't give it a second thought.",
        "Quite alright, {honorific}.",
        "Nothing to apologize for, {honorific}.",
        # User is good
        "Glad to hear it, {honorific}.",
        "Excellent, {honorific}.",
        "Good to hear, {honorific}.",
        "Splendid.",
        "Pleased to hear it, {honorific}.",
        "That's good to know, {honorific}.",
        "Wonderful, {honorific}.",
        # You're welcome
        "Thank you, {honorific}.",
        "Most kind, {honorific}.",
        "Appreciated, {honorific}.",
        "Very gracious of you, {honorific}.",
        "You're too kind, {honorific}. Though I won't stop you.",
        # No help needed
        "Very well, {honorific}. I'll be here if you need me.",
        "Understood, {honorific}. I'll be here when you need me.",
        "Of course, {honorific}. Just say the word.",
        "Very good, {honorific}. Standing by.",
        "Alright, {honorific}. I'm here if anything comes up.",
        "No problem, {honorific}. You know where to find me.",
        "Understood. I'll try not to take it personally, {honorific}.",
        "Right, {honorific}. I'll just be here. Waiting. Patiently.",
        "Understood, {honorific}. Standing by.",
        "Alright, {honorific}. I'm here if you need anything.",
        "Right then, {honorific}. Just say the word.",
        # Small talk
        "I'm here if you need a distraction, {honorific}.",
        "I may not be the most entertaining company, but I'm reliable.",
        "I could recite pi to a thousand digits, if that helps.",
        "Might I suggest asking me something? I do enjoy being useful.",
        "Well, {honorific}, I'm at your disposal. Name your diversion.",
        "I'm better at tasks than entertainment, but I'll give it my best.",
        "If it helps, I find your company rather enjoyable as well.",
        "I'm told I have a dry wit. Whether that's a compliment remains unclear.",
        "I'm here, {honorific}. For whatever that's worth.",
        "Perhaps I can help with something productive? Just a thought.",
        # Meta-questions
        "I'm JARVIS — a personal voice assistant, built right here at home. How can I help, {honorific}?",
        "I'm your personal assistant, {honorific}. Voice-activated, locally hosted, and at your service.",
        "JARVIS, {honorific}. Personal assistant. I handle weather, reminders, news, system tasks, and quite a bit more.",
        "I'm an AI assistant running on local hardware, {honorific}. No cloud required.",
        "I'm JARVIS. I was built to be helpful, {honorific}, and I take the job seriously.",
        "Personal assistant, {honorific}. Built from scratch, runs on your hardware, answers to you.",
        "I'm the voice in the room that actually listens, {honorific}. What would you like to know?",
        "JARVIS, at your service. I handle tasks, answer questions, and try not to be insufferable about it.",
        # What's up
        "Not much, {honorific}. Ready to assist.",
        "All quiet on the home front, {honorific}.",
        "Standing by, {honorific}. What do you need?",
        "Just monitoring systems, {honorific}. The usual.",
        "The usual, {honorific}. What can I do for you?",
        "Keeping things running smoothly, {honorific}.",
        "Nothing out of the ordinary, {honorific}. How can I help?",
        "All systems humming along nicely. What's on your mind?",
        "Keeping an eye on things, {honorific}. What do you need?",
        "Same as always, {honorific}. Ready when you are.",
        "Oh, you know. Processing data, contemplating existence. The usual.",
        "Just here, eagerly awaiting your commands, {honorific}.",
    ]

    # Primary honorifics to pre-generate at startup
    _CAL_L0_PRIMARY_HONORIFICS = ["sir", "ma'am"]

    def _build_cal_l0_cache(self):
        """Generate any missing CAL-L0 response audio via TTSCache.

        Runs in a background thread after startup. On first ever startup,
        generates all 308 phrases (~200s throttled). On subsequent startups,
        load_all() already loaded from disk — this just fills gaps (new
        honorific, new template). Typically completes in 0s.
        """
        self._cal_l0_generating = True
        generated = self._tts_cache.generate_missing(
            templates=self._CAL_L0_TEMPLATES,
            honorifics=self._CAL_L0_PRIMARY_HONORIFICS,
            synthesize_fn=self._synthesize_to_pcm,
            version="1",
            throttle_sleep=0.05,
        )
        self._cal_l0_generating = False
        self.logger.info(
            "CAL-L0 cache complete: %d phrases in %.1fs", count, elapsed,
        )

    def _synthesize_to_pcm(self, text: str) -> bytes | None:
        """Synthesize text to raw PCM bytes via Kokoro. Returns None on failure."""
        try:
            chunks = []
            for gs, ps, audio in self._kokoro_pipeline(
                text, voice=self._kokoro_voice, speed=self._kokoro_speed
            ):
                chunks.append(audio)
            if chunks:
                full = self._np.concatenate(chunks)
                return (full * 32767).astype(self._np.int16).tobytes()
        except Exception as e:
            self.logger.warning(f"CAL-L0 cache: failed to synthesize '{text[:50]}': {e}")
        return None

    def cache_cal_l0_phrase(self, text: str, pcm: bytes):
        """Lazy-cache a CAL-L0 phrase after first synthesis (for non-primary honorifics)."""
        self._tts_cache.put(text, pcm, template="", honorific="", mood="neutral")

    def speak_cached(self, text: str) -> bool:
        """Play a pre-cached CAL-L0 response. Returns False if not cached.

        Caller should fall back to speak() if this returns False.
        """
        pcm = self._tts_cache.get(text)
        if pcm is None:
            return False

        with self._tts_lock:
            try:
                aplay = self._open_aplay()
                if aplay is None:
                    self.logger.error("speak_cached: failed to open audio device")
                    return False
                self._track_proc(aplay)
                aplay.stdin.write(pcm)
                aplay.stdin.close()
                aplay.wait(timeout=10)
                self._untrack_proc(aplay)
                self.logger.info(f"CAL-L0 cached playback: '{text[:50]}'")
                return aplay.returncode == 0
            except Exception as e:
                self.logger.error(f"speak_cached failed: {e}")
                if aplay is not None:
                    self._untrack_proc(aplay)
                return False

    def generate_wav(self, text: str, normalize: bool = True) -> bytes:
        """Generate WAV audio from text without playing it.

        Uses Kokoro to synthesize speech and returns a complete WAV file
        as bytes (24kHz, mono, int16). Does NOT acquire _tts_lock — no
        audio hardware is touched.

        Args:
            text: Text to synthesize
            normalize: Whether to apply TTS normalization (default: True)

        Returns:
            WAV file as bytes, or empty bytes on failure

        Raises:
            RuntimeError: If engine is not Kokoro
        """
        if self.engine != "kokoro":
            raise RuntimeError("generate_wav() requires Kokoro engine")

        if not text or not text.strip():
            return b""

        if normalize and self.normalization_enabled and self.normalizer:
            text = self.normalizer.normalize(text)

        import io
        import wave

        chunks = []
        for gs, ps, audio in self._kokoro_pipeline(
            text, voice=self._kokoro_voice, speed=self._kokoro_speed
        ):
            chunks.append(self._np.asarray(audio))

        if not chunks:
            return b""

        full = self._np.concatenate(chunks)
        int16 = (full * 32767).astype(self._np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(int16.tobytes())

        return buf.getvalue()

    def speak_ack(self, style_hint: str = None, cancel_check=None) -> bool:
        """Play a random pre-cached acknowledgment phrase instantly.

        Args:
            style_hint: Preferred style tag ("checking", "working", "research").
                        Falls back to "neutral" if no match, then any phrase.
            cancel_check: Optional callable returning True to abort after
                          acquiring the TTS lock (prevents stale acks when
                          the response arrived while waiting for the lock).

        Returns True if played, False if cache empty or playback failed.
        Call this when the LLM is slow to respond to fill the silence.
        """
        if not self._ack_cache:
            return False

        with self._tts_lock:
            # Recheck after acquiring lock — response may have arrived
            # while we were blocked waiting for the lock (race fix).
            if cancel_check and cancel_check():
                self.logger.debug("Ack cancelled after lock acquisition")
                return False

            # Filter candidates by style hint (with neutral fallback)
            if style_hint:
                candidates = [p for p, (_, s) in self._ack_cache.items()
                              if s == style_hint]
                if not candidates:
                    candidates = [p for p, (_, s) in self._ack_cache.items()
                                  if s == "neutral"]
            else:
                candidates = list(self._ack_cache.keys())
            if not candidates:
                candidates = list(self._ack_cache.keys())

            phrase = random.choice(candidates)
            pcm, style = self._ack_cache[phrase]
            self.logger.info(f"Ack: '{phrase}' (style={style})")

            try:
                aplay = self._open_aplay()
                if aplay is None:
                    self.logger.error("Ack: failed to open audio device")
                    return False
                self._track_proc(aplay)
                aplay.stdin.write(pcm)
                aplay.stdin.close()
                # Set flag immediately — audio is committed to the pipe.
                # Must be visible to the streaming thread BEFORE aplay finishes,
                # otherwise the first LLM chunk races past the strip check.
                self._ack_played = True
                aplay.wait(timeout=5)
                self._untrack_proc(aplay)
                return aplay.returncode == 0
            except Exception as e:
                self.logger.error(f"Ack playback failed: {e}")
                if aplay is not None:
                    self._untrack_proc(aplay)
                return False

    @property
    def ack_played(self) -> bool:
        """Whether an ack phrase was played since last clear."""
        return self._ack_played

    def clear_ack_played(self):
        """Reset the ack-played flag (call after first LLM chunk is processed)."""
        self._ack_played = False

    # ── Scoped subprocess control ─────────────────────────────────────

    def _track_proc(self, proc):
        """Register an audio subprocess for scoped interrupt control."""
        with self._active_procs_lock:
            self._active_procs.append(proc)

    def _untrack_proc(self, proc):
        """Unregister an audio subprocess after it finishes."""
        with self._active_procs_lock:
            try:
                self._active_procs.remove(proc)
            except ValueError:
                pass

    def kill_active(self):
        """Kill all tracked audio subprocesses (scoped, no global pkill)."""
        with self._active_procs_lock:
            procs = list(self._active_procs)
            self._active_procs.clear()
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
                    self.logger.info(f"Killed audio subprocess pid={proc.pid}")
            except Exception as e:
                self.logger.warning(f"Failed to kill audio subprocess: {e}")

    # ── Kokoro speak ──────────────────────────────────────────────────

    def _open_aplay(self, max_retries: int = 5, retry_delay: float = 0.5):
        """Open an aplay process, retrying if the device is temporarily busy.

        PipeWire can briefly hold the ALSA device after a previous aplay
        exits, causing 'Device or resource busy' on immediate re-open.
        We verify the device actually opened by writing a tiny silent frame;
        if the write fails (BrokenPipeError), we retry after a delay.

        Returns:
            subprocess.Popen or None on failure.
        """
        for attempt in range(max_retries):
            self.logger.debug("aplay open: attempt %d/%d device=%s",
                              attempt + 1, max_retries, self.audio_device)
            proc = subprocess.Popen(
                ["aplay", "-D", self.audio_device, "-t", "raw",
                 "-r", str(self.sample_rate), "-c", "1", "-f", "S16_LE"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            # Verify device opened by writing a silent sample.
            # The test write can land in the OS pipe buffer BEFORE aplay
            # actually opens the ALSA device. So after writing, we wait
            # briefly and check if aplay is still alive — if it exited,
            # the device open failed (e.g. "Device or resource busy").
            try:
                proc.stdin.write(b'\x00\x00')  # 1 silent S16_LE sample
                proc.stdin.flush()
                # Give aplay time to actually open the ALSA device
                time.sleep(0.15)
                if proc.poll() is not None:
                    # aplay exited — device open failed despite test write
                    err = ""
                    try:
                        err = proc.stderr.read().decode().strip()
                    except Exception:
                        pass
                    self.logger.warning(
                        f"aplay exited after test write (attempt {attempt + 1}/{max_retries}): {err}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    continue
                return proc  # aplay still running — device is open
            except (BrokenPipeError, OSError):
                err = ""
                try:
                    err = proc.stderr.read().decode().strip()
                except Exception:
                    pass
                self.logger.warning(
                    f"aplay open failed (attempt {attempt + 1}/{max_retries}): {err}"
                )
                proc.wait()
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
        return None

    def _trim_trailing_silence(self, audio_np, threshold=0.01, keep_ms=150):
        """Trim trailing silence from an audio chunk, keeping keep_ms of it.

        Reduces the long comma/clause pauses Kokoro inserts between segments
        while preserving a natural brief gap.
        """
        abs_audio = self._np.abs(audio_np)
        above = self._np.where(abs_audio > threshold)[0]
        if len(above) == 0:
            return audio_np
        last_sound = above[-1]
        keep_samples = int(24000 * keep_ms / 1000)
        trim_point = min(last_sound + keep_samples, len(audio_np))
        return audio_np[:trim_point]

    def _compress_pauses(self, audio_np, max_silence_ms=50, threshold=0.015):
        """Squeeze mid-utterance silences to max_silence_ms.

        Kokoro inserts prosodic pauses at clause boundaries (commas, titles,
        etc.) that sound robotic.  This keeps the natural prosody but caps
        how long any interior silence can last.
        """
        np = self._np
        sr = self.sample_rate
        window = int(sr * 0.01)  # 10ms analysis window
        max_silence_samples = int(sr * max_silence_ms / 1000)
        n_windows = len(audio_np) // window
        if n_windows < 2:
            return audio_np

        rms = np.array([
            np.sqrt(np.mean(audio_np[i * window:(i + 1) * window] ** 2))
            for i in range(n_windows)
        ])
        voiced = np.where(rms >= threshold)[0]
        if len(voiced) < 2:
            return audio_np
        first_voiced = voiced[0]
        last_voiced = voiced[-1]

        result = [audio_np[:first_voiced * window]]
        silent_run = 0
        for i in range(first_voiced, last_voiced + 1):
            chunk = audio_np[i * window:(i + 1) * window]
            if rms[i] < threshold:
                silent_run += window
                if silent_run <= max_silence_samples:
                    result.append(chunk)
            else:
                silent_run = 0
                result.append(chunk)
        result.append(audio_np[(last_voiced + 1) * window:])
        return np.concatenate(result)

    def _speak_kokoro(self, text: str) -> bool:
        """Generate and play audio via Kokoro — streaming with lazy aplay.

        Defers aplay open until the first Kokoro chunk is ready so that
        PipeWire has Kokoro-generation time (~200ms+) to release the ALSA
        device after any previous playback.  This eliminates the multi-second
        gap between device open and first data write that caused
        'Device or resource busy' failures.
        """
        t0 = time.time()

        aplay = None
        total_samples = 0
        first_chunk_time = None
        stdin_closed = False
        pipe_error = False
        try:
            for gs, ps, audio in self._kokoro_pipeline(
                text, voice=self._kokoro_voice, speed=self._kokoro_speed
            ):
                audio_np = self._np.asarray(audio)
                pcm = (audio_np * 32767).astype(self._np.int16).tobytes()
                self.logger.debug("Kokoro chunk: %d samples, pcm=%d bytes",
                                  len(audio_np), len(pcm))

                # Lazy open: defer aplay until first audio is ready.
                # Gives PipeWire time to release the device.
                if aplay is None:
                    first_chunk_time = time.time() - t0
                    self.logger.info(f"Kokoro first chunk in {first_chunk_time:.3f}s")
                    aplay = self._open_aplay()
                    if aplay is None:
                        self.logger.error("Failed to open audio device after retries")
                        return False
                    self._track_proc(aplay)

                aplay.stdin.write(pcm)
                total_samples += len(audio)
        except BrokenPipeError:
            self.logger.error("aplay broken pipe (device busy?)")
            pipe_error = True
        finally:
            # Single cleanup path for aplay stdin — close exactly once.
            if aplay is not None and not stdin_closed:
                try:
                    aplay.stdin.close()
                except BrokenPipeError:
                    pass  # Already broken, close is best-effort
                stdin_closed = True

        if pipe_error:
            if aplay is not None:
                try:
                    aplay_err = aplay.stderr.read().decode().strip()
                    self.logger.error(f"aplay stderr: {aplay_err}")
                except Exception:
                    pass
                aplay.kill()
                try:
                    aplay.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.logger.warning("aplay did not exit after kill — zombie possible")
                self._untrack_proc(aplay)
            return False

        if total_samples == 0:
            self.logger.error("Kokoro produced no audio")
            if aplay is not None:
                aplay.kill()
                try:
                    aplay.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.logger.warning("aplay did not exit after kill — zombie possible")
                self._untrack_proc(aplay)
            return False

        gen_time = time.time() - t0
        duration = total_samples / self.sample_rate
        self.logger.info(
            f"Kokoro streamed {duration:.1f}s audio in {gen_time:.3f}s "
            f"(RTF: {duration/gen_time:.1f}x)"
        )

        try:
            aplay_return = aplay.wait(timeout=max(15, duration + 5))
        except subprocess.TimeoutExpired:
            self.logger.error("aplay timed out — killing")
            aplay.kill()
            aplay.wait()
            return False
        finally:
            self._untrack_proc(aplay)

        if aplay_return != 0:
            aplay_err = aplay.stderr.read().decode()
            self.logger.error(f"aplay error (code {aplay_return}): {aplay_err}")
            return False

        self.logger.info("TTS playback completed successfully")
        return True

    # ── Piper speak ───────────────────────────────────────────────────

    def _speak_piper(self, text: str) -> bool:
        """Generate and play audio via Piper subprocess."""
        try:
            self.logger.info("Starting Piper subprocess...")

            piper_cmd = [
                self.piper_bin,
                "-m", self.model_path,
                "-c", self.config_path,
                "--length-scale", str(self.length_scale),
                "--noise-scale", str(self.noise_scale),
                "--noise-w-scale", str(self.noise_w_scale),
                "--sentence-silence", str(self.sentence_silence),
                "--output-raw",
            ]

            piper = subprocess.Popen(
                piper_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._track_proc(piper)

            aplay_cmd = [
                "aplay",
                "-D", self.audio_device,
                "-t", "raw",
                "-r", str(self.sample_rate),
                "-c", "1",
                "-f", "S16_LE",
            ]

            aplay = subprocess.Popen(
                aplay_cmd,
                stdin=piper.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._track_proc(aplay)

            piper.stdin.write(text.encode("utf-8"))
            piper.stdin.close()

            self.logger.info("Waiting for playback...")

            try:
                aplay_return = aplay.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.logger.error("aplay timed out after 15s (audio device likely busy) — killing")
                aplay.kill()
                aplay.wait()
                piper.kill()
                piper.wait()
                return False
            finally:
                self._untrack_proc(aplay)
                self._untrack_proc(piper)

            piper_return = piper.wait(timeout=5)

            if piper_return != 0:
                piper_err = piper.stderr.read().decode()
                self.logger.error(f"Piper error (code {piper_return}): {piper_err}")
                return False

            if aplay_return != 0:
                aplay_err = aplay.stderr.read().decode()
                self.logger.error(f"aplay error (code {aplay_return}): {aplay_err}")
                return False

            self.logger.info("TTS playback completed successfully")
            return True

        except FileNotFoundError as e:
            self.logger.error(f"TTS binary not found: {e}")
            self.logger.error("Please ensure Piper is installed and in PATH")
            return False

    def test(self) -> bool:
        """Test TTS system."""
        self.logger.info(f"Testing TTS system (engine: {self.engine})...")

        test_phrases = [
            "Hello, I am Jarvis.",
            "System initialized successfully.",
            "The system is running at 192.168.1.1 on port 8080.",
        ]

        for phrase in test_phrases:
            self.logger.info(f"Speaking: {phrase}")
            if not self.speak(phrase):
                self.logger.error("TTS test failed")
                return False

        self.logger.info("TTS test completed successfully")
        return True


# Convenience function for quick TTS
def speak(text: str, config=None) -> bool:
    """Quick speak function."""
    if config is None:
        from core.config import get_config
        try:
            config = get_config()
        except RuntimeError:
            from core.config import load_config
            config = load_config()

    tts = TextToSpeech(config)
    return tts.speak(text)
