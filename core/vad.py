"""
Voice Activity Detection (VAD)

Detects when speech is present in audio stream.
Uses Silero VAD v6 (ONNX) for neural network-based detection —
16% fewer errors than WebRTC VAD on noisy data, with stateful
context across chunks for better barge-in and speech/noise distinction.
"""

import numpy as np
from collections import deque
from typing import Optional, Tuple

from core.logger import get_logger


class VoiceActivityDetector:
    """Voice Activity Detection using Silero VAD v6 (ONNX)."""

    def __init__(self, config, aggressiveness: int = None, on_speech_detected: Optional[callable] = None):
        """
        Initialize VAD

        Args:
            config: Configuration object
            aggressiveness: Speech threshold mapping (0-3, default from config or 2).
                            Maps to Silero probability threshold:
                            0=0.3 (most sensitive), 1=0.4, 2=0.5, 3=0.7 (least sensitive)
            on_speech_detected: Optional callback when speech is detected
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        self.on_speech_detected = on_speech_detected

        # Aggressiveness → probability threshold mapping
        if aggressiveness is None:
            aggressiveness = config.get("vad.aggressiveness", 2)
        threshold_map = {0: 0.3, 1: 0.4, 2: 0.5, 3: 0.7}
        self._speech_threshold = threshold_map.get(aggressiveness, 0.5)

        # Silero VAD operates at 16kHz with 512-sample chunks (32ms)
        self.sample_rate = 16000
        self.frame_duration_ms = 32
        self.frame_size = 512  # Fixed for Silero at 16kHz

        # Speech detection parameters from config
        self.speech_frames_threshold = config.get("vad.speech_frames_threshold", 10)
        self.silence_frames_threshold = config.get("vad.silence_frames_threshold", 20)

        # Audio buffer (ring buffer for last N seconds)
        self.buffer_duration = config.get("vad.buffer_duration", 3.0)  # seconds
        self.buffer_frames = int(self.buffer_duration * 1000 / self.frame_duration_ms)
        self.audio_buffer = deque(maxlen=self.buffer_frames)

        # State
        self.speech_frames = 0
        self.silence_frames = 0
        self.is_speech = False

        # Lazy-loaded Silero model
        self._model = None

        self.logger.info(
            f"VAD initialized (Silero v6 ONNX, threshold={self._speech_threshold}, "
            f"aggressiveness={aggressiveness})"
        )
        self.logger.info(f"Audio buffer: {self.buffer_duration}s ({self.buffer_frames} frames)")

    def _get_model(self):
        """Lazy-load the Silero VAD ONNX model."""
        if self._model is None:
            self.logger.info("Loading Silero VAD v6 (ONNX)...")
            from silero_vad import load_silero_vad
            self._model = load_silero_vad(onnx=True)
            self.logger.info("Silero VAD loaded")
        return self._model

    def is_speech_frame(self, audio_frame: bytes) -> bool:
        """
        Check if audio frame contains speech

        Args:
            audio_frame: Audio data (16-bit PCM, int16)

        Returns:
            True if speech detected
        """
        try:
            import torch
            # Convert bytes to int16 numpy, then to float32 torch tensor
            samples = np.frombuffer(audio_frame, dtype=np.int16).astype(np.float32) / 32767.0
            # Pad or truncate to 512 samples
            if len(samples) < self.frame_size:
                samples = np.pad(samples, (0, self.frame_size - len(samples)))
            elif len(samples) > self.frame_size:
                samples = samples[:self.frame_size]
            tensor = torch.tensor(samples)
            model = self._get_model()
            prob = model(tensor, self.sample_rate).item()
            return prob >= self._speech_threshold
        except Exception as e:
            self.logger.error(f"VAD error: {e}")
            return False

    def process_frame(self, audio_frame: np.ndarray) -> Tuple[bool, bool]:
        """
        Process audio frame and update speech detection state

        Args:
            audio_frame: Audio frame (int16)

        Returns:
            Tuple of (is_currently_speech, speech_state_changed)
        """
        import torch

        # Ensure correct format
        if audio_frame.dtype != np.int16:
            audio_frame = (audio_frame * 32767).astype(np.int16)

        # Pad or truncate to 512 samples (Silero's required chunk size at 16kHz)
        if len(audio_frame) < self.frame_size:
            audio_frame = np.pad(audio_frame, (0, self.frame_size - len(audio_frame)))
        elif len(audio_frame) > self.frame_size:
            audio_frame = audio_frame[:self.frame_size]

        # Add to ring buffer
        self.audio_buffer.append(audio_frame.copy())

        # Convert to float32 torch tensor for Silero
        samples = audio_frame.astype(np.float32) / 32767.0
        tensor = torch.tensor(samples)

        try:
            model = self._get_model()
            prob = model(tensor, self.sample_rate).item()
            contains_speech = prob >= self._speech_threshold
        except Exception as e:
            self.logger.error(f"VAD error: {e}")
            return self.is_speech, False

        # Debug: Log speech detection (every 100 frames to avoid spam)
        if not hasattr(self, '_frame_count'):
            self._frame_count = 0
        self._frame_count += 1

        if self._frame_count % 100 == 0:
            self.logger.debug(f"VAD check: prob={prob:.3f} speech={contains_speech}, in_speech={self.is_speech}, speech_frames={self.speech_frames}")

        # Update counters
        if contains_speech:
            self.speech_frames += 1
            self.silence_frames = 0
        else:
            self.silence_frames += 1
            self.speech_frames = 0

        # Determine if speech state changed
        state_changed = False

        if not self.is_speech and self.speech_frames >= self.speech_frames_threshold:
            # Speech started
            self.is_speech = True
            state_changed = True
            self.logger.debug("Speech detected")

            # Trigger callback
            if self.on_speech_detected:
                self.on_speech_detected()

        elif self.is_speech and self.silence_frames >= self.silence_frames_threshold:
            # Speech ended
            self.is_speech = False
            state_changed = True
            self.logger.debug("Speech ended")

        return self.is_speech, state_changed

    def reset(self):
        """Reset VAD state, including Silero's internal hidden states."""
        self.speech_frames = 0
        self.silence_frames = 0
        self.is_speech = False
        if self._model is not None:
            self._model.reset_states()

    def get_buffered_audio(self) -> np.ndarray:
        """
        Get buffered audio (last N seconds)

        Returns:
            Concatenated audio frames as float32 array
        """
        if not self.audio_buffer:
            return np.array([], dtype=np.float32)

        # Concatenate all frames and convert to float32
        frames = [frame.astype(np.float32) / 32767.0 for frame in self.audio_buffer]
        return np.concatenate(frames)

    def clear_buffer(self):
        """Clear the audio buffer"""
        self.audio_buffer.clear()


def get_vad(config, aggressiveness: int = None, on_speech_detected: callable = None) -> VoiceActivityDetector:
    """Get VAD instance"""
    return VoiceActivityDetector(config, aggressiveness, on_speech_detected)
