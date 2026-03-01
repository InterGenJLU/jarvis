# TODO — Next Session

**Updated:** March 1, 2026 (session 110)

---

## Session 110 Completed

- Verified session 109 fixes: generated test PPTX, confirmed stat slide, comparison slide, no ghost placeholder
- Committed 7 doc gen fixes across both repos + published
- Analyzed 4 professional presentation templates for layout engine overhaul
- Saved detailed plan: `memory/plan_presentation_engine.md`

---

## Tier 1: High ROI — Do Now/Soon

### 0. Presentation Engine Overhaul (#49 extended)
**Priority:** HIGH — Reference templates analyzed, plan saved
**What:** Expand from 4 slide types to 12+. Add card grids, timelines, data tables, section dividers, closing slides. Manual shape composition (no new libs). Template-based theming optional.
**Reference files:** `share/Presentation 6.odp`, `share/Presentation 10.odp`, `share/Presentation_Deck.pptx`, `share/Presentation 13.odp`
**Plan:** `memory/plan_presentation_engine.md`

### 1. Inject user facts into web research (#7)
### 2. "Onscreen please" — retroactive visual display (#11)
### 3. Document refinement follow-ups (#49)
### 4. Vision pipeline integration — console/web image input

### 5. Mobile iOS App — COMING VERY SOON (#60)
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
