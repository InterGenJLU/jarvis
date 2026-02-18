#!/usr/bin/env python3
"""Enroll a speaker's voice for JARVIS speaker identification.

Records short audio clips from the microphone, extracts d-vector embeddings
via resemblyzer, and saves the averaged embedding to the user's profile.

Usage:
    python3 scripts/enroll_speaker.py                    # Interactive enrollment
    python3 scripts/enroll_speaker.py --user your_username  # Enroll specific user
    python3 scripts/enroll_speaker.py --test              # Test identification
    python3 scripts/enroll_speaker.py --list              # List enrollment status
    python3 scripts/enroll_speaker.py --clips 5           # Record 5 clips (default 3)
    python3 scripts/enroll_speaker.py --duration 4        # 4-second clips (default 3)
"""

import sys
import time
import argparse
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config
from core.user_profile import get_profile_manager
from core.speaker_id import SpeakerIdentifier


def get_mic_device(config):
    """Resolve the microphone device index from config."""
    import sounddevice as sd

    device_name = config.get("audio.mic_device")
    if not device_name:
        return None  # Use system default

    for i, dev in enumerate(sd.query_devices()):
        if device_name.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i

    print(f"  Warning: mic device '{device_name}' not found, using system default")
    return None


def record_clip(duration: float, sample_rate: int = 16000,
                device=None) -> np.ndarray:
    """Record a single audio clip from the microphone.

    Returns:
        Float32 mono audio array at the given sample rate.
    """
    import sounddevice as sd

    print(f"  Recording for {duration}s...", end="", flush=True)
    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device,
    )
    sd.wait()
    print(" done.")
    return audio.flatten()


def check_audio_level(audio: np.ndarray) -> float:
    """Return RMS energy of the audio clip."""
    return float(np.sqrt(np.mean(audio ** 2)))


def enroll_interactive(config, user_id: str, num_clips: int = 3,
                       clip_duration: float = 3.0):
    """Interactive enrollment flow: record clips, enroll, verify."""
    pm = get_profile_manager(config)
    profile = pm.get_profile(user_id)
    if not profile:
        print(f"  Error: profile '{user_id}' not found. Run init_profiles.py first.")
        return False

    sid = SpeakerIdentifier(config, pm)
    sid.load_embeddings()

    mic_device = get_mic_device(config)
    sample_rate = 16000

    print(f"\n  Enrolling: {profile['name']} ({profile['honorific']})")
    print(f"  Clips: {num_clips} x {clip_duration}s")
    print(f"  Mic: {mic_device or 'system default'}")
    print()
    print("  Just speak naturally during each recording — say anything at all.")
    print("  Content doesn't matter; it's capturing your voice signature.")
    print()

    # Record clips
    clips = []
    for i in range(num_clips):
        input(f"  Press Enter to record clip {i + 1}/{num_clips}...")

        # Countdown
        for sec in [3, 2, 1]:
            print(f"    {sec}...", flush=True)
            time.sleep(1)

        audio = record_clip(clip_duration, sample_rate, mic_device)
        rms = check_audio_level(audio)
        print(f"    Audio level: {rms:.4f} RMS")

        if rms < 0.005:
            print("    Warning: very low audio level — mic may not be picking up speech")
            retry = input("    Retry this clip? [y/N] ").strip().lower()
            if retry == "y":
                continue

        clips.append((audio, sample_rate))
        print()

    if not clips:
        print("  No valid clips recorded. Aborting.")
        return False

    # Enroll
    print(f"  Enrolling from {len(clips)} clips...")
    success = sid.enroll_from_multiple(user_id, clips)

    if success:
        print(f"  Enrollment successful for {profile['name']}!")

        # Verification test
        print("\n  Let's verify — record one more clip for testing.")
        input("  Press Enter when ready...")
        for sec in [3, 2, 1]:
            print(f"    {sec}...", flush=True)
            time.sleep(1)

        test_audio = record_clip(clip_duration, sample_rate, mic_device)
        is_match, score = sid.verify(user_id, test_audio, sample_rate)
        threshold = config.get("user_profiles.similarity_threshold", 0.85)

        print(f"\n  Verification: {'PASS' if is_match else 'FAIL'}")
        print(f"  Score: {score:.3f} (threshold: {threshold})")

        if not is_match:
            print("  Tip: try enrolling again with more clips or in a quieter environment.")
    else:
        print("  Enrollment failed — audio may be too short or noisy.")

    return success


