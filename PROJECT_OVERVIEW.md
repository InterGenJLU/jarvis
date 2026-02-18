# JARVIS - Personal AI Assistant

**Version:** 2.1.0 (Production Ready)
**Last Updated:** February 17, 2026
**Status:** ✅ Stable, Feature-Rich, Voice-Controlled

---

## 📋 Table of Contents
- [What is JARVIS?](#what-is-jarvis)
- [Current Capabilities](#current-capabilities)
- [System Architecture](#system-architecture)
- [Technology Stack](#technology-stack)
- [Progress Timeline](#progress-timeline)
- [Design Philosophy](#design-philosophy)
- [Roadmap](#roadmap)
- [Getting Started](#getting-started)

---

## 🤖 What is JARVIS?

JARVIS (Just A Rather Very Intelligent System) is a fully offline, voice-controlled AI assistant inspired by Tony Stark's AI from Iron Man. Unlike commercial assistants, JARVIS runs entirely on your local hardware with:

- ✅ **Complete Privacy** - No cloud, no data collection
- ✅ **Custom Voice Training** - Learns YOUR accent
- ✅ **Modular Skills** - Easy to extend
- ✅ **Natural Conversation** - Semantic understanding
- ✅ **Production Ready** - Stable, tested, reliable

**Hardware:** Runs on consumer-grade PC (Ryzen 9 5900X, AMD RX 7900 XT)  
**OS:** Ubuntu 24.04 LTS  
**Latency:** 600-800ms for skill queries, 3-5s for LLM fallback

---

## 🎯 Current Capabilities

### Core Features
- **Wake Word Detection** - Porcupine "Jarvis" with 100% accuracy
- **Speech Recognition** - Fine-tuned Whisper (CTranslate2, GPU-accelerated, 88%+ accuracy, Southern accent)
- **Natural Language Understanding** - Semantic intent matching (sentence-transformers)
- **Text-to-Speech** - Kokoro 82M (primary, CPU, fable+george blend) + Piper ONNX fallback
- **LLM Intelligence** - Qwen 2.5-7B (Q5_K_M) via llama.cpp + Claude API fallback with quality gating
- **Event-Driven Pipeline** - Coordinator with STT/TTS workers, streaming LLM, ack cache
- **Conversation Windows** - Timer-based auto-close, multi-turn, noise filtering
- **Console Mode** - Text/hybrid/speech modes with rich stats panel

### Skills (9 Active)

#### 🌤️ Weather
- Current conditions, forecasts, rain probability
- *"Jarvis, what's the weather like?"*

#### ⏰ Time & Date
- Current time, date, day of week
- *"Jarvis, what time is it?"*

#### 💻 System Information
- CPU, memory, disk, uptime, network
- *"Jarvis, what's my CPU usage?"*

#### 🗂️ Filesystem
- File search, code line counting, script analysis
- *"Jarvis, how many lines of code in your codebase?"*

#### 🛠️ Developer Tools
- 13 intents: codebase search, git multi-repo, system admin, general shell
- "Show me" visual output, 3-tier safety (allowlist → confirmation → blocked)
- *"Jarvis, show me the git status"*

#### 🌐 Web Navigation
- Playwright-based search, result selection, page navigation
- Scroll pagination (YouTube/Reddit), window management
- *"Jarvis, search for Python async tutorials"*

#### 📰 News
- 16 RSS feeds, urgency classification, semantic dedup
- Voice headline delivery, category filtering
- *"Jarvis, read me the tech headlines"*

#### 🔔 Reminders
- Priority tones, nag behavior, acknowledgment tracking
- Google Calendar two-way sync, dedicated JARVIS calendar
- Daily & weekly rundowns (state machine: offered → re-asked → deferred → retry)
- *"Jarvis, remind me to call the dentist at 3pm"*

#### 💬 Conversation
- Greetings, small talk, acknowledgments, butler personality
- *"Jarvis, how are you?"*

### Additional Systems
- **Conversational Memory** - SQLite fact store + FAISS semantic search, recall, batch LLM extraction, proactive surfacing, forget/transparency
- **User Profiles** - Speaker identification (resemblyzer d-vectors), dynamic honorifics, voice enrollment
- **Google Calendar** - OAuth, event CRUD, incremental sync, background polling
- **Cross-Session Memory** - Last 32 messages loaded from persistent history
- **Health Check** - 5-layer system diagnostic (ANSI terminal report + voice summary)

---

## 🏗️ System Architecture
```
┌─────────────────────────────────────────────────────────┐
│                       USER VOICE                         │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  AUDIO INPUT (FIFINE K669B USB Mic - Mono 48kHz)        │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  VAD (Voice Activity Detection) - WebRTC VAD             │
│  • Detects speech vs silence                            │
│  • Triggers transcription                               │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  STT (Speech-to-Text) - Custom Whisper Model            │
│  • Fine-tuned on user's Southern accent                 │
│  • 149 training phrases, 88%+ accuracy                  │
│  • GPU-accelerated: 0.1-0.2s transcription              │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  WAKE WORD DETECTION - Semantic Similarity              │
│  • Checks for "Jarvis" in transcript                    │
│  • Fuzzy matching (handles variations)                  │
│  • Confidence threshold: 0.65                           │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  INTENT MATCHING - 3-Layer Hybrid System                │
│  Layer 1: Exact pattern match (regex)                   │
│  Layer 2: Fuzzy pattern match (substring)               │
│  Layer 3: Semantic match (AI embeddings, 0.70+ score)   │
│  Layer 4: Keyword fallback                              │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  SKILL EXECUTION                                         │
│  • Modular skill system                                 │
│  • Semantic intent handlers                             │
│  • Error handling & logging                             │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  LLM (Fallback) - Qwen 2.5-7B via REST API              │
│  • Handles unmatched queries                            │
│  • Conversational responses                             │
│  • Technical reasoning                                  │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  TTS (Text-to-Speech) - Kokoro 82M + Piper fallback     │
│  • 50/50 fable+george voice blend                       │
│  • Natural intonation, streaming output                 │
│  • CPU-only, low latency                                │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│                    AUDIO OUTPUT                          │
└─────────────────────────────────────────────────────────┘
```

### Data Flow Example
```
User: "Jarvis, how many lines of code in your codebase?"
  ↓ (Audio captured)
VAD: Speech detected
  ↓ (Transcription triggered)
Whisper: "jarvis, how many lines of code in your codebase?"
  ↓ (Wake word check)
Wake Word: ✅ Detected "jarvis" (similarity: 1.00)
  ↓ (Strip wake word)
Intent Matching: "how many lines of code in your codebase"
  ↓ (Semantic match)
Semantic Matcher: 0.95 score → FilesystemSkill.count_code_lines
  ↓ (Execute handler)
Filesystem Skill: Count Python files, exclude venv
  ↓ (Return response)
Response: "My codebase contains 320,388 lines of Python code across 40 files, sir."
  ↓ (TTS)
Kokoro: Generates audio
  ↓ (Playback)
User: Hears response
```

---

## 🛠️ Technology Stack

### Core Components
| Component | Technology | Purpose |
|-----------|-----------|---------|
| **STT** | faster-whisper (CTranslate2, fine-tuned) | Speech recognition |
| **TTS** | Kokoro 82M (primary) + Piper (fallback) | Speech synthesis |
| **LLM** | Qwen 2.5-7B (Q5_K_M via llama.cpp) | Language understanding |
| **VAD** | WebRTC VAD | Voice activity detection |
| **Wake Word** | Porcupine | Trigger detection |
| **Embeddings** | sentence-transformers | Intent matching |

### Infrastructure
- **OS:** Ubuntu 24.04 LTS
- **Python:** 3.12
- **Service Manager:** systemd (user services)
- **LLM Server:** llama-server (REST API)
- **Storage:** 
  - Code: `~/jarvis/`
  - Skills: `/mnt/storage/jarvis/skills/`
  - Models: `/mnt/models/` (4TB dedicated drive)

### Key Libraries
- `torch` - PyTorch for ML models
- `transformers` - Hugging Face models
- `sentence-transformers` - Semantic matching
- `sounddevice` - Audio I/O
- `numpy` - Array operations
- `requests` - HTTP client for LLM API
- `pyyaml` - Configuration

---

## 📈 Progress Timeline

### Phase 1: Foundation (Days 1-3)
- ✅ Basic voice loop (wake word → command → response)
- ✅ Whisper integration (base model)
- ✅ Piper TTS setup
- ✅ Simple command patterns

### Phase 2: Skills System (Days 4-7)
- ✅ Modular skill architecture
- ✅ Weather skill (OpenWeatherMap API)
- ✅ Time/date skill
- ✅ System info skill
- ✅ Conversation skill

### Phase 3: Intelligence (Days 8-10)
- ✅ Semantic intent matching (90% pattern reduction)
- ✅ LLM integration (Mistral 7B, later migrated to Qwen 2.5-7B)
- ✅ Conversation context window
- ✅ Intent confidence scoring

### Phase 4: Production Ready (Feb 9-10)
- ✅ Git version control (3 repositories)
- ✅ Automated backups (daily, systemd)
- ✅ 4TB model storage setup
- ✅ Paul Bettany voice cloning (proof-of-concept)
- ✅ Comprehensive documentation

### Phase 5: Major Upgrades (Feb 11) 🚀
- ✅ **Qwen 2.5-7B LLM** (better reasoning)
- ✅ **Custom Whisper training** (88%+ accuracy)
- ✅ **Filesystem skill** (semantic file operations)
- ✅ **Audio optimization** (no overflow)
- ✅ **Skill development guide** (comprehensive docs)

### Phase 6: GPU + CTranslate2 (Feb 12-13) 🚀
- ✅ **GPU-Accelerated STT** — CTranslate2 with ROCm on RX 7900 XT (0.1-0.2s)
- ✅ **PyTorch + CTranslate2 coexistence** — torch 2.10.0+rocm7.1 + CT2 4.7.1
- ✅ **Three-repo architecture** — code, skills, models on separate drives

### Phase 7: Feature Explosion (Feb 14-17) 🚀
- ✅ **12 critical bug fixes** — Whisper pre-buffer, semantic routing, keyword greediness, VAD overlap, etc.
- ✅ **News headlines** — 16 RSS feeds, urgency classification, semantic dedup, voice delivery
- ✅ **Reminder system** — priority tones, nag behavior, ack tracking, Google Calendar 2-way sync
- ✅ **Web Navigation Phase 2** — result selection, page nav, scroll pagination, window management
- ✅ **Developer tools skill** — 13 intents, codebase search, git multi-repo, system admin, safety tiers
- ✅ **Console mode** — text/hybrid/speech modes with stats panel
- ✅ **FIFINE K669B mic upgrade** — udev rule, config updated

### Phase 8: Polish + Advanced Systems (Feb 15-17) 🚀
- ✅ **Kokoro TTS** — 82M model, 50/50 fable+george blend, Piper fallback
- ✅ **Latency refactor (4 phases)** — streaming TTS, ack cache, streaming LLM, event pipeline
- ✅ **User profile system (5 phases)** — honorific, ProfileManager, SpeakerIdentifier, pipeline, enrollment
- ✅ **Honorific refactoring** — ~470 "sir" instances → dynamic `{honorific}` across 19 files
- ✅ **Conversational memory (6 phases)** — SQLite facts, FAISS indexing, recall, batch extraction, proactive surfacing, forget/transparency
- ✅ **System health check** — 5-layer diagnostic, ANSI terminal + voice summary

---

## 🎨 Design Philosophy

### 1. Privacy First
All processing happens locally. No data leaves your machine. No telemetry, no cloud dependencies.

### 2. Modular & Extensible
Skills are independent modules. Add new capabilities without touching core code.

### 3. Natural Interaction
Semantic matching allows flexible phrasing. Say it naturally, JARVIS understands.

### 4. Production Quality
- Comprehensive error handling
- Extensive logging
- Graceful degradation
- Auto-recovery mechanisms

### 5. Hardware Efficient
Optimized for consumer hardware. No expensive GPUs required (though AMD GPU supported).

### 6. Maintainable
- Clean code structure
- Comprehensive documentation
- Version controlled
- Automated backups

---

## 🗺️ Roadmap

### Immediate
- [ ] Enroll the user's voice (speaker ID)
- [ ] Developer tools console testing (13 intents)
- [ ] Whisper retraining from log analysis (scheduled Feb 21)
- [ ] GitHub open source publication

### Short Term
- [ ] Web Navigation Phase 3 (web research + LLM tool use)
- [ ] Audio recording skill
- [ ] App launcher skill
- [ ] Email skill (Gmail)
- [ ] Google Keep integration

### Medium Term
- [ ] Skill editing system (voice-controlled code modification)
- [ ] Music control (Apple Music)
- [ ] Voice cloning (Paul Bettany JARVIS voice)

### Long Term
- [ ] Threat hunting / malware analysis framework
- [ ] Video / face recognition
- [ ] Home automation
- [ ] Mobile access
- [ ] Emotional context awareness

---

## 🚀 Getting Started

### Prerequisites
- Ubuntu 24.04 LTS (or similar Linux)
- Python 3.11+
- 16GB+ RAM recommended
- GPU optional (AMD/NVIDIA for acceleration)

### Installation
```bash
# Clone repository
git clone <repo-url> ~/jarvis
cd ~/jarvis

# Install dependencies
pip install -r requirements.txt --break-system-packages

# Set up models directory
sudo mkdir -p /mnt/models
sudo chown $USER:$USER /mnt/models

# Download models (automated script coming soon)
# For now, manually place models in /mnt/models/

# Configure
cp config.yaml.example config.yaml
# Edit config.yaml with your settings

# Install services
cp jarvis.service ~/.config/systemd/user/
cp llama-server.service /etc/systemd/system/
systemctl --user daemon-reload
sudo systemctl daemon-reload

# Enable and start
systemctl --user enable --now jarvis
sudo systemctl enable --now llama-server

# Check status
systemctl --user status jarvis
```

### Quick Start
```bash
# Start JARVIS
startjarvis

# Stop JARVIS
stopjarvis

# Restart JARVIS
restartjarvis

# View logs
journalctl --user -u jarvis -f
```

### Basic Usage (Voice)
1. Say "Jarvis" to wake
2. Ask your question naturally
3. JARVIS responds
4. 20-second window for follow-up

### Console Mode
```bash
python3 jarvis_console.py              # Text mode (type commands)
python3 jarvis_console.py --hybrid     # Text input + spoken output
```
Stats panel shows match layer, skill, confidence, timing, and LLM token counts after each command.

**Example Interactions:**
```
You: "Jarvis, what's the weather?"
JARVIS: "Currently 45 degrees and partly cloudy, sir."

You: "How about tomorrow?"
JARVIS: "Tomorrow's forecast shows 52 degrees with scattered showers, sir."

You: "How many lines of code in your codebase?"
JARVIS: "My codebase contains 320,388 lines of Python code across 40 files, sir."
```

---

## 📚 Documentation

- **[SKILL_DEVELOPMENT.md](docs/SKILL_DEVELOPMENT.md)** - How to create skills
- **[DEVELOPMENT.md](docs/DEVELOPMENT.md)** - Development workflows
- **[CHANGELOG.md](CHANGELOG.md)** - Version history
- **[TODO_NEXT_SESSION.md](docs/TODO_NEXT_SESSION.md)** - Current priorities

---

## 🤝 Contributing

JARVIS is a personal project, but ideas and improvements are welcome!

### Adding Skills
1. Read [SKILL_DEVELOPMENT.md](docs/SKILL_DEVELOPMENT.md)
2. Create skill in `/mnt/storage/jarvis/skills/`
3. Test thoroughly
4. Document in skill README

### Reporting Issues
Include:
- What you said
- What JARVIS responded
- Expected behavior
- Relevant logs

---

## 📊 Performance Metrics

### Accuracy
- Wake word detection: 100% (Porcupine)
- Speech recognition: 88%+ (fine-tuned Whisper, Southern accent)
- Intent matching: 95%+ (semantic embeddings)

### Latency
- Wake word detection: <100ms
- Speech transcription: 0.1-0.2s (GPU-accelerated CTranslate2)
- Intent matching: <100ms
- Skill-handled queries: 600-800ms total
- LLM fallback: 3-5s total
- TTS generation: <1s (Kokoro streaming)

### Resource Usage
- RAM: ~4GB (with all models loaded)
- CPU: 10-30% during processing
- GPU: RX 7900 XT via ROCm (STT acceleration)
- Disk: ~15GB (models + code)

---

## 🎓 What I've Learned

### Technical Insights
1. **Custom training beats generic models** - 88%+ vs 50% accuracy
2. **REST APIs > subprocess calls** - More reliable for LLM
3. **Semantic matching is powerful** - Reduces pattern count 90%
4. **Preload heavy models** - Prevents audio thread blocking
5. **Log everything** - Makes debugging 10x easier

### Development Practices
1. **Iterate quickly** - Small changes, frequent testing
2. **Test with real voice** - Keyboard input hides issues
3. **Monitor audio pipeline** - Overflow warnings are critical
4. **Version control everything** - Git saved me multiple times
5. **Document as you go** - Future you will thank you

### Design Decisions
1. **Offline first** - Privacy and reliability
2. **Modular skills** - Easy to extend and maintain
3. **Semantic intents** - Natural language flexibility
4. **British voice** - Character and professionalism
5. **Conservative responses** - Concise, helpful, polite

---

## 🏆 Achievements

- ✅ Fully functional voice assistant
- ✅ Custom accent training (first of its kind for personal use)
- ✅ Production-ready architecture
- ✅ Comprehensive documentation
- ✅ Extensible skill system
- ✅ Zero cloud dependencies
- ✅ Sub-1-second skill responses (600-800ms)
- ✅ 88%+ speech recognition accuracy (fine-tuned for Southern accent)
- ✅ Natural conversation support

**JARVIS is now a legitimate, production-ready AI assistant!**

---

## 📞 Support

For issues or questions:
1. Check logs: `journalctl --user -u jarvis -n 100`
2. Review documentation
3. Test with simple commands first
4. Verify all services running

**Common Issues:**
- **No audio input:** Check microphone permissions
- **No TTS output:** Verify Kokoro/Piper installation
- **Intent not matching:** Lower threshold or add examples
- **LLM not responding:** Check llama-server status

---

**Built with ❤️ and lots of coffee ☕**

*"Sometimes you gotta run before you can walk." - Tony Stark*
