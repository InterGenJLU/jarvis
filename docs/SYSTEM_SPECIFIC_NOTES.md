# JARVIS System-Specific Configuration Notes

**IMPORTANT:** These are system-specific settings that differ from defaults.

## Piper TTS Binary Location

**CRITICAL:** Piper is installed at:
```
/home/user/.local/bin/piper
```

**NOT** at `/usr/bin/piper`

This must be reflected in `config.yaml`:
```yaml
tts:
  piper_bin: "/home/user/.local/bin/piper"  # CORRECT
```

**Why this matters:**
- Piper was installed via pip user install, not system-wide
- Using wrong path causes "piper not found" errors
- TTS will fail silently if path is incorrect

## Whisper STT Model

**CURRENT:** Fine-tuned Whisper via faster-whisper (CTranslate2) with GPU acceleration:
```
/mnt/models/voice_training/whisper_finetuned_ct2    (production - GPU-optimized CTranslate2 format)
/mnt/models/voice_training/whisper_finetuned/final  (source - HuggingFace format, used for conversion)
```

Fallback base model:
```
/mnt/models/whisper/ggml-base.bin
```

**GPU Performance:** 0.1-0.2s transcription (10-20x faster than CPU)

## Other System-Specific Paths

### Jarvis Home
```
/home/user/jarvis/
```

### Storage Mount
```
/mnt/storage/jarvis/
```

### Models
- **Whisper (fine-tuned, production):** `/mnt/models/voice_training/whisper_finetuned_ct2`
- **Whisper (fine-tuned, source):** `/mnt/models/voice_training/whisper_finetuned/final`
- **Whisper (base fallback):** `/mnt/models/whisper/ggml-base.bin`
- **Piper TTS:** `/mnt/models/piper/en_GB-northern_english_male-medium.onnx`
- **Qwen LLM:** `/mnt/models/llm/Qwen2.5-7B-Instruct-Q5_K_M.gguf`

### Audio Devices
- **Microphone:** FIFINE K669B USB condenser mic (hw:fifine,0 via udev rule)
- **Speakers:** ALCS1200A Analog (plughw:0,0)
- **Secondary mic (unused):** EMEET SmartCam Nova 4K webcam (hw:2,0)

## Configuration Checklist

When updating `config.yaml`, always verify:
- [ ] Piper path is `/home/user/.local/bin/piper`
- [ ] Whisper model is `ggml-base.bin` (NOT medium or large)
- [ ] All paths use correct username (your_username, not generic)
- [ ] Model paths point to `/mnt/models/`
- [ ] Skills/storage paths point to `/mnt/storage/jarvis/`
- [ ] Audio devices are correct (plughw:0,0 for output)

## Common Mistakes to Avoid

1. ❌ Using `/usr/bin/piper` → Causes TTS failure
2. ❌ Using `ggml-medium.bin` → 10 second delays (too slow!)
3. ❌ Using relative paths for models → Models not found
4. ❌ Hardcoding `/home/user/` → Won't work on this system
5. ✅ Always use full absolute paths
6. ✅ Always test TTS after config changes
7. ✅ Always use base.bin for Whisper (speed matters!)

---

**Last Updated:** February 16, 2026
**System:** ubuntu2404 (the user's workstation)
