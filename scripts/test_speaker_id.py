#!/usr/bin/env python3
"""
Speaker Identification tests — embedding extraction, enrollment, identification, verification.

Tests:
  Part 1: Embedding extraction (dtype, resampling, short audio)
  Part 2: Enrollment (single + multi-clip, cache, file I/O)
  Part 3: Identification (cosine similarity, threshold, multi-speaker)
  Part 4: Verification (match/mismatch, unknown user)
  Part 5: Cache management (load_embeddings, reload_profile)

Usage:
  python3 scripts/test_speaker_id.py --verbose
"""

import os
import sys
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, "/home/user/jarvis")
os.environ['JARVIS_LOG_FILE_ONLY'] = '1'

# Stub resemblyzer before importing speaker_id
_mock_resemblyzer = MagicMock()
sys.modules["resemblyzer"] = _mock_resemblyzer
sys.modules["resemblyzer.VoiceEncoder"] = MagicMock()


# ─── Test infrastructure ──────────────────────────────────────────────────

VERBOSE = "--verbose" in sys.argv


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


results: list[TestResult] = []


def log(msg):
    if VERBOSE:
        print(msg)


def assert_eq(name, actual, expected, context=""):
    ok = actual == expected
    detail = f"expected={expected!r}, got={actual!r}"
    if context:
        detail += f" [{context}]"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


def assert_true(name, condition, detail=""):
    ok = bool(condition)
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


def assert_close(name, actual, expected, tol=1e-4, detail=""):
    ok = abs(actual - expected) < tol
    if not detail:
        detail = f"expected≈{expected}, got={actual}, diff={abs(actual-expected)}"
    results.append(TestResult(name, ok, "" if ok else detail))
    if VERBOSE:
        status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  [{status}] {name}" + (f" — {detail}" if not ok else ""))
    return ok


# ─── Helpers ──────────────────────────────────────────────────────────────

def make_config(threshold=0.85):
    data = {
        "user_profiles.similarity_threshold": threshold,
        "logging.level": "WARNING",
    }
    config = MagicMock()
    config.get = lambda key, default=None: data.get(key, default)
    return config


def make_profile_manager(tmpdir):
    """Create a mock ProfileManager backed by dicts."""
    pm = MagicMock()
    pm.embeddings_dir = Path(tmpdir)
    profiles = {}

    def get_profile(uid):
        return profiles.get(uid)

    def update_profile(uid, **kwargs):
        if uid in profiles:
            profiles[uid].update(kwargs)

    def get_profiles_with_embeddings():
        return [p for p in profiles.values() if p.get("embedding_path")]

    pm.get_profile = get_profile
    pm.update_profile = update_profile
    pm.get_profiles_with_embeddings = get_profiles_with_embeddings
    pm._profiles = profiles  # Expose for test setup
    return pm


def make_deterministic_encoder(embed_fn=None):
    """Create a mock VoiceEncoder that returns deterministic embeddings.

    Default: normalised hash of the first 100 samples → 256-dim vector.
    This ensures different audio → different embeddings, same audio → same embedding.
    """
    mock_encoder = MagicMock()

    def default_embed(processed):
        # Use first few samples to seed a deterministic embedding
        rng = np.random.RandomState(int(abs(processed[:10].sum()) * 1000) % (2**31))
        emb = rng.randn(256).astype(np.float32)
        emb /= np.linalg.norm(emb)
        return emb

    mock_encoder.embed_utterance = embed_fn or default_embed
    return mock_encoder


def make_speaker_id(tmpdir, threshold=0.85, embed_fn=None):
    """Create a SpeakerIdentifier with mocked dependencies."""
    from core.speaker_id import SpeakerIdentifier

    config = make_config(threshold)
    pm = make_profile_manager(tmpdir)
    sid = SpeakerIdentifier(config, pm)

    # Inject mock encoder to skip lazy-loading resemblyzer
    sid._encoder = make_deterministic_encoder(embed_fn)

    return sid, pm


