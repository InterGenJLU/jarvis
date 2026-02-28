# JARVIS — GPU-Accelerated Voice Assistant

A fully local, privacy-first voice assistant built on AMD ROCm, fine-tuned speech recognition, and a local LLM. No cloud required for core operation.

**Built on:** Ubuntu 24.04 | Python 3.12 | ROCm 7.2 | AMD RX 7900 XT

---

## Highlights

- **0.1-0.2s speech recognition** — Fine-tuned Whisper v2 (198 phrases, 94%+ accuracy) on AMD GPU via CTranslate2 + ROCm
- **LLM-centric tool calling** — Qwen3.5-35B-A3B (MoE, 3B active params) decides which of 8 tools to call per query, 100% accuracy across 1,200+ trials. Adding a new tool = create one Python file
- **Natural blended voice** — Kokoro TTS with custom voice blend (fable + george), gapless streaming playback
- **Conversational flow engine** — Persona module (24 response pools, ~90 templates), adaptive conversation windows (4-7s), contextual acknowledgments, turn tracking
- **Self-awareness + task planning** — capability manifest, system state injection, compound request detection, LLM plan generation, sequential execution with per-step evaluation, pause/resume/cancel
- **Social introductions** — "Meet my niece Arya" triggers butler-style multi-turn introduction flow with name confirmation, pronunciation checks, and persistent people database
- **Always-on listening** — Porcupine wake word + WebRTC VAD + ambient wake word filter + multi-turn conversation windows
- **11 skills + 8 LLM tools** — time, weather, system info, filesystem, developer tools, reminders, news, web search (LLM tools) + file editor, desktop control, social introductions (skill-only)
- **Three frontends** — voice (production), console (debug/hybrid), web UI (browser-based chat with streaming + sessions)
- **Privacy by design** — everything runs locally; Claude API is a last-resort quality fallback only

---

## Demo

<video src="https://github.com/user-attachments/assets/857b4737-eef8-4bc5-8d71-d493a35c3934" width="100%" controls></video>

*3-minute demo: wake word activation, voice commands, web research, document generation, and desktop control — all running locally on AMD GPU.*

