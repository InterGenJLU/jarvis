# JARVIS Development Vision — LLM-Centric Architecture

**Created:** February 18, 2026
**Updated:** February 27, 2026
**Context:** Qwen3.5-35B-A3B in production with 7 LLM tools (Phases 1-2 COMPLETE, get_time removed), tool-connector plugin system live

---

## Background

JARVIS was built with hard-coded skill handlers because early development focused on reliability over LLM flexibility. The skill system (semantic routing, keyword matching, priority layers) worked but required significant maintenance — greedy keyword bugs, threshold tuning, priority conflicts, and per-skill handler code.

With Qwen3.5's native tool calling proven at 100% accuracy across 1,200+ trials, the project has successfully migrated to an LLM-centric approach where the model acts as an **agent with tools** rather than a dispatcher to pre-built handlers.

**Migration progress (Feb 26-27):**
- **Phase 1 COMPLETE** — time, system_info, filesystem as tools. 100% accuracy (600/600 trials). Commit `06dd741`
- **Phase 2 COMPLETE** — weather, reminders, conversation (disabled — LLM handles natively), developer_tools, news. 7 tools total (6 domain + web_search; get_time removed — time/date handled by TimeInfoSkill). 100% accuracy on domain categories; 99.6% overall (523/525, 2 stochastic conversation borderlines). Commits: `1be0cb1` → `49eca5c` → `aa2f524` → `a6ae616` → `578e3c9`
- **Tool-connector plugin system** — one-file tool definitions, auto-discovery registry, dependency injection. Adding a new tool = create one file in `core/tools/`. Commit `ba80e5a`
- **5-6 tool cliff DEBUNKED** — tested with up to 8 tools, zero XML fallback, 1,200+ trials. Tool description quality + semantic domain separation matter more than raw count. Now at 7 tools after get_time removal
- **Vision unblocked** — mmproj-F16.gguf (~900MB) available, llama.cpp support merged. Waiting for RX 7600 display offload (arrives Feb 28)

---

## Core Principle

**Skills became tools, not destinations.** This is now implemented.

The original skill handlers weren't wasted work — they became the tool execution layer the LLM calls into. The migration path was:

1. Convert skill handlers into **tool definitions** (OpenAI-compatible function schemas + handlers) — DONE
2. Let the LLM decide which tools to call — DONE (P4-LLM routing with semantic pruning)
3. Keep the skill infrastructure as the **execution layer** — DONE (tool-connector plugin system)

The tool-connector plugin system (`core/tool_registry.py` + `core/tools/*.py`) makes adding new tools trivial: create one Python file with `TOOL_NAME`, `SKILL_NAME`, `SCHEMA`, `SYSTEM_PROMPT_RULE`, and `handler(args)`. The registry auto-discovers it at import time.

---

## Routing Architecture — Current State

### Production Architecture (after Phase 2 migration)
```
P1:       Rundown acceptance/deferral
P2:       Reminder acknowledgment
P2.5:     Memory forget confirmation
P2.6:     Intro state machine (multi-turn social introductions)
P2.7:     Dismissal detection (in conversation)
P2.8:     Bare acknowledgment filter (in conversation)
P3:       Memory operations (forget, recall, transparency)
P3.5:     Research follow-up (in conversation)
P3.7:     News article pull-up
Pre-P4:   Multi-step task planning (compound detection)
Pre-P4b:  Self-hardware queries ("you/your" + hw keyword → direct LLM answer)
Pre-P4c:  Pending skill confirmations
P4-LLM:   ★ TOOL CALLING — semantic pruning → LLM with 7 tools (PRIMARY PATH)
P4:       Skill routing — stateful skills (app_launcher, file_editor, social_intros)
P5:       News continuation
Fallback:  LLM streaming with tools (Qwen3.5 → quality gate → Claude API)
```

**P4-LLM is now the primary routing path** for most queries. The semantic pruner (threshold 0.40, hard cap 4 domain tools) selects relevant tools, then Qwen3.5 decides which to call via `stream_with_tools()`. 7 tools: get_system_info, find_files, get_weather, manage_reminders, developer_tools, get_news, web_search (always included). Time/date queries are handled by the TimeInfoSkill (instant response via semantic matching, no LLM needed).

