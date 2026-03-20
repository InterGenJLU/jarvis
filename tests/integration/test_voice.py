#!/usr/bin/env python3
"""Quick TTS pronunciation test — speaks whatever you pass as an argument.

Usage:
    python3 scripts/test_voice.py "System notification, sir."
    python3 scripts/test_voice.py "off line" "online" "offline"
"""

import sys
import os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.tts import TextToSpeech
from core.config import Config

if len(sys.argv) < 2:
    print("Usage: python3 scripts/test_voice.py \"phrase to speak\" [\"another phrase\" ...]")
    sys.exit(1)

config = Config("config.yaml")
tts = TextToSpeech(config)

for phrase in sys.argv[1:]:
    print(f"\n🔊 Speaking: {phrase}")
    tts.speak(phrase)
