"""
Presence Detector — continuous face detection and identification.

Uses InsightFace (RetinaFace detection + ArcFace 512-dim recognition) for
both detection and identification in a single pass. 99.83% LFW accuracy.

Fires proactive greetings when known people appear, following the
reminder_manager background thread + EventTTSProxy pattern.
"""

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from core.logger import get_logger
from core.honorific import set_honorific


# Singleton
_instance: Optional["PresenceDetector"] = None


def get_presence_detector(config=None, tts=None, webcam_manager=None,
                          people_manager=None, conversation=None,
                          reminder_manager=None) -> Optional["PresenceDetector"]:
    """Get or create the singleton PresenceDetector."""
    global _instance
    if _instance is None and config is not None:
        _instance = PresenceDetector(
            config, tts, webcam_manager, people_manager,
            conversation, reminder_manager,
        )
    return _instance


class PresenceState(Enum):
    ABSENT = auto()
    DETECTED = auto()
    GREETED = auto()
    PRESENT = auto()


@dataclass
class PersonPresence:
    person_id: Optional[str]  # None = unknown face
    state: PresenceState = PresenceState.ABSENT
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_greeted: float = 0.0
    confidence: float = 0.0


class PresenceDetector:
    """Continuous face detection engine with proactive greetings."""

    def __init__(self, config, tts, webcam_manager, people_manager,
                 conversation, reminder_manager=None):
        self.config = config
        self.tts = tts
        self._webcam_manager = webcam_manager
        self._people_manager = people_manager
        self.conversation = conversation
        self._reminder_manager = reminder_manager
        self.logger = get_logger("presence", config)

        # Config
        presence_cfg = config.get("vision.presence", {}) if hasattr(config, 'get') else {}
        self._interval = presence_cfg.get("detection_interval", 10)
        self._cooldown = presence_cfg.get("greeting_cooldown", 7200)
        self._confidence_threshold = presence_cfg.get("face_confidence_threshold", 0.6)
        self._min_face_size = presence_cfg.get("min_face_size", 80)
        self._greet_unknown = presence_cfg.get("greet_unknown", False)
        self._absence_threshold = presence_cfg.get("absence_threshold", 30)

        # Face embeddings directory
        self._embeddings_dir = Path(
            config.get("system.storage_path", "/mnt/storage/jarvis")
        ) / "data" / "face_embeddings"
        self._embeddings_dir.mkdir(parents=True, exist_ok=True)

        # State tracking
        self._person_states: dict[str, PersonPresence] = {}
        self._face_cache: dict[str, np.ndarray] = {}  # person_id -> 512-dim encoding

        # InsightFace app (lazy-loaded)
        self._face_app = None

        # Callbacks (set by jarvis_continuous.py)
        self._pause_listener_callback: Optional[Callable] = None
        self._resume_listener_callback: Optional[Callable] = None
        self._window_callback: Optional[Callable] = None

        # Thread control
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

        # Load existing face embeddings
        self._load_face_embeddings()

        self.logger.info(
            f"Presence detector initialized "
            f"({len(self._face_cache)} enrolled faces, "
            f"interval={self._interval}s, cooldown={self._cooldown}s)"
        )

    # ------------------------------------------------------------------
    # Lazy loading
    # ------------------------------------------------------------------

    def _get_face_app(self):
        """Lazy-load the InsightFace application (RetinaFace + ArcFace)."""
        if self._face_app is None:
            self.logger.info("Loading InsightFace buffalo_l...")
            from insightface.app import FaceAnalysis
            self._face_app = FaceAnalysis(
                name="buffalo_l",
                providers=["CPUExecutionProvider"],
            )
            self._face_app.prepare(ctx_id=-1, det_size=(640, 640))
            self.logger.info("InsightFace loaded")
        return self._face_app

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Launch the background polling thread."""
        if self._running:
            return
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="presence-detector"
        )
        self._poll_thread.start()
        self.logger.info("Presence detection started")

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        self.logger.info("Presence detection stopped")

    def set_listener_callbacks(self, pause: Callable, resume: Callable):
        """Set callbacks for pausing/resuming the listener during greetings."""
        self._pause_listener_callback = pause
        self._resume_listener_callback = resume

    def set_window_callback(self, callback: Callable):
        """Set callback for opening a conversation window after greeting."""
        self._window_callback = callback

    # ------------------------------------------------------------------
    # Detection loop
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """Main loop: detect faces every interval seconds."""
        # Delay first check to let audio/webcam initialize
        time.sleep(5)

        while self._running:
            try:
                if self._should_skip():
                    time.sleep(self._interval)
                    continue

                self._check_presence()

            except Exception as e:
                self.logger.error(f"Presence poll error: {e}", exc_info=True)

            time.sleep(self._interval)

    def _should_skip(self) -> bool:
        """Skip detection during active conversation or TTS playback."""
        # Skip if conversation is active (don't interrupt)
        if hasattr(self.conversation, 'conversation_active') and \
                self.conversation.conversation_active:
            return True

        # Skip if no webcam available
        if not self._webcam_available():
            return True

        return False

    def _webcam_available(self) -> bool:
        """Check if the desktop webcam is accessible."""
        if self._webcam_manager is None:
            return False
        return self._webcam_manager.device_available

    # ------------------------------------------------------------------
    # Frame capture (sync bridge for background thread)
    # ------------------------------------------------------------------

    def _grab_frame(self) -> Optional[np.ndarray]:
        """Grab a frame from the webcam and decode to numpy array."""
        try:
            wm = self._webcam_manager
            loop = wm._loop

            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    wm.get_frame(timeout=5.0), loop
                )
                jpeg_bytes = future.result(timeout=8.0)
            else:
                # No event loop — can't grab frame
                return None

            # Decode JPEG to numpy BGR array
            arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return frame

        except (TimeoutError, RuntimeError, FileNotFoundError) as e:
            self.logger.debug(f"Frame grab failed: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected frame grab error: {e}")
            return None

    # ------------------------------------------------------------------
    # Face detection + identification (single-pass with InsightFace)
    # ------------------------------------------------------------------

    def _detect_and_identify(self, frame: np.ndarray) -> list[tuple[Optional[str], float]]:
        """Detect all faces and identify them in a single pass.

        Returns list of (person_id, confidence) tuples.
        person_id is None for unknown faces.
        """
        app = self._get_face_app()
        faces = app.get(frame)

        if not faces:
            return []

        results = []
        for face in faces:
            # Filter by face size
            bbox = face.bbox.astype(int)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            if w < self._min_face_size or h < self._min_face_size:
                continue

            # If no enrolled faces, report as unknown
            if not self._face_cache:
                results.append((None, 0.0))
                continue

            # Get 512-dim ArcFace embedding
            embedding = face.normed_embedding  # Already L2-normalized

            # Compare against enrolled faces via cosine similarity
            best_match: Optional[str] = None
            best_score = -1.0

            for person_id, enrolled_emb in self._face_cache.items():
                score = float(np.dot(embedding, enrolled_emb))
                if score > best_score:
                    best_score = score
                    best_match = person_id

            if best_score >= self._confidence_threshold:
                results.append((best_match, best_score))
            else:
                results.append((None, best_score))

        return results

    # ------------------------------------------------------------------
    # Presence checking + state machine
    # ------------------------------------------------------------------

    def _check_presence(self):
        """Main detection cycle: grab frame → detect+identify → greet."""
        frame = self._grab_frame()
        if frame is None:
            return

        # Single-pass detection + identification
        face_results = self._detect_and_identify(frame)

        if not face_results:
            self._update_all_absent()
            return

        self.logger.debug(f"Detected {len(face_results)} face(s)")

        now = time.time()
        seen_ids = set()

        for person_id, confidence in face_results:
            if person_id:
                seen_ids.add(person_id)
                self._handle_detection(person_id, confidence, now)
            elif not self._greet_unknown:
                # Unknown face — silent
                self.logger.debug(f"Unknown face detected (confidence={confidence:.2f})")

        # Mark unseen people as absent
        for pid in list(self._person_states.keys()):
            if pid not in seen_ids:
                state = self._person_states[pid]
                if state.state != PresenceState.ABSENT:
                    # Check if they've been gone long enough
                    if now - state.last_seen > self._absence_threshold:
                        state.state = PresenceState.ABSENT
                        self.logger.debug(f"Person {pid} → ABSENT")

    def _handle_detection(self, person_id: str, confidence: float, now: float):
        """State machine: manage transitions and fire greetings."""
        if person_id not in self._person_states:
            self._person_states[person_id] = PersonPresence(
                person_id=person_id,
                state=PresenceState.ABSENT,
            )

        state = self._person_states[person_id]
        state.last_seen = now
        state.confidence = confidence

        if state.state == PresenceState.ABSENT:
            # Transition: ABSENT → DETECTED
            state.state = PresenceState.DETECTED
            state.first_seen = now
            self.logger.info(
                f"Person {person_id} detected (confidence={confidence:.2f})"
            )

            # Check if we should greet
            if self._greeting_allowed(person_id, now):
                was_long_absence = self._was_long_absence(person_id, now)
                self._fire_greeting(person_id, was_long_absence)
                state.state = PresenceState.GREETED
                state.last_greeted = now
            else:
                # Cooldown active — skip straight to PRESENT
                state.state = PresenceState.PRESENT

        elif state.state == PresenceState.DETECTED:
            # Already detected, waiting — transition to PRESENT
            state.state = PresenceState.PRESENT

        elif state.state == PresenceState.GREETED:
            # Already greeted — transition to PRESENT
            state.state = PresenceState.PRESENT

        # PRESENT stays PRESENT until they leave

    def _update_all_absent(self):
        """Mark all tracked people as absent if they've been gone long enough."""
        now = time.time()
        for pid, state in self._person_states.items():
            if state.state != PresenceState.ABSENT:
                if now - state.last_seen > self._absence_threshold:
                    state.state = PresenceState.ABSENT
                    self.logger.debug(f"Person {pid} → ABSENT (no faces detected)")

    def _greeting_allowed(self, person_id: str, now: float) -> bool:
        """Check cooldown: only greet once per cooldown period."""
        state = self._person_states.get(person_id)
        if state and state.last_greeted > 0:
            return (now - state.last_greeted) > self._cooldown
        return True  # Never greeted before

    def _was_long_absence(self, person_id: str, now: float) -> bool:
        """Check if this person was absent for >30 minutes (return greeting)."""
        state = self._person_states.get(person_id)
        if state and state.last_greeted > 0:
            # Use last_greeted as proxy for last known presence
            return (now - state.last_greeted) > 1800  # 30 minutes
        return False  # First detection ever — not a "return"

    # ------------------------------------------------------------------
    # Greeting delivery
    # ------------------------------------------------------------------

    def _fire_greeting(self, person_id: str, is_return: bool = False):
        """Speak a presence greeting. Follows reminder_manager._fire_reminder() pattern."""
        from core.persona import presence_greeting, _time_of_day

        # Restore owner honorific for greeting
        set_honorific("sir")

        # Check for pending reminders (for return-with-reminders greeting)
        has_pending = False
        if is_return and self._reminder_manager:
            try:
                pending = self._reminder_manager.get_pending_acks()
                has_pending = len(pending) > 0
            except Exception:
                pass

        # Build greeting
        tod = _time_of_day()
        greeting = presence_greeting(tod, is_return=is_return,
                                     has_pending_reminders=has_pending)

        self.logger.info(
            f"Greeting {person_id}: '{greeting}' "
            f"(return={is_return}, pending_reminders={has_pending})"
        )

        # Pause listening → speak → open conversation window
        if self._pause_listener_callback:
            self._pause_listener_callback()

        tts_ok = self.tts.speak(greeting)

        if self._resume_listener_callback:
            self._resume_listener_callback()

        # Open a conversation window so user can respond
        if self._window_callback:
            self._window_callback(8.0)

    # ------------------------------------------------------------------
    # Face enrollment
    # ------------------------------------------------------------------

    def enroll_face(self, person_id: str, frame_bytes: bytes,
                    person_name: str = "") -> tuple[bool, str]:
        """Extract face encoding from a single JPEG frame and save.

        For single-shot enrollment. Prefer enroll_face_multi() for better
        accuracy across angles and conditions (glasses, lighting).

        Returns:
            (success, message)
        """
        encoding = self._extract_encoding(frame_bytes)
        if isinstance(encoding, str):
            return False, encoding  # Error message

        self._save_encoding(person_id, encoding, person_name)
        name_label = person_name or person_id
        return True, f"Face enrolled successfully for {name_label}."

    def enroll_face_multi(self, person_id: str, frames: list[bytes],
                          person_name: str = "") -> tuple[bool, str]:
        """Extract face encodings from multiple frames and save the average.

        Multiple angles and conditions (glasses on/off) produce a more robust
        encoding that handles real-world variation better than a single shot.

        Args:
            person_id: ID from people_manager
            frames: List of JPEG byte frames
            person_name: Display name for logging

        Returns:
            (success, message) — message includes count of successful extractions
        """
        encodings = []
        errors = []
        for i, frame_bytes in enumerate(frames):
            result = self._extract_encoding(frame_bytes)
            if isinstance(result, str):
                errors.append(f"Frame {i + 1}: {result}")
                self.logger.debug("Enrollment frame %d failed: %s", i + 1, result)
            else:
                encodings.append(result)

        if not encodings:
            return False, "No faces could be extracted from any frame. Please try again."

        # Average all successful encodings and re-normalize
        avg_encoding = np.mean(encodings, axis=0)
        avg_encoding = avg_encoding / np.linalg.norm(avg_encoding)

        self._save_encoding(person_id, avg_encoding, person_name)
        name_label = person_name or person_id
        self.logger.info(
            f"Multi-image enrollment for {name_label}: "
            f"{len(encodings)}/{len(frames)} frames successful"
        )
        return True, (
            f"Face enrolled for {name_label} using {len(encodings)} images. "
            f"Recognition should work across different angles and conditions."
        )

    def _extract_encoding(self, frame_bytes: bytes):
        """Extract a single 512-dim face encoding from JPEG bytes.

        Returns numpy array on success, or error string on failure.
        """
        app = self._get_face_app()

        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return "Could not decode the camera frame."

        faces = app.get(frame)
        if not faces:
            return "No face detected in the frame."

        if len(faces) > 1:
            return f"Multiple faces detected ({len(faces)}). Please ensure only one person is in frame."

        return faces[0].normed_embedding

    def _save_encoding(self, person_id: str, encoding, person_name: str = ""):
        """Save encoding to disk and update caches."""
        embed_path = self._embeddings_dir / f"face_{person_id}.npy"
        np.save(str(embed_path), encoding)

        self._face_cache[person_id] = encoding

        if self._people_manager:
            self._people_manager.set_face_embedding_path(
                person_id, str(embed_path)
            )

        name_label = person_name or person_id
        self.logger.info(
            f"Enrolled face for {name_label} "
            f"(saved to {embed_path}, {len(encoding)}-dim encoding)"
        )

    def _load_face_embeddings(self):
        """Load all enrolled face embeddings from disk."""
        loaded = 0
        if self._people_manager:
            people = self._people_manager.get_people_with_face_embeddings()
            for person in people:
                path = person.get("face_embedding_path")
                if path and Path(path).exists():
                    try:
                        encoding = np.load(path)
                        self._face_cache[person["person_id"]] = encoding
                        loaded += 1
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to load face embedding for "
                            f"{person['name']}: {e}"
                        )

        # Also check embeddings directory for .npy files not in DB
        for npy_path in self._embeddings_dir.glob("face_*.npy"):
            pid = npy_path.stem.replace("face_", "")
            if pid not in self._face_cache:
                try:
                    encoding = np.load(str(npy_path))
                    self._face_cache[pid] = encoding
                    loaded += 1
                    self.logger.debug(f"Loaded orphan embedding: {npy_path.name}")
                except Exception as e:
                    self.logger.warning(f"Failed to load {npy_path}: {e}")

        if loaded:
            self.logger.info(f"Loaded {loaded} face embeddings")

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return current presence detection status for health check."""
        return {
            "running": self._running,
            "enrolled_faces": len(self._face_cache),
            "tracked_people": len(self._person_states),
            "interval": self._interval,
            "cooldown": self._cooldown,
            "states": {
                pid: {
                    "state": s.state.name,
                    "confidence": s.confidence,
                    "last_seen": s.last_seen,
                }
                for pid, s in self._person_states.items()
            },
        }