def test_identification(config, clip_duration: float = 3.0):
    """Record a clip and identify the speaker."""
    pm = get_profile_manager(config)
    sid = SpeakerIdentifier(config, pm)
    sid.load_embeddings()

    if not sid._cache:
        print("  No enrolled speakers. Run enrollment first.")
        return

    print(f"\n  Enrolled speakers: {list(sid._cache.keys())}")
    mic_device = get_mic_device(config)
    sample_rate = 16000

    input("\n  Press Enter to record a test clip...")
    for sec in [3, 2, 1]:
        print(f"    {sec}...", flush=True)
        time.sleep(1)

    audio = record_clip(clip_duration, sample_rate, mic_device)
    rms = check_audio_level(audio)
    print(f"  Audio level: {rms:.4f} RMS")

    user_id, confidence = sid.identify(audio, sample_rate)
    threshold = config.get("user_profiles.similarity_threshold", 0.85)

    if user_id:
        profile = pm.get_profile(user_id)
        print(f"\n  Identified: {profile['name']} ({profile['honorific']})")
        print(f"  Confidence: {confidence:.3f} (threshold: {threshold})")
    else:
        print(f"\n  Unknown speaker (best score: {confidence:.3f}, threshold: {threshold})")


def list_enrollment(config):
    """Show enrollment status for all profiles."""
    pm = get_profile_manager(config)
    profiles = pm.get_all()

    if not profiles:
        print("  (no profiles)")
        return

    for p in profiles:
        emb_path = p.get("embedding_path")
        if emb_path and Path(emb_path).exists():
            emb = np.load(emb_path)
            status = f"enrolled (dim={emb.shape[0]}, norm={np.linalg.norm(emb):.3f})"
        elif emb_path:
            status = "ERROR: embedding file missing"
        else:
            status = "not enrolled"

        print(f"  {p['id']:15s} | {p['name']:15s} | {p['honorific']:6s} | {status}")


def main():
    parser = argparse.ArgumentParser(
        description="JARVIS Speaker Enrollment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user", type=str, help="User ID to enroll")
    parser.add_argument("--test", action="store_true", help="Test identification")
    parser.add_argument("--list", action="store_true", help="List enrollment status")
    parser.add_argument("--clips", type=int, default=3, help="Number of clips (default: 3)")
    parser.add_argument("--duration", type=float, default=3.0,
                        help="Clip duration in seconds (default: 3.0)")
    args = parser.parse_args()

    print("JARVIS Speaker Enrollment")
    print("=" * 40)

    config = Config()
    pm = get_profile_manager(config)

    if args.list:
        print("\nEnrollment status:")
        list_enrollment(config)
        return

    if args.test:
        test_identification(config, args.duration)
        return

    # Enrollment mode
    if args.user:
        user_id = args.user
    else:
        # Interactive: pick from available profiles
        profiles = pm.get_all()
        if not profiles:
            print("  No profiles found. Run init_profiles.py first.")
            return

        print("\nAvailable profiles:")
        for i, p in enumerate(profiles, 1):
            emb = "enrolled" if p.get("embedding_path") else "not enrolled"
            print(f"  {i}. {p['id']} ({p['name']}, {p['honorific']}) — {emb}")

        choice = input("\nSelect profile number: ").strip()
        try:
            idx = int(choice) - 1
            user_id = profiles[idx]["id"]
        except (ValueError, IndexError):
            print("  Invalid selection.")
            return

    enroll_interactive(config, user_id, args.clips, args.duration)


if __name__ == "__main__":
    main()
