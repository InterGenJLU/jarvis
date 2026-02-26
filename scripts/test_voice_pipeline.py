#!/usr/bin/env python3
"""
Voice Pipeline Round-Trip Test Suite for JARVIS

Automated TTS→STT round-trip tests. Generates WAV audio via Kokoro TTS,
feeds it into Whisper STT, and compares transcription against expectations.

This validates the full audio pipeline without a microphone — catches
pronunciation issues, Whisper correction gaps, and TTS normalization
round-trip failures.

Requirements:
    - Kokoro TTS initialized (CPU, ~3s)
    - Whisper STT initialized (GPU/CTranslate2, ~2s)
    - No mic or speakers needed

Usage:
    python3 scripts/test_voice_pipeline.py              # All phases
    python3 scripts/test_voice_pipeline.py --phase V2   # Single phase
    python3 scripts/test_voice_pipeline.py --id V2-03   # Single test
    python3 scripts/test_voice_pipeline.py --verbose     # Show all (not just failures)
    python3 scripts/test_voice_pipeline.py --save-wav /tmp/debug  # Save WAVs
    python3 scripts/test_voice_pipeline.py --json        # JSON output
"""

import os
os.environ['HSA_OVERRIDE_GFX_VERSION'] = '11.0.0'
os.environ['ROCM_PATH'] = '/opt/rocm-7.2.0'
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

# Line-buffer stdout — Kokoro/espeak phonemizer forks during init,
# and the child process flushes inherited stdout buffers on exit,
# causing duplicate output. Line buffering keeps the buffer empty.
import sys
sys.stdout.reconfigure(line_buffering=True)

import time
import json
import argparse
import warnings
import re
import io
import wave
from dataclasses import dataclass, field
from typing import Optional, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


# ===========================================================================
# Test case data structure
# ===========================================================================

@dataclass
class VoiceTestCase:
    id: str                                    # "V1-01"
    input_text: str                            # Text to synthesize
    phase: str                                 # "V1", "V2", etc.
    category: str                              # Human-readable group name
    normalize: bool = True                     # Whether to TTS-normalize

    # Comparison expectations (at least one required)
    expect_exact: Optional[str] = None         # Normalized exact match
    expect_contains: Optional[List[str]] = None  # Must contain all keywords
    expect_not_contains: Optional[List[str]] = None  # Must NOT contain these
    max_wer: float = 0.3                       # Max word error rate (0.0-1.0)

    notes: str = ""


# ===========================================================================
# Test results tracking
# ===========================================================================

class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results = []

    def record(self, test_id, passed, detail=""):
        if passed:
            self.passed += 1
            self.results.append((test_id, "PASS", detail))
        else:
            self.failed += 1
            self.results.append((test_id, "FAIL", detail))

    def skip(self, test_id, reason=""):
        self.skipped += 1
        self.results.append((test_id, "SKIP", reason))

    @property
    def total(self):
        return self.passed + self.failed

    def to_json(self):
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "pass_rate": f"{self.passed / self.total * 100:.1f}%" if self.total else "N/A",
            "tests": [{"id": r[0], "status": r[1], "detail": r[2]} for r in self.results],
        }


# ===========================================================================
# Comparison helpers
# ===========================================================================

def normalize_text(text: str) -> str:
    """Normalize for comparison: lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Compute WER using Levenshtein distance on word sequences."""
    ref_words = normalize_text(reference).split()
    hyp_words = normalize_text(hypothesis).split()

    if not ref_words:
        return 0.0 if not hyp_words else 1.0

    # Dynamic programming edit distance
    d = [[0] * (len(hyp_words) + 1) for _ in range(len(ref_words) + 1)]
    for i in range(len(ref_words) + 1):
        d[i][0] = i
    for j in range(len(hyp_words) + 1):
        d[0][j] = j

    for i in range(1, len(ref_words) + 1):
        for j in range(1, len(hyp_words) + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j], d[i][j - 1], d[i - 1][j - 1])

    return d[len(ref_words)][len(hyp_words)] / len(ref_words)


# ===========================================================================
# Test definitions
# ===========================================================================

