"""Flux.1-schnell image generation server.

Standalone FastAPI process — NOT imported by JARVIS core.
Started/stopped on demand by GPUSwapManager to share VRAM with llama-server.

Usage:
    uvicorn services.flux_server:app --host 127.0.0.1 --port 8190
"""

import gc
import logging
import os
import time
from pathlib import Path

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("flux_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_PATH = os.environ.get(
    "FLUX_MODEL_PATH",
    "/mnt/storage/jarvis/models/flux/FLUX.1-schnell",
)
GGUF_PATH = os.environ.get(
    "FLUX_GGUF_PATH",
    "/mnt/storage/jarvis/models/flux/FLUX.1-schnell-GGUF/flux1-schnell-Q8_0.gguf",
)
OUTPUT_DIR = os.environ.get("FLUX_OUTPUT_DIR", "/tmp/jarvis_images")
DEVICE = "cuda"  # ROCm HIP-mapped — targets RX 7900 XT

# ---------------------------------------------------------------------------
# App + state
# ---------------------------------------------------------------------------

app = FastAPI(title="JARVIS Flux Server", version="1.0.0")
_pipeline = None  # Loaded on startup
_model_ready = False


class GenerateRequest(BaseModel):
    prompt: str
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)
    steps: int = Field(default=4, ge=1, le=8)
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
    """Load Flux.1-schnell — GGUF Q8_0 transformer on GPU, T5 via CPU offload."""
    global _pipeline, _model_ready

    logger.info("Loading Flux.1-schnell from %s ...", MODEL_PATH)
    t0 = time.time()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    from diffusers import FluxPipeline, FluxTransformer2DModel, GGUFQuantizationConfig

    # Load pre-quantized GGUF transformer directly (~12.6GB Q8_0, no OOM risk)
    transformer = FluxTransformer2DModel.from_single_file(
        GGUF_PATH,
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
        torch_dtype=torch.bfloat16,
    )

    # Load pipeline with GGUF transformer; T5 + VAE stay in CPU RAM via offload
    _pipeline = FluxPipeline.from_pretrained(
        MODEL_PATH,
        transformer=transformer,
        torch_dtype=torch.bfloat16,
    )
    _pipeline.enable_model_cpu_offload()
    _pipeline.vae.enable_slicing()
    _pipeline.vae.enable_tiling()

    elapsed = time.time() - t0
    _model_ready = True
    logger.info("Flux.1-schnell loaded in %.1fs", elapsed)


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
        "model": "flux-schnell-Q8_0-gguf",
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
        guidance_scale=0.0,  # schnell requires 0
        num_inference_steps=req.steps,
        max_sequence_length=256,  # schnell max
        width=req.width,
        height=req.height,
        generator=generator,
    ).images[0]

    elapsed = time.time() - t0

    # Save with timestamp
    filename = f"flux_{int(time.time())}_{seed}.png"
    out_path = str(Path(OUTPUT_DIR) / filename)
    image.save(out_path)

    logger.info("Generated in %.1fs → %s", elapsed, out_path)

    return GenerateResponse(path=out_path, seed=seed, elapsed_seconds=round(elapsed, 2))
