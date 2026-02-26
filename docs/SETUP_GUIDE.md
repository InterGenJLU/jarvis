# JARVIS Setup Guide

Complete setup instructions for getting JARVIS running with GPU acceleration.

## Prerequisites

### Hardware
- **Minimum:** x86_64 CPU, 8GB RAM, microphone, speakers
- **Recommended:** AMD GPU (RX 7900 XT tested), 16GB+ RAM
- **Storage:** 10GB+ free space

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
# Audio
sudo apt update
sudo apt install portaudio19-dev python3-pyaudio

# Build tools (for GPU)
sudo apt install build-essential cmake

# ROCm (for GPU - optional but recommended)
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
# Porcupine Wake Word (required)
PORCUPINE_ACCESS_KEY=<get from https://picovoice.ai/>

# Anthropic Claude API (required)
ANTHROPIC_API_KEY=<get from https://console.anthropic.com/>

# OpenWeather API (optional)
OPENWEATHER_API_KEY=<get from https://openweathermap.org/api>

# ROCm GPU (auto-set if GPU present)
HSA_OVERRIDE_GFX_VERSION=11.0.0
ROCM_PATH=/opt/rocm-7.2.0
LD_LIBRARY_PATH=/opt/rocm-7.2.0/lib
```

### 5. Download Models
```bash
# Whisper base model (for CPU fallback)
cd /mnt/models/whisper
wget https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin

# Piper TTS model
cd /mnt/models/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx.json

# Qwen LLM (optional - uses Claude API by default)
# Instructions at: https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF
```

### 6. Configure Paths

Edit `config.yaml`:
```bash
nano ~/jarvis/config.yaml
```

Verify all paths point to your model locations.

### 7. Set Up Systemd Service
```bash
# Copy service file
cp ~/jarvis/jarvis.service ~/.config/systemd/user/

# Enable and start
systemctl --user enable jarvis
systemctl --user start jarvis

# Check status
systemctl --user status jarvis
```

### 8. Test
```bash
# Watch logs
journalctl --user -u jarvis -f

# Say the wake word
# "Jarvis, what time is it?"
```

## GPU Acceleration Setup

### Requirements
- AMD GPU (RDNA 2/3 recommended)
- ROCm 7.2+
- 8GB+ VRAM

### Installation

1. **Install ROCm:**
```bash
   # Follow official guide
   # https://rocm.docs.amd.com/
```

2. **Build CTranslate2 with ROCm:**
```bash
   cd ~/dev_ctranslate2
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

3. **Verify GPU:**
```bash
   rocm-smi
   journalctl --user -u jarvis | grep "🚀 GPU ACTIVE"
```

See the CTranslate2 build instructions in the README for detailed GPU setup.

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

# See GPU_TROUBLESHOOTING.md
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
- **LLM (Claude API):** 1-3s
- **TTS:** 0.5-1s

### System Resources
- **Idle:** ~250MB RAM
- **Active:** ~600MB RAM
- **GPU VRAM:** ~2GB (with model loaded)

## File Structure
```
~/jarvis/
├── core/               # Core modules
├── docs/               # Documentation
├── skills/             # Skill definitions
├── config.yaml         # Main configuration
├── .env                # API keys (not in git)
└── jarvis_continuous.py  # Main entry point

/mnt/models/     # AI models
/mnt/storage/    # Backups and skills
```

## API Keys

### Porcupine (Wake Word)
- **Get:** https://picovoice.ai/
- **Free Tier:** Yes (limited)
- **Required:** Yes

### Anthropic Claude
- **Get:** https://console.anthropic.com/
- **Free Tier:** $5 credit for new accounts
- **Required:** No (quality fallback only — local Qwen3.5-35B-A3B handles most queries)

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
systemctl --user restart jarvis
```

## Backups

JARVIS automatically backs up to:
- `~/jarvis/.backup/` (primary)
- `/mnt/storage/jarvis-backup/` (secondary)
- `/mnt/models/jarvis-backup/` (tertiary)

## Support

- **Documentation:** `~/jarvis/docs/`
- **Logs:** `journalctl --user -u jarvis -f`
- **System Check:** `~/jarvis/system_check.sh`

---

**Version:** 2.6.0
**Last Updated:** February 23, 2026
**Status:** Production Ready
