# Development Guide

## Git Repository Structure

JARVIS uses **three separate Git repositories** for organization:

### 1. Core Repository: `~/jarvis`
**Tracks:** Core code, configuration, documentation
```bash
cd ~/jarvis
git status
```

**Contents:**
- `core/` - Main application code
- `tools/` - Utility scripts
- `docs/` - Documentation
- `config.yaml` - Configuration
- `README.md`, `CHANGELOG.md`

### 2. Skills Repository: `/mnt/storage/jarvis/skills`
**Tracks:** Skill implementations
```bash
cd /mnt/storage/jarvis/skills
git status
```

**Contents:**
- `system/` - System skills
- `personal/` - Personal skills
- Individual skill directories with `skill.py`, `metadata.yaml`

### 3. Models Repository: `/mnt/models`
**Tracks:** Training data, dataset metadata (NOT models themselves)
```bash
cd /mnt/models
git status
```

**Contents:**
- `voice_training/` - Training datasets, scripts
- `.gitignore` - Excludes large model files

**Note:** Large model files (`.gguf`, `.bin`, `.onnx`) are NOT tracked by git.

## Making Changes

**Core code changes:**
```bash
cd ~/jarvis
# Make changes
git add .
git commit -m "Description"
```

**Skill changes:**
```bash
cd /mnt/storage/jarvis/skills
# Make changes
git add .
git commit -m "Description"
```

**Training data changes:**
```bash
cd /mnt/models
# Add training phrases, datasets
git add voice_training/
git commit -m "Description"
```

## Why Multiple Repos?

- **Separation of concerns** - Code vs data vs skills
- **Easier backups** - Can backup/restore independently
- **Size management** - Keep repos small, exclude huge model files
- **Flexibility** - Can share skills repo without sharing personal training data

