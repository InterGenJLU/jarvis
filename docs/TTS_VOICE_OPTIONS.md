# TTS Voice — Current State & History

**Updated:** February 17, 2026
**Current Engine:** Kokoro 82M (primary) + Piper ONNX (fallback)
**Voice Config:** 50/50 blend of `bm_fable` + `bm_george`, speed 1.0
**Deployed:** February 16, 2026

---

## Current Setup

Kokoro TTS is the primary engine, running in-process on CPU. Piper serves as automatic fallback if Kokoro fails to initialize.

**Config (config.yaml):**
```yaml
tts:
  engine: kokoro
  kokoro_voice_a: bm_fable
  kokoro_voice_b: bm_george
  kokoro_blend_ratio: 0.5
  kokoro_speed: 1.0
```

**Why Kokoro won:** Dramatically more natural than Piper, CPU-only (no GPU contention with STT), 82M parameters (~350MB model), 26 voice options. The 50/50 fable+george blend gives a mature, authoritative British-leaning voice that fits the JARVIS persona.

**Piper fallback:** `en_GB-northern_english_male-medium.onnx` — activates automatically if Kokoro fails. Lower quality but guaranteed to work.

---

## Evaluation History (Feb 15)

This document originally evaluated four TTS options. Here's the outcome:

| Option | Result |
|--------|--------|
| **Piper voice swap** | Tested several voices; none solved the pronunciation/maturity issues |
| **Kokoro TTS** | Selected and deployed Feb 16. CPU-only, excellent quality |
| **StyleTTS 2** | Tested and rejected. CUDA-only, ROCm compatibility issues on RX 7900 XT |
| **Coqui XTTS-v2** | Not tested. GPU-heavy, company defunct. Voice cloning remains a future option |

---

## Future: Voice Cloning

Paul Bettany JARVIS voice cloning remains a long-term goal. Coqui XTTS-v2 proof-of-concept was done earlier but needs better source audio + fine-tuning. Listed in `docs/TODO_NEXT_SESSION.md` under Long-Term Roadmap.

---

## Reference Links

- Kokoro: https://huggingface.co/hexgrad/Kokoro-82M
- Kokoro local setup: https://github.com/PierrunoYT/Kokoro-TTS-Local
- Piper samples: https://rhasspy.github.io/piper-samples/
- Piper voices: https://huggingface.co/rhasspy/piper-voices
