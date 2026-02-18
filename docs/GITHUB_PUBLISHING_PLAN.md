# JARVIS GitHub Publishing Plan

**Status:** Pre-publication — sanitization + fresh repo needed
**Organization:** InterGenStudios
**License:** MIT (maximizes adoption)
**Name:** JARVIS 2.0 (keeping the name - Marvel can reach out if needed)
**Updated:** February 17, 2026

---

## Critical Blocker: API Keys in Git History

The current local repo has `.env` and `config.yaml` **tracked by git** since the initial commit. Three live API keys (Porcupine, Anthropic, OpenWeather) are baked into history.

**Required before publishing:**
1. **Rotate all 3 API keys** — regenerate in Picovoice, Anthropic, and OpenWeather consoles
2. **Create a fresh GitHub repo** — don't push local history (keys are in every commit)
3. **Untrack `.env` and `config.yaml`** — add to `.gitignore`, `git rm --cached`

A fresh repo is simpler and safer than BFG/filter-branch. Local history stays intact on the dev machine.

---

## Sanitization Audit (Feb 17)

### Already Clean
- [x] No hardcoded `/home/user` paths in Python code (all config-driven or `~/`)
- [x] No hardcoded API keys in Python code (all from `.env` via `os.getenv()`)
- [x] No hardcoded location data in Python code (all from config.yaml)
- [x] Voice profiles and conversation history already gitignored (`*.db`, `chat_history/`)

### Still Needs Work
- [ ] **Rotate + regenerate all 3 API keys** (Porcupine, Anthropic, OpenWeather)
- [ ] **Add `.env` and `config.yaml` to `.gitignore`** (currently tracked!)
- [ ] **Personal names in `scripts/init_profiles.py`** — hardcoded "primary_user" and "secondary_user" profiles; make configurable or document as example
- [ ] **Comment in `core/honorific.py`** — references "User" by name; make generic
- [ ] **One hardcoded path in config.yaml** — `/home/user/.local/bin/piper` should be `~/.local/bin/piper`
- [ ] **Your City, ST news feeds in config.yaml** — document as example, note customization needed
- [ ] **`/mnt/storage/` and `/mnt/models/` paths** — document storage layout for other users

---

## Pre-Publication Checklist

### 1. Create Template Files
- [ ] `.env.example` — all 3 API key names + ROCm env vars with placeholder values
- [ ] `config.example.yaml` — full structure with placeholder paths and comments
- [ ] `LICENSE` — MIT license file

### 2. Documentation (What Exists vs What's Needed)

**Already exists:**
- [x] `README.md` — current, accurate (Feb 16)
- [x] `docs/SKILL_DEVELOPMENT.md` — accurate
- [x] `docs/SEMANTIC_INTENT_MATCHING.md` — accurate
- [x] `docs/VOICE_TRAINING_GUIDE.md` — accurate
- [x] `docs/GPU_TROUBLESHOOTING.md` — accurate
- [x] `docs/SETUP_GUIDE.md` — accurate

**Needs creation:**
- [ ] `CONTRIBUTING.md` — contribution guidelines
- [ ] `docs/INSTALLATION.md` — detailed setup (models, ROCm, systemd, audio devices)
- [ ] `docs/API_KEYS.md` — where to get each key, free tier notes

**Nice to have (not blocking):**
- [ ] `setup.sh` — automated installation script
- [ ] `scripts/download_models.sh` — model download automation
- [ ] `docs/FAQ.md` — common questions

### 3. .gitignore Additions Needed

Current `.gitignore` is mostly good. Additions needed:
```gitignore
# Secrets (CRITICAL — currently tracked, must untrack)
.env
config.yaml

# Personal data
*.db
chat_history.jsonl
*.jsonl
```

### 4. Repository Structure (Actual)
```
jarvis/
├── README.md
├── LICENSE (MIT) ← needs creation
├── CHANGELOG.md
├── PROJECT_OVERVIEW.md
├── .gitignore
├── .env.example ← needs creation
├── config.example.yaml ← needs creation
├── requirements.txt
├── jarvis_continuous.py          # Production entry point (voice mode)
├── jarvis_console.py             # Console mode (text/hybrid/speech)
├── core/
│   ├── stt.py                    # Speech-to-text (CTranslate2 + faster-whisper)
│   ├── tts.py                    # Text-to-speech (Kokoro primary, Piper fallback)
│   ├── llm_router.py             # Qwen 2.5-7B + Claude API fallback
│   ├── skill_manager.py          # Skill loading + semantic matching
│   ├── semantic_matcher.py       # Sentence transformer matching
│   ├── continuous_listener.py    # VAD + wake word + conversation windows
│   ├── pipeline.py               # Event-driven coordinator
│   ├── conversation.py           # History, cross-session memory, follow-up
│   ├── memory_manager.py         # Conversational memory (FAISS + SQLite)
│   ├── reminder_manager.py       # Reminders, rundowns, Google Calendar sync
│   ├── google_calendar.py        # OAuth, event CRUD, incremental sync
│   ├── news_manager.py           # RSS monitoring, classification, voice delivery
│   ├── tts_normalizer.py         # Human-readable text conversion
│   ├── user_profile.py           # User profile management
│   ├── speaker_id.py             # Speaker identification (resemblyzer d-vectors)
│   ├── honorific.py              # Dynamic honorific system
│   └── health_check.py           # 5-layer system diagnostic
├── skills/ → /mnt/storage/jarvis/skills/
│   ├── system/
│   │   ├── time_info/
│   │   ├── weather/
│   │   ├── system_info/
│   │   ├── filesystem/
│   │   ├── web_navigation/
│   │   └── developer_tools/
│   └── personal/
│       ├── conversation/
│       ├── reminders/
│       └── news/
├── scripts/
│   ├── enroll_speaker.py         # Voice enrollment
│   ├── init_profiles.py          # User profile seeding
│   ├── backfill_memory.py        # Memory system backfill
│   └── query_reminders.py        # Reminder DB query tool
├── docs/
│   ├── SKILL_DEVELOPMENT.md
│   ├── SEMANTIC_INTENT_MATCHING.md
│   ├── VOICE_TRAINING_GUIDE.md
│   ├── GPU_TROUBLESHOOTING.md
│   ├── SETUP_GUIDE.md
│   └── GITHUB_PUBLISHING_PLAN.md (this file)
└── CONTRIBUTING.md ← needs creation
```

