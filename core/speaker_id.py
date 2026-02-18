"""
Speaker Identification

Uses resemblyzer d-vectors (256-dim) for speaker verification and
identification. Runs on CPU only — the model is tiny (~5MB).

Workflow:
    1. Enrollment: record audio → extract d-vector → save as .npy
    2. Identification: incoming audio → extract d-vector → cosine similarity
       against enrolled profiles → return best match (or "unknown")

Designed to run in parallel with Whisper inside STTWorker (Phase 4).
"""

import numpy as np
from pathlib import Path
from typing import Optional, Tuple, List, Dict

from core.logger import get_logger


class SpeakerIdentifier:
    """Speaker identification using resemblyzer d-vectors."""

    def __init__(self, config, profile_manager):
        """
        Args:
            config: JARVIS config object
            profile_manager: ProfileManager instance (for embedding paths + honorifics)
        """
        self.config = config
        self.profile_manager = profile_manager
        self.logger = get_logger(__name__, config)

        # Config
        self.similarity_threshold = config.get(
            "user_profiles.similarity_threshold", 0.85
        )

        # Lazy-loaded resemblyzer encoder
        self._encoder = None

        # In-memory cache: user_id -> (embedding_np, honorific)
        self._cache: Dict[str, Tuple[np.ndarray, str]] = {}

        self.logger.info("SpeakerIdentifier initialized (encoder loads on first use)")

    # ------------------------------------------------------------------
    # Lazy loading
    # ------------------------------------------------------------------

    def _get_encoder(self):
        """Lazy-load the resemblyzer voice encoder (CPU only)."""
        if self._encoder is None:
            self.logger.info("Loading resemblyzer VoiceEncoder...")
            from resemblyzer import VoiceEncoder
            self._encoder = VoiceEncoder(device="cpu")
            self.logger.info("VoiceEncoder loaded")
        return self._encoder

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def load_embeddings(self):
        """Load all enrolled embeddings from disk into memory."""
        profiles = self.profile_manager.get_profiles_with_embeddings()
        self._cache.clear()

        for profile in profiles:
            emb_path = Path(profile["embedding_path"])
            if emb_path.exists():
                embedding = np.load(str(emb_path))
                self._cache[profile["id"]] = (embedding, profile["honorific"])
                self.logger.debug(f"Loaded embedding for {profile['id']}")
            else:
                self.logger.warning(
                    f"Embedding file missing for {profile['id']}: {emb_path}"
                )

        self.logger.info(f"Loaded {len(self._cache)} speaker embeddings")

    def reload_profile(self, user_id: str):
        """Reload a single profile's embedding (after enrollment update)."""
        profile = self.profile_manager.get_profile(user_id)
        if not profile or not profile.get("embedding_path"):
            self._cache.pop(user_id, None)
            return

        emb_path = Path(profile["embedding_path"])
        if emb_path.exists():
            embedding = np.load(str(emb_path))
            self._cache[user_id] = (embedding, profile["honorific"])
            self.logger.info(f"Reloaded embedding for {user_id}")

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def extract_embedding(self, audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
        """Extract a d-vector embedding from audio.

        Args:
            audio: Float32 audio samples (mono)
            sample_rate: Sample rate (resemblyzer expects 16kHz)

        Returns:
            256-dim d-vector (numpy float32 array)
        """
        encoder = self._get_encoder()

        # resemblyzer expects float64 or float32, mono, 16kHz
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # Resample if needed
        if sample_rate != 16000:
            duration = len(audio) / sample_rate
            target_len = int(duration * 16000)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, target_len),
                np.arange(len(audio)),
                audio,
            ).astype(np.float32)

        # resemblyzer preprocess_wav expects raw samples
        from resemblyzer import preprocess_wav
        processed = preprocess_wav(audio, source_sr=16000)

        if len(processed) < 1600:  # <100ms, too short
            self.logger.warning("Audio too short for speaker embedding")
            return np.zeros(256, dtype=np.float32)

        embedding = encoder.embed_utterance(processed)
        return embedding

    def enroll(self, user_id: str, audio: np.ndarray,
               sample_rate: int = 16000) -> bool:
        """Enroll a speaker by saving their d-vector embedding.

        Args:
            user_id: Profile ID to enroll
            audio: Audio samples (float32, mono)
            sample_rate: Sample rate

        Returns:
            True if enrollment succeeded
        """
        profile = self.profile_manager.get_profile(user_id)
        if not profile:
            self.logger.error(f"Cannot enroll: profile {user_id} not found")
            return False

        embedding = self.extract_embedding(audio, sample_rate)
        if np.all(embedding == 0):
            self.logger.error(f"Enrollment failed: audio too short for {user_id}")
            return False

        # Save embedding
        emb_path = self.profile_manager.embeddings_dir / f"{user_id}.npy"
        np.save(str(emb_path), embedding)

        # Update profile with embedding path
        self.profile_manager.update_profile(
            user_id, embedding_path=str(emb_path)
        )

        # Update cache
        self._cache[user_id] = (embedding, profile["honorific"])
        self.logger.info(f"Enrolled speaker: {user_id} ({emb_path})")
        return True

    def enroll_from_multiple(self, user_id: str,
                              audio_samples: List[Tuple[np.ndarray, int]]) -> bool:
        """Enroll a speaker from multiple audio samples (averaged embedding).

        Args:
            user_id: Profile ID to enroll
            audio_samples: List of (audio, sample_rate) tuples

        Returns:
            True if enrollment succeeded
        """
        if not audio_samples:
            return False

        embeddings = []
        for audio, sr in audio_samples:
            emb = self.extract_embedding(audio, sr)
            if not np.all(emb == 0):
                embeddings.append(emb)

        if not embeddings:
            self.logger.error(f"No valid embeddings from {len(audio_samples)} samples")
            return False

        # Average the embeddings and normalize
        avg_embedding = np.mean(embeddings, axis=0)
        avg_embedding = avg_embedding / np.linalg.norm(avg_embedding)

        profile = self.profile_manager.get_profile(user_id)
        if not profile:
            self.logger.error(f"Cannot enroll: profile {user_id} not found")
            return False

        emb_path = self.profile_manager.embeddings_dir / f"{user_id}.npy"
        np.save(str(emb_path), avg_embedding)

        self.profile_manager.update_profile(
            user_id, embedding_path=str(emb_path)
        )

        self._cache[user_id] = (avg_embedding, profile["honorific"])
        self.logger.info(
            f"Enrolled speaker {user_id} from {len(embeddings)} samples"
        )
        return True

    def identify(self, audio: np.ndarray,
                 sample_rate: int = 16000) -> Tuple[Optional[str], float]:
        """Identify who is speaking from audio.

        Args:
            audio: Audio samples (float32, mono)
            sample_rate: Sample rate

        Returns:
            (user_id, confidence) if matched above threshold,
            (None, best_score) if no match
        """
        if not self._cache:
            return None, 0.0

        embedding = self.extract_embedding(audio, sample_rate)
        if np.all(embedding == 0):
            return None, 0.0

        best_id = None
        best_score = -1.0

        for user_id, (enrolled_emb, _honorific) in self._cache.items():
            # Cosine similarity
            score = float(np.dot(embedding, enrolled_emb) / (
                np.linalg.norm(embedding) * np.linalg.norm(enrolled_emb) + 1e-8
            ))
            if score > best_score:
                best_score = score
                best_id = user_id

        if best_score >= self.similarity_threshold:
            self.logger.debug(
                f"Speaker identified: {best_id} (score={best_score:.3f})"
            )
            return best_id, best_score

        self.logger.debug(
            f"Speaker unknown (best={best_id}, score={best_score:.3f}, "
            f"threshold={self.similarity_threshold})"
        )
        return None, best_score

    def verify(self, user_id: str, audio: np.ndarray,
               sample_rate: int = 16000) -> Tuple[bool, float]:
        """Verify that audio matches a specific user.

        Args:
            user_id: Expected user ID
            audio: Audio samples
            sample_rate: Sample rate

        Returns:
            (is_match, score)
        """
        if user_id not in self._cache:
            return False, 0.0

        embedding = self.extract_embedding(audio, sample_rate)
        if np.all(embedding == 0):
            return False, 0.0

        enrolled_emb, _ = self._cache[user_id]
        score = float(np.dot(embedding, enrolled_emb) / (
            np.linalg.norm(embedding) * np.linalg.norm(enrolled_emb) + 1e-8
        ))

        return score >= self.similarity_threshold, score
