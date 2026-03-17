"""Tool definition: enroll_face — capture and save a face for presence recognition.

Voice-triggered: "remember my face", "learn my face", "this is what I look like"
Captures a webcam frame, extracts the face encoding, and saves it for
future presence detection greetings.
"""

import asyncio
import logging

logger = logging.getLogger("jarvis.tools.enroll_face")

TOOL_NAME = "enroll_face"
ALWAYS_INCLUDED = True

DEPENDENCIES = {"presence_detector": "_presence_detector"}
_presence_detector = None  # Set at runtime via inject_dependencies

INTENT_EXAMPLES = [
    "remember my face",
    "learn my face",
    "this is what I look like",
    "enroll my face",
    "save my face",
    "register my face",
]

SCHEMA = {
    "type": "function",
    "function": {
        "name": "enroll_face",
        "description": (
            "Capture multiple photos and save the user's face for automatic recognition. "
            "Used for presence detection — JARVIS greets the user when they "
            "sit down at the desk. Captures 5 images from different angles "
            "and with/without glasses for robust recognition."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_name": {
                    "type": "string",
                    "description": (
                        "Name of the person to enroll. "
                        "Defaults to the current user if not specified."
                    ),
                },
            },
            "required": [],
        },
    },
}

SYSTEM_PROMPT_RULE = (
    "RULE: Face enrollment. Use enroll_face when the user wants JARVIS to "
    "remember or learn their face for automatic recognition. "
    "Captures multiple images for robust recognition across angles and conditions. "
    "Only one person should be in frame during enrollment."
)


def handler(args: dict) -> str:
    """Capture a frame and enroll the face."""
    if _presence_detector is None:
        return (
            "Face enrollment is not available — presence detection is not initialized. "
            "Enable vision.presence in config.yaml first."
        )

    person_name = args.get("person_name", "").strip()

    # Get current user's person_id from people_manager
    pm = _presence_detector._people_manager
    if not pm:
        return "Error: People manager not available."

    # Look up or create the person record
    if person_name:
        person = pm.get_person_by_name(person_name)
        if not person:
            # Create a new person record
            person_id = pm.add_person(person_name, relationship="contact")
            person = {"person_id": person_id, "name": person_name}
    else:
        # Default to owner
        person_name = "User"
        person = pm.get_person_by_name(person_name)
        if not person:
            person_id = pm.add_person(person_name, relationship="owner")
            person = {"person_id": person_id, "name": person_name}

    person_id = person["person_id"]

    # Multi-image guided enrollment for robust recognition.
    # Captures 5 frames with spoken prompts between each.
    # TTS is accessed via the presence detector's TTS proxy.
    tts = getattr(_presence_detector, '_tts', None)

    poses = [
        ("Look straight at the camera", 2.0),
        ("Now turn slightly to your left", 2.5),
        ("Turn slightly to your right", 2.5),
        ("Put your glasses on if you have them, or take them off if you're wearing them", 4.0),
        ("One more — look straight at the camera again", 2.0),
    ]

    frames = []
    for i, (instruction, wait_time) in enumerate(poses):
        if tts and i > 0:
            # Speak instruction between captures (skip first — tool synthesis handles it)
            import time
            tts.speak(instruction)
            time.sleep(wait_time)
        elif i == 0:
            # First capture — just give a moment to settle
            import time
            time.sleep(1.0)

        frame_bytes = _capture_frame()
        if isinstance(frame_bytes, str):
            logger.warning("Enrollment frame %d failed: %s", i + 1, frame_bytes)
            continue
        frames.append(frame_bytes)

    if not frames:
        return "Could not capture any frames. Please check the webcam."

    # Enroll using averaged multi-image encoding
    success, message = _presence_detector.enroll_face_multi(
        person_id, frames, person_name=person_name
    )
    return message


def _capture_frame() -> bytes | str:
    """Capture a frame from the desktop webcam. Returns bytes or error string."""
    try:
        from core.webcam_manager import get_webcam_manager
        wm = get_webcam_manager()
    except RuntimeError:
        return "Error: Webcam not initialized. Please try again."

    if not wm.device_available:
        return "Error: No webcam available. Please connect a camera."

    try:
        loop = wm._loop
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(wm.get_frame(), loop)
            return future.result(timeout=10)
        else:
            _loop = asyncio.new_event_loop()
            try:
                return _loop.run_until_complete(wm.get_frame())
            finally:
                _loop.close()
    except TimeoutError:
        return "Error: Camera timed out. Please make sure the webcam is working."
    except Exception as e:
        logger.error("Frame capture for enrollment failed: %s", e)
        return f"Error: Could not capture frame — {e}"
