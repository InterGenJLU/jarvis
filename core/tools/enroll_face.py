"""Tool definition: enroll_face — capture and save a face for presence recognition.

Voice-triggered: "remember my face", "learn my face", "this is what I look like"
Multi-turn guided enrollment: captures with glasses on, then glasses off,
user-paced via "ready" signals. Produces averaged 128-dim encoding for
robust recognition across angles and conditions.
"""

import asyncio
import logging
import time

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
            "Start face enrollment for automatic recognition. "
            "Guides the user through multiple captures with and without glasses. "
            "User-paced — waits for 'ready' between phases."
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
    "This starts a multi-step guided process — the user will be prompted "
    "to say 'ready' between phases."
)


def handler(args: dict) -> str:
    """Start phase 1 of face enrollment — sets up state for multi-turn flow."""
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
            person_id = pm.add_person(person_name, relationship="contact")
            person = {"person_id": person_id, "name": person_name}
    else:
        person_name = "User"
        person = pm.get_person_by_name(person_name)
        if not person:
            person_id = pm.add_person(person_name, relationship="owner")
            person = {"person_id": person_id, "name": person_name}

    person_id = person["person_id"]

    # Set enrollment state on the presence detector for the router to check
    _presence_detector._enrollment_state = {
        "person_id": person_id,
        "person_name": person_name,
        "phase": "glasses_on",  # glasses_on → glasses_off → complete
        "frames": [],
        "expires": time.time() + 300,  # 5 minute total timeout
    }

    return (
        f"Let's enroll you in the vision recognition system, {person_name}. "
        "We'll start with glasses on first, so go ahead and put them on. "
        "Say 'ready' when you're set."
    )


def handle_enrollment_ready(presence_detector) -> tuple[str, bool]:
    """Called by conversation router when user says 'ready' during enrollment.

    Captures 3 frames for the current phase, then either advances to
    next phase or completes enrollment.

    Returns:
        (response_text, is_complete)
    """
    state = presence_detector._enrollment_state
    if not state or time.time() > state["expires"]:
        presence_detector._enrollment_state = None
        return "Enrollment timed out. Please start again with 'remember my face'.", True

    tts = getattr(presence_detector, '_tts', None)
    phase = state["phase"]

    # Capture 3 poses for this phase
    poses = [
        ("Look straight at the camera.", 1.5),
        ("Now turn slightly to your left.", 2.0),
        ("And slightly to your right.", 2.0),
    ]

    for instruction, wait_time in poses:
        if tts:
            tts.speak(instruction)
        time.sleep(wait_time)

        frame_bytes = _capture_frame()
        if isinstance(frame_bytes, str):
            logger.warning("Enrollment frame failed (%s): %s", phase, frame_bytes)
        else:
            state["frames"].append(frame_bytes)

    if phase == "glasses_on":
        # Advance to glasses-off phase
        state["phase"] = "glasses_off"
        captured = len(state["frames"])
        return (
            f"Great shots, those will work well. "
            f"Now I'll need you to take your glasses off for the last few captures. "
            f"Please do so, and say 'ready' when you're set."
        ), False

    elif phase == "glasses_off":
        # Complete enrollment
        frames = state["frames"]
        person_id = state["person_id"]
        person_name = state["person_name"]
        presence_detector._enrollment_state = None

        if not frames:
            return "No frames captured. Please try again.", True

        success, message = presence_detector.enroll_face_multi(
            person_id, frames, person_name=person_name
        )
        if success:
            return (
                f"All done. {message} "
                "To activate presence detection, set vision.presence.enabled to true "
                "in the config and restart."
            ), True
        else:
            return message, True

    # Shouldn't reach here
    presence_detector._enrollment_state = None
    return "Enrollment error. Please try again.", True


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