P4 skill routing handles 3 stateful skills that require deterministic routing: app_launcher (desktop verbs), file_editor (nested LLM doc gen + confirmation), social_introductions (multi-turn state machine). A skill guard in the pruner ensures these still route correctly.

### Future Target Architecture (Phase 4)
```
Layer 1: Stateful fast-paths (reminders, memory, intros — deterministic state machines)
Layer 2: LLM agent with tools (everything else)
```

Phase 4 will evaluate whether the remaining stateful priorities (P1-P3.7) can be simplified or whether the LLM can handle them directly. The semantic matcher and keyword routing may become fully unnecessary once all skills are migrated.

---

## Migration Plan — Incremental, Skill by Skill

### Phase 1: Low-Stakes Skills — COMPLETE (Feb 26)

**3 skills migrated:** time, system_info, filesystem

- **Results:** 100% accuracy (600/600 trials), 822ms avg tool-calling latency, 266/266 edge case tests pass
- **Architecture:** P4-LLM routing with semantic pruning (threshold 0.40), `stream_with_tools()` with `tool_choice=auto`, prescriptive system prompt rules
- **Key finding:** Prescriptive numbered RULES format reliable for Qwen3.5. `presence_penalty=0.0` and `temperature=0.0` optimal for tool selection
- **Commit:** `06dd741`

### Phase 2: API-Backed + Complex Skills — COMPLETE (Feb 27)

**5 sub-phases completed:**

| Sub-phase | What | Result | Commit |
|-----------|------|--------|--------|
| 2.1a | Weather as tool | 100% (270/270) | `1be0cb1` |
| 2.1b | Reminders as tool (5 actions: add/list/cancel/ack/snooze) | 100% (1,095 cumulative) | `49eca5c` |
| 2.2 | Conversation skill DISABLED — LLM handles natively | 266/266 edge cases, 99.3% tool-calling | `aa2f524` |
| 2.3 | Developer tools (13 actions: git, shell, codebase search) | 160/160 (100%) | `a6ae616` |
| 2.4 | News headlines (read/count, category/priority filters) | 150/150 news, 523/525 overall (99.6%) | `578e3c9` |

**Tool-connector plugin system** (built after Phase 2.4):
- One-file tool definitions in `core/tools/` with auto-discovery registry (`core/tool_registry.py`)
- `tool_executor.py` reduced from 1,057 to 27 lines (backward-compat shim)
- Adding a new tool = create one Python file. Zero wiring changes needed
- **Commit:** `ba80e5a`

**Key findings from Phase 2:**
- 5-6 tool cliff is DEBUNKED — tested with up to 8 tools, 1,200+ trials, zero XML fallback (now 7 tools after get_time removal)
- Tool description quality + semantic domain separation matter more than raw count
- Tool-calling latency is LLM-bound (~2.5s total: 1s tool decision + 1.5s response generation), not pipeline-bound
- Conversation history in tool-calling messages causes over-triggering — keep messages array clean (system + user only)
- Skill guard essential: pruner checks all skills, defers to skill routing when a stateful skill scores higher

**Stateful skills (remain as skills — correct abstraction):**
- **app_launcher** — desktop verbs (launch, close, fullscreen) need direct desktop integration
- **file_editor** — nested LLM calls for document generation + destructive confirmation state machine
- **social_introductions** — multi-turn state machine (may revisit: split data ops as tool, let LLM drive conversation)

### Phase 3: Vision-Enabled (Unblocked — Native in Qwen3.5) — NEXT

Vision is NOT a future dependency — it's available now. Qwen3.5's early-fusion multimodal architecture means the model already running in production can process images when the mmproj file is loaded.

**Web Navigation with Vision**
- Currently uses per-site CSS selectors and structured scraping
- With mmproj loaded: screenshot the page, let the LLM see it, decide what to click
- Replaces brittle CSS selectors with visual understanding
- VRAM cost: +900MB when active (requires display offload or dynamic loading)

