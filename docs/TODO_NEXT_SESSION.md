# TODO — Next Session

**Updated:** March 1, 2026

---

## Immediate: Verify & Commit Session 109 Fixes

### 7 fixes applied to document generation pipeline (UNCOMMITTED):

1. **Wired `web_researcher` into TaskPlanner** — `_web_research()` pseudo-skill was completely broken (fell back to LLM hallucination). Now does real DuckDuckGo search + page fetch + LLM synthesis. Files: `task_planner.py`, `jarvis_web.py`, `jarvis_console.py`, `pipeline.py`

2. **Fixed search query pollution** — TaskPlanner sent entire context blob as DuckDuckGo query. Now extracts first paragraph only, capped at 200 chars. File: `task_planner.py`

3. **Added `stream_start`/`stream_end` for task plans in web UI** — Progress updates were silently dropped (frontend needs `streamingBubble`). File: `jarvis_web.py`

4. **Added TTS for task plan results** — Web UI never spoke plan results. Now speaks via `real_tts` thread. File: `jarvis_web.py`

5. **Removed original request prepending** — Prepending original request caused step 4 "open file" to re-match to `create_presentation`. File: `task_planner.py`

6. **Removed title slide ghost placeholder** — `_add_title_slide()` left layout placeholder visible as "PUT YOUR TITLE TEXT HERE". File: `document_generator.py`

7. **Mandated slide type variety** — Prompt was too permissive; Qwen defaulted to all-bullet walls. Now prescriptive: MUST include 1 stat slide + 1 comparison slide. Also fixed date. File: `file_editor/skill.py`

### Verification checklist:
- [ ] Live test: stat + comparison slides appear in generated PPTX
- [ ] Live test: progress updates visible in web UI chat bubble
- [ ] Live test: voice speaks plan result
- [ ] Commit all changes
- [ ] Publish to public repo

### Poison cleanup notes
- Chat history cleaned 3x during testing. Backup: `chat_history.jsonl.bak.pre_fix`

---

## Tier 1: High ROI — Do Now/Soon

### 0. Mobile iOS App — COMING VERY SOON (#60)
**Priority:** HIGHEST — Active Development
**Concept:** Native Swift iPhone app with full JARVIS access from anywhere.
**Key Features:**
- Always-listening "Jarvis" wake word (Porcupine iOS SDK, on-device, background mode)
- Real-time voice via WebRTC (server-side Whisper STT + Kokoro TTS — same voice as desktop)
- Full chat UI via WKWebView (existing web UI, zero rebuild)
- Secure remote access via Tailscale VPN (no public exposure)
- Apple Shortcuts integration ("Hey Siri, ask Jarvis...")
- iOS 18 Vocal Shortcuts as zero-battery wake word fallback
**Phases:** 6 phases, ~5-8 days total
**Prerequisites:** Apple Developer account ($99/yr), Mac with Xcode, Tailscale (free), Picovoice (free)

### 1. Inject user facts into web research (#7)
### 2. "Onscreen please" — retroactive visual display (#11)
### 3. Document refinement follow-ups (#49)
### 4. Vision pipeline integration — console/web image input
