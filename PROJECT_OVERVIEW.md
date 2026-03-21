# JARVIS — Project Overview

**Version:** 6.0.0
**Last Updated:** March 21, 2026
**Status:** Production — running 24/7 as systemd services

---

## What Is This?

JARVIS is a voice assistant built on two cooperating local LLMs, a 6-phase conversational awareness system, and face + voice identity fusion — all running on consumer AMD hardware. No cloud APIs in the critical path. The 35B-parameter model reasons and calls tools. The 4B model synthesizes results, generates contextual acknowledgments, and composes natural-language briefings. If either model fails, the other takes over transparently.

It started as a wake-word-to-response loop in February 2026. Six weeks and ~66,000 lines of Python later, it has an 18-layer conversation router, 11 LLM tools with 100% calling accuracy, a self-managing memory system, computer vision, a conversational awareness layer that proactively briefs you on what you need to know, and three frontends (voice, console, web) all sharing the same routing engine.

**Hardware:** Ryzen 9 5900X, 64GB RAM, RX 7900 XT (20GB, 35B LLM) + RX 7600 (8GB, 4B LLM + Whisper + embeddings), FIFINE K669B mic
**Stack:** Python 3.12, ROCm 7.2, llama.cpp (rocWMMA flash attention), CTranslate2, Kokoro TTS, faster-whisper, FAISS, SpeechBrain, InsightFace, Silero VAD, aiohttp

---

## Dual-Model Architecture

Two Qwen3.5 models run simultaneously on separate GPUs:

```
                    User speaks
                        │
            ┌───────────▼───────────┐
            │   RX 7600 (8GB)       │
            │   Whisper STT         │
            │   Speaker ID (ECAPA)  │
            │   Embeddings (nomic)  │
            │   Qwen3.5-4B (8081)   │
            └───────────┬───────────┘
                        │ text + identity
            ┌───────────▼───────────┐
            │   ConversationRouter  │
            │   18-layer priority   │
            └───┬─────────────┬─────┘
                │             │
    ┌───────────▼──┐   ┌──────▼──────────┐
    │  Fast paths  │   │  RX 7900 XT     │
    │  CAL-L0      │   │  (20GB)         │
    │  Skills      │   │  Qwen3.5-35B    │
    │  (<50ms)     │   │  (port 8080)    │
    └───────────┬──┘   │  Tool selection │
                │      │  Reasoning      │
                │      └──────┬──────────┘
                │             │ tool result
                │      ┌──────▼──────────┐
                │      │  RX 7600        │
                │      │  Qwen3.5-4B     │
                │      │  Synthesis      │
                │      │  (60% faster)   │
                │      └──────┬──────────┘
                │             │
            ┌───▼─────────────▼─────┐
            │  Persona + TTS        │
            │  Kokoro → aplay       │
            └───────────────────────┘
```

**How it works:**
- The **35B** (MoE, ~3B active per token, 19.5GB VRAM) handles reasoning, tool selection, and complex conversation
- The **4B** (Q4_K_M, 2.6GB VRAM) handles three tasks:
  1. **Tool result synthesis** — converts structured tool data into natural speech (60% TTFT reduction vs 35B)
  2. **Contextual acknowledgments** — generates butler-like acks in ~600ms ("Let me look into those buffer overflow CVEs for you, sir") while the 35B is still routing
  3. **Briefing composition** — weaves awareness items into natural spoken briefings
- **Transparent fallback** — if the 4B fails (HTTP error, connection refused, quality issue), the 35B handles it. The user never knows which model answered
- **Call chain tracking** — every pipeline run records which model handled each phase (routing, synthesis) with TTFT per model

### Focused Synthesis Prompt

The 4B doesn't receive the same bloated system prompt as the 35B. Tool-calling rules, web search guidelines, and tool schemas are stripped — the 4B gets `persona.system_prompt_brief()` (identity + honorific + anti-filler rules only). This cuts ~1,000 tokens of prefill and is the primary driver of the 60% TTFT reduction.

---

## Conversational Awareness Layer (CAL)

A 6-phase system that transforms JARVIS from reactive command-response into a contextually aware conversational participant.

### Phase 1: Greeting Flow Foundation

When the presence detector identifies a user via face recognition, it:
1. Sets `conversation.current_user` (face ID is authoritative)
2. Tags the conversation window as `"presence_greeting"` or `"presence_return"`
3. Opens an 8-second response window

When the user responds with a greeting, CAL-L0 absorbs it silently (no double-greeting) and triggers the briefing pipeline.

### Phase 2: Awareness Accumulator

An always-on priority queue that pulls from four data sources via thin adapters:

| Source | Adapter | What It Surfaces |
|--------|---------|-----------------|
| **Calendar** | `adapt_calendar()` | Events within 4h (morning) / 2h (afternoon). Queries BOTH primary + JARVIS calendars. All-day events pre-naturalized ("Today is Malikai's Birthday") |
| **Weather** | `adapt_weather()` | Active NWS alerts with severity-based urgency (extreme=1.0, severe=0.9, moderate=0.7) |
| **Reminders** | `adapt_reminders()` | Pending acks + upcoming within 2h |
| **News** | `adapt_news()` | Critical/high priority unread headlines (priority 1-2 only) |

