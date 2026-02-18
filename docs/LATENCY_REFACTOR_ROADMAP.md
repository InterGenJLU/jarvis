# JARVIS Latency Refactor Roadmap

**Created:** February 16, 2026
**Goal:** Minimize perceived latency — eliminate all "awkward pauses" between user speech and JARVIS response
**Scope:** 4-phase refactor of the core audio/LLM/TTS pipeline
**Status:** Phase 4 COMPLETE (Feb 16, 2026) — ALL PHASES DONE

---

## Table of Contents

1. [Current Latency Profile](#current-latency-profile)
2. [Phase 1 — Quick Wins](#phase-1--quick-wins-zero-architectural-risk)
3. [Phase 2 — Streaming TTS](#phase-2--streaming-tts)
4. [Phase 3 — Streaming LLM-to-TTS Pipeline](#phase-3--streaming-llm-to-tts-pipeline)
5. [Phase 4 — Event Pipeline Architecture](#phase-4--event-pipeline-architecture)
6. [Appendix A — Files Changed Per Phase](#appendix-a--files-changed-per-phase)
7. [Appendix B — Quality Gate Strategy for Streaming](#appendix-b--quality-gate-strategy-for-streaming)
8. [Appendix C — Word Timestamps Decision](#appendix-c--word-timestamps-decision)

---

## Current Latency Profile

### End-to-end: User speaks → JARVIS responds

```
┌─────────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ VAD + Buffer │ → │ Whisper  │ → │ Skill Match  │ → │  LLM (Qwen)  │ → │  TTS (Kokoro) │
│  ~300-500ms  │   │ 100-200ms│   │   ~100ms     │   │  1000-2000ms │   │  400-800ms    │
│  (inherent)  │   │  (GPU)   │   │ (embeddings) │   │ (if needed)  │   │  + playback   │
└─────────────┘   └──────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

**Best case** (skill-handled, no LLM): ~600-800ms speech-to-first-audio
**Typical case** (LLM fallback): ~1800-3500ms speech-to-first-audio
**Worst case** (LLM retry + quality gate): ~4000-6000ms

### Where the time goes (LLM path)

| Stage | Current | Blocking? | Notes |
|-------|---------|-----------|-------|
| VAD speech collection | 300-500ms | Inherent | Can't reduce — need complete utterance |
| Audio resampling (np.interp in callback) | ~1-3ms/frame | Yes (callback) | CPU spikes in realtime thread |
| Debug WAV write | 5-15ms | Yes | Unconditional disk I/O every transcription |
| Whisper transcribe (beam=5, word_timestamps) | 100-200ms | Yes | GPU, could be faster |
| Semantic matching | ~100ms | Yes | sentence-transformers, already fast |
| LLM generation (full response) | 1000-2000ms | **Yes — biggest** | Synchronous, waits for complete response |
| TTS generation (full audio) | 400-800ms | **Yes — second biggest** | Generates ALL audio before playback |
| aplay spawn + playback start | ~20-50ms | Yes | Process spawn overhead |

### The two elephants

1. **LLM blocks until full response** — user waits for Qwen to generate the entire reply before hearing anything
2. **TTS blocks until full audio generated** — even after LLM completes, Kokoro generates the entire waveform before aplay starts

These two stages are **sequential and blocking**, adding 1400-2800ms of dead silence.

---

## Phase 1 — Quick Wins (Zero Architectural Risk) ✅ COMPLETE

**Completed:** February 16, 2026
**Risk:** Minimal — config changes and parameter tuning
**Actual gain:** ~50-100ms per interaction, plus reduced CPU jitter + blocksize fix for blank transcriptions

### 1.1 Gate debug WAV writes behind config

**File:** `core/stt.py`
**Current (line 207):** `self._debug_save_audio(audio_data)` — runs unconditionally
**Problem:** 5-15ms disk I/O on every single transcription, including glob + cleanup of old files

**Change:**
```python
# In __init__():
self.debug_save_audio = config.get("stt.debug_save_audio", False)

# In transcribe():
if self.debug_save_audio:
    self._debug_save_audio(audio_data)
```

**Config addition** (`config.yaml`):
```yaml
stt:
  debug_save_audio: false  # Set true for diagnosis sessions only
```

### 1.2 Disable word_timestamps

**File:** `core/stt.py` (line 220)
**Current:** `word_timestamps=True`
**Problem:** Adds compute overhead to every transcription; data is generated but never consumed

**Change:** `word_timestamps=False`

**Impact on planned features:** None.
- Vocal signatures use resemblyzer d-vectors (utterance-level, not word-level)
- Conversational memory uses sentence-level embeddings
- No downstream code reads word timing data
- See [Appendix C](#appendix-c--word-timestamps-decision) for full analysis

**Reversibility:** Trivially re-enabled if a future feature needs it.

### 1.3 Reduce beam_size from 5 to 3

**File:** `core/stt.py` (line 213)
**Current:** `beam_size=5`
**Rationale:** beam=5 is overkill for short voice commands (typically 3-15 words). beam=3 provides nearly identical accuracy for short utterances with measurably less compute.

**Change:** `beam_size=3`

**Not going to beam=1** because:
- We have a fine-tuned Southern accent model — accuracy matters
- beam=1 (greedy) is noticeably worse on accented speech
- beam=3 is the sweet spot for command-length utterances

### 1.4 Fix sounddevice blocksize mismatch

**File:** `core/continuous_listener.py` (line 360)
**Current:** `blocksize=self.frame_size` where `frame_size` is calculated from VAD rate (16000 Hz)
**Problem:** Stream opens at `device_sr` (likely 48000 Hz) but uses a blocksize calculated for 16000 Hz. This means:
- At 48kHz device with 30ms frame_duration: should be 1440 samples, but gets 480
- VAD receives wrong-duration frames
- Likely contributes to blank transcriptions and garbage output ("wrwwwwww")

**Change:**
```python
# Calculate blocksize in device sample rate frames
device_blocksize = int(device_sr * self.frame_duration_ms / 1000)

self.stream = sd.InputStream(
    device=device_index,
    channels=channels,
    samplerate=device_sr,
    blocksize=device_blocksize,  # Was: self.frame_size
    callback=self._audio_callback
)
```

### 1.5 Replace np.interp with proper resampling

**File:** `core/continuous_listener.py` (line 165)
**Current:** `np.interp()` linear interpolation in the audio callback (runs on every frame)
**Problem:** Linear interpolation is both CPU-expensive for a realtime callback and acoustically poor (aliasing)

**Options (pick one):**
1. **Best: Open stream at 16kHz directly** — eliminates resampling entirely. Many USB mics support 16kHz natively. Test with `sd.query_devices()` to verify.
2. **Alternative: Use `soxr` or `samplerate` library** — proper anti-aliased resampling, still fast. Move resampling to the speech processing thread instead of the callback.
3. **Minimum viable: Move resampling out of callback** — keep np.interp but do it in `_process_speech()` on the accumulated buffer, not per-frame.

**Recommended approach:** Try option 1 first (open at 16kHz). If the device refuses, fall back to option 3 (batch resample in processing thread). The callback should do as little work as possible.

---

## Phase 2 — Streaming TTS ✅ COMPLETE

**Completed:** February 16, 2026
**Risk:** Low — self-contained change within `core/tts.py`
**Actual gain:** TTFA drops from ~500-800ms to ~80-150ms; ack cache fills LLM wait silence
**Dependency:** None (independent of Phase 1)

### 2.1 Stream Kokoro chunks directly to aplay

**File:** `core/tts.py`
**Current flow (lines 164-228):**
```
Kokoro pipeline yields (gs, ps, audio) chunks
  → collect ALL chunks into list
  → np.concatenate into single array
  → convert float32 → int16 PCM (one big buffer)
  → spawn aplay
  → write entire PCM to stdin
  → wait for aplay to finish
```

**New flow:**
```
spawn aplay FIRST
  → Kokoro pipeline yields (gs, ps, audio) chunks
  → for each chunk: convert to PCM, write to aplay.stdin immediately
  → close stdin when pipeline exhausted
  → wait for aplay to finish
```

**Implementation sketch:**
```python
def _speak_kokoro(self, text: str) -> bool:
    """Generate and play audio via Kokoro — streaming."""
    t0 = time.time()

    # Start aplay BEFORE generation
    aplay = subprocess.Popen(
        ["aplay", "-D", self.audio_device, "-t", "raw",
         "-r", str(self.sample_rate), "-c", "1", "-f", "S16_LE"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    total_samples = 0
    try:
        for gs, ps, audio in self._kokoro_pipeline(
            text, voice=self._kokoro_voice, speed=self._kokoro_speed
        ):
            pcm = (audio * 32767).astype(self._np.int16).tobytes()
            aplay.stdin.write(pcm)  # Plays while generating
            total_samples += len(audio)

        aplay.stdin.close()
    except BrokenPipeError:
        aplay_err = aplay.stderr.read().decode().strip()
        self.logger.error(f"aplay broken pipe: {aplay_err}")
        aplay.wait()
        return False

    gen_time = time.time() - t0
    duration = total_samples / self.sample_rate
    self.logger.info(
        f"Kokoro streamed {duration:.1f}s audio in {gen_time:.3f}s "
        f"(RTF: {duration/gen_time:.1f}x)"
    )

    try:
        aplay_return = aplay.wait(timeout=max(15, duration + 5))
    except subprocess.TimeoutExpired:
        self.logger.error("aplay timed out — killing")
        aplay.kill()
        aplay.wait()
        return False

    if aplay_return != 0:
        aplay_err = aplay.stderr.read().decode()
        self.logger.error(f"aplay error (code {aplay_return}): {aplay_err}")
        return False

    self.logger.info("TTS playback completed successfully")
    return True
```

**Key details:**
- `bufsize=0` on aplay's stdin ensures immediate write-through
- aplay starts consuming audio from kernel pipe buffer as soon as first chunk arrives
- TTFA becomes "time for Kokoro to yield first chunk" (~80-150ms) instead of "total generation time"
- The dynamic timeout (`max(15, duration + 5)`) accounts for long utterances
- BrokenPipeError handling preserved from current code
- Logging still reports total generation time and RTF

### 2.2 Pre-synthesize acknowledgment phrases (fast-ack cache)

**File:** `core/tts.py` (new method)
**Purpose:** When the LLM is slow, play a quick verbal filler immediately while waiting

**Implementation:**
- At startup, pre-generate ~10-15 short phrases as raw PCM byte arrays:
  ```
  "One moment, sir."
  "Right away, sir."
  "Let me check on that."
  "Of course, sir."
  "Working on it, sir."
  ```
- Store in a dict: `self._ack_cache: Dict[str, bytes]`
- New method: `speak_ack()` — picks a random cached phrase and plays instantly (no Kokoro overhead)
- Called from `jarvis_continuous.py` when the LLM hasn't responded within ~300ms

**This is a perception optimization** — it makes JARVIS feel responsive even when the LLM is still generating. Not a substitute for real streaming, but highly effective for user experience.

---

## Phase 3 — Streaming LLM-to-TTS Pipeline ✅ COMPLETE

**Completed:** February 16, 2026
**Risk:** Medium — touches LLM router, quality gate, and command processing flow
**Actual gain:** LLM TTFA drops from ~1800-3500ms to ~400-800ms (first sentence spoken while rest generates)
**Dependency:** Phase 2 (streaming TTS must work first)

### 3.1 Add streaming to LLMRouter

**File:** `core/llm_router.py`
**Current:** Synchronous `requests.post()` to llama.cpp `/v1/chat/completions`

**New method: `stream()`**

The llama.cpp server already supports SSE streaming via `"stream": true`. We add a generator method:

```python
def stream(self, user_message: str, conversation_history=None,
           max_tokens: int = 512) -> Iterator[str]:
    """Stream tokens from the local LLM as they're generated."""
    system_prompt = self._build_system_prompt()
    messages = self._build_messages(system_prompt, user_message, conversation_history)

    try:
        response = requests.post(
            "http://127.0.0.1:8080/v1/chat/completions",
            json={
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                "stream": True,
            },
            timeout=30,
            stream=True,
        )
        response.raise_for_status()

        for line in response.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8")
            if line.startswith("data: "):
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                chunk = json.loads(data)
                delta = chunk["choices"][0].get("delta", {})
                token = delta.get("content", "")
                if token:
                    yield token

    except Exception as e:
        self.logger.error(f"LLM streaming error: {e}")
```

**The existing `chat()` method stays unchanged** — it remains the non-streaming path for quality-gated interactions. `stream()` is a new parallel capability.

### 3.2 Build a SpeechChunker utility

**New file:** `core/speech_chunker.py`
**Purpose:** Accumulate streamed tokens into speakable sentence/phrase chunks

**Logic:**
```
Accumulate tokens into a buffer
Emit a chunk when:
  1. Buffer contains a sentence boundary (. ? ! followed by space/newline)
  2. OR buffer exceeds ~15 words without a sentence boundary
  3. OR stream ends (flush remaining buffer)
```

**Interface:**
```python
class SpeechChunker:
    def feed(self, token: str) -> Optional[str]:
        """Feed a token, return a speakable chunk if ready, else None."""

    def flush(self) -> Optional[str]:
        """Flush any remaining buffered text."""
```

**Why this matters:** You can't TTS one token at a time — Kokoro needs enough context for natural prosody. A sentence is the natural unit. But you also don't want to wait for a period if the LLM produces a long clause, hence the word-count fallback.

### 3.3 Wire streaming into the command processing pipeline

**Files:** `jarvis_continuous.py`, `jarvis_console.py`

**New flow (LLM fallback path only):**
```
1. User command enters LLM fallback
2. Start LLM streaming
3. Feed tokens to SpeechChunker
4. For first chunk ONLY: run quality gate (check for gibberish/echo/artifacts)
   - If bad: abort stream, fall back to non-streaming chat() with retry
   - If good: speak first chunk, continue streaming
5. For subsequent chunks: speak each as it arrives
6. Accumulate full response for conversation history
```

**Quality gate strategy:** See [Appendix B](#appendix-b--quality-gate-strategy-for-streaming) for detailed analysis.

**Console mode adaptation:** In console mode, streamed tokens are printed as they arrive (typewriter effect) and only spoken in hybrid mode after full accumulation.

### 3.4 Integrate with mic pause/resume

**Current behavior:** Mic pauses when TTS starts, resumes when TTS finishes.
**New behavior:** Mic pauses when first TTS chunk starts, resumes when last chunk's playback finishes.

The `_speaking_event` in `continuous_listener.py` already handles this correctly — it's set/cleared by the TTS module, not by the command processor. As long as TTS calls pause/resume correctly per chunk, this should work without changes to the listener.

**Potential issue:** If there's a gap between chunks (LLM slow to produce next sentence), the mic might briefly unpause. Solution: keep the pause active for the duration of the streaming session, not per-chunk.

---

## Phase 4 — Event Pipeline Architecture ✅ COMPLETE

**Completed:** February 16, 2026
**Risk:** High — architectural refactor of the main processing loop
**Actual gain:** Centralized event coordination; queue-connected workers; main thread as coordinator
**Dependency:** Phases 1-3 complete and stable

### 4.1 Current architecture (synchronous)

```
Audio Callback Thread          Main Thread
─────────────────────         ──────────────────────────────
frames → VAD → buffer   →    dequeue → STT → skill/LLM → TTS → resume
                              (everything sequential, blocking)
```

### 4.2 Target architecture (event-driven)

```
Audio Callback Thread     STT Worker          LLM Worker          TTS Worker
─────────────────────    ──────────────      ──────────────      ──────────────
frames → VAD → buffer → audio_queue →       text_queue →        speech_queue →
                         transcribe →        stream tokens →     stream PCM →
                         emit text           emit chunks         play audio

                         Main Thread (coordinator)
                         ──────────────────────────
                         receives events from all workers
                         manages conversation state
                         routes commands to skills
```

**Key design decisions:**
- **Thread-based, not async** — matches existing codebase style, avoids asyncio migration
- **Queue-connected workers** — `queue.Queue` between each stage
- **Coordinator pattern** — main thread receives typed events, makes routing decisions
- **Skill execution stays synchronous** — skills are fast enough; only LLM+TTS need parallelism

### 4.3 Implementation sketch

```python
# Event types
@dataclass
class UserTextEvent:
    text: str
    timestamp: float

@dataclass
class LLMChunkEvent:
    chunk: str
    is_first: bool
    is_last: bool

@dataclass
class SpeechChunkEvent:
    pcm_data: bytes
    is_last: bool

# Worker threads
class STTWorker(Thread):
    """Pulls audio from queue, transcribes, pushes text events."""

class LLMWorker(Thread):
    """Pulls text from queue, streams LLM, pushes chunk events."""

class TTSWorker(Thread):
    """Pulls speech chunks, streams to aplay."""
```

### 4.4 Migration strategy

This is NOT a rewrite. It's a gradual extraction:

1. Extract STT call from `continuous_listener.py` callback into a dedicated worker
2. Extract LLM call from `jarvis_continuous.py` into a worker
3. Extract TTS playback into a worker
4. Replace direct calls in the main loop with queue puts/gets
5. Add the coordinator event loop

Each step is independently testable. The system can run in "hybrid mode" during migration where some stages are workers and others are still inline.

---

## Appendix A — Files Changed Per Phase

| Phase | Files Modified | Files Created | Risk |
|-------|---------------|---------------|------|
| **1** | `core/stt.py`, `core/continuous_listener.py`, `config.yaml` | — | Low |
| **2** | `core/tts.py` | — | Low |
| **3** | `core/llm_router.py`, `jarvis_continuous.py`, `jarvis_console.py` | `core/speech_chunker.py` | Medium |
| **4** | `jarvis_continuous.py`, `core/continuous_listener.py`, `core/tts.py`, `core/llm_router.py` | `core/pipeline.py` (coordinator + workers) | High |

---

## Appendix B — Quality Gate Strategy for Streaming

### The problem

Currently, `LLMRouter.chat()` follows this flow:
```
generate full response → quality_check() → if bad, retry with nudge → if still bad, Claude API
```

With streaming, we commit to speaking before the full response exists. We can't un-say words.

### The solution: First-chunk gating

```
Stream begins
  → accumulate tokens until first sentence boundary (or ~15 words)
  → run quality_check() on this first chunk
  → if PASS: speak it, continue streaming remainder (no further checks)
  → if FAIL: abort stream, fall back to non-streaming chat() with full retry logic
```

**Why this works:**
- Quality failures (gibberish, echo, prompt artifacts) are almost always visible in the first sentence
- If the first sentence is coherent, the rest virtually always is too
- The fallback path uses the existing proven retry mechanism
- Worst case: ~200ms extra latency on the rare quality failure (accumulating first chunk before aborting)

**What we lose:** The ability to catch quality degradation mid-response. This is acceptable because:
- Qwen quality failures are almost always total (gibberish from the start), not partial
- Partial degradation (good start, bad finish) is extremely rare with Q5_K_M quantization

---

## Appendix C — Word Timestamps Decision

### Current state
- `word_timestamps=True` is set in `core/stt.py:220`
- The word timing data is **generated but immediately discarded** — `transcribe()` returns only the joined text string
- No downstream code anywhere in the codebase reads word-level timing

### Planned features that might need it
- **Vocal signatures:** Uses resemblyzer d-vectors (utterance-level acoustic embeddings). Does not use word timing.
- **Conversational memory:** Uses sentence-transformers embeddings on full messages. Does not use word timing.
- **Audio recording skill:** Records raw audio with message-level timestamps. Does not use word timing.

### Decision
**Disable `word_timestamps` in Phase 1.** No current or planned feature requires it. If a future feature needs word-level alignment (e.g., karaoke-style display, precise quote extraction), it can be trivially re-enabled with a one-line change.

### What word_timestamps costs
- Additional compute per transcription (Whisper must run alignment pass)
- Exact overhead depends on utterance length; typically 10-30% of transcription time
- For a 3-second utterance at beam=5: ~20-40ms additional

---

## Testing Strategy

### Phase 1 testing
- Run JARVIS in console mode, verify transcription still works accurately
- Compare transcription accuracy: beam=5 vs beam=3 on 20 test utterances
- Verify blocksize fix: check logs for "Audio length" values (should be consistent)
- Monitor CPU usage during continuous listening (should drop with callback optimization)

### Phase 2 testing
- Compare TTFA (time-to-first-audio) before and after streaming
- Test with short phrases ("Yes, sir.") and long responses (full rundown)
- Verify BrokenPipeError handling still works
- Test fast-ack cache: verify phrases sound natural and play instantly

### Phase 3 testing
- Measure end-to-end latency: user speech → first audio response
- Test quality gate: intentionally trigger bad responses (gibberish prompts)
- Verify conversation history captures full response (not just chunks)
- Test mic pause/resume across streaming chunks

### Phase 4 testing
- Full regression: all 9 skills, rundowns, reminders, news
- Stress test: rapid successive commands
- Verify no audio glitches (gaps between chunks, clicks, pops)
- Memory/thread leak testing over extended sessions

---

## Success Criteria

| Metric | Current | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|--------|---------|---------|---------|---------|---------|
| STT latency | 100-200ms | 80-150ms | (unchanged) | (unchanged) | (unchanged) |
| Skill response TTFA | 500-800ms | 450-700ms | 150-300ms | 150-300ms | 100-250ms |
| LLM response TTFA | 1800-3500ms | 1700-3400ms | 1200-2800ms | **400-800ms** | **300-600ms** |
| Blank transcription rate | ~15-20% | **<5%** (blocksize fix) | (unchanged) | (unchanged) | (unchanged) |
| CPU jitter in callback | High (np.interp) | Low | (unchanged) | (unchanged) | (unchanged) |

**The target:** By end of Phase 3, the LLM path should feel conversational — JARVIS starts speaking within ~500ms of the LLM beginning to generate, while still generating the rest of the response.

---

*This document supersedes ad-hoc latency notes. Update after each phase completion.*
