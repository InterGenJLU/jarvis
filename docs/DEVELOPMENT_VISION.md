# JARVIS Development Vision — LLM-Centric Architecture

**Created:** February 18, 2026
**Context:** Qwen 3.5-9B imminent release (native multimodal, hybrid attention, improved tool calling)

---

## Background

JARVIS was built with hard-coded skill handlers because early development focused on reliability over LLM flexibility. The skill system (semantic routing, keyword matching, priority layers) works well but requires significant maintenance — greedy keyword bugs, threshold tuning, priority conflicts, and per-skill handler code.

With Qwen 3.5's improved tool calling, native vision, and hybrid attention architecture, the project can begin shifting toward an LLM-centric approach where the model acts as an **agent with tools** rather than a dispatcher to pre-built handlers.

---

## Core Principle

**Skills become tools, not destinations.**

The current skill handlers aren't wasted work — they're the tool execution layer an agentic LLM needs. The migration path is:

1. Convert skill handlers into **tool definitions** (function signatures + descriptions)
2. Let the LLM decide which tools to call and how to compose them
3. Keep the skill infrastructure as the **execution layer** the LLM calls into

The web research implementation (Qwen 3-8B + DuckDuckGo tool calling, commit `8ae35ce`) already proves this pattern works in JARVIS.

---

## Routing Simplification

### Current Architecture (4+ layers)
```
Layer 1: Wake word detection
Layer 2: Keyword matching (with generic keyword blocklist, word boundaries)
Layer 3: Semantic similarity (sentence-transformers, threshold tuning)
Layer 4: LLM fallback (Qwen 3-8B → quality gate → Claude API)
```

### Target Architecture
```
Layer 1: Wake word detection
Layer 2: Hard-coded fast-paths (time, dismissals, greetings — latency-critical)
Layer 3: LLM agent with tools (everything else)
```

The semantic matcher, keyword routing, generic keyword blocklist, priority levels, and threshold tuning all exist because routing needed to be cheap and fast without hitting the LLM. If the LLM is fast and reliable enough at tool selection, much of that routing machinery becomes unnecessary.

---

## Migration Plan — Incremental, Skill by Skill

### Phase 1: Low-Stakes Skills (First Candidates)

**System Info + Filesystem**
- Low stakes if the LLM picks a slightly wrong command
- Benefits from flexible query interpretation ("how much disk space do I have" vs "show me storage" vs "am I running low on space")
- Implementation: `run_command` tool with a whitelist of safe commands
- The LLM decides whether to run `free -h`, `lscpu`, `df -h`, `du`, `find`, etc.

**Time/Date**
- Trivial tool call, no need for a dedicated skill
- Could be a simple function tool that returns current time/date info
- LLM handles formatting naturally ("quarter past three" vs "3:15 PM")

### Phase 2: API-Backed Skills

**Weather**
- LLM calls weather API tool, formats response naturally
- Eliminates hard-coded response templates
- Can handle complex queries ("do I need an umbrella tomorrow?" "is it colder than yesterday?")

### Phase 3: Vision-Enabled (The Qwen 3.5 Win)

**Web Navigation with Vision**
- Currently uses per-site CSS selectors and structured scraping
- With Qwen 3.5's native vision: screenshot the page, let the LLM see it, decide what to click
- Replaces brittle CSS selectors with visual understanding
- This is what Qwen 3.5's "visual agentic capabilities" are designed for

**IoT Camera Integration (Future)**
- Security camera feeds processed by the same model handling conversation
- "Is anyone at the front door?" — LLM sees the camera frame directly
- No separate vision pipeline needed

### Phase 4: Routing Layer Evaluation

After Phases 1-3 are stable, evaluate:
- Can the semantic matcher be removed entirely?
- Can keyword routing be reduced to just the fast-paths?
- What's the latency impact of routing everything through the LLM?

---

## What Must Stay Hard-Coded

### Non-Negotiable — Keep as Structured Code