[![Watch on YouTube](https://img.youtube.com/vi/WsqLyUdl9ac/maxresdefault.jpg)](https://youtu.be/WsqLyUdl9ac)

*Watch on YouTube for full resolution with chapters.*

---

## Screenshots

### Web UI
![JARVIS Web UI](images/JARVIS_WEBUI_2.png)
*Browser-based chat with streaming responses, health check HUD, and system diagnostics*

![JARVIS Web UI — Session Sidebar](images/JARVIS_WEBUI_3.png)
*Session sidebar with conversation history, auto-detected sessions, and rename support*

### Console Mode
![JARVIS Console](images/JARVIS_CONSOLE.png)
*Terminal interface with rich stats panel showing match layer, skill routing, confidence, and timing*

---

## Table of Contents

- [Demo](#demo)
- [Architecture](#architecture)
- [Skills & Capabilities](#skills--capabilities)
- [Hardware Requirements](#hardware-requirements)
- [Installation](#installation)
- [Model Setup](#model-setup)
- [The Kokoro Voice](#the-kokoro-voice)
- [Usage](#usage)
- [Project Structure](#project-structure)
- [Configuration Reference](#configuration-reference)
- [Python & ROCm Pitfalls](#python--rocm-pitfalls)
- [Fine-Tuning Whisper](#fine-tuning-whisper)
- [Development](#development)
- [License](#license)

---

## Architecture

```
                          ┌─────────────────────┐
                          │   Porcupine Wake     │
                          │   Word Detection     │
                          └──────────┬──────────┘
                                     │ "Jarvis"
                          ┌──────────▼──────────┐
                          │   Ambient Filter     │
                          │   (position, copula, │
                          │    threshold, length) │
                          └──────────┬──────────┘
                                     │ verified wake word
                          ┌──────────▼──────────┐
                          │   WebRTC VAD +       │
                          │   Continuous Listener │
                          │   (speech detection) │
                          └──────────┬──────────┘
                                     │ audio frames
                          ┌──────────▼──────────┐
                          │   Whisper STT v2     │
                          │   (CTranslate2/GPU)  │
                          │   198 phrases, 94%+  │
                          └──────────┬──────────┘
                                     │ text
                  ┌──────────────────▼───────────────────┐
                  │      ConversationRouter               │
                  │  Shared priority chain:               │
                  │  1. Confirmations / dismissals        │
                  │  2. Memory / introductions            │
                  │  3. Task planner (compound detect)    │
                  │  ★ P4-LLM: Tool calling (8 tools)    │
                  │     semantic pruner → Qwen3.5 decides │
                  │  4. Skill routing (stateful skills)    │
                  │  5. LLM fallback (Qwen → Claude)     │
                  └──────┬──────────────┬────────────────┘
                         │              │
              ┌──────────▼───┐   ┌──────▼──────────┐
              │  Skill       │   │  LLM Router     │
              │  Handler     │   │  Qwen → Claude  │
              │  (3 skills)  │   │  + 8 LLM Tools  │
              └──────────┬───┘   └──────┬──────────┘
                         │              │
                  ┌──────▼──────────────▼────────┐
                  │   Persona + Contextual Acks   │
                  │   (10 pools, style-tagged)     │
                  └──────────────┬────────────────┘
                                │
                  ┌──────────────▼────────────────┐
                  │        Kokoro TTS             │
                  │   StreamingAudioPipeline      │
                  │   (gapless multi-sentence)    │
                  └──────────────┬────────────────┘
                                │ PCM audio
                          ┌─────▼──────┐
                          │   aplay    │
                          │  (ALSA)    │
                          └────────────┘
```

The system uses an **event-driven pipeline** with a Coordinator managing STT/TTS workers. The LLM response streams token-by-token, is chunked into sentences, and each sentence is synthesized and played as it arrives — so the user hears the first sentence while the LLM is still generating the rest.

### Tools vs Skills

JARVIS has two extension mechanisms. The distinction matters:

| | **LLM Tools** | **Skills** |
|---|---|---|
| **What they are** | Stateless query→response functions the LLM calls | Stateful modules with multi-turn flows, confirmations, or desktop control |
| **Who decides** | Qwen3.5 selects which tool to call based on the user's query | Priority chain routes to the skill before the LLM sees the query |
| **How to add one** | Create one `.py` file in `core/tools/` — auto-discovered | Create a skill directory with `skill.py` + `metadata.yaml` |
| **Examples** | get_time, get_weather, find_files, developer_tools | app_launcher (desktop verbs), file_editor (doc gen + confirmation), social_introductions (multi-turn) |
| **Count** | 8 tools (7 domain + web_search) | 3 skill-only + 7 with companion tools |

Most functionality lives in **tools** now. Skills remain for things that need deterministic state machines, desktop integration, or nested LLM pipelines — things where "let the LLM decide" isn't reliable enough.

### Key Subsystems

| Subsystem | What It Does |
|-----------|-------------|
| **Conversation Router** | Shared priority chain for voice/console/web — one router, three frontends |
| **Tool Registry** | Auto-discovers `core/tools/*.py`, builds schemas, injects dependencies — adding a tool = one file |
| **Self-Awareness** | Capability manifest + system state injected into LLM context — JARVIS knows what it can do |
| **Task Planner** | Compound request detection (22 signals), LLM plan generation, sequential execution with per-step LLM evaluation, pause/resume/cancel/skip voice interrupts, predictive timing, error-aware planning |
| **Persona Engine** | 24 response pools (~90 templates), system prompts, honorific injection, style-tagged ack selection |
| **People Manager** | SQLite-backed contacts database with relationship tracking, TTS pronunciation overrides, LLM context injection for known people |
| **Conversation State** | Turn counting, intent history, question detection, research context tracking |
| **Ambient Filter** | Multi-signal wake word validation: position, copula, threshold (0.80), length — blocks ambient mentions |
| **Conversation Windows** | Adaptive follow-up windows (4-7s), extends with conversation depth, timeout cleanup |
| **Web Research** | Qwen3.5-35B-A3B calls DuckDuckGo + trafilatura to search and synthesize answers from live web sources |
| **Conversational Memory** | SQLite fact store + FAISS semantic search — remembers facts across sessions, surfaces them proactively |
| **Context Window** | Topic-segmented working memory with relevance-scored assembly across sessions |
| **Streaming TTS** | `StreamingAudioPipeline` — single persistent aplay process, background Kokoro generation, gapless playback |
| **Speaker ID** | Resemblyzer d-vector enrollment — identifies who's speaking and adjusts honorifics dynamically |

---

## Skills & Capabilities

### LLM Tools (Qwen3.5 decides when to call)

These are stateless query→response functions. The LLM receives the user's query, selects the right tool, calls it, and synthesizes a natural language answer from the result.

| Tool | Examples | What It Does |
|------|---------|-------------|
| **get_time** | "What time is it?" / "What day is it?" | Current time and date |
| **get_weather** | "What's the weather?" / "Will it rain?" | OpenWeatherMap API — current conditions, forecast, rain check |
| **get_system_info** | "What CPU do I have?" / "How much RAM?" | 8 sub-handlers: cpu, memory, disk, gpu, network, processes, uptime, all |
| **find_files** | "Find my config file" / "Count lines in main.py" | File search, line counting, directory listing |
| **developer_tools** | "Search codebase for TODO" / "Git status" / "Show me the network" | 13 actions: codebase search, git multi-repo, system admin, general shell, visual output, 3-tier safety |
| **manage_reminders** | "Remind me at 3pm" / "What's on my schedule?" | 5 actions: add, list, cancel, acknowledge, snooze. Priority tones, nag behavior |
| **get_news** | "Read me the headlines" / "Any cybersecurity news?" | 16 RSS feeds, urgency classification, semantic dedup, category/priority filtering |
| **web_search** | "Who won the Super Bowl?" / "How far is NYC from London?" | DuckDuckGo + trafilatura → multi-source synthesis (always available) |

### Skills (Deterministic routing — state machines and desktop control)

These handle things that need multi-turn flows, confirmations, or direct desktop integration.

| Skill | Examples | How It Works |
|-------|---------|-------------|
| **File Editor** | "Write a script that..." / "Edit my config file" / "Delete temp.txt" | 5 intents: write, edit, read, delete + list. Two-stage LLM content generation, confirmation flow for destructive ops |
| **Desktop Control** | "Open Chrome" / "Volume up" / "Switch to workspace 2" | 16 intents: app launch/close, window management, volume, workspaces, focus, clipboard via GNOME Shell extension D-Bus bridge |
| **Social Introductions** | "Meet my niece Arya" / "Who is Arya?" / "Forget Arya" | Multi-turn butler-style introduction flow: name confirmation, pronunciation check, fact gathering, persistent people database with TTS pronunciation overrides |

**Conversation** (greetings, small talk, "how are you?") is handled directly by the LLM — no dedicated skill needed.

### Additional Systems

- **GNOME Desktop Bridge** — Custom GNOME Shell extension providing Wayland-native window management, with wmctrl fallback for XWayland
- **Google Calendar** — Two-way sync with dedicated JARVIS calendar, OAuth, incremental sync, background polling
- **Daily & Weekly Rundowns** — Interactive state machine: offered → re-asked → deferred → retry → pending mention
- **Health Check** — 5-layer system diagnostic (GPU, LLM, STT, TTS, skills) with ANSI terminal report + voice summary
- **User Profiles** — Speaker identification via resemblyzer d-vectors, dynamic honorifics, voice enrollment
- **People Manager** — SQLite contacts database, relationship tracking, TTS pronunciation overrides, LLM context injection

---

## Hardware Requirements

### Minimum

| Component | Requirement |
|-----------|------------|
| CPU | x86_64, 4+ cores |
| RAM | 8GB |
| Storage | 10GB free |
| Audio | USB microphone + speakers |
| OS | Ubuntu 24.04 LTS |

### Recommended (What This Was Built On)

| Component | Spec |
|-----------|------|
| CPU | AMD Ryzen 9 5900X (24 threads) |
| GPU | AMD RX 7900 XT (20GB VRAM) |
| RAM | 16GB+ |
| Microphone | USB condenser mic (FIFINE K669B tested) |
| OS | Ubuntu 24.04 LTS |
| ROCm | 7.2.0 |

> **GPU acceleration is optional but transformative.** CPU-only Whisper takes 0.3-0.5s per transcription. With GPU: 0.1-0.2s. The local LLM (Qwen3.5-35B-A3B) also benefits from GPU offloading via llama.cpp.

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USER/jarvis.git ~/jarvis
cd ~/jarvis
```

### 2. Install System Dependencies

```bash
sudo apt update
sudo apt install -y \
    portaudio19-dev python3-pyaudio \
    build-essential cmake \
    alsa-utils \
    ffmpeg
```

### 3. Install Python Dependencies

JARVIS uses **system Python 3.12** — not a virtualenv. See [Python & ROCm Pitfalls](#python--rocm-pitfalls) for why.

```bash
pip install --break-system-packages -r requirements.txt

# Additional packages not in requirements.txt
pip install --break-system-packages \
    faster-whisper \
    sentence-transformers \
    resemblyzer \
    kokoro \
    soundfile \
    trafilatura \
    duckduckgo-search \
    faiss-cpu
```

### 4. Install llama.cpp (for local LLM)

```bash
cd ~
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
mkdir build && cd build

# CPU-only build:
cmake .. -DCMAKE_BUILD_TYPE=Release
# OR with ROCm GPU:
cmake .. -DCMAKE_BUILD_TYPE=Release -DGGML_HIP=ON -DCMAKE_HIP_ARCHITECTURES=gfx1100

make -j$(nproc)
```

### 5. Install Piper TTS (fallback)

```bash
pip install --break-system-packages piper-tts
```

### 6. Configure API Keys

```bash
cp .env.example ~/jarvis/.env
nano ~/jarvis/.env
```

Fill in your keys:
- **PORCUPINE_ACCESS_KEY** — Free tier at [picovoice.ai](https://picovoice.ai/)
- **ANTHROPIC_API_KEY** — From [console.anthropic.com](https://console.anthropic.com/) (LLM fallback)
- **OPENWEATHER_API_KEY** — Free tier at [openweathermap.org](https://openweathermap.org/api) (weather skill)
- **PEXELS_API_KEY** — Free tier at [pexels.com/api](https://www.pexels.com/api/) (stock images for document generation — optional, text-only slides without it)

### 7. Download Models

See [Model Setup](#model-setup) below for detailed instructions with download links.

### 8. Configure

Edit `config.yaml` and update paths to match your model locations. See [Configuration Reference](#configuration-reference).

### 9. Set Up Systemd Service

```bash
mkdir -p ~/.config/systemd/user
cp jarvis.service ~/.config/systemd/user/

# If using a local LLM:
cp llama-server.service ~/.config/systemd/user/

# Enable linger (service runs without active login)
loginctl enable-linger $USER

# Enable and start
systemctl --user daemon-reload
systemctl --user enable jarvis
systemctl --user start jarvis

# Check status
systemctl --user status jarvis
journalctl --user -u jarvis -f
```

### 10. Test

Say: **"Jarvis, what time is it?"**

Or use console mode (no microphone needed):
```bash
python3 jarvis_console.py
```

---

## Model Setup

JARVIS uses several AI models. Here's where to get each one.

### Whisper STT (Speech-to-Text)

| Model | Source | Format | Purpose |
|-------|--------|--------|---------|
| **whisper-base** | [ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin) | GGML | CPU fallback |
| **faster-whisper base** | Auto-downloaded by `faster-whisper` | CTranslate2 | GPU-accelerated (recommended) |

```bash
# CPU fallback model (optional)
mkdir -p /path/to/models/whisper
wget -O /path/to/models/whisper/ggml-base.bin \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
```

The GPU model is auto-downloaded by `faster-whisper` on first run. You can also [fine-tune Whisper](#fine-tuning-whisper) on your accent.

### Qwen3.5-35B-A3B LLM

| Model | Source | Format | Quantization |
|-------|--------|--------|-------------|
| **Qwen3.5-35B-A3B** | [unsloth GGUF](https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF) | GGUF | Q3_K_M recommended (imatrix-calibrated) |

```bash
mkdir -p /path/to/models/llm
# Download pre-quantized Q3_K_M (~16GB) from unsloth (trusted, imatrix-calibrated):
huggingface-cli download unsloth/Qwen3.5-35B-A3B-GGUF \
    Qwen3.5-35B-A3B-Q3_K_M.gguf --local-dir /path/to/models/llm
```

The LLM runs via llama.cpp as a server process. The systemd service `llama-server.service` manages it. Qwen3.5-35B-A3B is a MoE model (256 experts, 8+1 active, ~3B active params) with native tool calling for web research. Vision available via mmproj but not loaded by default.

### Kokoro TTS (Primary Voice)

| Model | Source | Size | Runtime |
|-------|--------|------|---------|
| **Kokoro-82M** | [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | 82M params | CPU (in-process) |

Kokoro auto-downloads from HuggingFace Hub on first initialization. No manual download needed.

See [The Kokoro Voice](#the-kokoro-voice) for how the custom voice blend works.

### Piper TTS (Fallback)

| Model | Source | Format |
|-------|--------|--------|
| **en_GB-northern_english_male-medium** | [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/northern_english_male/medium) | ONNX |

```bash
mkdir -p /path/to/models/piper
cd /path/to/models/piper

wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx.json
```

### Sentence Transformers (Semantic Matching)

| Model | Source | Purpose |
|-------|--------|---------|
| **all-MiniLM-L6-v2** | [sentence-transformers](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | Intent matching, memory search |

Auto-downloads on first use. Set `cache_dir` in config.yaml to control where it's stored.

### Porcupine (Wake Word)

| Component | Source | Note |
|-----------|--------|------|
| **pvporcupine** | [picovoice.ai](https://picovoice.ai/) | Requires free API key |

Get a free access key from Picovoice and add it to your `.env` file.

### Resemblyzer (Speaker ID)

| Model | Source | Purpose |
|-------|--------|---------|
| **VoiceEncoder** | [resemblyzer](https://github.com/resemble-ai/Resemblyzer) | Speaker identification via d-vectors |

Auto-downloads on first use (~5MB model). Used for identifying who's speaking and adjusting behavior per user.

---

## The Kokoro Voice

JARVIS uses [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), a lightweight 82-million parameter TTS model that runs on CPU. What makes it special is the **voice blending** system.

### How Voice Blending Works

Kokoro ships with multiple voice presets as PyTorch tensors (`.pt` files). JARVIS loads two voices and blends them via linear interpolation:

```python
# From core/tts.py — the voice blend
voice_a = torch.load("bm_fable.pt")    # British male, warm
voice_b = torch.load("bm_george.pt")   # British male, deeper
blended = voice_a * 0.5 + voice_b * 0.5  # 50/50 blend
```

This creates a voice that has the warmth of "fable" with the depth of "george" — more natural than either voice alone.

### Configuration

```yaml
# config.yaml
tts:
  engine: kokoro
  kokoro_voice_a: bm_fable        # First voice
  kokoro_voice_b: bm_george       # Second voice
  kokoro_blend_ratio: 0.5         # 0.0 = all george, 1.0 = all fable
  kokoro_speed: 1.0               # Playback speed
```

### Available Voices

Kokoro includes several voice presets. The `bm_` prefix means "British male":
- `bm_fable` — warm, narrator-like
- `bm_george` — deeper, more authoritative
- `bf_emma` — British female
- `am_adam` — American male
- And more — check the [Kokoro model card](https://huggingface.co/hexgrad/Kokoro-82M) for the full list.

### Why Kokoro Over Other TTS?

We evaluated several TTS engines:

| Engine | Verdict | Notes |
|--------|---------|-------|
| **Kokoro 82M** | Primary | Best quality-to-speed ratio. CPU-only avoids GPU contention with STT. 82M params loads in <2s. |
| **Piper** | Fallback | Good but more robotic. ONNX format, subprocess-based. Used when Kokoro fails to initialize. |
| **StyleTTS 2** | Rejected | Superior quality but 10x slower, required GPU (competing with STT), and PyTorch dependency conflicts. |

### Streaming Playback

JARVIS doesn't wait for the full response before speaking. The `StreamingAudioPipeline` synthesizes each sentence in the background while the previous one plays, maintaining a single persistent `aplay` process for gapless audio.

---

## Usage

### Voice Mode (Production)

```bash
# Start via systemd
systemctl --user start jarvis

# Check status
systemctl --user status jarvis

# Live logs
journalctl --user -u jarvis -f
```

Say the wake word ("Jarvis") followed by your command:
- *"Jarvis, what time is it?"*
- *"Jarvis, what's the weather like?"*
- *"Jarvis, remind me to check the oven in 20 minutes"*
- *"Jarvis, search the codebase for TODO"*
- *"Jarvis, who won the Super Bowl?"*
- *"Jarvis, read me the tech headlines"*
- *"Jarvis, create a presentation about renewable energy"*
- *"Jarvis, open Chrome"*
- *"Jarvis, volume up"*

After JARVIS responds, you have a **conversation window** (4-7 seconds, adaptive) to ask follow-up questions without repeating the wake word. The window extends with conversation depth and when JARVIS asks a question.

### Console Mode

```bash
# Text-only mode — type commands, see responses + stats panel
python3 jarvis_console.py

# Hybrid mode — type commands, responses printed AND spoken
python3 jarvis_console.py --hybrid

# Speech mode — full voice pipeline via terminal
python3 jarvis_console.py --speech
```

The console displays a stats panel after each command showing match layer, skill, confidence score, timing, and LLM token counts.

### Web UI

```bash
# Start the web interface
python3 jarvis_web.py

# Then open http://127.0.0.1:8088 in your browser
```

The web UI provides the same full skill pipeline with streaming LLM responses, markdown rendering, drag/drop file handling, web research, conversation history with session sidebar, health check HUD, and an optional voice toggle for spoken output.

---

## Project Structure

```
jarvis/
├── jarvis_continuous.py          # Production entry point (voice mode)
├── jarvis_console.py             # Console/debug entry point
├── jarvis_web.py                 # Web UI entry point (browser-based chat)
├── config.yaml                   # Main configuration
├── .env                          # API keys (not in repo — see .env.example)
├── requirements.txt              # Python dependencies
│
├── core/                         # Core modules
│   ├── stt.py                    # Speech-to-text (faster-whisper + CTranslate2)
│   ├── tts.py                    # Text-to-speech (Kokoro + Piper)
│   ├── llm_router.py             # LLM routing (Qwen → quality gate → Claude fallback)
│   ├── tool_registry.py          # Auto-discovery registry for tool definitions
│   ├── tool_executor.py          # Tool dispatch (backward-compat shim)
│   ├── tools/                    # One-file tool definitions (8 tools)
│   │   ├── get_time.py           # Time and date queries
│   │   ├── get_weather.py        # Weather conditions + forecast
│   │   ├── get_system_info.py    # CPU, memory, disk, GPU, processes
│   │   ├── find_files.py         # File search, line counting
│   │   ├── developer_tools.py    # Git, codebase search, shell (13 actions)
│   │   ├── manage_reminders.py   # Reminder CRUD (5 actions)
│   │   ├── get_news.py           # News headlines (read/count)
│   │   └── web_search.py         # Web research (always included, frontend-dispatched)
│   ├── web_research.py           # DuckDuckGo + trafilatura web fetching
│   ├── pipeline.py               # Event-driven Coordinator + STT/TTS workers
│   ├── persona.py                # Response pools, system prompts, honorific injection
│   ├── conversation_state.py     # Turn tracking, intent history, question detection
│   ├── conversation_router.py    # Shared priority chain (voice/console/web)
│   ├── skill_manager.py          # Skill loading + semantic routing
│   ├── semantic_matcher.py       # Sentence transformer intent matching
│   ├── continuous_listener.py    # VAD + wake word + ambient filter + conversation windows
│   ├── tts_normalizer.py         # Text normalization for natural speech
│   ├── conversation.py           # History, cross-session memory, follow-up logic
│   ├── memory_manager.py         # Conversational memory (SQLite + FAISS)
│   ├── context_window.py         # Topic-segmented working memory
│   ├── reminder_manager.py       # Reminders, rundowns, calendar sync
│   ├── google_calendar.py        # Google Calendar OAuth + sync
│   ├── news_manager.py           # RSS monitoring + classification
│   ├── desktop_manager.py        # GNOME D-Bus bridge + wmctrl fallback + volume/clipboard
│   ├── self_awareness.py         # Capability manifest + system state for LLM context
│   ├── task_planner.py           # Compound request detection, LLM plan generation, execution
│   ├── people_manager.py         # People database, TTS pronunciation, LLM context injection
│   ├── metrics_tracker.py        # LLM metrics tracking (latency, tokens, errors)
│   ├── health_check.py           # System diagnostics
│   ├── user_profile.py           # User profiles + speaker ID
│   ├── speaker_id.py             # Resemblyzer d-vector enrollment
│   ├── honorific.py              # Dynamic honorific resolution
│   ├── config.py                 # Configuration loader
│   ├── logger.py                 # Logging setup
│   └── base_skill.py             # Skill base class
│
├── skills/                       # Skill implementations (stateful skills + tool companions)
│   ├── system/
│   │   ├── time_info/            # Time and date (companion to get_time tool)
│   │   ├── weather/              # Weather forecasts (companion to get_weather tool)
│   │   ├── system_info/          # CPU, RAM, disk info (companion to get_system_info tool)
│   │   ├── filesystem/           # File search, line counting (companion to find_files tool)
│   │   ├── file_editor/          # File write, edit, read, delete + document generation
│   │   ├── developer_tools/      # Codebase search, git, shell (companion to developer_tools tool)
│   │   ├── app_launcher/         # Desktop control (16 intents: apps, windows, volume, workspaces, clipboard)
│   │   └── web_navigation/       # Web search + browsing
│   └── personal/
│       ├── conversation/         # DISABLED — LLM handles conversation natively
│       ├── reminders/            # Voice reminders + calendar (companion to manage_reminders tool)
│       ├── news/                 # RSS headline delivery (companion to get_news tool)
│       └── social_introductions/ # Butler-style introductions + people database
│
├── web/                          # Web UI frontend
│   ├── index.html                # Chat layout
│   ├── style.css                 # Dark theme
│   └── app.js                    # WebSocket client + rendering
├── images/                       # Screenshots
├── extensions/                   # GNOME Shell extensions
│   └── jarvis-desktop@jarvis/    # Desktop Bridge (D-Bus service for window/workspace control)
├── assets/                       # Audio cues (generate your own .wav files)
├── scripts/                      # Utility scripts
│   ├── install_desktop_extension.sh  # Install GNOME Shell extension
│   ├── test_edge_cases.py            # Automated test suite (266 tests: 115 unit + 151 routing)
│   ├── test_tool_calling.py          # Tool-calling harness (175 queries, 10-category taxonomy, --sweep mode)
│   ├── unit_tests.sh                 # Test runner wrapper
│   ├── test_router.py                # Router test suite (38 tests)
│   ├── test_desktop_manager.py       # Test desktop manager module
│   ├── enroll_speaker.py             # Speaker voice enrollment
│   ├── init_profiles.py              # User profile initialization
│   └── ...
├── docs/                         # Documentation
│   ├── SETUP_GUIDE.md            # Detailed installation
│   ├── SKILL_DEVELOPMENT.md      # How to create tools and skills
│   ├── SEMANTIC_INTENT_MATCHING.md
│   ├── VOICE_TRAINING_GUIDE.md   # Whisper fine-tuning
│   └── ...
└── tools/                        # Backup, restore, and maintenance utilities
```

---

## Configuration Reference

The main configuration lives in `config.yaml`. Here are the key sections:

### Audio

```yaml
audio:
  mic_device: "USB PnP Audio Device"   # Your microphone name
  sample_rate: 16000                    # Don't change (Whisper expects 16kHz)
  channels: 1
  output_device: default                # PipeWire default (or plughw:0,0 for direct ALSA)
  device_monitor_interval: 5.0         # Hot-plug detection interval
```

### LLM

```yaml
llm:
  local:
    model_path: /path/to/models/llm/Qwen3.5-35B-A3B-Q3_K_M.gguf
    context_size: 8192
    gpu_layers: 999          # Offload all layers to GPU (if available)
    temperature: 0.6
    tool_calling: true       # Enable LLM tool calling (8 tools)
  api:
    provider: anthropic      # Fallback LLM
    model: claude-sonnet-4-20250514
    api_key_env: ANTHROPIC_API_KEY
```

### TTS

```yaml
tts:
  engine: kokoro                      # 'kokoro' or 'piper'
  kokoro_voice_a: bm_fable
  kokoro_voice_b: bm_george
  kokoro_blend_ratio: 0.5             # Voice blend
  kokoro_speed: 1.0
  # Piper fallback
  model_path: /path/to/models/piper/en_GB-northern_english_male-medium.onnx
  config_path: /path/to/models/piper/en_GB-northern_english_male-medium.onnx.json
```

### Semantic Matching

```yaml
semantic_matching:
  enabled: true
  model: all-MiniLM-L6-v2
  cache_dir: /path/to/models/sentence-transformers
  default_threshold: 0.85    # Minimum confidence for intent match
  fallback_to_llm: true      # Send unmatched queries to LLM
```

For the full configuration reference, see the comments in `config.yaml`.

---

## Python & ROCm Pitfalls

Building JARVIS on an AMD GPU with ROCm taught some hard lessons. Here's what to watch out for.

### Use System Python, Not Virtualenvs

JARVIS runs on **system Python 3.12** with `--break-system-packages`. This sounds wrong, but:

- **ROCm libraries** (`/opt/rocm-7.2.0/lib/`) must be on the system `LD_LIBRARY_PATH`
- **CTranslate2** is built from source against ROCm — it links to system-level `.so` files
- **PyTorch ROCm** (`torch+rocm7.1`) also needs the same ROCm libraries
- Virtualenvs create isolation that **breaks these shared library dependencies**

We tested a CUDA-targeted venv. It was 9.3GB and completely non-functional on AMD hardware. Lesson learned.

### CTranslate2 Must Be Built from Source for ROCm

The pip version of CTranslate2 is CUDA-only. For AMD GPUs:

```bash
git clone --recursive https://github.com/OpenNMT/CTranslate2.git
cd CTranslate2 && mkdir build && cd build

cmake .. \
  -DWITH_HIP=ON \
  -DWITH_MKL=OFF \
  -DWITH_OPENBLAS=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx1100 \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_HIP_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
  -DCMAKE_CXX_COMPILER=/opt/rocm/lib/llvm/bin/clang++ \
  -DCMAKE_C_COMPILER=/opt/rocm/lib/llvm/bin/clang \
  -DCMAKE_PREFIX_PATH=/opt/rocm \
  -DBUILD_CLI=OFF

make -j$(nproc)
sudo make install && sudo ldconfig

# Python bindings
cd ../python
pip install --break-system-packages .
```

Find your GPU architecture with `rocminfo | grep gfx`.

### PyTorch + CTranslate2 Coexistence

Both PyTorch (`torch 2.10.0+rocm7.1`) and CTranslate2 use `/opt/rocm-7.2.0/lib/`. They coexist fine **if**:

1. Both are installed at the system level (not in separate venvs)
2. `LD_LIBRARY_PATH` includes `/opt/rocm-7.2.0/lib/`
3. The ROCm environment variables are set:

```bash
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export ROCM_PATH=/opt/rocm-7.2.0
export LD_LIBRARY_PATH=/opt/rocm-7.2.0/lib
```

### Environment Variables for ROCm

These must be set for both the systemd service and any manual runs:

| Variable | Value | Purpose |
|----------|-------|---------|
| `HSA_OVERRIDE_GFX_VERSION` | `11.0.0` | GPU architecture override for RDNA 3 |
| `ROCM_PATH` | `/opt/rocm-7.2.0` | ROCm installation path |
| `LD_LIBRARY_PATH` | `/opt/rocm-7.2.0/lib` | Shared library search path |

### Import Order Matters (Historical)

Early in development, the order of `import torch` vs `import ctranslate2` determined which ROCm version was loaded. This is resolved now, but as a defensive practice, `core/stt.py` avoids importing torch directly.

---

## Fine-Tuning Whisper

JARVIS supports fine-tuning Whisper on your voice and accent. This is what transforms it from a generic model into one that understands *you*.

### Why Fine-Tune?

The base Whisper model struggles with:
- Regional accents (Southern US, British dialects, etc.)
- Domain-specific vocabulary (technical terms, project names)
- Proper nouns it hasn't seen

### Process

1. **Record training data** — 198 utterances covering problem words, domain vocabulary, and natural speech patterns
2. **Train** — Fine-tune from the base Whisper model using HuggingFace Transformers
3. **Convert** — Export to CTranslate2 format for GPU-accelerated inference
4. **Deploy** — Update `config.yaml` to point to the new model

See [docs/VOICE_TRAINING_GUIDE.md](docs/VOICE_TRAINING_GUIDE.md) for the complete process.

### Results (v2 — FIFINE K669B, 198 phrases)

| Metric | Before | After |
|--------|--------|-------|
| General accuracy | ~80% | 94%+ |
| Domain vocabulary | ~60% | ~95%+ |
| Wake word detection | ~90% | 100% |
| Contraction handling | ~70% | 100% |
| Latency (GPU) | 0.1-0.2s | 0.1-0.2s (unchanged) |

---

## Development

### Adding New Functionality

There are two paths depending on what you're building. See [docs/SKILL_DEVELOPMENT.md](docs/SKILL_DEVELOPMENT.md) for the full guide with examples.

**Path 1: LLM Tool** (stateless data query — most new features)

Create one `.py` file in `core/tools/`. The registry auto-discovers it.

```python
# core/tools/your_tool.py
TOOL_NAME = "your_tool"
SKILL_NAME = "your_skill"       # or None if not skill-gated
SCHEMA = { ... }                 # OpenAI function schema
SYSTEM_PROMPT_RULE = "..."       # When to use this tool
def handler(args):               # Execute and return result string
    return "result"
```

That's it — no wiring changes, no imports to update, no registry edits.

**Path 2: Skill** (stateful, multi-turn, desktop control, or nested LLM flows)

```
skills/system/your_skill/
├── skill.py           # Main logic (extends BaseSkill)
├── metadata.yaml      # Skill config + semantic intents
└── __init__.py        # Exports
```

Skills register semantic intents (natural language examples) and the sentence-transformer model matches user speech against them.

### Architecture Principles

1. **Privacy first** — Everything runs locally. Claude API is a quality fallback, not a dependency.
2. **LLM-centric** — Qwen3.5 decides which tools to call. Skills exist for things that need deterministic state machines.
3. **Streaming everything** — LLM tokens stream to TTS, TTS streams to audio. No waiting for full responses.
4. **Graceful degradation** — GPU fails? Fall back to CPU. Kokoro fails? Fall back to Piper. Local LLM fails? Fall back to Claude API.
5. **Defensive conventions** — If something caused 18 hours of debugging once, we keep the guard rails even after fixing the root cause.

---

## Acknowledgments

### Models & Libraries

- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad — TTS model
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) by SYSTRAN — GPU-accelerated Whisper
- [CTranslate2](https://github.com/OpenNMT/CTranslate2) by OpenNMT — Inference engine
- [llama.cpp](https://github.com/ggml-org/llama.cpp) by ggml-org — LLM inference
- [Qwen3.5-35B-A3B](https://huggingface.co/Qwen/Qwen3.5-35B-A3B) by Qwen — Local LLM (MoE, native tool calling)
- [Piper](https://github.com/rhasspy/piper) by rhasspy — Fallback TTS
- [sentence-transformers](https://github.com/UKPLab/sentence-transformers) — Semantic matching
- [Porcupine](https://picovoice.ai/) by Picovoice — Wake word detection
- [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) by Resemble AI — Speaker identification
- [Playwright](https://playwright.dev/) — Web navigation

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

**Version:** 3.0.0
**Status:** Production — actively developed
**Last Updated:** February 27, 2026