def get_voice_tests() -> List[VoiceTestCase]:
    """Define all voice round-trip test cases."""
    tests = []

    # ── V1: Core Commands ──────────────────────────────────────────────
    # These are routing-critical phrases that MUST survive TTS→STT
    tests.extend([
        VoiceTestCase(
            id="V1-01", phase="V1", category="Core Commands",
            input_text="What time is it?",
            expect_contains=["what", "time"],
        ),
        VoiceTestCase(
            id="V1-02", phase="V1", category="Core Commands",
            input_text="What's the weather like?",
            expect_contains=["weather"],
        ),
        VoiceTestCase(
            id="V1-03", phase="V1", category="Core Commands",
            input_text="Set a reminder for tomorrow at nine AM.",
            expect_contains=["reminder", "tomorrow"],
        ),
        VoiceTestCase(
            id="V1-04", phase="V1", category="Core Commands",
            input_text="Search the web for Python tutorials.",
            expect_contains=["search", "python"],
        ),
        VoiceTestCase(
            id="V1-05", phase="V1", category="Core Commands",
            input_text="Open Google Chrome.",
            expect_contains=["open", "chrome"],
        ),
        VoiceTestCase(
            id="V1-06", phase="V1", category="Core Commands",
            input_text="What CPU do I have?",
            expect_contains=["cpu"],
        ),
        VoiceTestCase(
            id="V1-07", phase="V1", category="Core Commands",
            input_text="Find my config file.",
            expect_contains=["find", "config"],
        ),
        VoiceTestCase(
            id="V1-08", phase="V1", category="Core Commands",
            input_text="Good morning, Jarvis.",
            expect_contains=["morning", "jarvis"],
        ),
    ])

    # ── V2: Brand Names ────────────────────────────────────────────────
    # Tests Whisper's ability to transcribe technical brand names
    # that Kokoro pronounces. Some of these have Whisper corrections.
    tests.extend([
        VoiceTestCase(
            id="V2-01", phase="V2", category="Brand Names",
            input_text="The AMD Ryzen processor is fast.",
            expect_contains=["amd", "ryzen"],
            notes="Tests AMD brand name pronunciation",
        ),
        VoiceTestCase(
            id="V2-02", phase="V2", category="Brand Names",
            input_text="NVIDIA graphics cards use CUDA.",
            expect_contains=["graphics", "cuda"],
            max_wer=0.5,
            notes="Kokoro pronounces NVIDIA as 'in-video' — Whisper can't recover. Check surrounding words.",
        ),
        VoiceTestCase(
            id="V2-03", phase="V2", category="Brand Names",
            input_text="Ubuntu Linux is a popular operating system.",
            expect_contains=["ubuntu", "linux"],
        ),
        VoiceTestCase(
            id="V2-04", phase="V2", category="Brand Names",
            input_text="Python is a programming language.",
            expect_contains=["python", "programming"],
        ),
        VoiceTestCase(
            id="V2-05", phase="V2", category="Brand Names",
            input_text="The Raspberry Pi runs on ARM chips.",
            expect_contains=["raspberry", "pi"],
        ),
        VoiceTestCase(
            id="V2-06", phase="V2", category="Brand Names",
            input_text="Docker containers run in Kubernetes.",
            expect_contains=["docker", "kubernetes"],
        ),
    ])

    # ── V3: Normalizer Round-Trip ──────────────────────────────────────
    # TTS normalizer converts IPs/ports/sizes to spoken form.
    # Whisper hears the SPOKEN form, so we check for spoken keywords.
    tests.extend([
        VoiceTestCase(
            id="V3-01", phase="V3", category="Normalizer Round-Trip",
            input_text="The server is at 192.168.1.100.",
            expect_contains=["server", "192"],
            notes="TTS normalizes IP to spoken form; Whisper reconstructs digits",
        ),
        VoiceTestCase(
            id="V3-02", phase="V3", category="Normalizer Round-Trip",
            input_text="Running on port 8080.",
            expect_contains=["port", "8080"],
            notes="TTS normalizes port to spoken form; Whisper reconstructs digits",
        ),
        VoiceTestCase(
            id="V3-03", phase="V3", category="Normalizer Round-Trip",
            input_text="The file is 2.5 megabytes.",
            expect_contains=["file", "25"],
            max_wer=0.5,
            notes="Whisper may reconstruct as '2.5Mb' or similar",
        ),
        VoiceTestCase(
            id="V3-04", phase="V3", category="Normalizer Round-Trip",
            input_text="Check the file at slash home slash user slash config.",
            normalize=False,
            expect_contains=["slash", "home", "config"],
            notes="Pre-normalized path — tests raw pronunciation",
        ),
    ])

    # ── V4: Contractions ───────────────────────────────────────────────
    # Whisper sometimes struggles with contractions
    tests.extend([
        VoiceTestCase(
            id="V4-01", phase="V4", category="Contractions",
            input_text="What's the temperature outside?",
            expect_contains=["temperature"],
        ),
        VoiceTestCase(
            id="V4-02", phase="V4", category="Contractions",
            input_text="I don't know the answer.",
            expect_contains=["don't", "answer"],
        ),
        VoiceTestCase(
            id="V4-03", phase="V4", category="Contractions",
            input_text="Can't you help me with this?",
            expect_contains=["help"],
        ),
        VoiceTestCase(
            id="V4-04", phase="V4", category="Contractions",
            input_text="It's been a long day, hasn't it?",
            expect_contains=["long", "day"],
        ),
    ])

    # ── V5: Edge Cases ─────────────────────────────────────────────────
    # Single words and short phrases
    tests.extend([
        VoiceTestCase(
            id="V5-01", phase="V5", category="Edge Cases",
            input_text="Yes.",
            expect_contains=["yes"],
        ),
        VoiceTestCase(
            id="V5-02", phase="V5", category="Edge Cases",
            input_text="No, thank you.",
            expect_contains=["no"],
        ),
        VoiceTestCase(
            id="V5-03", phase="V5", category="Edge Cases",
            input_text="Jarvis, stop.",
            expect_contains=["jarvis"],
        ),
    ])

    return tests


