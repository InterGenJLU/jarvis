# JARVIS Setup Guide

Complete setup instructions for getting JARVIS running with GPU acceleration.

## Prerequisites

### Hardware
- **Minimum:** x86_64 CPU, 16GB RAM, microphone, speakers
- **Recommended:** AMD GPU with 16GB+ VRAM (RX 7900 XT tested), 64GB RAM
- **Dual GPU (optional):** Second GPU for display offload (RX 7600 tested) — frees primary GPU for compute
- **Storage:** 25GB+ free space (models + data)

### Software
- Ubuntu 24.04 LTS (tested)
- Python 3.12
- ROCm 7.2+ (for GPU acceleration)
- Git

## Installation

### 1. Clone Repository
```bash
git clone <your-repo-url> ~/jarvis
cd ~/jarvis
```

### 2. Install System Dependencies
```bash
# Audio (ALSA + PortAudio)
sudo apt update
sudo apt install portaudio19-dev python3-pyaudio alsa-utils

# Build tools (for GPU)
sudo apt install build-essential cmake

# ROCm (for GPU - required for local LLM)
# Follow: https://rocm.docs.amd.com/en/latest/deploy/linux/quick_start.html
```

### 3. Install Python Dependencies
```bash
pip install --break-system-packages -r requirements.txt
```

### 4. Configure API Keys

Create `.env` file:
```bash
nano ~/jarvis/.env
```

Add your keys:
```ini
# Porcupine Wake Word (required for voice mode)
PORCUPINE_ACCESS_KEY=<get from https://picovoice.ai/>

# Anthropic Claude API (optional — quality fallback only)
# Local Qwen3.5-35B-A3B handles most queries; Claude is last resort
ANTHROPIC_API_KEY=<get from https://console.anthropic.com/>

# OpenWeather API (optional — weather skill)
OPENWEATHER_API_KEY=<get from https://openweathermap.org/api>

# Pexels API (optional — stock images for document generation)
PEXELS_API_KEY=<get from https://www.pexels.com/api/>

# ROCm GPU (auto-set if GPU present)
HSA_OVERRIDE_GFX_VERSION=11.0.0
ROCM_PATH=/opt/rocm-7.2.0
LD_LIBRARY_PATH=/opt/rocm-7.2.0/lib
```

### 5. Download Models

#### Qwen3.5 LLM (Primary — required for local inference)
```bash
mkdir -p /mnt/models/llm
cd /mnt/models/llm

# Download from Hugging Face (choose one quantization)
# Q3_K_M (~16GB, fits 20GB VRAM with ctx-size 32768)
wget https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen3.5-35B-A3B-Q3_K_M.gguf

# Vision support (optional — multimodal projector)
wget https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/resolve/main/mmproj-F16.gguf
```

#### llama.cpp (LLM inference server)
```bash
cd ~/
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp && mkdir build && cd build

# Build with ROCm support
cmake .. -DGGML_HIP=ON -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_HIP_ARCHITECTURES=gfx1100 \
  -DCMAKE_PREFIX_PATH=/opt/rocm
make -j$(nproc)
```

#### Whisper STT (Speech-to-Text)
```bash
mkdir -p /mnt/models/whisper
cd /mnt/models/whisper
# CTranslate2-converted model for GPU inference
# See core/stt.py for expected model path
```

#### Kokoro TTS (Text-to-Speech — primary)
```bash
mkdir -p /mnt/models/kokoro
# 82M parameter model, runs on CPU
# See core/tts.py for expected model path
```

#### Piper TTS (fallback)
```bash
mkdir -p /mnt/models/piper
cd /mnt/models/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx.json
```

### 6. Configure Paths

Edit `config.yaml`:
```bash
nano ~/jarvis/config.yaml
```

Key sections to verify:
- `llm.model_path` — path to GGUF model
- `llm.mmproj_path` — path to mmproj (if using vision)
- `stt.model_path` — path to Whisper model
- `tts.kokoro.model_path` — path to Kokoro model
- `tts.piper.model_path` — path to Piper model
- `web.auth_token` — generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

### 7. Set Up Systemd Services

#### LLM Server
```bash
# Copy and configure llama-server service
cp ~/jarvis/llama-server.service ~/.config/systemd/user/
systemctl --user enable llama-server
systemctl --user start llama-server
```

#### Voice Mode
```bash
cp ~/jarvis/jarvis.service ~/.config/systemd/user/
systemctl --user enable jarvis
systemctl --user start jarvis
```

#### Web UI
```bash
cp ~/jarvis/jarvis-web.service ~/.config/systemd/user/
systemctl --user enable jarvis-web
systemctl --user start jarvis-web
```

### 8. Test
```bash
# Watch logs
journalctl --user -u jarvis -f

# Say the wake word
# "Jarvis, what time is it?"

# Or use console mode (no mic needed)
python3 jarvis_console.py

# Or open the web UI
# https://localhost:8443?token=YOUR_AUTH_TOKEN
```

## GPU Acceleration Setup

### Requirements
- AMD GPU (RDNA 2/3 recommended)
- ROCm 7.2+
- 16GB+ VRAM recommended (20GB for Qwen3.5 Q3_K_M + ctx-size 32768)