**Screen Reading / OCR**
- "What does this say?" → screenshot active window → LLM describes content
- "Read this chart" → screenshot → structured data extraction
- Tesseract as fast CPU fallback for simple text extraction (~1-3s, zero VRAM)
- Full VLM path via mmproj for complex images (~3-6s with model cached)

**IoT Camera Integration (Future)**
- Security camera feeds processed by the same model handling conversation
- "Is anyone at the front door?" — LLM sees the camera frame directly
- No separate vision pipeline needed — same mmproj handles all image tasks

### Phase 4: Routing Layer Evaluation

After Phase 3 is stable, evaluate:
- Can the semantic matcher be removed entirely? (Currently only used for 3 non-migrated skills)
- Can keyword routing be reduced to just the fast-paths?
- Can stateful priorities (P1-P3.7) be simplified or LLM-driven?
- What's the latency impact of routing everything through the LLM? (Phase 2 baseline: ~2.5s for tool-calling queries vs <1s for skill routing — acceptable for most use cases)

---

## What Must Stay Hard-Coded

### Non-Negotiable — Keep as Structured Code

| Component | Reason |
|-----------|--------|
| **Audio pipeline** (STT, TTS, VAD, wake word) | Real-time audio processing, not an LLM problem |
| **Reminder state machine** | Scheduling, nag behavior, Google Calendar sync — too stateful, reliability-critical. A missed reminder is worse than awkward phrasing. (Note: reminder *queries* are LLM-driven via `manage_reminders` tool; the *state machine* stays hard-coded) |
| **News RSS polling** | Background daemon behavior, not request-response. (Note: headline *delivery* is LLM-driven via `get_news` tool; the *polling/classification* stays hard-coded) |
| **Conversation memory / context window** | Persistence and retrieval layers that *feed* the LLM |
| **Speaker identification** | Real-time d-vector matching during audio processing |
| **Streaming TTS pipeline** | Gapless playback, aplay management, chunking — all latency-critical |

### Keep as Fast-Paths (Pre-P4 in router)

| Query Type | Reason |
|------------|--------|
| **Dismissals** ("no thanks", "that's all") | Must be instant, no inference needed |
| **Bare acknowledgments** ("yeah", "ok") | Noise filtering, no inference needed |
| **Memory operations** | Deterministic recall/forget/transparency — reliability-critical |
| **Stateful confirmations** | Reminder ack, memory forget, skill confirmations — context-dependent state machines |

---

## Tradeoffs to Monitor

### What We Gain
- Dramatically less routing code to maintain
- No more "keyword X is too greedy" bugs
- Natural handling of ambiguous queries
- Vision capabilities without a separate pipeline
- Composable tool use (LLM chains multiple tools for complex queries)

### What We Risk
- **Latency** — every query hits LLM inference instead of fast keyword match
- **Reliability** — hard-coded handlers are deterministic; LLM tool selection can be wrong (e.g., "can you help me" returning a Wikipedia article about a 1985 song)
- **Debuggability** — skill handler bugs show exact line numbers; LLM mis-routing requires reading inference logs
- **Resource usage** — more GPU inference cycles per interaction

### Mitigation Strategies
- **Prescriptive prompts** — explicit rules for tool use, not vague guidance (proven with web research via `stream_with_tools()`)
- **Fast-paths bypass LLM** — keep latency-critical responses hard-coded
- **Tool whitelisting** — LLM can only call explicitly defined tools, not arbitrary code
- **Fallback to current system** — if LLM tool selection fails, fall back to semantic/keyword routing
- **Incremental migration** — one skill at a time, validate before moving to the next

---

## Dual-GPU Strategy — RX 7600 (Ordered)

Adding a second GPU changes the VRAM calculus for this entire migration. An RX 7600 (8GB, RDNA 3, officially ROCm-supported) was ordered and arrives Feb 28, 2026. This section analyzes three potential use cases, from lowest to highest risk.

### Current VRAM Budget (Single GPU)