# ===========================================================================
# Engine initialization
# ===========================================================================

def init_engines():
    """Initialize TTS and STT engines (one-time, shared across tests).

    STT must load BEFORE TTS to avoid CTranslate2/torch fork conflicts:
    Kokoro imports torch (which initializes ROCm), and if CTranslate2
    loads afterward in the same process, the CUDA context fork can hang.
    """
    import numpy as np
    from core.config import load_config

    print("Initializing engines...")
    t0 = time.perf_counter()

    config = load_config()

    # STT first — CTranslate2/ROCm must initialize before torch
    print("  Loading Whisper STT (GPU)...", end=" ", flush=True)
    t1 = time.perf_counter()
    from core.stt import SpeechToText
    stt = SpeechToText(config)
    print(f"done ({time.perf_counter() - t1:.1f}s)")

    print("  Loading Kokoro TTS (CPU)...", end=" ", flush=True)
    t1 = time.perf_counter()
    from core.tts import TextToSpeech
    tts = TextToSpeech(config)
    print(f"done ({time.perf_counter() - t1:.1f}s)")

    elapsed = time.perf_counter() - t0
    print(f"  Engines ready in {elapsed:.1f}s\n")

    return tts, stt, np


# ===========================================================================
# Test runner
# ===========================================================================

def run_voice_test(test: VoiceTestCase, tts, stt, np_mod, save_dir=None):
    """Run a single voice round-trip test.

    Returns (passed: bool, detail: str, wer: float, transcription: str)
    """
    # Generate WAV
    try:
        t0 = time.perf_counter()
        wav_bytes = tts.generate_wav(test.input_text, normalize=test.normalize)
        gen_time = time.perf_counter() - t0
    except Exception as e:
        return False, f"TTS failed: {e}", 1.0, ""

    if not wav_bytes:
        return False, "TTS produced empty audio", 1.0, ""

    # Save WAV if requested
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        safe_name = re.sub(r'[^\w]', '_', test.input_text[:40])
        wav_path = os.path.join(save_dir, f"{test.id}_{safe_name}.wav")
        with open(wav_path, 'wb') as f:
            f.write(wav_bytes)

    # Decode WAV to numpy float32 for STT
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)

        audio_int16 = np_mod.frombuffer(raw, dtype=np_mod.int16)
        audio_float = audio_int16.astype(np_mod.float32) / 32767.0
    except Exception as e:
        return False, f"WAV decode failed: {e}", 1.0, ""

    # Transcribe
    try:
        t0 = time.perf_counter()
        transcription = stt.transcribe(audio_float, sample_rate=sample_rate)
        stt_time = time.perf_counter() - t0
    except Exception as e:
        return False, f"STT failed: {e}", 1.0, ""

    if not transcription:
        return False, "STT returned empty transcription", 1.0, ""

    trans_norm = normalize_text(transcription)

    # Check expectations
    failures = []

    # 1. Exact match
    if test.expect_exact is not None:
        expected_norm = normalize_text(test.expect_exact)
        if trans_norm != expected_norm:
            failures.append(f"exact: expected '{expected_norm}', got '{trans_norm}'")

    # 2. Contains-all-keywords (normalize keyword too — strips punctuation)
    if test.expect_contains:
        for kw in test.expect_contains:
            kw_norm = normalize_text(kw)
            if kw_norm not in trans_norm:
                failures.append(f"missing keyword '{kw}'")

    # 3. Not-contains
    if test.expect_not_contains:
        for kw in test.expect_not_contains:
            kw_norm = normalize_text(kw)
            if kw_norm in trans_norm:
                failures.append(f"unwanted keyword '{kw}' found")

    # 4. WER
    wer = word_error_rate(test.input_text, transcription)
    if wer > test.max_wer:
        failures.append(f"WER {wer:.0%} exceeds max {test.max_wer:.0%}")

    passed = len(failures) == 0
    detail_parts = [f"WER={wer:.0%}", f"TTS={gen_time:.2f}s", f"STT={stt_time:.2f}s"]
    if failures:
        detail_parts.extend(failures)
    detail = " | ".join(detail_parts)

    return passed, detail, wer, transcription