def make_audio(duration_s=3.0, sample_rate=16000, freq=440.0):
    """Generate a synthetic sine wave audio signal."""
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), dtype=np.float32)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def setup_preprocess_wav_mock():
    """Configure the resemblyzer.preprocess_wav mock to pass-through audio."""
    def mock_preprocess(audio, source_sr=16000):
        return audio  # Identity transform for testing
    _mock_resemblyzer.preprocess_wav = mock_preprocess


# ═══════════════════════════════════════════════════════════════════════════
# Part 1: Embedding extraction
# ═══════════════════════════════════════════════════════════════════════════

def test_part1():
    log("\n── Part 1: Embedding extraction ─────────────────────────────\n")
    setup_preprocess_wav_mock()

    tmpdir = tempfile.mkdtemp(prefix="jarvis_spkid_test_")

    # --- Test: valid audio → 256-dim non-zero array ---
    sid, _ = make_speaker_id(tmpdir)
    audio = make_audio(3.0)
    emb = sid.extract_embedding(audio)
    assert_eq("embed: output is 256-dim", len(emb), 256)
    assert_true("embed: output is not all zeros",
                not np.all(emb == 0),
                "embedding is all zeros")

    # --- Test: short audio (<100ms = <1600 samples) → zeros ---
    sid2, _ = make_speaker_id(tmpdir)
    short_audio = make_audio(0.05)  # 50ms = 800 samples
    # Make preprocess_wav return something shorter than 1600
    _mock_resemblyzer.preprocess_wav = lambda a, source_sr=16000: a[:800]
    emb_short = sid2.extract_embedding(short_audio)
    assert_true("embed short: returns 256-dim zeros",
                np.all(emb_short == 0) and len(emb_short) == 256,
                f"got non-zero or wrong length: shape={emb_short.shape}")
    setup_preprocess_wav_mock()  # Restore

    # --- Test: non-float32 input → auto-converted ---
    sid3, _ = make_speaker_id(tmpdir)
    audio_f64 = make_audio(3.0).astype(np.float64)
    assert_eq("embed dtype: input is float64", audio_f64.dtype, np.float64)
    emb_f64 = sid3.extract_embedding(audio_f64)
    assert_eq("embed dtype: output is 256-dim", len(emb_f64), 256)
    assert_true("embed dtype: output is not all zeros",
                not np.all(emb_f64 == 0))

    # --- Test: non-16kHz input → resampled ---
    sid4, _ = make_speaker_id(tmpdir)
    audio_44k = make_audio(3.0, sample_rate=44100, freq=440.0)
    emb_44k = sid4.extract_embedding(audio_44k, sample_rate=44100)
    assert_eq("embed resample: output is 256-dim", len(emb_44k), 256)
    assert_true("embed resample: output is not all zeros",
                not np.all(emb_44k == 0))

    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Part 2: Enrollment
# ═══════════════════════════════════════════════════════════════════════════