---

## Current Feature Set (What Gets Published)

### Core Architecture
- **STT:** Fine-tuned Whisper (CTranslate2, GPU-accelerated, 88%+ accuracy, Southern accent)
- **TTS:** Kokoro 82M (primary, CPU, 50/50 fable+george blend) + Piper ONNX fallback
- **LLM:** Qwen 2.5-7B (Q5_K_M via llama.cpp REST API) + Claude API fallback with quality gating
- **Wake Word:** Porcupine
- **VAD:** WebRTC VAD
- **Intent Matching:** Semantic (sentence-transformers all-MiniLM-L6-v2) + keyword + exact pattern layers
- **Pipeline:** Event-driven Coordinator with STT/TTS workers, streaming LLM, ack cache

### 9 Active Skills
1. **Time & Date** — current time, date, day of week
2. **Weather** — conditions, forecasts, rain probability (OpenWeather API)
3. **System Info** — CPU, memory, disk, uptime, network
4. **Filesystem** — file search, code line counting, script analysis
5. **Developer Tools** — 13 intents: codebase search, git multi-repo, system admin, general shell, "show me" visual output, 3-tier safety
6. **Conversation** — greetings, small talk, acknowledgments, butler personality
7. **Reminders** — priority tones, nag behavior, ack tracking, daily/weekly rundowns
8. **Web Navigation** — Playwright-based search, result selection, page nav, scroll pagination (YouTube/Reddit), window management
9. **News** — 16 RSS feeds, urgency classification, semantic dedup, voice headline delivery, category filtering

### Integrations
- **Google Calendar** — two-way sync, dedicated JARVIS calendar, OAuth, incremental sync, background polling
- **Conversational Memory** — SQLite fact store + FAISS semantic search, recall, batch LLM extraction, proactive surfacing, forget/transparency commands
- **User Profiles** — speaker identification via resemblyzer d-vectors, dynamic honorifics, voice enrollment
- **Console Mode** — text/hybrid/speech modes with rich stats panel

### Not Included (Future Roadmap)
These are designed but not built — listed in `docs/TODO_NEXT_SESSION.md`:
- Threat hunting / malware analysis framework
- Email skill (Gmail)
- Google Keep integration
- Music control
- Video / face recognition
- Skill editing system (voice-controlled code editing)
- App launcher
- Audio recording skill

---

## README.md Template

The current `README.md` (Feb 16) is accurate and ready for GitHub. It covers:
- Feature highlights, skills table, hardware specs, tech stack
- Quick start (systemd + console modes)
- Project structure, configuration, documentation links
- Backup system

**Only change needed:** Add link to `LICENSE` once created.

---

## API Keys Required

**Required:**
- `PORCUPINE_ACCESS_KEY` — Wake word detection (free tier at picovoice.ai)
- `OPENWEATHER_API_KEY` — Weather data (free tier at openweathermap.org)

**Optional:**
- `ANTHROPIC_API_KEY` — Claude API fallback for complex queries (anthropic.com)

**Environment (non-secret):**
- `HSA_OVERRIDE_GFX_VERSION=11.0.0` — ROCm GPU override
- `ROCM_PATH=/opt/rocm-7.2.0` — ROCm installation path
- `LD_LIBRARY_PATH=/opt/rocm-7.2.0/lib` — ROCm libraries

**Google Calendar** uses OAuth (separate `credentials.json` from Google Cloud Console, gitignored).

---

## Post-Publication Maintenance

**Monthly tasks:**
- Review issues and pull requests
- Update documentation for new features
- Ensure example configs stay current
- Test installation instructions on clean system

**After major features:**
- Update README with new capabilities
- Update CHANGELOG
- Consider demo video or blog post

---

## Publication Steps (In Order)

1. Complete all sanitization checklist items above
2. Create `.env.example`, `config.example.yaml`, `LICENSE`, `CONTRIBUTING.md`
3. Write `docs/INSTALLATION.md` and `docs/API_KEYS.md`
4. **Rotate all 3 API keys** in their respective consoles
5. Add `.env` and `config.yaml` to `.gitignore`
6. `git rm --cached .env config.yaml` to untrack
7. Create fresh GitHub repo under InterGenStudios
8. Push clean working tree (not local history) to GitHub
9. Verify nothing sensitive in the published repo
10. Update README documentation links to point to GitHub

---

## Notes

- Community contributions could accelerate development
- Portfolio piece showcases AI/security/Linux expertise
- Could become valuable for job opportunities
- Minimal sanitization = easier to maintain, faster to publish
- MIT license encourages adoption and derivatives
- Keep Marvel's attention as badge of honor if they notice

**Blocker:** API key rotation + fresh repo creation
**Owner:** the user (with Claude's assistance)
