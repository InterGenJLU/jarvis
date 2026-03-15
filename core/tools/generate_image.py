"""Tool definition: generate_image — AI image generation via FLUX.1-schnell.

Triggers a GPU swap: stops llama-server, starts flux-server, generates
the image, then restores llama-server. The LLM is temporarily unavailable
during generation (~30-90 seconds total).
"""

import logging

import requests

from core.gpu_swap import get_gpu_swap_manager

logger = logging.getLogger("jarvis.tools.generate_image")

TOOL_NAME = "generate_image"
ALWAYS_INCLUDED = False
SKILL_NAME = None  # No skill gating — always available when semantically matched

FLUX_SERVER_URL = "http://127.0.0.1:8190"

INTENT_EXAMPLES = [
    "generate an image",
    "create a picture",
    "make me an image of",
    "draw a",
    "generate a photo of",
    "create artwork",
    "make a picture of",
    "AI image of",
    "design an image",
    "illustrate",
]

SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an AI image from a text description using FLUX.1-schnell. "
            "Use this when the user asks you to create, generate, draw, design, "
            "or make an image, picture, photo, artwork, or illustration. "
            "This temporarily pauses chat capability while the image is generated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed image description. Be specific about subject, "
                        "style, lighting, composition, and colors."
                    ),
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels (256-2048, default 1024)",
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels (256-2048, default 1024)",
                },
            },
            "required": ["prompt"],
        },
    },
}

SYSTEM_PROMPT_RULE = (
    "RULE: Image generation. When the user asks to create, generate, draw, "
    "design, or make an image/picture/photo/artwork/illustration, call "
    "generate_image with a detailed prompt. Enhance the user's description "
    "with artistic details (lighting, style, composition) to produce better results. "
    "Warn the user that chat will be briefly paused during generation."
)


def handler(args: dict) -> str:
    """Generate an image via Flux.1-schnell with GPU swap."""
    prompt = args.get("prompt", "").strip()
    if not prompt:
        return "Error: No image prompt provided."

    width = args.get("width", 1024)
    height = args.get("height", 1024)

    swap = get_gpu_swap_manager()

    # 1. Swap GPU to Flux
    logger.info("Initiating GPU swap to flux for image generation")
    if not swap.swap_to("flux"):
        return (
            "Error: Could not switch to image generation mode. "
            "The GPU swap failed — llama-server may still be running."
        )

    try:
        # 2. Generate image
        logger.info("Sending generation request: %.80s...", prompt)
        response = requests.post(
            f"{FLUX_SERVER_URL}/generate",
            json={"prompt": prompt, "width": width, "height": height},
            timeout=600,
        )

        if response.status_code != 200:
            detail = response.json().get("detail", response.text) if response.headers.get("content-type", "").startswith("application/json") else response.text
            return f"Error: Image generation failed — {detail}"

        result = response.json()
        path = result["path"]
        elapsed = result["elapsed_seconds"]
        seed = result["seed"]

        logger.info("Image generated in %.1fs: %s", elapsed, path)
        return (
            f"Image generated successfully.\n"
            f"Path: {path}\n"
            f"Size: {width}x{height}\n"
            f"Generation time: {elapsed:.1f}s\n"
            f"Seed: {seed}"
        )

    except requests.Timeout:
        return "Error: Image generation timed out after 600 seconds."
    except requests.ConnectionError:
        return "Error: Could not connect to the Flux server."
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return f"Error: Image generation failed — {e}"

    finally:
        # 3. Always swap back to LLM
        logger.info("Swapping GPU back to llama-server")
        if not swap.swap_back():
            logger.error("CRITICAL: Failed to restore llama-server after image generation")
