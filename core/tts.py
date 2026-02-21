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

        # Audio output device
        self.audio_device = config.get("audio.output_device", "default")

        # Normalization
        self.normalization_enabled = config.get("tts.normalization_enabled", True)
        self.normalizer = get_normalizer() if self.normalization_enabled else None

        # Engine selection
        self.engine = config.get("tts.engine", "piper")

        if self.engine == "kokoro":
            self._init_kokoro(config)
        else:
            self._init_piper(config)

        if self.normalization_enabled:
            self.logger.info("Text normalization enabled")

    # ── Kokoro initialization ──────────────────────────────────────────

    def _init_kokoro(self, config):
        """Initialize Kokoro TTS engine (in-process, CPU)."""
        from kokoro import KPipeline
        import torch
        import numpy as np

        self._np = np
        self.sample_rate = 24000

        self.logger.info("Initializing Kokoro TTS pipeline...")
        t0 = time.time()
        # Force CPU — faster than GPU for this 82M model, and avoids
        # stealing the ROCm device from CTranslate2/STT
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

        init_time = time.time() - t0
        self.logger.info(
            f"Kokoro TTS initialized in {init_time:.1f}s "
            f"(voice: {voice_a} {int(blend_ratio*100)}% + {voice_b} {int((1-blend_ratio)*100)}%, "
            f"speed: {self._kokoro_speed})"
        )

        # Pre-synthesize short acknowledgment phrases for instant playback
        self._ack_cache: Dict[str, bytes] = {}
        self._ack_played = False
        self._build_ack_cache()

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

            try:
                # Normalize text for human-readable speech
                if normalize and self.normalization_enabled and self.normalizer:
                    original_text = text
                    text = self.normalizer.normalize(text)
                    if text != original_text:
                        self.logger.debug(f"Normalized: '{original_text}' -> '{text}'")

                if self.engine == "kokoro":
                    return self._speak_kokoro(text)
                else:
                    return self._speak_piper(text)

            except Exception as e:
                self.logger.error(f"TTS error: {e}")
                return False

    # ── Acknowledgment cache ─────────────────────────────────────────

    def _build_ack_cache(self):
        """Pre-synthesize short phrases as raw PCM for instant playback."""
        phrases = [
            "One moment.",
            "Just a moment.",
            "Give me just a moment.",
            "One second.",
        ]
        t0 = time.time()
        for phrase in phrases:
            try:
                chunks = []
                for gs, ps, audio in self._kokoro_pipeline(
                    phrase, voice=self._kokoro_voice, speed=self._kokoro_speed
                ):
                    chunks.append(audio)
                if chunks:
                    full = self._np.concatenate(chunks)
                    self._ack_cache[phrase] = (full * 32767).astype(self._np.int16).tobytes()
            except Exception as e:
                self.logger.warning(f"Failed to cache ack phrase '{phrase}': {e}")

        elapsed = time.time() - t0
        self.logger.info(
            f"Ack cache: {len(self._ack_cache)} phrases pre-synthesized in {elapsed:.1f}s"
        )

    def speak_ack(self) -> bool:
        """Play a random pre-cached acknowledgment phrase instantly.

        Returns True if played, False if cache empty or playback failed.
        Call this when the LLM is slow to respond to fill the silence.
        """
        if not self._ack_cache:
            return False

        with self._tts_lock:
            phrase = random.choice(list(self._ack_cache.keys()))
            pcm = self._ack_cache[phrase]
            self.logger.info(f"Ack: '{phrase}'")

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
        try:
            for gs, ps, audio in self._kokoro_pipeline(
                text, voice=self._kokoro_voice, speed=self._kokoro_speed
            ):
                audio_np = self._np.asarray(audio)
                pcm = (audio_np * 32767).astype(self._np.int16).tobytes()

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

            if aplay is not None:
                aplay.stdin.close()
        except BrokenPipeError:
            if aplay is not None:
                self._untrack_proc(aplay)
                aplay_err = aplay.stderr.read().decode().strip()
                self.logger.error(f"aplay broken pipe (device busy?): {aplay_err}")
                aplay.wait()
            return False

        if total_samples == 0:
            self.logger.error("Kokoro produced no audio")
            if aplay is not None:
                self._untrack_proc(aplay)
                aplay.stdin.close()
                aplay.wait()
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
