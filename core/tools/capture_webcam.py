"""Tool definition: capture_webcam — grab a frame from the live webcam feed.

Supports two capture paths:
  - Desktop: ffmpeg MJPEG feed via WebcamManager (V4L2)
  - Mobile:  WebSocket frame relay via MobileCameraRelay (getUserMedia)
"""

import asyncio
import base64
import io
import logging

logger = logging.getLogger("jarvis.tools.capture_webcam")

TOOL_NAME = "capture_webcam"
ALWAYS_INCLUDED = True

# Dependency injection — wired by tool_executor.set_mobile_camera_relay()
DEPENDENCIES = {"mobile_camera_relay": "_mobile_relay"}
_mobile_relay = None  # MobileCameraRelay instance, set at runtime

INTENT_EXAMPLES = [
    "what do you see",
    "take a photo",
    "webcam",
    "look at me",
    "who's there",
    "what's in the room",
    "what am I holding",
    "can you see me",
    "describe what's in front of you",
    "what's on my desk",
]

SCHEMA = {
    "type": "function",
    "function": {
        "name": "capture_webcam",
        "description": (
            "Capture a frame from the live webcam to see the physical world. "
            "Use this for questions about the room, objects, people, or anything "
            "the user is showing to the camera. Do NOT use this for screen content "
            "— use take_screenshot for that. "
            "Set source='mobile' when the user is on a mobile device, "
            "mentions their phone camera, or mentions their iPhone camera."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["auto", "desktop", "mobile"],
                    "description": (
                        "Which camera to capture from. "
                        "'auto' tries desktop webcam first, then mobile. "
                        "'desktop' uses only the server-side V4L2 webcam. "
                        "'mobile' uses only the phone camera via WebSocket relay."
                    ),
                },
            },
            "required": [],
        },
    },
}

SYSTEM_PROMPT_RULE = (
    "RULE: Physical world vs screen content. "
    "Use capture_webcam for physical-world questions: what do you see, look at me, "
    "what am I holding, who's there, what's in the room, take a photo. "
    "Use take_screenshot for screen/monitor/display content: what's on my screen, "
    "screenshot, describe my display. "
    "If the user message source is mobile, the user mentions their phone camera, "
    "or the user mentions their iPhone camera, use source='mobile'. "
    "After capturing a webcam frame, describe what you see in detail."
)


def _process_frame(frame_bytes: bytes) -> dict | str:
    """Convert raw JPEG frame bytes to base64 PNG for multimodal LLM input."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(frame_bytes))
        orig_w, orig_h = img.size

        # Downscale if wider than 1920
        if orig_w > 1920:
            ratio = 1920 / orig_w
            img = img.resize((1920, int(orig_h * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        image_bytes = buf.getvalue()
        img.close()

        image_data = base64.b64encode(image_bytes).decode("utf-8")
        size_kb = len(image_bytes) / 1024

        return {
            "text": (
                f"Webcam frame captured ({orig_w}x{orig_h}, {size_kb:.0f} KB). "
                "The image is attached — describe what you see."
            ),
            "image_data": image_data,
        }
    except ImportError:
        return "Error: PIL not available — cannot process webcam frame."
    except Exception as e:
        logger.error("Frame processing failed: %s", e)
        return f"Error: Frame processing failed — {e}"


def _capture_desktop() -> bytes | str | None:
    """Capture a frame from the desktop webcam (V4L2/ffmpeg).

    Returns:
        bytes: raw JPEG frame
        str: error message (terminal — don't try mobile)
        None: no desktop webcam available (try mobile fallback)
    """
    try:
        from core.webcam_manager import get_webcam_manager
        wm = get_webcam_manager()
    except RuntimeError:
        return None  # Not initialized — try mobile fallback

    if not wm.device_available:
        return None  # No device — try mobile fallback

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
        return "Error: Webcam frame timeout — camera may not be responding."
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.error("Desktop webcam capture failed: %s", e)
        return f"Error: Webcam capture failed — {e}"


def _capture_mobile() -> bytes | str:
    """Capture a frame from the mobile browser via WebSocket relay."""
    relay = _mobile_relay
    if not relay or not relay.is_connected:
        return "Error: No camera available. Connect a webcam or open the camera panel on your phone."

    try:
        # relay.request_frame() is async — bridge to sync
        loop = relay._loop
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(relay.request_frame(), loop)
            return future.result(timeout=35)
        else:
            return "Error: Mobile camera relay loop not available."
    except TimeoutError:
        logger.warning("Mobile camera sync bridge timed out (25s)")
        return ("Error: Mobile camera timed out. "
                "Make sure the camera panel is open on your phone.")
    except RuntimeError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.error("Mobile camera capture failed: %s", e)
        return f"Error: Mobile camera capture failed — {e}"


def handler(args: dict) -> dict | str:
    """Capture a single frame from the webcam feed.

    Tries desktop webcam first; falls back to mobile camera relay.
    source='mobile' skips desktop and goes straight to phone camera.
    source='desktop' skips mobile fallback.
    Returns dict with {"text": str, "image_data": str} for multimodal LLM input,
    or an error string.
    """
    source = args.get("source", "auto")

    if source == "mobile":
        result = _capture_mobile()
    elif source == "desktop":
        result = _capture_desktop()
        if result is None:
            result = "Error: No desktop webcam available."
    else:
        # Auto: try desktop first, fall back to mobile
        result = _capture_desktop()
        if result is None:
            logger.debug("No desktop webcam — trying mobile camera relay")
            result = _capture_mobile()
        elif isinstance(result, bytes):
            logger.debug("Captured frame from desktop webcam (%d bytes)", len(result))

    # String result = error message
    if isinstance(result, str):
        return result

    # bytes result = raw JPEG frame — process it
    return _process_frame(result)