| Component | Typical VRAM | Notes |
|-----------|-------------|-------|
| Qwen3.5-35B-A3B Q3_K_M weights | ~17.5 GB | 16GB on disk + compute buffers |
| KV cache (ctx-size 7168) | ~5.0 GB | Pre-allocated by llama.cpp |
| CTranslate2 Whisper (transient) | ~0.4 GB | Loaded during transcription only |
| Sentence Transformer | ~0.15 GB | Semantic matching (in-memory) |
| Kokoro TTS (when active) | ~0.2 GB | 82M params, CPU primary but some GPU |
| System overhead | ~0.5 GB | Allocators, buffers, Python |
| **GNOME compositor** | **~0.5-1.0 GB** | **Display rendering on same GPU** |
| **Total used** | **~19.5 GB** | **of 20.0 GB** |
| **Free** | **~1.8 GB** | Measured after ctx-size reduction (session 63) |

**The problem:** The GNOME compositor shares the GPU with LLM inference. Under peak load (Feb 24), compositor starvation caused `Failed to pin framebuffer with error -12` (ENOMEM) and crashed the desktop. The ctx-size reduction from 8192→7168 freed 1.2 GB as a band-aid, but the fundamental contention remains.

### RX 7600 Specs

| Spec | Value |
|------|-------|
| Architecture | RDNA 3 (Navi 33, gfx1102) |
| VRAM | 8 GB GDDR6, 128-bit bus |
| Memory Bandwidth | 288 GB/s |
| Compute Units | 32 (2,048 stream processors) |
| TDP | 165W |
| ROCm Status | **Officially supported** — same RDNA 3 family as RX 7900 XT |
| Price | $200-250 |

**Hardware compatibility:** X570 Pro4 motherboard has a second PCIe x16 slot (x4 electrical). 850W PSU confirmed adequate for dual GPU (~435W peak realistic draw).

### Use Case A: Display Offload (HIGH VALUE, LOW RISK) — NEXT

Move the GNOME compositor to the RX 7600. The RX 7900 XT becomes a dedicated compute GPU.

**What it gives you:**
- Frees ~500MB-1GB of VRAM on the primary GPU (compositor overhead eliminated)
- Eliminates ENOMEM crash risk entirely — compositor can never starve the LLM
- Total usable VRAM for inference grows from ~19.0 GB to ~19.5-20.0 GB
- Enough headroom to load mmproj (~900MB) for vision without exceeding budget
- Potentially enough to increase ctx-size from 7168 → 8192+ or try Q4_K_S quantization

**Implementation:** Connect monitor to RX 7600 output. GNOME/Mutter renders on the display-connected GPU. ROCm is NOT required for this — standard Mesa/AMDGPU kernel driver suffices.

**Risk:** Near-zero. Display offload is a standard multi-GPU configuration. Both GPUs are RDNA 3 — no mixed-architecture driver concerns.

### Use Case B: Dedicated Image Generation (MEDIUM VALUE, LOW RISK)

Run image generation models on the RX 7600 independently from the primary GPU.

**What fits in 8GB:**
- SDXL Lightning (~7GB) — 4-step generation, ~4-8s per image
- Stable Diffusion 1.5 (~4GB) — older but fast
- FLUX.1-schnell does NOT fit (13-16GB FP8)

**Implementation:** ROCm required. RX 7600 is officially supported (gfx1102, same RDNA 3 family as RX 7900 XT gfx1100). Per-GPU HSA overrides supported since ROCm 6.2:
```bash
export HSA_OVERRIDE_GFX_VERSION_0=11.0.0  # GPU 0: RX 7900 XT (gfx1100)
export HSA_OVERRIDE_GFX_VERSION_1=11.0.0  # GPU 1: RX 7600 (gfx1102 → gfx1100)
```

**Risk:** Low. Both GPUs are RDNA 3 with official ROCm support. Image generation is a non-critical feature — failures are annoying, not catastrophic.

### Use Case C: Model Splitting Across GPUs (CAUTIOUSLY OPTIMISTIC)

Split Qwen3.5 transformer layers across both GPUs to fit a larger quantization (Q4_K_M or Q5_K_M).

**Why it's tempting:** Q4_K_M or Q5_K_M would significantly improve model quality. The combined 28GB (20+8) could theoretically fit Q5_K_M.