def run_tests(tests: List[VoiceTestCase], tts, stt, np_mod,
              verbose=False, save_dir=None):
    """Run all tests and return results."""
    results = TestResults()
    current_phase = None

    for test in tests:
        # Phase header
        if test.phase != current_phase:
            current_phase = test.phase
            print(f"\n{'─' * 60}")
            print(f"  Phase {test.phase}: {test.category}")
            print(f"{'─' * 60}")

        passed, detail, wer, transcription = run_voice_test(
            test, tts, stt, np_mod, save_dir
        )
        results.record(test.id, passed, detail)

        # Display
        status = "✓" if passed else "✗"
        if not passed or verbose:
            print(f"  {status} {test.id}: {test.input_text}")
            if transcription:
                print(f"         → \"{transcription}\"")
            print(f"         {detail}")
            if test.notes:
                print(f"         Note: {test.notes}")
        elif passed:
            # Compact pass line
            print(f"  {status} {test.id}: {test.input_text[:50]}  (WER={wer:.0%})")

    return results


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="JARVIS Voice Pipeline Tests")
    parser.add_argument("--phase", help="Run single phase (V1, V2, ...)")
    parser.add_argument("--id", help="Run single test by ID (V1-01, V2-03, ...)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show all test details (not just failures)")
    parser.add_argument("--save-wav", metavar="DIR",
                        help="Save generated WAV files to directory")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    print("=" * 60)
    print("  JARVIS Voice Pipeline Tests")
    print("=" * 60)

    # Get test cases
    all_tests = get_voice_tests()

    # Filter
    if args.id:
        all_tests = [t for t in all_tests if t.id == args.id]
        if not all_tests:
            print(f"\nNo test with ID '{args.id}' found.")
            sys.exit(1)
    elif args.phase:
        all_tests = [t for t in all_tests if t.phase == args.phase]
        if not all_tests:
            print(f"\nNo tests in phase '{args.phase}' found.")
            sys.exit(1)

    print(f"\n  Running {len(all_tests)} test(s)...\n")

    # Init engines
    tts, stt, np_mod = init_engines()

    # Run
    t0 = time.perf_counter()
    results = run_tests(all_tests, tts, stt, np_mod,
                        verbose=args.verbose, save_dir=args.save_wav)
    elapsed = time.perf_counter() - t0

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  Results: {results.passed}/{results.total} passed "
          f"({results.passed / results.total * 100:.0f}%) "
          f"in {elapsed:.1f}s")
    if results.failed:
        print(f"  FAILED: {results.failed}")
        failed = [r for r in results.results if r[1] == "FAIL"]
        for test_id, _, detail in failed:
            print(f"    {test_id}: {detail}")
    print(f"{'=' * 60}")

    if args.json:
        output = results.to_json()
        output["elapsed_seconds"] = round(elapsed, 1)
        print(json.dumps(output, indent=2))

    # Use os._exit to avoid ROCm/ONNX teardown abort()
    os._exit(0 if results.failed == 0 else 1)


if __name__ == "__main__":
    main()
