#!/usr/bin/env python3
"""
Pronunciation Audit Tool for JARVIS

Generates WAV files from curated phrases via Kokoro TTS, saves them to a
review directory with metadata, and optionally plays back for human verdict.

This is the subjective companion to test_voice_pipeline.py — automated tests
verify round-trip accuracy, this tool lets you LISTEN to how JARVIS sounds.

Usage:
    python3 scripts/pronunciation_audit.py                    # Generate all to /tmp
    python3 scripts/pronunciation_audit.py --play             # Generate + interactive review
    python3 scripts/pronunciation_audit.py -o ~/audit         # Persistent save
    python3 scripts/pronunciation_audit.py --custom "Hello"   # Ad-hoc phrase
    python3 scripts/pronunciation_audit.py --custom "Hello" --play  # Ad-hoc + play
    python3 scripts/pronunciation_audit.py --category Names   # Filter by category
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

import sys
sys.stdout.reconfigure(line_buffering=True)

import time
import json
import argparse
import subprocess
import warnings
import re
from dataclasses import dataclass, asdict
from typing import List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# ===========================================================================
# Phrase definitions
# ===========================================================================

@dataclass
class AuditPhrase:
    id: str
    text: str
    category: str
    notes: str = ""
    normalize: bool = True


def get_audit_phrases() -> List[AuditPhrase]:
    """Curated phrases for pronunciation review."""
    phrases = []

    # ── Names ──────────────────────────────────────────────────────────
    phrases.extend([
        AuditPhrase("P-01", "Hello Sophia, welcome to the house.", "Names",
                     notes="Tests multi-syllable name pronunciation"),
        AuditPhrase("P-02", "Lydia is in the living room.", "Names",
                     notes="Tests name with soft consonants"),
        AuditPhrase("P-03", "Good evening, sir. How may I assist you?", "Names",
                     notes="Core butler address form"),
    ])

    # ── Persona ────────────────────────────────────────────────────────
    phrases.extend([
        AuditPhrase("P-04", "Very good, sir. I'll see to it right away.", "Persona",
                     notes="Butler acknowledgment style"),
        AuditPhrase("P-05", "I'm looking into that for you now.", "Persona",
                     notes="Research acknowledgment"),
        AuditPhrase("P-06", "That's everything for today's headlines, sir.", "Persona",
                     notes="News wrap-up"),
        AuditPhrase("P-07", "I've set a reminder for tomorrow morning at nine.", "Persona",
                     notes="Reminder confirmation"),
    ])

    # ── Technical ──────────────────────────────────────────────────────
    phrases.extend([
        AuditPhrase("P-08", "The Qwen 3.5 model is running with 35 billion parameters.", "Technical",
                     notes="Model name pronunciation"),
        AuditPhrase("P-09", "Kerberoasting is a common Active Directory attack.", "Technical",
                     notes="Cybersecurity term"),
        AuditPhrase("P-10", "Cobalt Strike uses beacon for command and control.", "Technical",
                     notes="Cybersecurity tool name"),
        AuditPhrase("P-11", "The SIEM detected anomalous network traffic.", "Technical",
                     notes="Acronym: S-I-E-M or 'seem'"),
    ])

    # ── Normalizer ─────────────────────────────────────────────────────
    phrases.extend([
        AuditPhrase("P-12", "The server is at 192.168.1.100 on port 443.", "Normalizer",
                     notes="IP + port normalization"),
        AuditPhrase("P-13", "Check the file at /home/user/config.yaml.", "Normalizer",
                     notes="File path normalization"),
        AuditPhrase("P-14", "The backup is 4.7 gigabytes.", "Normalizer",
                     notes="File size normalization"),
        AuditPhrase("P-15", "HTTPS connections use TLS encryption.", "Normalizer",
                     notes="Acronym pronunciation: H-T-T-P-S, T-L-S"),
        AuditPhrase("P-16", "The CPU temperature is 65 degrees Celsius.", "Normalizer",
                     notes="Number + unit normalization"),
    ])

    return phrases


# ===========================================================================
# Generation
# ===========================================================================

def generate_wavs(phrases: List[AuditPhrase], tts, output_dir: str):
    """Generate WAV files for all phrases, return manifest data."""
    os.makedirs(output_dir, exist_ok=True)
    manifest = []

    for phrase in phrases:
        # Generate filename from text
        safe_name = re.sub(r'[^\w]', '_', phrase.text[:40]).strip('_')
        filename = f"{phrase.id}_{safe_name}.wav"
        filepath = os.path.join(output_dir, filename)

        # Get normalized text for display
        normalized = phrase.text
        if phrase.normalize and tts.normalization_enabled and tts.normalizer:
            normalized = tts.normalizer.normalize(phrase.text)

        # Generate
        t0 = time.perf_counter()
        wav_bytes = tts.generate_wav(phrase.text, normalize=phrase.normalize)
        gen_time = time.perf_counter() - t0

        if not wav_bytes:
            print(f"  ! {phrase.id}: TTS produced no audio — skipping")
            continue

        # Compute duration from WAV
        import wave, io
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
            duration = wf.getnframes() / wf.getframerate()

        # Save
        with open(filepath, 'wb') as f:
            f.write(wav_bytes)

        entry = {
            "id": phrase.id,
            "category": phrase.category,
            "original": phrase.text,
            "normalized": normalized,
            "notes": phrase.notes,
            "filename": filename,
            "duration_s": round(duration, 2),
            "gen_time_s": round(gen_time, 2),
        }
        manifest.append(entry)
        print(f"  {phrase.id}: {phrase.text[:50]}  ({duration:.1f}s, gen {gen_time:.2f}s)")

    # Save manifest
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    return manifest


# ===========================================================================
# Interactive playback
# ===========================================================================

def interactive_review(manifest: list, output_dir: str, audio_device: str):
    """Play each WAV and prompt for pass/fail/skip/replay."""
    print(f"\n{'=' * 60}")
    print("  Interactive Pronunciation Review")
    print(f"  Controls: [p]ass  [f]ail  [s]kip  [r]eplay  [q]uit")
    print(f"{'=' * 60}\n")

    results = {"pass": 0, "fail": 0, "skip": 0}
    fail_list = []

    for entry in manifest:
        filepath = os.path.join(output_dir, entry["filename"])
        if not os.path.exists(filepath):
            continue

        print(f"  {entry['id']} [{entry['category']}]")
        print(f"  Original:   {entry['original']}")
        if entry['normalized'] != entry['original']:
            print(f"  Normalized: {entry['normalized']}")
        if entry['notes']:
            print(f"  Notes:      {entry['notes']}")

        while True:
            # Play audio
            try:
                proc = subprocess.run(
                    ["aplay", "-D", audio_device, filepath],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                print("  (playback timed out)")
            except FileNotFoundError:
                print("  (aplay not found — cannot play)")
                break

            # Get verdict
            try:
                choice = input("  Verdict [p/f/s/r/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = 'q'

            if choice == 'p':
                results["pass"] += 1
                print(f"  -> PASS\n")
                break
            elif choice == 'f':
                results["fail"] += 1
                fail_list.append(entry['id'])
                note = input("  Issue (optional): ").strip()
                if note:
                    print(f"  -> FAIL: {note}\n")
                else:
                    print(f"  -> FAIL\n")
                break
            elif choice == 's':
                results["skip"] += 1
                print(f"  -> SKIP\n")
                break
            elif choice == 'r':
                print("  (replaying...)")
                continue
            elif choice == 'q':
                print("\n  Review ended early.")
                break
        else:
            continue

        if choice == 'q':
            break

    # Summary
    print(f"\n{'=' * 60}")
    total = results["pass"] + results["fail"]
    print(f"  Review complete: {results['pass']}/{total} passed"
          f" ({results['skip']} skipped)")
    if fail_list:
        print(f"  Failed: {', '.join(fail_list)}")
    print(f"{'=' * 60}")


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="JARVIS Pronunciation Audit")
    parser.add_argument("-o", "--output", default="/tmp/jarvis_pronunciation_audit",
                        help="Output directory for WAV files (default: /tmp/...)")
    parser.add_argument("--play", action="store_true",
                        help="Interactive playback after generation")
    parser.add_argument("--custom", metavar="TEXT",
                        help="Synthesize a custom phrase instead of the curated list")
    parser.add_argument("--category", metavar="CAT",
                        help="Filter by category (Names, Persona, Technical, Normalizer)")
    args = parser.parse_args()

    print("=" * 60)
    print("  JARVIS Pronunciation Audit")
    print("=" * 60)

    # Init TTS
    print("\nInitializing Kokoro TTS...", end=" ", flush=True)
    from core.config import load_config
    from core.tts import TextToSpeech
    config = load_config()
    tts = TextToSpeech(config)
    audio_device = config.get("audio.output_device", "default")
    print("done\n")

    # Build phrase list
    if args.custom:
        phrases = [AuditPhrase("C-01", args.custom, "Custom")]
    else:
        phrases = get_audit_phrases()
        if args.category:
            phrases = [p for p in phrases if p.category.lower() == args.category.lower()]
            if not phrases:
                print(f"No phrases in category '{args.category}'")
                print(f"Available: Names, Persona, Technical, Normalizer")
                sys.exit(1)

    print(f"Generating {len(phrases)} phrase(s) to {args.output}/\n")

    # Generate
    manifest = generate_wavs(phrases, tts, args.output)

    print(f"\n  {len(manifest)} WAV files saved to {args.output}/")
    print(f"  Manifest: {args.output}/manifest.json")

    # Interactive review
    if args.play:
        interactive_review(manifest, args.output, audio_device)

    # Use os._exit to avoid ROCm/ONNX teardown abort()
    os._exit(0)


if __name__ == "__main__":
    main()