**Scoring formula (deterministic, no LLM):**
```
score = (urgency × 0.3) + (time_pressure × 0.3) + (novelty × 0.2) + (user_relevance × 0.2)
```

**Delivery log:** SQLite dedup with 24h TTL prevents re-surfacing the same item.

### Phase 3: Briefing Composer

The 4B model synthesizes ranked awareness items into natural butler-style speech.

**Split prompt architecture:**
- **Single item (12-word cap, 16 max_tokens):** Tight, focused — no room for hallucination. "Malikai's birthday is today, sir."
- **Multi-item (35-word cap, 60 max_tokens):** Room to weave and connect. "Your open meeting in two hours, and today marks Malikai's birthday, sir."

**Prompt engineering lessons (hard-won):**
- Qwen treats prompt examples as output content — specific words in examples WILL appear in generation. Use generic placeholders.
- Token budget is the mechanical enforcer, word cap is the intent signal — use BOTH.
- BANNED word list for negative reinforcement (explain, delve, comprehensive, fascinating, compile).
- User identity flows through: face ID → `current_user` → `user_name` in prompt → "your" substitution for the user's own items.

### Phase 4: Adapters

News and reminder adapters extend the Accumulator with additional data sources. News filters to critical/high priority only (urgency: critical=0.85, high=0.5). Reminders surface pending acks and upcoming events within 2 hours.

### Phase 5: Moment Expansion

Five trigger types with different budgets:

| Moment | Budget | Threshold | Trigger |
|--------|--------|-----------|---------|
| Morning greeting | 3 items | 0.3 | Face detected, user responds with greeting |
| Return from absence | 2 items | 0.4 | Face detected after >30 min absence |
| "Catch me up" | 5 items | 0.1 | User explicitly asks (10 keyword phrases) |
| Post-task nudge | 1 item | 0.6 | After substantive task completes (not conversational exchanges) |
| Ambient awareness | 1 item | 0.85 | User PRESENT, conversation idle, 60s cooldown — critical/safety only |

### Phase 6: Ambient Awareness

Critical items (score >= 0.85) spoken unprompted when the user is present and not in active conversation. Only extreme/severe weather alerts and critical news qualify. Fires in the presence detector's 10-second poll cycle with a 60-second cooldown between deliveries.

---

## Face + Voice Identity Fusion

Two biometric systems cooperate with a clear authority model:

| System | Model | Accuracy | Role |
|--------|-------|----------|------|
| **Face ID** | InsightFace ArcFace (512-dim) | 99.83% LFW | **Authoritative** — sets identity at detection time |
| **Speaker ID** | SpeechBrain ECAPA-TDNN (192-dim) | 0.80% EER | **Confirmatory** — can upgrade to identified, never downgrades to guest |

**Flow:** Face detection → `current_user` set → greeting fired → user speaks → speaker ID runs → if speaker ID matches: confirmed. If speaker ID misses: face ID holds (no guest mode override).

**Enrollment:** Multi-clip enrollment (5 clips × 5 seconds) with `scipy.signal.resample_poly` for bandlimited resampling. Production audio path uses identical resampling — mismatched resampling methods destroy embedding consistency.

---

## Contextual Acknowledgments

Instead of generic cached phrases ("Let me check"), the 4B generates contextual acks in ~600ms:

```
User: "Search for recent buffer overflow CVEs"

Generic ack:    "Let me check."
Contextual ack: "Let me search for the recent buffer overflow CVEs, sir."
```

**How it works:**
1. Command arrives → `_classify_ack()` determines if an ack is needed
2. If yes, fire 4B generation in a background thread immediately (parallel with routing)
3. 1.5-second timer fires → check if contextual ack is ready
4. If ready → speak it. If not → fall back to generic cached ack
5. Zero added latency — the 4B runs in parallel, not in series

---

## 18-Layer Conversation Router

Every request — voice, console, or web — enters `ConversationRouter.route()`. One router, three frontends.

- **CAL-L0 (P2.9)** — Reflexive layer: 13 categories, ~200 patterns, <50ms. Greetings, farewells, thanks, compliments, small talk, meta-questions. Defers to CAL briefing during presence windows.
- **P0-P2.8** — Deterministic fast paths: delivery modes, rundown acceptance, task planner control, reminder acks, memory forget, face enrollment, self-identification → memory, introduction state machine, dismissals, bare ack filtering.
- **P3-P3.7** — Memory ops, structured readback, artifact references, news pull-up.
- **Pre-P4** — Compound detection (22 regex patterns) → task planner → LLM plan generation → sequential execution with voice interrupts.
- **P4-LLM** — Semantic pruner selects top 4 tools → 35B decides → tool executes → 4B synthesizes → 17-domain classifier → domain-specific anti-hallucination prompt.
- **P4-Skill** — Stateful skills via 5-layer semantic intent matching.
- **Fallback** — Pure 35B streaming with quality gating → Claude API last resort.

---

## The Numbers

