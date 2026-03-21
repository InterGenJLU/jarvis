# JARVIS System-Specific Configuration Notes

**IMPORTANT:** These are the owner's system-specific settings that differ from defaults.

## TTS Configuration

**Primary:** Kokoro 82M (in-process CPU, 50/50 fable+george blend)
- Auto-downloads from HuggingFace Hub on first run
- No manual path configuration needed

**Fallback:** Piper TTS at:
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
- Piper TTS will fail silently if path is incorrect

## Whisper STT Model

**CURRENT:** Fine-tuned Whisper via faster-whisper (CTranslate2) with GPU acceleration:
```
/mnt/models/voice_training/whisper_finetuned_ct2    (production - GPU-optimized CTranslate2 format)
/mnt/models/voice_training/whisper_finetuned/final  (source - HuggingFace format, used for conversion)
```

Fallback base model (CPU only):
```
/mnt/models/whisper/ggml-base.bin
```

**GPU Performance:** 0.1-0.2s transcription (10-20x faster than CPU)

## LLM Configuration

**Model:** Qwen3.5-35B-A3B (Q3_K_M, MoE 256 experts/8+1 active, 3B active params)
```
/mnt/models/llm/Qwen3.5-35B-A3B-Q3_K_M.gguf
```

**Server:** llama.cpp via systemd service
```
/etc/systemd/system/llama-server.service
```
- Port 8080 (35B model), ROCm backend, GPU offload, `--parallel 1`
- `systemctl status llama-server` to check
- Qwen3.5 supports native tool calling (web research) and thinking mode (disabled via `--reasoning-budget 0`)
- mmproj vision encoder (mmproj-F16.gguf) available for vision queries via `--no-mmproj-offload` (CPU). Vision is fully operational across all 7 phases

**Secondary Model:** Qwen3.5-4B on port 8081
- Handles synthesis, contextual acknowledgments, and briefing composition
- Lower latency for lightweight tasks that don't need the full 35B model

**Fallback:** Claude API (Anthropic) — used when local quality gate fails

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
- **Qwen LLM:** `/mnt/models/llm/Qwen3.5-35B-A3B-Q3_K_M.gguf`

### Audio Devices
- **Microphone:** FIFINE K669B USB condenser mic (hw:fifine,0 via udev rule)
- **Output:** PipeWire default device (`output_device: default` in config.yaml)
  - Changed from `plughw:0,0` (Feb 23) to enable PipeWire routing and OBS coexistence
- **Secondary mic (unused):** EMEET SmartCam Nova 4K webcam (hw:2,0)

## Configuration Checklist

When updating `config.yaml`, always verify:
- [ ] Piper path is `/home/user/.local/bin/piper`
- [ ] Fine-tuned Whisper model path is correct (CTranslate2 format)
- [ ] LLM path points to `Qwen3.5-35B-A3B-Q3_K_M.gguf`
- [ ] All paths use correct username (your_username, not generic)
- [ ] Model paths point to `/mnt/models/`
- [ ] Skills/storage paths point to `/mnt/storage/jarvis/`
- [ ] Audio output device is `default` (PipeWire routing)
- [ ] llama-server systemd service is running

## Common Mistakes to Avoid

1. ❌ Using `/usr/bin/piper` → Causes TTS failure
2. ❌ Using `ggml-medium.bin` for STT → 10 second delays (too slow!)
3. ❌ Using relative paths for models → Models not found
4. ❌ Hardcoding `/home/user/` → Won't work on this system
5. ❌ Referencing old models → Current is Qwen3.5-35B-A3B Q3_K_M (Feb 24)
6. ✅ Always use full absolute paths
7. ✅ Always test TTS after config changes
8. ✅ Fine-tuned Whisper for production, base.bin only as CPU fallback

---

**Last Updated:** March 12, 2026
**System:** ubuntu2404 (the owner's workstation)
