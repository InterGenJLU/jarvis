# Development Guide

## Git Repository Structure

JARVIS uses **four separate Git repositories**:

### 1. Core Repository: `~/jarvis`
**Tracks:** Core code, configuration, documentation, tests
```bash
cd ~/jarvis
git status
```

**Key directories:**
- `core/` — Main application code (32K+ lines)
- `core/tools/` — LLM tool definitions (auto-discovered)
- `scripts/` — Test suites and utilities
- `docs/` — Documentation
- `config.yaml` — Configuration (gitignored)

### 2. Skills Repository: `/mnt/storage/jarvis/skills`
**Tracks:** Skill implementations (symlinked into `~/jarvis/skills/`)
```bash
cd /mnt/storage/jarvis/skills
git status
```

**Contents:**
- `system/` — app_launcher, developer_tools, file_editor, filesystem, system_info, time_info, weather, web_navigation
- `personal/` — conversation (DISABLED), news, reminders, social_introductions

### 3. Models Repository: `/mnt/models`
**Tracks:** Training data, dataset metadata (NOT model files themselves)
```bash
cd /mnt/models
git status
```

Large model files (`.gguf`, `.bin`, `.onnx`) are excluded via `.gitignore`.

### 4. Public Repository: `~/jarvis-public`
**Tracks:** Redacted copy of main repo, published to GitHub
```bash
cd ~/jarvis-public
git status
```

Published via `./scripts/publish/publish.sh --auto` after each commit to main.

## Adding New Functionality

### Tools (Stateless, LLM-called)
Create one Python file in `core/tools/`. The tool registry auto-discovers it at import time. No wiring changes needed.

Required exports: `TOOL_NAME`, `SKILL_NAME`, `SCHEMA`, `SYSTEM_PROMPT_RULE`, and `handler(args)`.

### Skills (Stateful, Multi-turn)
Create a directory in `skills/system/` or `skills/personal/` with `skill.py` and `metadata.yaml`.

**Full guide:** `docs/SKILL_DEVELOPMENT.md` — covers both tools and skills with real examples.

## Running Tests

```bash
# Unit/routing tests (314 tests, no LLM needed for Tier 1-2)
python3 -m pytest tests/unit/ tests/integration/ tests/components/ --verbose

# Conversation tests (V3 suite, requires jarvis-web running)
python3 tests/v3_runner.py --verbose --save tests/v3_results/run_NNN

# Voice pipeline tests (25 TTS→STT round-trip tests)
python3 tests/components/test_voice_pipeline.py --verbose
```

**Important:** Never run tests in parallel — all test scripts hit llama-server sequentially.

## Entry Points

| Command | Mode | Notes |
|---------|------|-------|
| `python3 jarvis_continuous.py` | Voice | Wake word + mic, systemd service |
| `python3 jarvis_console.py` | Console | Text input, `--hybrid` for voice output |
| `python3 jarvis_web.py` | Web | Browser chat on port 8088/8443, systemd service |

## Why Multiple Repos?

- **Separation of concerns** — code vs data vs skills
- **Size management** — keep repos small, exclude model files
- **Independent backups** — can backup/restore each independently
- **Privacy** — public repo gets redacted copy only
