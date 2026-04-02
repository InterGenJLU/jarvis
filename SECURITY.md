# Security Policy

## JARVIS Privacy Model

JARVIS runs entirely on local hardware. LLM inference, speech recognition, and text-to-speech all execute on your GPU/CPU. No voice data or conversations are transmitted to external servers.

**Exception:** Web research and certain LLM tools may make outbound network requests when explicitly invoked by the user (e.g., "search the web for..."). These are user-initiated, not background telemetry.

## Reporting a Vulnerability

If you discover a security or privacy vulnerability in JARVIS, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, use [GitHub's private vulnerability reporting](https://github.com/InterGenJLU/jarvis/security/advisories/new) to submit a confidential report.

You can expect:
- Acknowledgment within 48 hours
- An assessment of severity and impact
- A fix or mitigation plan

## Scope

This policy covers:
- The JARVIS core system (routing, memory, LLM integration)
- Speech processing pipeline (STT, TTS)
- Tool calling and plugin system
- Web and mobile frontends
- Face enrollment and computer vision

It does **not** cover vulnerabilities in upstream dependencies (PyTorch, ROCm, llama.cpp, Whisper, etc.) — those should be reported to their respective projects.
