"""
RNNoise Python Wrapper

Simple ctypes wrapper for the RNNoise C library.
Provides real-time noise suppression for audio streams.
"""

import ctypes
import numpy as np
from pathlib import Path


class RNNoise:
    """Python wrapper for RNNoise library"""
    
    # RNNoise processes 480 samples (10ms) at 48kHz
    FRAME_SIZE = 480
    SAMPLE_RATE = 48000
    
    def __init__(self):
        """Initialize RNNoise"""
        # Load library
        try:
            self.lib = ctypes.CDLL('librnnoise.so.0')
        except OSError:
            try:
                self.lib = ctypes.CDLL('/usr/local/lib/librnnoise.so.0')
            except OSError:
                self.lib = ctypes.CDLL('/usr/local/lib/librnnoise.so')
        
        # Define function signatures
        self.lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_create.restype = ctypes.c_void_p
        
        self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_destroy.restype = None
        
        self.lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,  # state
            ctypes.POINTER(ctypes.c_float),  # output
            ctypes.POINTER(ctypes.c_float)   # input
        ]
        self.lib.rnnoise_process_frame.restype = ctypes.c_float
        
        # Create RNNoise state
        self.state = self.lib.rnnoise_create(None)
        if not self.state:
            raise RuntimeError("Failed to create RNNoise state")
    
    def process_frame(self, audio_in: np.ndarray) -> np.ndarray:
        """
        Process audio frame through RNNoise
        
        Args:
            audio_in: Input audio as float32 array (480 samples at 48kHz)
                     Values should be in range [-1.0, 1.0]
        
        Returns:
            Denoised audio as float32 array (same size as input)
        """
        # Ensure correct size
        if len(audio_in) != self.FRAME_SIZE:
            # Pad or truncate
            if len(audio_in) < self.FRAME_SIZE:
                audio_in = np.pad(audio_in, (0, self.FRAME_SIZE - len(audio_in)))
            else:
                audio_in = audio_in[:self.FRAME_SIZE]
        
        # Ensure float32
        if audio_in.dtype != np.float32:
            audio_in = audio_in.astype(np.float32)
        
        # Create output buffer
        audio_out = np.zeros(self.FRAME_SIZE, dtype=np.float32)
        
        # Create ctypes pointers
        in_ptr = audio_in.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        out_ptr = audio_out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        
        # Process frame
        vad_prob = self.lib.rnnoise_process_frame(self.state, out_ptr, in_ptr)
        
        return audio_out
    
    def __del__(self):
        """Cleanup RNNoise state"""
        if hasattr(self, 'state') and self.state:
            self.lib.rnnoise_destroy(self.state)