| Metric | Value | Notes |
|--------|-------|-------|
| Tool calling accuracy | 100% | 1,200+ trials, 10-category taxonomy |
| LLMs | 2 | Qwen3.5-35B (reasoning) + Qwen3.5-4B (synthesis) |
| LLM tools | 11 | Auto-discovered one-file plugin system |
| Skills | 14 | 3 stateful + 8 with companion tools + 3 internal |
| CAL-L0 patterns | 13 categories, ~200 | <50ms reflexive responses |
| Unit tests | 314/314 | tests/{unit,integration,memory,components} |
| Conversation tests | 52 conversations | V3 suite with dual-model tracking |
| STT accuracy | 94%+ | Fine-tuned Whisper, 198 phrases |
| STT latency | 0.1-0.2s | CTranslate2 on GPU |
| LLM throughput | 48-63 tok/s | 35B Q3_K_M, full GPU offload |
| 4B synthesis TTFT | 4-5s | 60% reduction from focused prompt |
| TTS cache | 281 phrases | 11ms load, zero-latency on hits |
| Persona templates | 38 pools, ~184 | Style-tagged, honorific-injected |
| Domain classifiers | 17 domains | 14 anti-hallucination synthesis prompts |
| Codebase | ~66,000 lines | ~40 modules + 14 skills + test suites |

---

## Latency Budget

| Path | Time | What Happens |
|------|------|-------------|
| **CAL-L0 / Skill** | <500ms | Pattern match → cached TTS → aplay |
| **Knowledge (35B)** | 2-4s | Router → 35B stream → Kokoro → aplay |
| **Tool call (4B synth)** | 3-8s | Router → 35B tool selection → tool exec → 4B synthesis → Kokoro |
| **Web search** | 5-10s | Router → 35B → Serper API → page fetch → 4B synthesis → Kokoro |
| **CAL briefing** | ~2s | Accumulator query → 4B compose → Kokoro (items precomputed at detection) |
| **Contextual ack** | ~600ms | 4B generates in parallel with routing (zero added latency) |
| **TTS cache hit** | <5ms | Pre-generated PCM → aplay (no Kokoro) |

---

## Development Timeline

| Period | What Happened |
|--------|-------------|
| **Feb 9-13** | Foundation: voice loop, Whisper, Piper TTS, basic skills, GPU CTranslate2 on ROCm |
| **Feb 14-17** | Feature explosion: news, reminders, web nav, developer tools, Kokoro TTS, streaming, profiles, memory |
| **Feb 18-21** | Web research, prompt design, GNOME desktop, web UI, file editor, Whisper fine-tuning (94%+) |
| **Feb 22-25** | Doc gen, Qwen3.5-35B-A3B upgrade, task planner, social introductions |
| **Feb 26-28** | LLM-centric migration (1,200+ trials, 100%), dual GPU, ctx-size 32768 |
| **Mar 1-3** | MCP bridge, artifact cache (5 phases), self-managing memory (CMA 6/6) |
| **Mar 4-7** | Vision (7 phases), 180 tests, presence detection, face recognition |
| **Mar 7-11** | V3 test suite (52 conversations), 17-domain classifier, 314 unit tests |
| **Mar 12-17** | Component upgrade wave: SpeechBrain ECAPA-TDNN, InsightFace ArcFace, nomic embeddings, Silero VAD |
| **Mar 18-19** | Latency Phase 1: tool gate, CAL-L0, TTSCache (11ms), KV cache reuse. ROCm stack rebuild (PyTorch from source). Qwen3.5-4B deployed |
| **Mar 19-21** | Dual-model dispatch (60% TTFT), CAL all 6 phases, contextual acks, FAISS rebuild, test consolidation, speaker ID resampling fix, watchdog fix, doc overhaul |

---

## Design Principles

1. **Local first** — Claude API is a quality fallback that fires <1% of the time. Everything else runs on-box.
2. **Two models cooperate** — The 35B reasons, the 4B synthesizes. Each does what it's best at.
3. **Face ID is authoritative** — Speaker ID confirms but never overrides. The more accurate system wins.
4. **Stream everything** — LLM tokens → sentence chunker → Kokoro → aplay. No buffering full responses.
5. **One router, three frontends** — Voice, console, web share the same 18-layer priority chain.
6. **One file, one tool** — Drop a `.py` in `core/tools/` and it's live. No wiring needed.
7. **Degrade gracefully** — 4B fails → 35B. GPU fails → CPU. Kokoro fails → Piper. Qwen fails → Claude.
8. **The butler model** — Greet, brief, then be quiet. Silence IS a valid response when there's nothing to say.
9. **Prompt examples are output** — Qwen treats every word in a prompt example as a candidate for generation. Design prompts accordingly.

---

## Getting Started

See the [README](README.md) for installation, model downloads, the [AMD ROCm Build Guide](README.md#amd-rocm-build-guide), and configuration reference.

```bash
# Console mode (no mic needed)
python3 jarvis_console.py

# Voice mode
systemctl --user start jarvis

# Web UI
python3 jarvis_web.py
# Open http://127.0.0.1:8088
```

---

*Built iteratively. Tested obsessively. Improved daily.*
