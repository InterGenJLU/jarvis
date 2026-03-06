"""
Speech-to-Text Engine using faster-whisper

Ultra-fast inference with CTranslate2 backend.
Supports multiple models keyed by speaker — fine-tuned for known users,
stock base for everyone else.
"""

import numpy as np
from pathlib import Path
from typing import Dict, Optional

from core.logger import get_logger


class SpeechToText:
    """Speech-to-text engine using faster-whisper with per-speaker model routing"""

    def __init__(self, config):
        """
        Initialize STT engine.

        When stt_finetuned is enabled, loads two models:
        - Fine-tuned model keyed to ``stt_finetuned.user`` (default ``christopher``)
        - Stock whisper-base keyed to ``default`` (for all other / unknown speakers)
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        self.models: Dict[str, object] = {}  # user_id | "default" → WhisperModel

        # Check for fine-tuned model
        self.use_finetuned = config.get("stt_finetuned.enabled", False)

        if self.use_finetuned:
            model_path = config.get("stt_finetuned.model_path")
            model_parent = Path(model_path).parent.parent
            ct2_path = str(model_parent / "whisper_finetuned_ct2")

            if not Path(ct2_path).exists():
                self.logger.error("CTranslate2 model not found at expected location")
                self.logger.error(f"Expected: {ct2_path}")
                raise FileNotFoundError(f"CTranslate2 model not found: {ct2_path}")

            finetuned_user = config.get("stt_finetuned.user", "primary_user")
            fallback_model = config.get("stt_finetuned.fallback_model", "base")

            self.logger.info(f"Loading fine-tuned Whisper for '{finetuned_user}'")
            self._load_model(finetuned_user, ct2_path)

            self.logger.info(f"Loading fallback Whisper ({fallback_model}) for other speakers")
            self._load_model("default", fallback_model)
        else:
            self.logger.info("Using base Whisper (faster-whisper)")
            self._load_model("default", "base")

        self.use_fallback = False
        self.language = config.get("stt.language", "en")

        # Software gain boost for low-output mics (e.g. webcam mics)
        # Normalizes audio to a target peak level before transcription
        self.gain_target_peak = config.get("stt.gain_target_peak", 0.7)
        self.gain_enabled = config.get("stt.gain_enabled", True)

        # Debug audio saving — disabled by default to save ~5-15ms per transcription
        self.debug_save_audio = config.get("stt.debug_save_audio", False)

    def _load_model(self, key: str, model_path: str):
        """Load a WhisperModel and store it under *key*, then warm it up."""
        try:
            from faster_whisper import WhisperModel
            import ctranslate2

            # Assert GPU availability early (production safeguard)
            devs = ctranslate2.get_supported_compute_types("cuda")
            if not devs:
                raise RuntimeError("ROCm GPU not available to CTranslate2")

            self.logger.info(f"Loading model '{key}' from {model_path}...")

            model = WhisperModel(
                model_path,
                device="cuda",
                compute_type="float16",
                num_workers=1,
            )

            # Warm-up: force CTranslate2 to fully load weights into GPU memory.
            dummy = np.zeros(16000, dtype=np.float32)
            list(model.transcribe(dummy, language="en")[0])

            self.models[key] = model
            self.logger.info(f"Model '{key}' ready (warm-up complete)")

        except ImportError:
            self.logger.error("faster-whisper not available")
            raise ImportError("Install: pip install faster-whisper")
        except Exception as e:
            self.logger.error(f"Failed to load model '{key}': {e}")
            raise
    def _apply_gain(self, audio: np.ndarray) -> np.ndarray:
        """Normalize audio to target peak level.

        Low-output mics (webcam mics) produce peaks of 0.04-0.40 when
        Whisper expects 0.5-0.8. This scales the signal up so quiet
        consonants like 'J' in 'Jarvis' aren't lost.

        Clipping protection ensures we never exceed [-1.0, 1.0].
        """
        if not self.gain_enabled:
            return audio

        peak = np.max(np.abs(audio))
        if peak < 0.001:
            return audio  # Dead silence, don't amplify noise

        if peak < self.gain_target_peak:
            gain_factor = self.gain_target_peak / peak
            # Cap gain at 10x to avoid amplifying pure noise
            gain_factor = min(gain_factor, 10.0)
            audio = audio * gain_factor
            # Clip to prevent distortion
            audio = np.clip(audio, -1.0, 1.0)
            self.logger.debug(
                f"Gain boost: {gain_factor:.1f}x (peak {peak:.3f} → {np.max(np.abs(audio)):.3f})"
            )

        return audio

    def _trim_leading_silence(self, audio: np.ndarray, sr: int = 16000,
                               threshold: float = 0.008, margin_s: float = 0.3) -> np.ndarray:
        """Trim leading silence, keeping a margin before the first energy.

        This prevents the 3-second VAD pre-buffer (mostly silence) from
        causing Whisper's internal VAD to reject the entire clip.
        """
        frame_size = int(sr * 0.03)  # 30ms frames
        for i in range(0, len(audio) - frame_size, frame_size):
            rms = np.sqrt(np.mean(audio[i:i + frame_size] ** 2))
            if rms > threshold:
                start = max(0, i - int(margin_s * sr))
                trimmed = audio[start:]
                if len(trimmed) < len(audio):
                    self.logger.debug(
                        f"Trimmed {(len(audio) - len(trimmed)) / sr:.1f}s leading silence"
                    )
                return trimmed
        return audio  # No trimming needed

    def _debug_save_audio(self, audio: np.ndarray, sr: int = 16000):
        """Save audio clip to /tmp for debugging (keeps last 5)."""
        try:
            import wave, glob, os, time
            debug_dir = "/tmp/jarvis_audio_debug"
            os.makedirs(debug_dir, exist_ok=True)

            # Clean old files (keep last 5)
            existing = sorted(glob.glob(f"{debug_dir}/clip_*.wav"))
            for old in existing[:-4]:
                os.remove(old)

            ts = int(time.time() * 1000) % 100000
            path = f"{debug_dir}/clip_{ts}.wav"
            int16 = (audio * 32767).astype(np.int16)
            with wave.open(path, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(int16.tobytes())
            self.logger.debug(f"Debug audio saved: {path}")
        except Exception as e:
            self.logger.debug(f"Debug save failed: {e}")

    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000,
                   speaker_user_id: Optional[str] = None) -> str:
        """
        Transcribe audio data to text using the speaker-appropriate model.

        Args:
            audio_data: Audio samples as numpy array (float32, mono)
            sample_rate: Sample rate of audio (default: 16000)
            speaker_user_id: Identified speaker; selects fine-tuned model if
                available, otherwise falls back to ``default``.

        Returns:
            Transcribed text
        """
        if self.use_fallback:
            return self.fallback.transcribe(audio_data, sample_rate)

        # Select the right model for this speaker
        model = self.models.get(speaker_user_id) if speaker_user_id else None
        if model is None:
            model = self.models["default"]
            used_key = "default"
        else:
            used_key = speaker_user_id
        self.logger.debug(f"STT model: '{used_key}' (speaker={speaker_user_id})")

        try:
            # Ensure mono
            if len(audio_data.shape) > 1:
                audio_data = audio_data.mean(axis=1)

            # Ensure float32 (faster-whisper/ONNX requirement)
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)

            # Resample if needed
            if sample_rate != 16000:
                import librosa
                audio_data = librosa.resample(
                    audio_data,
                    orig_sr=sample_rate,
                    target_sr=16000
                )

            # Trim leading silence to prevent Whisper's internal VAD
            # from rejecting clips dominated by the 3s pre-buffer
            _pre_len = len(audio_data) if hasattr(audio_data, '__len__') else 0
            audio_data = self._trim_leading_silence(audio_data)
            _post_len = len(audio_data) if hasattr(audio_data, '__len__') else 0
            self.logger.debug("STT preprocess: trim %d→%d samples, gain next",
                              _pre_len, _post_len)

            # Boost quiet audio to target peak level
            audio_data = self._apply_gain(audio_data)

            # Debug: save audio clips to /tmp for diagnosis
            if self.debug_save_audio:
                self._debug_save_audio(audio_data)

            # Transcribe with optimizations
            segments, info = model.transcribe(
                audio_data,
                language=self.language,
                beam_size=3,  # Sweet spot for short voice commands (was 5)
                vad_filter=True,  # Built-in VAD
                vad_parameters=dict(
                    threshold=0.3,
                    min_speech_duration_ms=100,
                    min_silence_duration_ms=200
                ),
                word_timestamps=False,  # Disabled — data was never consumed downstream
                condition_on_previous_text=False,
                temperature=0.0,  # Deterministic
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6
            )
            
            # Collect all segments
            seg_list = list(segments)
            text = " ".join([segment.text for segment in seg_list])
            self.logger.debug("STT result: %d segments, text_len=%d, lang_prob=%.2f",
                              len(seg_list), len(text), info.language_probability)

            return text.strip()
            
        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            return ""