| Component | Reason |
|-----------|--------|
| **Audio pipeline** (STT, TTS, VAD, wake word) | Real-time audio processing, not an LLM problem |
| **Reminder state machine** | Scheduling, nag behavior, Google Calendar sync — too stateful, reliability-critical. A missed reminder is worse than awkward phrasing |
| **News RSS polling** | Background daemon behavior, not request-response |
| **Conversation memory / context window** | Persistence and retrieval layers that *feed* the LLM |
| **Speaker identification** | Real-time d-vector matching during audio processing |
| **Streaming TTS pipeline** | Gapless playback, aplay management, chunking — all latency-critical |

### Keep as Fast-Paths

| Query Type | Reason |
|------------|--------|
| **Time queries** | ~50ms hard-coded vs ~1-2s through LLM |
| **Dismissals** ("no thanks", "that's all") | Must be instant, no inference needed |
| **Minimal greetings** | "Good morning sir" shouldn't require LLM inference |

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
- **Prescriptive prompts** — explicit rules for tool use, not vague guidance (proven with web research, commit `8ae35ce`)
- **Fast-paths bypass LLM** — keep latency-critical responses hard-coded
- **Tool whitelisting** — LLM can only call explicitly defined tools, not arbitrary code
- **Fallback to current system** — if LLM tool selection fails, fall back to semantic/keyword routing
- **Incremental migration** — one skill at a time, validate before moving to the next

---

## Qwen 3.5-9B Capabilities That Enable This

| Feature | Impact |
|---------|--------|
| **Native multimodal** (text + image + video from pretraining) | Vision tasks without separate pipeline |
| **Hybrid attention** (standard + linear layers) | Efficient long-context, faster inference |
| **9B dense parameters** | Fits in 20GB VRAM (RX 7900 XT) with Q5_K_M quantization |
| **Improved tool calling** | Better function selection reliability |
| **2M token context window** | Can hold much more conversation + tool context |
| **248K vocab size** | Better tokenization efficiency |

### Architecture Specs (from transformers config)
- Hidden size: 4,096
- Intermediate size: 12,288
- Layers: 32
- Attention heads: 16 (GQA with 4 KV heads)
- Head dim: 256
- Linear attention layers with conv kernel dim 4

---

## Success Criteria

Before declaring a skill "migrated" to LLM-driven:

1. **Accuracy**: LLM tool selection matches hard-coded routing for 95%+ of test queries
2. **Latency**: Response time stays under 3 seconds for common queries
3. **Reliability**: No regressions in edge cases (test with the same voice test methodology used for web research — systematic multi-run validation)
4. **Graceful failure**: When the LLM picks the wrong tool, the result is unhelpful but not harmful

---

## Key Lessons From Web Research Implementation

These lessons (Feb 18, sessions 2-14) directly apply to future LLM-driven skills:

1. **Prescriptive > permissive** — "MUST search for X / ONLY skip for Y" works; "when in doubt, search" gets ignored
2. **Context history poisons tool calling** — if conversation history shows a pattern, Qwen copies it instead of following instructions
3. **Start with `tool_choice=auto`** — let the LLM decide, but make the decision rules crystal clear in the prompt
4. **Test systematically** — 15 runs x N queries, not "try it once and ship it"
5. **Small LLMs can't reason about when to use tools for familiar topics** — they'll confidently answer from stale training data. Prompt design must account for this.

---

## Timeline

No fixed dates — this is a directional vision, not a sprint plan.

- **Immediate**: Monitor Qwen 3.5-9B release on HuggingFace, test with llama.cpp when GGUF quants are available
- **Near-term**: Migrate system info + filesystem skills as proof of concept
- **Medium-term**: Weather, then web nav with vision
- **Long-term**: Evaluate full routing simplification, IoT camera integration

---

*This document captures the architectural direction discussed on February 18, 2026. Update as the migration progresses and lessons are learned.*