def test_part2():
    log("\n── Part 2: Enrollment ───────────────────────────────────────\n")
    setup_preprocess_wav_mock()

    tmpdir = tempfile.mkdtemp(prefix="jarvis_spkid_test_")

    # --- Test: enroll with valid audio → True, file saved, cache updated ---
    sid, pm = make_speaker_id(tmpdir)
    pm._profiles["alice"] = {"id": "alice", "honorific": "ma'am", "name": "Alice"}
    audio = make_audio(3.0)

    result = sid.enroll("alice", audio)
    assert_eq("enroll: returns True", result, True)
    npy_path = Path(tmpdir) / "alice.npy"
    assert_true("enroll: .npy file saved", npy_path.exists(),
                f"{npy_path} does not exist")
    assert_true("enroll: cache updated", "alice" in sid._cache,
                "alice not in cache")
    if "alice" in sid._cache:
        cached_emb, cached_hon = sid._cache["alice"]
        assert_eq("enroll: cache embedding is 256-dim", len(cached_emb), 256)
        assert_eq("enroll: cache honorific correct", cached_hon, "ma'am")

    # --- Test: enroll with short audio → False ---
    sid2, pm2 = make_speaker_id(tmpdir)
    pm2._profiles["bob"] = {"id": "bob", "honorific": "sir", "name": "Bob"}
    _mock_resemblyzer.preprocess_wav = lambda a, source_sr=16000: a[:800]
    result2 = sid2.enroll("bob", make_audio(0.05))
    assert_eq("enroll short: returns False", result2, False)
    assert_true("enroll short: bob NOT in cache", "bob" not in sid2._cache)
    setup_preprocess_wav_mock()

    # --- Test: enroll unknown profile → False ---
    sid3, pm3 = make_speaker_id(tmpdir)
    # No profile for "unknown_user"
    result3 = sid3.enroll("unknown_user", make_audio(3.0))
    assert_eq("enroll unknown: returns False", result3, False)

    # --- Test: enroll_from_multiple → averaged + normalized embedding ---
    sid4, pm4 = make_speaker_id(tmpdir)
    pm4._profiles["carol"] = {"id": "carol", "honorific": "Ms.", "name": "Carol"}

    # Use fixed embeddings to verify averaging
    call_count = [0]
    fixed_embeddings = [
        np.array([1.0, 0.0] + [0.0]*254, dtype=np.float32),
        np.array([0.0, 1.0] + [0.0]*254, dtype=np.float32),
    ]
    def fixed_embed(processed):
        idx = min(call_count[0], len(fixed_embeddings)-1)
        call_count[0] += 1
        return fixed_embeddings[idx]

    sid4._encoder.embed_utterance = fixed_embed

    samples = [(make_audio(3.0, freq=440), 16000),
               (make_audio(3.0, freq=880), 16000)]
    result4 = sid4.enroll_from_multiple("carol", samples)
    assert_eq("enroll multi: returns True", result4, True)
    if "carol" in sid4._cache:
        emb, _ = sid4._cache["carol"]
        # Average of [1,0,...] and [0,1,...] → [0.5,0.5,...] → normalized
        expected_avg = np.array([0.5, 0.5] + [0.0]*254, dtype=np.float32)
        expected_norm = expected_avg / np.linalg.norm(expected_avg)
        assert_close("enroll multi: averaged embedding[0]",
                      float(emb[0]), float(expected_norm[0]), tol=1e-4)
        assert_close("enroll multi: averaged embedding[1]",
                      float(emb[1]), float(expected_norm[1]), tol=1e-4)
        norm = np.linalg.norm(emb)
        assert_close("enroll multi: embedding is normalized",
                      float(norm), 1.0, tol=1e-4)

    # --- Test: enroll_from_multiple with empty list → False ---
    sid5, _ = make_speaker_id(tmpdir)
    result5 = sid5.enroll_from_multiple("anyone", [])
    assert_eq("enroll multi empty: returns False", result5, False)

    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Part 3: Identification
# ═══════════════════════════════════════════════════════════════════════════

