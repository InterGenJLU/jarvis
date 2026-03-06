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
    import threading

    num_frames = int(duration * sample_rate)
    audio = np.zeros((num_frames, 1), dtype="float32")
    done_event = threading.Event()

    print(f"  Recording for {duration}s...", end="", flush=True)

    try:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=device,
        )
        stream.start()
        frames_read = 0
        deadline = time.time() + duration + 3  # 3s grace
        while frames_read < num_frames:
            remaining = num_frames - frames_read
            chunk, overflowed = stream.read(min(remaining, sample_rate // 10))
            n = min(len(chunk), remaining)
            audio[frames_read:frames_read + n] = chunk[:n]
            frames_read += n
            if time.time() > deadline:
                print(f" TIMEOUT!")
                break
        stream.stop()
        stream.close()
    except Exception as e:
        print(f" ERROR: {e}")
        print("    Hint: is the JARVIS voice service holding the mic?")
        print("    Try: systemctl --user stop jarvis")
        return audio.flatten()

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

    import sounddevice as sd
    mic_device = get_mic_device(config)
    device_info = sd.query_devices(mic_device if mic_device is not None else sd.default.device[0])
    sample_rate = int(device_info['default_samplerate'])

    print(f"\n  Enrolling: {profile['name']} ({profile['honorific']})")
    print(f"  Clips: {num_clips} x {clip_duration}s")
    print(f"  Mic: {mic_device or 'system default'} ({sample_rate} Hz)")
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


def _compute_all_scores(sid, audio, sample_rate):
    """Extract embedding and compute per-speaker cosine similarities.

    Returns:
        (embedding, scores_dict)  where scores_dict maps user_id → float score.
        embedding may be all-zeros if audio was too short.
    """
    embedding = sid.extract_embedding(audio, sample_rate)
    scores = {}
    for user_id, (enrolled_emb, _hon) in sid._cache.items():
        score = float(np.dot(embedding, enrolled_emb) / (
            np.linalg.norm(embedding) * np.linalg.norm(enrolled_emb) + 1e-8
        ))
        scores[user_id] = score
    return embedding, scores


def test_identification(config, clip_duration: float = 3.0):
    """Record a clip and identify the speaker (with full diagnostics)."""
    from resemblyzer import preprocess_wav

    pm = get_profile_manager(config)
    sid = SpeakerIdentifier(config, pm)
    sid.load_embeddings()

    if not sid._cache:
        print("  No enrolled speakers. Run enrollment first.")
        return

    print(f"\n  Enrolled speakers: {list(sid._cache.keys())}")
    threshold = config.get("user_profiles.similarity_threshold", 0.85)
    print(f"  Threshold: {threshold}")

    # Show cross-similarity between enrolled embeddings
    ids = list(sid._cache.keys())
    if len(ids) >= 2:
        print("\n  Enrolled embedding cross-similarity:")
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                e1 = sid._cache[ids[i]][0]
                e2 = sid._cache[ids[j]][0]
                cross = float(np.dot(e1, e2) / (
                    np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-8
                ))
                print(f"    {ids[i]} vs {ids[j]}: {cross:.3f}")

    import sounddevice as sd
    mic_device = get_mic_device(config)
    device_info = sd.query_devices(mic_device if mic_device is not None else sd.default.device[0])
    sample_rate = int(device_info['default_samplerate'])

    input(f"\n  Press Enter to record a test clip ({clip_duration}s)...")
    for sec in [3, 2, 1]:
        print(f"    {sec}...", flush=True)
        time.sleep(1)

    audio = record_clip(clip_duration, sample_rate, mic_device)
    rms = check_audio_level(audio)
    print(f"\n  Audio stats:")
    print(f"    Duration: {len(audio)/sample_rate:.2f}s at {sample_rate}Hz")
    print(f"    RMS: {rms:.4f}")

    # Show post-preprocessing stats
    if sample_rate != 16000:
        duration = len(audio) / sample_rate
        target_len = int(duration * 16000)
        audio_16k = np.interp(
            np.linspace(0, len(audio) - 1, target_len),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
    else:
        audio_16k = audio
    processed = preprocess_wav(audio_16k, source_sr=16000)
    print(f"    After preprocess_wav: {len(processed)/16000:.2f}s "
          f"({len(processed)} samples)")

    # Per-speaker scores
    embedding, scores = _compute_all_scores(sid, audio, sample_rate)
    emb_norm = float(np.linalg.norm(embedding))
    print(f"    Embedding norm: {emb_norm:.3f}")

    print(f"\n  Per-speaker scores:")
    best_id = max(scores, key=scores.get) if scores else None
    for uid, score in sorted(scores.items(), key=lambda x: -x[1]):
        profile = pm.get_profile(uid)
        name = profile['name'] if profile else uid
        marker = " ← BEST" if uid == best_id else ""
        above = " ✓" if score >= threshold else " ✗"
        print(f"    {name:15s}: {score:.3f}{above}{marker}")

    if best_id and scores[best_id] >= threshold:
        profile = pm.get_profile(best_id)
        print(f"\n  Result: IDENTIFIED as {profile['name']}")
    else:
        print(f"\n  Result: UNKNOWN (best={scores.get(best_id, 0):.3f}, "
              f"need≥{threshold})")


def diagnose_speaker_id(config, clip_duration: float = 3.0, num_clips: int = 5):
    """Comprehensive speaker ID diagnostic: multiple clips + statistical analysis."""
    from resemblyzer import preprocess_wav

    pm = get_profile_manager(config)
    sid = SpeakerIdentifier(config, pm)
    sid.load_embeddings()

    if not sid._cache:
        print("  No enrolled speakers. Run enrollment first.")
        return

    threshold = config.get("user_profiles.similarity_threshold", 0.85)
    ids = list(sid._cache.keys())

    print(f"\n  Enrolled speakers: {ids}")
    print(f"  Current threshold: {threshold}")
    print(f"  Clips to record: {num_clips} x {clip_duration}s")

    # Cross-similarity
    if len(ids) >= 2:
        print("\n  Enrolled cross-similarity:")
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                e1 = sid._cache[ids[i]][0]
                e2 = sid._cache[ids[j]][0]
                cross = float(np.dot(e1, e2) / (
                    np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-8
                ))
                print(f"    {ids[i]} vs {ids[j]}: {cross:.3f}")

    import sounddevice as sd
    mic_device = get_mic_device(config)
    device_info = sd.query_devices(mic_device if mic_device is not None else sd.default.device[0])
    sample_rate = int(device_info['default_samplerate'])

    print(f"\n  We'll record {num_clips} clips. Speak naturally — vary your "
          f"sentences, volume, and distance from mic.")
    print(f"  This simulates real usage conditions.\n")

    # Collect clips and scores
    all_scores = {uid: [] for uid in ids}
    all_rms = []
    all_post_duration = []

    for clip_num in range(1, num_clips + 1):
        input(f"  Press Enter for clip {clip_num}/{num_clips}...")
        for sec in [3, 2, 1]:
            print(f"    {sec}...", flush=True)
            time.sleep(1)

        audio = record_clip(clip_duration, sample_rate, mic_device)
        rms = check_audio_level(audio)
        all_rms.append(rms)

        # Preprocess stats
        if sample_rate != 16000:
            duration = len(audio) / sample_rate
            target_len = int(duration * 16000)
            audio_16k = np.interp(
                np.linspace(0, len(audio) - 1, target_len),
                np.arange(len(audio)),
                audio,
            ).astype(np.float32)
        else:
            audio_16k = audio
        processed = preprocess_wav(audio_16k, source_sr=16000)
        post_dur = len(processed) / 16000
        all_post_duration.append(post_dur)

        # Scores
        _emb, scores = _compute_all_scores(sid, audio, sample_rate)
        for uid in ids:
            all_scores[uid].append(scores.get(uid, 0.0))

        best_uid = max(scores, key=scores.get)
        best_s = scores[best_uid]
        scores_str = " | ".join(f"{u}={s:.3f}" for u, s in scores.items())
        match = "✓" if best_s >= threshold else "✗"
        print(f"    RMS={rms:.4f} post={post_dur:.2f}s | {scores_str} {match}\n")

    # Statistical summary
    print("=" * 60)
    print("  DIAGNOSTIC SUMMARY")
    print("=" * 60)
    print(f"\n  Audio quality:")
    print(f"    RMS range: {min(all_rms):.4f} – {max(all_rms):.4f} "
          f"(mean={np.mean(all_rms):.4f})")
    print(f"    Post-preprocess duration: {min(all_post_duration):.2f}s – "
          f"{max(all_post_duration):.2f}s "
          f"(mean={np.mean(all_post_duration):.2f}s)")

    print(f"\n  Per-speaker score statistics:")
    for uid in ids:
        scores = all_scores[uid]
        profile = pm.get_profile(uid)
        name = profile['name'] if profile else uid
        above = sum(1 for s in scores if s >= threshold)
        print(f"    {name:15s}: mean={np.mean(scores):.3f} "
              f"min={min(scores):.3f} max={max(scores):.3f} "
              f"std={np.std(scores):.3f} "
              f"hits={above}/{len(scores)}")

    # Recommendations
    print(f"\n  Current threshold: {threshold}")
    best_speaker = max(ids, key=lambda u: np.mean(all_scores[u]))
    best_mean = np.mean(all_scores[best_speaker])
    best_max = max(all_scores[best_speaker])

    print(f"\n  RECOMMENDATIONS:")
    if best_max >= 0.85:
        print(f"    Scores CAN reach 0.85+ (max={best_max:.3f}).")
        print(f"    Consider re-enrolling with more clips at your desk for "
              f"higher average scores.")
    elif best_max >= 0.75:
        print(f"    Scores peak at {best_max:.3f}. Lower threshold to 0.75 "
              f"and re-enroll with 5-6 clips.")
    elif best_max >= 0.65:
        print(f"    Scores peak at {best_max:.3f}. The enrollment embedding "
              f"doesn't match well.")
        print(f"    RE-ENROLL with --clips 6 --duration 5 at your desk, then "
              f"re-diagnose.")
    else:
        print(f"    Scores are very low (max={best_max:.3f}). Something may be "
              f"wrong with audio capture.")
        print(f"    Check mic placement, background noise, and audio device.")

    # Suggest optimal threshold
    if len(ids) >= 2:
        # Find threshold that separates target from others
        target_scores = all_scores[best_speaker]
        other_scores = [s for uid in ids if uid != best_speaker
                        for s in all_scores[uid]]
        if other_scores:
            gap_mid = (np.mean(target_scores) + max(other_scores)) / 2
            print(f"\n    Suggested threshold (midpoint between best speaker "
                  f"avg and other max): {gap_mid:.3f}")


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
    parser.add_argument("--diagnose", action="store_true",
                        help="Comprehensive multi-clip diagnostic")
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

    if args.diagnose:
        diagnose_speaker_id(config, args.duration, args.clips or 5)
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
