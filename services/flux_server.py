"""Flux.2 Klein 4B image generation server.

Standalone FastAPI process — NOT imported by JARVIS core.
Started/stopped on demand by GPUSwapManager to share VRAM with llama-server.

Supports both text-to-image and image-to-image (img2img) generation.
Uses Flux2KleinPipeline (BF16 safetensors, not GGUF — AMD noise bug with GGUF).

Usage:
    uvicorn services.flux_server:app --host 127.0.0.1 --port 8190
"""

import base64
import gc
import io
import logging
import os
import time
from pathlib import Path

import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field

logger = logging.getLogger("flux_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_PATH = os.environ.get(
    "FLUX_MODEL_PATH",
    "/mnt/storage/jarvis/models/flux/FLUX.2-klein-4B",
)
OUTPUT_DIR = os.environ.get("FLUX_OUTPUT_DIR", "/home/user/jarvis/generated_images")
DEVICE = "cuda"  # ROCm HIP-mapped — targets RX 7900 XT

# ---------------------------------------------------------------------------
# App + state
# ---------------------------------------------------------------------------

app = FastAPI(title="JARVIS Flux Server", version="2.0.0")
_pipeline = None
_model_ready = False


class GenerateRequest(BaseModel):
    prompt: str
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)
    steps: int = Field(default=20, ge=1, le=50)
    seed: int = Field(default=-1)


class Img2ImgRequest(BaseModel):
    prompt: str
    image: str  # base64-encoded source image
    strength: float = Field(default=0.75, ge=0.1, le=1.0)  # kept for API compat, not used by Flux.2
    steps: int = Field(default=20, ge=1, le=50)
    seed: int = Field(default=-1)


class GenerateResponse(BaseModel):
    path: str
    seed: int
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# Model lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def load_model():
    """Load Flux.2 Klein 4B — BF16 safetensors with CPU offload."""
    global _pipeline, _model_ready

    logger.info("Loading Flux.2 Klein 4B from %s ...", MODEL_PATH)
    t0 = time.time()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    from diffusers import Flux2KleinPipeline

    _pipeline = Flux2KleinPipeline.from_pretrained(
        MODEL_PATH,
        torch_dtype=torch.bfloat16,
    )
    _pipeline.enable_model_cpu_offload()
    _pipeline.vae.enable_slicing()
    _pipeline.vae.enable_tiling()

    elapsed = time.time() - t0
    _model_ready = True
    logger.info("Flux.2 Klein 4B loaded in %.1fs", elapsed)


@app.on_event("shutdown")
def unload_model():
    """Free VRAM on shutdown."""
    global _pipeline, _model_ready
    _model_ready = False

    if _pipeline is not None:
        del _pipeline
        _pipeline = None

    torch.cuda.empty_cache()  # ROCm HIP-mapped — frees AMD GPU memory
    gc.collect()
    logger.info("Flux pipeline unloaded, VRAM freed")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ready" if _model_ready else "loading",
        "model": "flux2-klein-4B-bf16",
        "device": DEVICE,
    }


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    if not _model_ready or _pipeline is None:
        raise HTTPException(503, "Model not loaded yet")

    seed = req.seed if req.seed >= 0 else int(time.time()) % (2**32)
    generator = torch.Generator("cpu").manual_seed(seed)

    logger.info(
        "Generating %dx%d, steps=%d, seed=%d: %.80s...",
        req.width, req.height, req.steps, seed, req.prompt,
    )
    t0 = time.time()

    image = _pipeline(
        prompt=req.prompt,
        guidance_scale=1.0,
        num_inference_steps=req.steps,
        width=req.width,
        height=req.height,
        generator=generator,
    ).images[0]

    elapsed = time.time() - t0

    filename = f"flux_{int(time.time())}_{seed}.png"
    out_path = str(Path(OUTPUT_DIR) / filename)
    image.save(out_path)

    logger.info("Generated in %.1fs → %s", elapsed, out_path)

    return GenerateResponse(path=out_path, seed=seed, elapsed_seconds=round(elapsed, 2))


@app.post("/img2img", response_model=GenerateResponse)
def img2img(req: Img2ImgRequest):
    if not _model_ready or _pipeline is None:
        raise HTTPException(503, "Model not loaded yet")

    # Decode base64 source image
    try:
        image_data = base64.b64decode(req.image)
        source_image = Image.open(io.BytesIO(image_data)).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"Invalid image data: {e}")

    # Resize to nearest multiple of 16 (required by VAE), cap at 1024
    w, h = source_image.size
    scale = min(1024 / max(w, h), 1.0)
    w, h = int(w * scale) // 16 * 16, int(h * scale) // 16 * 16
    source_image = source_image.resize((w, h), Image.LANCZOS)

    seed = req.seed if req.seed >= 0 else int(time.time()) % (2**32)
    generator = torch.Generator("cpu").manual_seed(seed)

    logger.info(
        "Img2img %dx%d, strength=%.2f, steps=%d, seed=%d: %.80s...",
        w, h, req.strength, req.steps, seed, req.prompt,
    )
    t0 = time.time()

    # Flux.2 Klein uses image as conditioning (not traditional img2img with strength)
    image = _pipeline(
        prompt=req.prompt,
        image=source_image,
        guidance_scale=1.0,
        num_inference_steps=req.steps,
        generator=generator,
    ).images[0]

    elapsed = time.time() - t0

    filename = f"flux_i2i_{int(time.time())}_{seed}.png"
    out_path = str(Path(OUTPUT_DIR) / filename)
    image.save(out_path)

    logger.info("Img2img generated in %.1fs → %s", elapsed, out_path)

    return GenerateResponse(path=out_path, seed=seed, elapsed_seconds=round(elapsed, 2))