def test_part3():
    log("\n── Part 3: Identification ───────────────────────────────────\n")
    setup_preprocess_wav_mock()

    tmpdir = tempfile.mkdtemp(prefix="jarvis_spkid_test_")

    # Create known unit-vector embeddings for controlled cosine similarity
    emb_a = np.zeros(256, dtype=np.float32)
    emb_a[0] = 1.0  # [1, 0, 0, ...]

    emb_b = np.zeros(256, dtype=np.float32)
    emb_b[1] = 1.0  # [0, 1, 0, ...]

    # --- Test: identify with no enrolled profiles → (None, 0.0) ---
    sid, _ = make_speaker_id(tmpdir)
    user, score = sid.identify(make_audio(3.0))
    assert_eq("identify empty: user is None", user, None)
    assert_close("identify empty: score is 0.0", score, 0.0)

    # --- Test: identify enrolled speaker above threshold ---
    sid2, _ = make_speaker_id(tmpdir, threshold=0.85)
    sid2._cache["alice"] = (emb_a, "ma'am")
    # Make extract_embedding return emb_a (identical to enrolled)
    sid2._encoder.embed_utterance = lambda proc: emb_a.copy()

    user2, score2 = sid2.identify(make_audio(3.0))
    assert_eq("identify match: user is alice", user2, "alice")
    assert_close("identify match: score ≈ 1.0 (identical vectors)",
                  score2, 1.0, tol=0.01)

    # --- Test: identify with zero embedding → (None, 0.0) ---
    sid3, _ = make_speaker_id(tmpdir)
    sid3._cache["alice"] = (emb_a, "ma'am")
    _mock_resemblyzer.preprocess_wav = lambda a, source_sr=16000: a[:800]
    user3, score3 = sid3.identify(make_audio(0.05))
    assert_eq("identify zero: user is None", user3, None)
    assert_close("identify zero: score is 0.0", score3, 0.0)
    setup_preprocess_wav_mock()

    # --- Test: two enrolled speakers → correct one identified ---
    sid4, _ = make_speaker_id(tmpdir, threshold=0.5)
    sid4._cache["alice"] = (emb_a, "ma'am")
    sid4._cache["bob"] = (emb_b, "sir")

    # Audio that matches alice (emb_a direction)
    sid4._encoder.embed_utterance = lambda proc: emb_a.copy()
    user4a, score4a = sid4.identify(make_audio(3.0))
    assert_eq("identify 2-speaker: matches alice", user4a, "alice")
    assert_close("identify 2-speaker: alice score ≈ 1.0", score4a, 1.0, tol=0.01)

    # Audio that matches bob (emb_b direction)
    sid4._encoder.embed_utterance = lambda proc: emb_b.copy()
    user4b, score4b = sid4.identify(make_audio(3.0))
    assert_eq("identify 2-speaker: matches bob", user4b, "bob")
    assert_close("identify 2-speaker: bob score ≈ 1.0", score4b, 1.0, tol=0.01)

    # --- Test: below threshold → (None, best_score) ---
    sid5, _ = make_speaker_id(tmpdir, threshold=0.95)
    sid5._cache["alice"] = (emb_a, "ma'am")

    # Create embedding that's partially similar to emb_a (cosine ≈ 0.7)
    mixed = np.zeros(256, dtype=np.float32)
    mixed[0] = 0.7
    mixed[1] = 0.714  # norm ≈ 1.0
    mixed /= np.linalg.norm(mixed)  # normalize
    sid5._encoder.embed_utterance = lambda proc: mixed.copy()

    user5, score5 = sid5.identify(make_audio(3.0))
    assert_eq("identify below threshold: user is None", user5, None)
    assert_true("identify below threshold: score < 0.95",
                score5 < 0.95,
                f"score={score5}")
    assert_true("identify below threshold: score > 0.0",
                score5 > 0.0,
                f"score={score5}")

    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Part 4: Verification
# ═══════════════════════════════════════════════════════════════════════════

def test_part4():
    log("\n── Part 4: Verification ──────────────────────────────────────\n")
    setup_preprocess_wav_mock()

    tmpdir = tempfile.mkdtemp(prefix="jarvis_spkid_test_")

    emb_a = np.zeros(256, dtype=np.float32)
    emb_a[0] = 1.0

    emb_b = np.zeros(256, dtype=np.float32)
    emb_b[1] = 1.0

    # --- Test: verify correct speaker → (True, score) ---
    sid, _ = make_speaker_id(tmpdir, threshold=0.85)
    sid._cache["alice"] = (emb_a, "ma'am")
    sid._encoder.embed_utterance = lambda proc: emb_a.copy()

    is_match, score = sid.verify("alice", make_audio(3.0))
    assert_eq("verify match: is_match=True", is_match, True)
    assert_close("verify match: score ≈ 1.0", score, 1.0, tol=0.01)

    # --- Test: verify wrong speaker → (False, score) ---
    sid._encoder.embed_utterance = lambda proc: emb_b.copy()
    is_match2, score2 = sid.verify("alice", make_audio(3.0))
    assert_eq("verify mismatch: is_match=False", is_match2, False)
    assert_close("verify mismatch: score ≈ 0.0 (orthogonal vectors)",
                  score2, 0.0, tol=0.01)

    # --- Test: verify unknown user_id → (False, 0.0) ---
    is_match3, score3 = sid.verify("unknown", make_audio(3.0))
    assert_eq("verify unknown: is_match=False", is_match3, False)
    assert_close("verify unknown: score=0.0", score3, 0.0)

    # --- Test: verify with zero embedding → (False, 0.0) ---
    _mock_resemblyzer.preprocess_wav = lambda a, source_sr=16000: a[:800]
    is_match4, score4 = sid.verify("alice", make_audio(0.05))
    assert_eq("verify zero embed: is_match=False", is_match4, False)
    assert_close("verify zero embed: score=0.0", score4, 0.0)
    setup_preprocess_wav_mock()

    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Part 5: Cache management
