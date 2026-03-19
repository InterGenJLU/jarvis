"""Internal webcam frame server — exposes WebcamManager over localhost HTTP.

Runs on the voice service's existing async event loop and serves frames to the
web service (which no longer opens its own ffmpeg).  Only binds to 127.0.0.1
so it's never exposed externally.

Endpoints:
    GET /frame   — single JPEG snapshot
    GET /stream  — MJPEG multipart stream (same boundary format the web UI expects)
    GET /status  — JSON {available, running}
"""

import asyncio
import logging
import time

from aiohttp import web

from core.logger import get_logger
logger = get_logger("jarvis.webcam_server")

_BOUNDARY = "jarvisframe"
_DEFAULT_PORT = 8089


async def _frame_handler(request: web.Request) -> web.Response:
    """Return the latest JPEG frame."""
    wm = request.app["webcam_manager"]
    if not wm.device_available:
        return web.Response(status=503, text="Webcam device not connected")
    try:
        frame = await wm.get_frame(timeout=10)
        return web.Response(
            body=frame,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-cache"},
        )
    except (TimeoutError, RuntimeError) as exc:
        return web.Response(status=503, text=str(exc))


async def _stream_handler(request: web.Request) -> web.StreamResponse:
    """MJPEG multipart stream — mirrors the format the web UI expects."""
    wm = request.app["webcam_manager"]
    if not wm.device_available:
        return web.Response(status=503, text="Webcam device not connected")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
            "Cache-Control": "no-cache, no-store",
            "Connection": "close",
        },
    )
    await response.prepare(request)

    await wm.register_client()
    try:
        async for frame in wm.stream_frames():
            try:
                header = (
                    f"--{_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n"
                    f"\r\n"
                ).encode()
                await response.write(header + frame + b"\r\n")
            except (ConnectionResetError, ConnectionAbortedError):
                break
    finally:
        await wm.unregister_client()

    return response


async def _status_handler(request: web.Request) -> web.Response:
    """Webcam status JSON."""
    wm = request.app["webcam_manager"]
    return web.json_response({
        "available": wm.device_available,
        "running": wm.is_running,
    })


async def start_webcam_server(
    webcam_manager,
    port: int = _DEFAULT_PORT,
) -> web.AppRunner:
    """Start the internal frame server.  Returns the runner for later cleanup."""
    app = web.Application()
    app["webcam_manager"] = webcam_manager
    app.router.add_get("/frame", _frame_handler)
    app.router.add_get("/stream", _stream_handler)
    app.router.add_get("/status", _status_handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("Webcam frame server listening on 127.0.0.1:%d", port)
    return runner
