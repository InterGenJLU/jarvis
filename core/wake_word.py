"""
Wake Word Detection

Porcupine-based wake word detection with follow-up listening support.
Listens for "Jarvis" wake word, then triggers command recording.
"""

import time
import threading
import queue
import numpy as np
import sounddevice as sd
import pvporcupine
from typing import Callable, Optional

from core.logger import get_logger


class WakeWordDetector:
    """Wake word detection using Porcupine"""
    
    def __init__(self, config, on_wake: Callable = None):
        """
        Initialize wake word detector
        
        Args:
            config: Configuration object
            on_wake: Callback function when wake word detected
        """
        self.config = config
        self.logger = get_logger(__name__, config)
        self.on_wake = on_wake
        
        # Get configuration
        self.wake_word = config.get("system.wake_word", "jarvis")
        access_key = config.get_env(config.get("wake_word.access_key_env"))
        
        if not access_key:
            raise ValueError("Porcupine access key not found in environment")
        
        # Initialize Porcupine
        try:
            self.porcupine = pvporcupine.create(
                access_key=access_key,
                keywords=[self.wake_word],
            )
        except Exception as e:
            self.logger.error(f"Failed to initialize Porcupine: {e}")
            raise
        
        # Porcupine parameters
        self.porc_sample_rate = self.porcupine.sample_rate  # 16000
        self.porc_frame_length = self.porcupine.frame_length  # 512
        
        # Get microphone configuration
        self.mic_device_name = config.get("audio.mic_device")
        self.mic_device_index = self._find_mic_device()
        
        # Get device sample rate
        mic_info = sd.query_devices(self.mic_device_index, "input")
        self.device_sample_rate = int(mic_info["default_samplerate"])
        
        # State management
        self.running = False
        self.audio_thread = None
        self.last_wake_time = 0
        self.debounce_seconds = 1.0
        
        # Follow-up window support
        self.follow_up_active = False
        self.follow_up_expires = 0.0
        
        # Buffer for Porcupine (int16 @ 16kHz)
        self.porc_buffer = np.zeros(0, dtype=np.int16)
        
        self.logger.info(f"Wake word detector initialized for '{self.wake_word}'")
        self.logger.info(f"Mic: {mic_info['name']} (device {self.mic_device_index})")
        self.logger.info(f"Device SR: {self.device_sample_rate}, Porcupine SR: {self.porc_sample_rate}")
    
    def _find_mic_device(self) -> int:
        """Find microphone device index by name"""
        if not self.mic_device_name:
            return sd.default.device[0]  # Default input device
        
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if (self.mic_device_name.lower() in dev['name'].lower() and 
                dev.get('max_input_channels', 0) > 0):
                return i
        
        self.logger.warning(f"Microphone '{self.mic_device_name}' not found, using default")
        return sd.default.device[0]
    
    def _resample_linear(self, audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Simple linear resampling"""
        if src_rate == dst_rate:
            return audio.astype(np.float32, copy=False)
        
        n = int(round(len(audio) * (dst_rate / float(src_rate))))
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        
        x_old = np.arange(len(audio), dtype=np.float32)
        x_new = np.linspace(0, len(audio) - 1, n, dtype=np.float32)
        
        return np.interp(x_new, x_old, audio.astype(np.float32)).astype(np.float32)
    
    def _audio_callback(self, indata, frames, time_info, status):
        """
        Audio input callback
        
        Processes incoming audio and detects wake word.
        """
        if status:
            self.logger.warning(f"Audio callback status: {status}")
        
        # Get mono audio (float32 @ device sample rate)
        audio = indata[:, 0].copy()
        
        # Check if follow-up window is active
        now = time.time()
        if self.follow_up_active and now < self.follow_up_expires:
            # Follow-up window active - skip wake word detection
            # (The conversation manager will handle this audio)
            return
        
        # Resample to 16kHz for Porcupine
        audio_16k = self._resample_linear(audio, self.device_sample_rate, self.porc_sample_rate)
        audio_i16 = np.clip(audio_16k * 32767.0, -32768, 32767).astype(np.int16)
        
        # Add to buffer
        self.porc_buffer = np.concatenate([self.porc_buffer, audio_i16])
        
        # Process complete frames
        while len(self.porc_buffer) >= self.porc_frame_length:
            frame = self.porc_buffer[:self.porc_frame_length]
            self.porc_buffer = self.porc_buffer[self.porc_frame_length:]
            
            # Check debounce
            if time.time() - self.last_wake_time < self.debounce_seconds:
                continue
            
            # Detect wake word
            keyword_index = self.porcupine.process(frame.tolist())
            
            if keyword_index >= 0:
                self.last_wake_time = time.time()
                self.logger.info(f"Wake word '{self.wake_word}' detected")
                
                # Call wake callback
                if self.on_wake:
                    threading.Thread(target=self.on_wake, daemon=True).start()
    
    def start(self):
        """Start listening for wake word"""
        if self.running:
            self.logger.warning("Wake word detector already running")
            return
        
        self.running = True
        self.logger.info("Starting wake word detection...")
        
        try:
            # Start audio stream
            self.stream = sd.InputStream(
                samplerate=self.device_sample_rate,
                channels=1,
                dtype='float32',
                blocksize=0,
                device=self.mic_device_index,
                callback=self._audio_callback,
            )
            
            self.stream.start()
            self.logger.info("🎤 Listening for wake word...")
            
        except Exception as e:
            self.logger.error(f"Failed to start audio stream: {e}")
            self.running = False
            raise
    
    def stop(self):
        """Stop listening"""
        if not self.running:
            return
        
        self.logger.info("Stopping wake word detection...")
        self.running = False
        
        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()
        
        self.logger.info("Wake word detection stopped")
    
    def open_follow_up_window(self, duration: float):
        """
        Open follow-up listening window (no wake word needed)
        
        Args:
            duration: Window duration in seconds
        """
        self.follow_up_active = True
        self.follow_up_expires = time.time() + duration
        self.logger.debug(f"Follow-up window opened for {duration}s")
    
    def close_follow_up_window(self):
        """Close follow-up window"""
        self.follow_up_active = False
        self.follow_up_expires = 0.0
        self.logger.debug("Follow-up window closed")
    
    def __del__(self):
        """Cleanup"""
        if hasattr(self, 'porcupine'):
            self.porcupine.delete()


# Convenience functions for testing
def test_wake_word(config=None):
    """
    Test wake word detection
    
    Args:
        config: Configuration object (optional)
    """
    if config is None:
        from core.config import load_config
        config = load_config()
    
    from core.logger import get_logger
    logger = get_logger(__name__, config)
    
    def on_wake():
        logger.info("🟡 WAKE WORD DETECTED!")
        print("\n🟡 Wake word detected!")
    
    detector = WakeWordDetector(config, on_wake=on_wake)
    
    try:
        print("\n" + "=" * 60)
        print("WAKE WORD DETECTION TEST")
        print("=" * 60)
        print(f"\nListening for '{detector.wake_word}'...")
        print("Say the wake word to test detection.")
        print("Press Ctrl+C to stop.\n")
        
        detector.start()
        
        # Keep running
        while True:
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\n\nStopping...")
        detector.stop()
        print("Test complete.")