# ═══════════════════════════════════════════════════════════════════════════

def test_part5():
    log("\n── Part 5: Cache management ──────────────────────────────────\n")

    tmpdir = tempfile.mkdtemp(prefix="jarvis_spkid_test_")

    # --- Test: load_embeddings loads from disk ---
    sid, pm = make_speaker_id(tmpdir)

    # Create .npy files on disk
    emb_a = np.random.randn(256).astype(np.float32)
    emb_b = np.random.randn(256).astype(np.float32)
    np.save(os.path.join(tmpdir, "alice.npy"), emb_a)
    np.save(os.path.join(tmpdir, "bob.npy"), emb_b)

    pm.get_profiles_with_embeddings = MagicMock(return_value=[
        {"id": "alice", "honorific": "ma'am", "embedding_path": os.path.join(tmpdir, "alice.npy")},
        {"id": "bob", "honorific": "sir", "embedding_path": os.path.join(tmpdir, "bob.npy")},
    ])

    sid.load_embeddings()
    assert_eq("load: 2 profiles in cache", len(sid._cache), 2)
    assert_true("load: alice in cache", "alice" in sid._cache)
    assert_true("load: bob in cache", "bob" in sid._cache)
    if "alice" in sid._cache:
        assert_true("load: alice embedding matches",
                     np.allclose(sid._cache["alice"][0], emb_a))

    # --- Test: load_embeddings clears old cache ---
    sid._cache["stale_user"] = (np.zeros(256), "test")
    sid.load_embeddings()
    assert_true("load: stale_user cleared from cache",
                "stale_user" not in sid._cache)

    # --- Test: load_embeddings handles missing file ---
    pm.get_profiles_with_embeddings = MagicMock(return_value=[
        {"id": "ghost", "honorific": "sir", "embedding_path": "/nonexistent/ghost.npy"},
    ])
    sid.load_embeddings()
    assert_true("load missing: ghost NOT in cache", "ghost" not in sid._cache)
    assert_eq("load missing: cache is empty", len(sid._cache), 0)

    # --- Test: reload_profile updates single entry ---
    sid2, pm2 = make_speaker_id(tmpdir)
    emb_c = np.random.randn(256).astype(np.float32)
    np.save(os.path.join(tmpdir, "carol.npy"), emb_c)
    pm2._profiles["carol"] = {
        "id": "carol", "honorific": "Ms.", "name": "Carol",
        "embedding_path": os.path.join(tmpdir, "carol.npy"),
    }
    sid2.reload_profile("carol")
    assert_true("reload: carol in cache", "carol" in sid2._cache)
    if "carol" in sid2._cache:
        assert_true("reload: embedding matches",
                     np.allclose(sid2._cache["carol"][0], emb_c))

    # --- Test: reload_profile removes user without embedding ---
    sid2._cache["dave"] = (np.zeros(256), "test")
    pm2._profiles["dave"] = {"id": "dave", "honorific": "sir", "name": "Dave"}
    # No embedding_path → should be removed
    sid2.reload_profile("dave")
    assert_true("reload: dave removed from cache (no embedding)",
                "dave" not in sid2._cache)

    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════

def main():
    log("Speaker Identification — Test Suite\n")

    test_part1()
    test_part2()
    test_part3()
    test_part4()
    test_part5()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print(f"\n{'═' * 60}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f" ({failed} failed)")
        for r in results:
            if not r.passed:
                print(f"  FAIL: {r.name} — {r.detail}")
    else:
        print(" ✓")
    print(f"{'═' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
