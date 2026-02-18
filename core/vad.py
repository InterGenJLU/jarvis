"""
Voice Activity Detection (VAD)

Detects when speech is present in audio stream.
Uses WebRTC VAD for lightweight, efficient detection.
"""

import numpy as np
import webrtcvad
from collections import deque
from typing import Optional, Tuple

from core.logger import get_logger


class VoiceActivityDetector:
    """Voice Activity Detection using WebRTC VAD"""
    
    def __init__(self, config, aggressiveness: int = None, on_speech_detected: Optional[callable] = None):
        """
        Initialize VAD
        
        Args:
            config: Configuration object
            aggressiveness: VAD aggressiveness (0-3, default from config or 2)
            on_speech_detected: Optional callback when speech is detected
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        self.on_speech_detected = on_speech_detected
        
        # VAD configuration
        if aggressiveness is None:
            aggressiveness = config.get("vad.aggressiveness", 2)
        
        # Create VAD instance
        self.vad = webrtcvad.Vad(aggressiveness)
        
        # VAD requires specific sample rates: 8000, 16000, 32000, or 48000 Hz
        self.sample_rate = config.get("audio.sample_rate", 16000)
        
        # Frame duration in ms (10, 20, or 30)
        self.frame_duration_ms = 30
        self.frame_size = int(self.sample_rate * self.frame_duration_ms / 1000)
        
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
        
        self.logger.info(f"VAD initialized (aggressiveness={aggressiveness})")
        self.logger.info(f"Audio buffer: {self.buffer_duration}s ({self.buffer_frames} frames)")
    
    def is_speech_frame(self, audio_frame: bytes) -> bool:
        """
        Check if audio frame contains speech
        
        Args:
            audio_frame: Audio data (16-bit PCM, int16)
            
        Returns:
            True if speech detected
        """
        try:
            return self.vad.is_speech(audio_frame, self.sample_rate)
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
        # Ensure correct format
        if audio_frame.dtype != np.int16:
            audio_frame = (audio_frame * 32767).astype(np.int16)
        
        # Ensure correct size
        if len(audio_frame) != self.frame_size:
            # Pad or truncate to correct size
            if len(audio_frame) < self.frame_size:
                audio_frame = np.pad(audio_frame, (0, self.frame_size - len(audio_frame)))
            else:
                audio_frame = audio_frame[:self.frame_size]
        
        # Add to ring buffer
        self.audio_buffer.append(audio_frame.copy())
        
        # Convert to bytes for VAD
        audio_bytes = audio_frame.tobytes()
        
        # Check if this frame contains speech
        try:
            contains_speech = self.vad.is_speech(audio_bytes, self.sample_rate)
        except Exception as e:
            self.logger.error(f"VAD error: {e}")
            return self.is_speech, False
        
        # Debug: Log speech detection (every 100 frames to avoid spam)
        if not hasattr(self, '_frame_count'):
            self._frame_count = 0
        self._frame_count += 1
        
        if self._frame_count % 100 == 0:
            self.logger.debug(f"VAD check: speech={contains_speech}, in_speech={self.is_speech}, speech_frames={self.speech_frames}")
        
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
        """Reset VAD state"""
        self.speech_frames = 0
        self.silence_frames = 0
        self.is_speech = False
    
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

