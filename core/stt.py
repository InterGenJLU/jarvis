"""
Speech-to-Text Engine using faster-whisper

Ultra-fast inference with CTranslate2 backend
"""

import numpy as np
from pathlib import Path
from typing import Optional

from core.logger import get_logger


class SpeechToText:
    """Speech-to-text engine using faster-whisper"""
    
    def __init__(self, config):
        """
        Initialize STT engine
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        
        # Check for fine-tuned model
        self.use_finetuned = config.get("stt_finetuned.enabled", False)
        
        if self.use_finetuned:
            model_path = config.get("stt_finetuned.model_path")
            # Use CTranslate2 version if available
            # CTranslate2 model is in voice_training directory
            model_parent = Path(model_path).parent.parent  # Go up from final/ to voice_training/
            ct2_path = str(model_parent / "whisper_finetuned_ct2")
            
            if Path(ct2_path).exists():
                self.logger.info("Using fine-tuned Whisper (faster-whisper/CTranslate2)")
                self._init_faster_whisper(ct2_path)
            else:
                # CTranslate2 model not found
                self.logger.error("CTranslate2 model not found at expected location")
                self.logger.error(f"Expected: {ct2_path}")
                self.logger.error("Run conversion first!")
                raise FileNotFoundError(f"CTranslate2 model not found: {ct2_path}")
        else:
            # Use base model with faster-whisper
            self.logger.info("Using base Whisper (faster-whisper)")
            self._init_faster_whisper("base")
        
        self.use_fallback = False
        self.language = config.get("stt.language", "en")

        # Software gain boost for low-output mics (e.g. webcam mics)
        # Normalizes audio to a target peak level before transcription
        self.gain_target_peak = config.get("stt.gain_target_peak", 0.7)
        self.gain_enabled = config.get("stt.gain_enabled", True)

        # Debug audio saving — disabled by default to save ~5-15ms per transcription
        self.debug_save_audio = config.get("stt.debug_save_audio", False)
    
    def _init_faster_whisper(self, model_path: str):
        """Initialize faster-whisper"""
        try:
            from faster_whisper import WhisperModel
            import ctranslate2
            
            # Assert GPU availability early (production safeguard)
            devs = ctranslate2.get_supported_compute_types("cuda")
            if not devs:
                raise RuntimeError("ROCm GPU not available to CTranslate2")
            
            self.logger.info(f"Loading model from {model_path}...")
            
            # GPU mode - no torch import to avoid ROCm library conflicts
            device = "cuda"
            compute_type = "float16"
            
            self.logger.info("Initializing faster-whisper on GPU")
            
            self.model = WhisperModel(
                model_path,
                device=device,
                compute_type=compute_type,
                num_workers=1
            )
            
            self.logger.info("🚀 GPU ACTIVE")

            # Warm-up: run a dummy transcription to force CTranslate2 to
            # fully load weights into GPU memory.  Without this, the first
            # real transcription pays a ~500ms-1s lazy-load penalty.
            dummy = np.zeros(16000, dtype=np.float32)  # 1s silence
            list(self.model.transcribe(dummy, language="en")[0])
            self.logger.info("✓ faster-whisper model loaded (warm-up complete)")

        except ImportError:
            self.logger.error("faster-whisper not available")
            raise ImportError("Install: pip install faster-whisper")
        except Exception as e:
            self.logger.error(f"Failed to load model: {e}")
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

    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000) -> str:
        """
        Transcribe audio data to text
        
        Args:
            audio_data: Audio samples as numpy array (float32, mono)
            sample_rate: Sample rate of audio (default: 16000)
            
        Returns:
            Transcribed text
        """
        if self.use_fallback:
            return self.fallback.transcribe(audio_data, sample_rate)
        
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
            audio_data = self._trim_leading_silence(audio_data)

            # Boost quiet audio to target peak level
            audio_data = self._apply_gain(audio_data)

            # Debug: save audio clips to /tmp for diagnosis
            if self.debug_save_audio:
                self._debug_save_audio(audio_data)

            # Transcribe with optimizations
            segments, info = self.model.transcribe(
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
            text = " ".join([segment.text for segment in segments])
            
            return text.strip()
            
        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            return ""