**RX 7600 advantage over RX 6600:** Both GPUs are RDNA 3, eliminating the mixed-architecture bugs that plagued RDNA 2 + RDNA 3 combinations:
- [Issue #4030](https://github.com/ggml-org/llama.cpp/issues/4030): RX 7900 XTX + RX 6900 XT (gfx1100 + gfx1030) → segfault. **Different arch — doesn't apply to 7900 XT + 7600.**
- [Issue #19518](https://github.com/ggml-org/llama.cpp/issues/19518): Mixed GPU crash with Qwen models. **Same arch family may avoid this.**

**Remaining concerns:**
- **Layer split is serialized** — only one GPU computes at a time (`--split-mode layer`). Performance may be worse than single GPU due to inter-GPU communication overhead via PCIe x4.
- **Row split (`--split-mode row`) is unstable** on ROCm — reports of garbage output.
- The `ik_llama.cpp` fork achieved 3-4x multi-GPU improvement, but only for CUDA — not yet available for ROCm.

**Recommendation:** Worth testing after display offload is stable. Same-family RDNA 3 GPUs have a much better chance of working than the previously analyzed mixed-arch scenario. Monitor llama.cpp multi-GPU ROCm progress.

### Use Case Summary

| Use Case | Value | Risk | ROCm Required? | Recommendation |
|----------|-------|------|-----------------|----------------|
| A: Display offload | High | Near-zero | No (Mesa only) | **Do this first** (arrives Feb 28) |
| B: Image generation | Medium | Low | Yes (official) | Viable after A is stable |
| C: Model splitting | High (if it works) | Medium | Yes (same arch) | Worth testing after A is stable |

### VRAM Scenarios With RX 7600

| Scenario | Primary GPU (RX 7900 XT) | Secondary GPU (RX 7600) | Status |
|----------|--------------------------|-------------------------|--------|
| Current (single GPU) | 19.5 / 20.0 GB (~1.8 GB free) | N/A | Stable but tight |
| + display offload | ~19.0 / 20.0 GB (~2.3-2.8 GB free) | Compositor only | Comfortable |
| + display offload + mmproj (vision) | ~19.9 / 20.0 GB (~1.4-1.9 GB free) | Compositor | Workable |
| + display offload + SDXL on secondary | ~19.0 / 20.0 GB | ~7 / 8 GB | Both comfortable |
| + display offload + mmproj + SDXL | ~19.9 / 20.0 GB | ~7 / 8 GB | Tight primary, good secondary |
| Model split (Q5_K_M across both) | ~20 / 20.0 GB | ~8 / 8 GB | Same-arch — worth testing |

---

## Qwen3.5-35B-A3B — Confirmed Capabilities

### What's Running Now

| Feature | Status | Details |
|---------|--------|---------|
| **Native multimodal** | Confirmed | Early-fusion architecture — text + image + video trained jointly from pretraining. Outperforms Qwen3-VL on visual reasoning benchmarks |
| **MoE architecture** | Running | 35B total params, 256 experts, 8+1 active per token, 3B active params |
| **Tool calling** | Proven | `web_search` via `stream_with_tools()` with prescriptive prompts and `tool_choice=auto` |
| **Vision support** | Available | mmproj-F16.gguf (~900MB) from unsloth. llama.cpp support merged Feb 10 (PR #19468). Not yet activated |
| **Quantization** | Q3_K_M | ~16GB on disk, ~19.5GB VRAM with KV cache at ctx-size 7168 |
| **Context window** | 7168 tokens | VRAM-constrained (reduced from 8192 after GNOME compositor crash). Tool schemas consume ~100-200 tokens each |

### Vision Details

Qwen3.5 uses the same ViT architecture as Qwen3-VL but integrated via early fusion — no separate VL model release planned. The mmproj (multimodal projector) bridges the vision encoder to the language model:

| mmproj Variant | Size | Source |
|---|---|---|
| mmproj-F16.gguf | ~900 MB | unsloth/Qwen3.5-35B-A3B-GGUF |
| mmproj-BF16.gguf | ~903 MB | unsloth/Qwen3.5-35B-A3B-GGUF |
| mmproj-F32.gguf | ~1.79 GB | unsloth/Qwen3.5-35B-A3B-GGUF |

**Usage:** `llama-server -m Qwen3.5-35B-A3B-Q3_K_M.gguf --mmproj mmproj-F16.gguf`

**VRAM impact:** +900MB when loaded. At current utilization (~19.5/20.0 GB), this exceeds budget without either dynamic loading or display offload to a second GPU (see Dual-GPU Strategy above).

---

## Success Criteria — Validated

These criteria were defined before migration and met by both Phase 1 and Phase 2:

1. **Accuracy**: 100% on domain categories, 99.6% overall (1,200+ trials) — **EXCEEDED 95% target**
2. **Latency**: ~2.5s for tool-calling queries (1s tool decision + 1.5s response) — **WITHIN 3s target**
3. **Reliability**: 266/266 edge case tests pass across all phases — **ZERO regressions**
4. **Graceful failure**: Borderline queries ("what's the haps") occasionally trigger wrong tool but produce unhelpful-not-harmful results — **AS DESIGNED**

---

## Key Lessons From Migration (Feb 18-27)

### From Web Research (Feb 18, sessions 2-14)
1. **Prescriptive > permissive** — "MUST search for X / ONLY skip for Y" works; "when in doubt, search" gets ignored
2. **Context history poisons tool calling** — if conversation history shows a pattern, Qwen copies it instead of following instructions
3. **Start with `tool_choice=auto`** — let the LLM decide, but make the decision rules crystal clear in the prompt
4. **Test systematically** — 15 runs x N queries, not "try it once and ship it"
5. **Small LLMs can't reason about when to use tools for familiar topics** — they'll confidently answer from stale training data

### From Phase 1-2 Migration (Feb 26-27)
6. **Numbered RULES format > prose** — explicit numbered rules followed more reliably than paragraph descriptions
7. **"ALWAYS call the tool" > "call if you can"** — Qwen answers date questions from system prompt unless explicitly told "NEVER answer time/date from the prompt"
8. **Tool description quality is the differentiator** — well-scoped descriptions prevent cross-tool confusion. "Only use when user explicitly asks about..." eliminates casual misrouting
9. **Semantic pruner threshold 0.40 is the sweet spot** — sweep across 56 queries at 7 thresholds. 0.35 had cliff breaches, 0.45+ introduced false negatives
10. **5-6 tool cliff is model-dependent, not universal** — Qwen3.5 Instruct handled 8 tools at 100% (now 7 after get_time removal). The cliff documented for Qwen3-Coder doesn't apply
11. **Tool-calling latency is irreducibly LLM-bound** — ~2.5s total (1s tool decision + 1.5s response generation), all in C++ llama.cpp on GPU. Pipeline refactoring won't help
12. **Skill guard prevents routing regressions** — pruner must check ALL skills, not just tool-backed ones. If a stateful skill scores higher semantically, defer to skill routing

---

## Timeline

### Completed
- **Phase 1** (Feb 26): time, system_info, filesystem — 100% accuracy (600/600 trials)
- **Phase 2** (Feb 26-27): weather, reminders, conversation, developer_tools, news — 7 tools (get_time later removed), 99.6% overall
- **Tool-connector** (Feb 27): plugin system for one-file tool definitions

### Next Steps
1. **RX 7600 display offload** (Feb 28) — frees ~1GB VRAM on primary GPU, eliminates compositor crash risk
2. **Live testing** — verify tool-calling end-to-end via voice/console/web
3. **Phase 3 (vision)** — activate mmproj (~900MB), add vision tools (screen reading, web nav, image understanding). Practical once display offload frees VRAM headroom
4. **Phase 4 (routing evaluation)** — after Phase 3 is stable, assess what routing layers can be removed

### VRAM Budget With Display Offload
- Tool schema budget: 7 tools consume ~700-1,400 tokens. Dynamic pruning keeps active set to 4-5 tools per query
- mmproj: +900MB VRAM when loaded. With display offload, fits within budget (~19.9/20.0 GB)
- Larger context: could increase ctx-size from 7168 → 8192+ (more room for tool schemas + conversation)

---

*Originally discussed February 18, 2026. Updated February 27, 2026 — Phases 1-2 COMPLETE, tool-connector plugin system live, RX 7600 ordered for display offload.*
