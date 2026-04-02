# Contributing to JARVIS

JARVIS is a complex system — ~66,000 lines across ~40 modules, with tight integration between LLM inference, speech recognition, text-to-speech, computer vision, and tool calling. Contributions are welcome, but please read this first.

## Before You Start

1. **Open an issue first** — describe what you want to change and why
2. **Check the architecture** — JARVIS has an 18-layer routing chain, domain synthesis, and a plugin system. Changes in one area can ripple
3. **Run the tests** — 314 unit tests and 62 conversation behavioral tests must pass

## What We're Looking For

**High-value contributions:**
- New LLM tool plugins (one-file, auto-discovered)
- STT/TTS performance improvements
- ROCm compatibility fixes for different AMD hardware
- VRAM optimization techniques
- Bug reports with reproduction steps and logs

**Please discuss first:**
- Changes to the routing chain or domain classifier
- New LLM model integrations
- Frontend changes (voice, web, mobile)
- Memory system modifications

## Development Setup

- **Hardware:** AMD GPU with ROCm support (RX 7900 XT tested, others may work)
- **OS:** Ubuntu 24.04
- **Python:** 3.12
- **ROCm:** 7.2
- **VRAM:** 16GB+ recommended (20GB for full Qwen3.5-35B-A3B)

See the README for detailed setup instructions.

## Testing

```bash
# Unit tests (all 4 tiers)
python -m pytest tests/ -v

# Conversation behavioral suite
python -m pytest tests/conversations/ -v
```

All tests must pass before submitting a PR. If you add a new feature, add tests for it.

## Code Style

- Follow existing patterns — the codebase is consistent
- Docstrings for public functions
- Type hints where practical
- No unnecessary dependencies — JARVIS runs on a tight VRAM budget

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