### Build CTranslate2 with ROCm (for Whisper STT)
```bash
cd ~/
git clone --recursive https://github.com/OpenNMT/CTranslate2.git
cd CTranslate2
mkdir build && cd build

cmake .. \
  -DWITH_HIP=ON \
  -DWITH_MKL=OFF \
  -DWITH_OPENBLAS=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx1100 \
  -DCMAKE_BUILD_TYPE=Release \
  -DOPENMP_RUNTIME=COMP \
  -DCMAKE_HIP_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
  -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
  -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
  -DCMAKE_PREFIX_PATH=/opt/rocm \
  -DBUILD_CLI=OFF

make -j$(nproc)
sudo make install
sudo ldconfig

# Install Python bindings
cd ../python
pip install --break-system-packages .
```

### Verify GPU
```bash
rocm-smi
journalctl --user -u jarvis | grep "GPU ACTIVE"
```

## Voice Training (Optional)

To train JARVIS on your voice/accent:
```bash
cd /mnt/models/voice_training
./record_training_data.sh
python3 train_whisper.py
```

See `docs/VOICE_TRAINING_GUIDE.md` for details.

## Troubleshooting

### No Audio Detection
```bash
# List audio devices
python3 -c "import pyaudio; pa = pyaudio.PyAudio(); [print(f'{i}: {pa.get_device_info_by_index(i)[\"name\"]}') for i in range(pa.get_device_count())]"

# Update config.yaml with correct device
```

### Wake Word Not Working
- Check Porcupine API key in .env
- Verify microphone is working
- Check logs: `journalctl --user -u jarvis -f`

### GPU Not Loading
```bash
# Check ROCm
rocm-smi

# Check environment
systemctl --user show jarvis | grep Environment
```

### SIGSEGV on Service Restart
When restarting JARVIS (`systemctl --user restart jarvis`), you may see:
```
jarvis.service: Main process exited, code=dumped, status=11/SEGV
jarvis.service: Failed with result 'core-dump'.
```
**This is normal and harmless.** Native GPU libraries (CTranslate2, PyTorch/ROCm) sometimes segfault during teardown when receiving SIGTERM. The service restarts cleanly every time thanks to `Restart=always`. No data loss or functional impact.

### Service Won't Start
```bash
# Check logs
journalctl --user -u jarvis -n 50

# Run manually for debugging (voice mode)
cd ~/jarvis
python3 jarvis_continuous.py

# Or use console mode (no mic/speaker needed)
python3 jarvis_console.py
```

## Performance

### Expected Latency
- **Wake word detection:** <100ms
- **STT (GPU):** 0.1-0.2s
- **STT (CPU):** 0.3-0.5s
- **LLM tool calling (local Qwen3.5):** ~2.5s (1s tool decision + 1.5s response)
- **LLM direct (local Qwen3.5):** 1-4s depending on response length
- **LLM (Claude API fallback):** 1-3s
- **TTS (Kokoro):** 0.3-0.8s

### System Resources
- **Idle:** ~500MB RAM, ~18.8GB VRAM
- **Active:** ~1GB RAM, ~19.1GB VRAM peak
- **GPU VRAM (Qwen3.5 Q3_K_M, ctx-32768):** ~19.1GB of 20GB

## File Structure
```
~/jarvis/
├── core/               # Core modules (32K+ lines)
│   └── tools/          # 11 LLM tool definitions (auto-discovered)
├── skills/ → symlink   # Skill definitions (on SSD)
├── docs/               # Documentation
├── scripts/            # Test suites and utilities
├── config.yaml         # Main configuration
├── .env                # API keys (not in git)
├── jarvis_continuous.py  # Voice mode entry point
├── jarvis_console.py     # Console mode entry point
└── jarvis_web.py         # Web UI entry point

/mnt/models/     # AI models (LLM, STT, TTS)
/mnt/storage/    # Skills, data, backups
```

## API Keys

### Porcupine (Wake Word)
- **Get:** https://picovoice.ai/
- **Free Tier:** Yes (limited)
- **Required:** Yes (voice mode only — console/web don't need it)

### Anthropic Claude
- **Get:** https://console.anthropic.com/
- **Free Tier:** $5 credit for new accounts
- **Required:** No — quality fallback only. Local Qwen3.5-35B-A3B handles most queries

### OpenWeather
- **Get:** https://openweathermap.org/api
- **Free Tier:** Yes
- **Required:** No (weather skill only)

### Pexels
- **Get:** https://www.pexels.com/api/
- **Free Tier:** Yes
- **Required:** No (stock images for document generation — text-only slides without it)

## Updating
```bash
cd ~/jarvis
git pull
pip install --break-system-packages -r requirements.txt
systemctl --user restart llama-server jarvis jarvis-web
```

## Backups

JARVIS automatically backs up to:
- `~/jarvis/.backup/` (primary)
- `/mnt/storage/jarvis-backup/` (secondary)
- `/mnt/models/backups/` (tertiary)

## Support

- **Documentation:** `~/jarvis/docs/`
- **Development guide:** `docs/SKILL_DEVELOPMENT.md`
- **Logs:** `journalctl --user -u jarvis -f`

---

**Version:** 5.0.0
**Last Updated:** March 12, 2026
**Status:** Production Ready
