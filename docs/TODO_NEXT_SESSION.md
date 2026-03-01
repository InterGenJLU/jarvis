# TODO — Next Session

**Updated:** March 1, 2026 (session 112)

---

## Session 112 Completed

- **Audio fix (again)**: Same broken jack detect issue — came back after church
  - Diagnosed: WirePlumber's `policy-device-profile.lua` checks `profile.available` on ALL code paths — no config-only override possible
  - Created `~/.config/systemd/user/fix-audio-profile.service` (replaces old `audio-fix.service`) — forces profile 3s after WirePlumber starts, enabled
  - Created `~/.config/wireplumber/main.lua.d/51-alsa-realtek-fix.lua` — priority boost for analog output node
  - Created HDA firmware patch at `/tmp/hda-jack-retask.fw` — disables jack detection on pin 0x14 (line-out) and 0x1b (headphone)

## IMMEDIATE: Run These Sudo Commands Before Reboot

The kernel-level jack detect fix is staged but needs sudo:

```bash
sudo cp /tmp/hda-jack-retask.fw /lib/firmware/hda-jack-retask.fw
echo 'options snd-hda-intel patch=,,hda-jack-retask.fw' | sudo tee -a /etc/modprobe.d/alsa-base.conf
sudo update-initramfs -u
```

After reboot, verify:
1. `pactl list cards | grep -A 5 "Active Profile"` — Realtek should show `output:analog-stereo+input:analog-stereo`
2. `pactl list sinks short` — analog sink should exist
3. `pactl get-default-sink` — should be `alsa_output.pci-0000_10_00.4.analog-stereo`
4. Play a test sound: `paplay /usr/share/sounds/freedesktop/stereo/bell.oga`

If the kernel patch works, the `fix-audio-profile.service` becomes a safety net only. If it doesn't work (unlikely), the service alone will keep audio working.

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
