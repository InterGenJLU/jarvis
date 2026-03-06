"""Tool definition: capture_webcam — grab a frame from the live webcam feed."""

import asyncio
import base64
import io
import logging

logger = logging.getLogger("jarvis.tools.capture_webcam")

TOOL_NAME = "capture_webcam"
ALWAYS_INCLUDED = True

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
            "— use take_screenshot for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
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
    "After capturing a webcam frame, describe what you see in detail."
)


def handler(args: dict) -> dict | str:
    """Capture a single frame from the webcam feed.

    Returns dict with {"text": str, "image_data": str} for multimodal LLM input,
    or an error string.
    """
    try:
        from core.webcam_manager import get_webcam_manager

        wm = get_webcam_manager()
    except RuntimeError:
        return "Error: Webcam manager not initialized."

    if not wm.device_available:
        return "Error: No webcam device found. Is the camera connected?"

    # Bridge async get_frame() into sync tool handler
    # Use the loop stored by WebcamManager (same pattern as MCP client)
    try:
        loop = wm._loop
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(wm.get_frame(), loop)
            frame_bytes = future.result(timeout=10)
        else:
            # Fallback: create a temporary loop (console /webcam uses its own)
            _loop = asyncio.new_event_loop()
            try:
                frame_bytes = _loop.run_until_complete(wm.get_frame())
            finally:
                _loop.close()
    except TimeoutError:
        return "Error: Webcam frame timeout — camera may not be responding."
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.error("Webcam capture failed: %s", e)
        return f"Error: Webcam capture failed — {e}"

    # Convert JPEG frame to PNG for LLM (consistent with take_screenshot)
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(frame_bytes))
        orig_w, orig_h = img.size

        # Downscale if wider than 1280
        if orig_w > 1280:
            ratio = 1280 / orig_w
            img = img.resize((1280, int(orig_h * ratio)), Image.LANCZOS)

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
