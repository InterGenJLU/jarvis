"""WebcamManager — singleton ffmpeg MJPEG feed manager.

Runs a single ffmpeg subprocess that reads native MJPEG frames from the webcam
(zero CPU transcoding via -c:v copy passthrough) and fans them out to:
  - /api/webcam/stream  (MJPEG multipart for web UI)
  - /api/webcam/snapshot (single JPEG)
  - get_frame()          (LLM tool capture)

Auto-starts on first client, auto-stops 30s after last client disconnects.

MobileCameraRelay — WebSocket-based frame relay for mobile cameras.
When the desktop webcam is unavailable, the capture_webcam tool can request
a frame from a connected mobile browser via getUserMedia + canvas capture.
"""

import asyncio
import base64
import logging
import os
import time
import uuid

logger = logging.getLogger("jarvis.webcam")

# JPEG markers
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

# Singleton
_instance: "WebcamManager | None" = None


def get_webcam_manager(config: dict | None = None) -> "WebcamManager":
    """Get or create the singleton WebcamManager."""
    global _instance
    if _instance is None:
        if config is None:
            raise RuntimeError("WebcamManager not initialized — pass config on first call")
        _instance = WebcamManager(config)
    return _instance


class WebcamManager:
    """Manages a persistent ffmpeg MJPEG feed from a V4L2 webcam."""

    def __init__(self, config: dict):
        vision_cfg = config.get("vision", {})
        self._device = vision_cfg.get("webcam_device", "/dev/video0")
        self._fps = vision_cfg.get("webcam_fps", 15)

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None

        self._current_frame: bytes | None = None
        self._frame_condition = asyncio.Condition()
        self._client_count = 0
        self._running = False
        self._last_frame_time: float = 0
        self._loop: asyncio.AbstractEventLoop | None = None  # set on start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the ffmpeg subprocess and frame reader."""
        if self._running:
            return

        if not os.path.exists(self._device):
            raise FileNotFoundError(f"Webcam device not found: {self._device}")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720",
            "-framerate", str(self._fps),
            "-i", self._device,
            "-an", "-c:v", "copy", "-f", "mjpeg", "pipe:1",
        ]

        self._loop = asyncio.get_event_loop()
        logger.info("Starting webcam feed: %s @ %d fps", self._device, self._fps)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True
        self._reader_task = asyncio.create_task(self._read_frames())

        # Cancel idle timer if one was pending
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    async def stop(self) -> None:
        """Stop the ffmpeg subprocess and clean up."""
        if not self._running:
            return

        self._running = False
        logger.info("Stopping webcam feed")

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None

        self._current_frame = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def device_available(self) -> bool:
        return os.path.exists(self._device)

    # ------------------------------------------------------------------
    # Client tracking
    # ------------------------------------------------------------------

    async def register_client(self) -> None:
        """Register a stream client. Auto-starts feed if needed."""
        self._client_count += 1
        logger.debug("Client registered (count=%d)", self._client_count)
        if not self._running:
            await self.start()
        # Cancel idle shutdown if pending
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    async def unregister_client(self) -> None:
        """Unregister a stream client. Starts idle timer when count hits 0."""
        self._client_count = max(0, self._client_count - 1)
        logger.debug("Client unregistered (count=%d)", self._client_count)
        if self._client_count == 0:
            self._idle_task = asyncio.create_task(self._idle_shutdown())

    async def _idle_shutdown(self) -> None:
        """Stop feed 30s after the last client disconnects."""
        try:
            await asyncio.sleep(30)
            if self._client_count == 0:
                logger.info("Idle timeout — stopping webcam feed")
                await self.stop()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Frame reading
    # ------------------------------------------------------------------

    async def _read_frames(self) -> None:
        """Read MJPEG stream from ffmpeg stdout, parse JPEG boundaries."""
        assert self._process and self._process.stdout
        buf = bytearray()
        stdout = self._process.stdout

        try:
            while self._running:
                chunk = await stdout.read(65536)
                if not chunk:
                    # ffmpeg exited or device disconnected
                    break
                buf.extend(chunk)

                # Extract complete JPEG frames from buffer
                while True:
                    soi = buf.find(_SOI)
                    if soi == -1:
                        buf.clear()
                        break

                    # Discard any garbage before SOI
                    if soi > 0:
                        del buf[:soi]

                    eoi = buf.find(_EOI, 2)  # skip past the SOI we just found
                    if eoi == -1:
                        break  # incomplete frame, wait for more data

                    # Complete frame: SOI through EOI+2
                    frame_end = eoi + 2
                    frame = bytes(buf[:frame_end])
                    del buf[:frame_end]

                    self._current_frame = frame
                    self._last_frame_time = time.monotonic()

                    # Notify waiting clients
                    async with self._frame_condition:
                        self._frame_condition.notify_all()

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Frame reader error: %s", e)

        # If we're still supposed to be running, the device disconnected
        if self._running and self._client_count > 0:
            logger.warning("Webcam disconnected — retrying in 3s")
            self._running = False
            self._process = None
            await asyncio.sleep(3)
            try:
                await self.start()
            except FileNotFoundError:
                logger.error("Webcam device gone — giving up")

    # ------------------------------------------------------------------
    # Frame access
    # ------------------------------------------------------------------

    async def get_frame(self, timeout: float = 10.0) -> bytes:
        """Get the latest JPEG frame. Auto-starts feed if needed.

        Returns raw JPEG bytes.
        Raises TimeoutError if no frame arrives within timeout.
        """
        if not self._running:
            await self.start()

        # If we already have a recent frame (<2s), return it immediately
        if self._current_frame and (time.monotonic() - self._last_frame_time) < 2.0:
            return self._current_frame

        # Wait for next frame
        async with self._frame_condition:
            try:
                await asyncio.wait_for(
                    self._frame_condition.wait(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"No webcam frame received within {timeout}s")

        if self._current_frame is None:
            raise RuntimeError("Webcam frame unavailable")
        return self._current_frame

    async def stream_frames(self):
        """Async generator yielding JPEG frames as they arrive.

        Used by the MJPEG stream endpoint.
        """
        while self._running:
            async with self._frame_condition:
                await self._frame_condition.wait()
            if self._current_frame:
                yield self._current_frame


# ==========================================================================
# MobileCameraRelay — WebSocket frame relay for mobile getUserMedia
# ==========================================================================

_relay_instance: "MobileCameraRelay | None" = None


def get_mobile_relay() -> "MobileCameraRelay":
    """Get or create the singleton MobileCameraRelay."""
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = MobileCameraRelay()
    return _relay_instance


class MobileCameraRelay:
    """Relays frame capture requests to a mobile browser via WebSocket.

    Protocol:
        Server → Client: {"type": "frame_request", "request_id": "<uuid>"}
        Client → Server: {"type": "frame_response", "request_id": "<uuid>",
                          "image_data": "<base64 jpeg>"}
                     OR: {"type": "frame_response", "request_id": "<uuid>",
                          "error": "<message>"}
    """

    def __init__(self):
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, asyncio.Future] = {}

    def set_ws(self, ws, loop: asyncio.AbstractEventLoop) -> None:
        """Register the mobile client's WebSocket for frame relay."""
        # Clear any previous connection
        self.clear_ws()
        self._ws = ws
        self._loop = loop
        logger.info("Mobile camera relay connected")

    def clear_ws(self) -> None:
        """Disconnect mobile client and cancel pending requests."""
        was_connected = self._ws is not None
        self._ws = None
        cancelled = 0
        for rid, fut in self._pending.items():
            if not fut.done():
                fut.cancel()
                cancelled += 1
        self._pending.clear()
        if was_connected:
            logger.info("Mobile camera relay disconnected (cancelled %d pending)", cancelled)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def request_frame(self, timeout: float = 30.0) -> bytes:
        """Request a frame from the mobile browser. Returns raw JPEG bytes.

        Raises TimeoutError if no response within timeout.
        Raises RuntimeError if no mobile client connected.
        """
        if not self.is_connected:
            raise RuntimeError("No mobile camera connected")

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut

        try:
            logger.debug("Requesting frame from mobile (id=%s)", request_id)
            await self._ws.send_json({
                "type": "frame_request",
                "request_id": request_id,
            })
            result = await asyncio.wait_for(fut, timeout=timeout)
            logger.debug("Mobile frame received (id=%s, %d bytes)", request_id, len(result))
            return result
        except asyncio.TimeoutError:
            logger.warning("Mobile frame request timed out (id=%s)", request_id)
            raise TimeoutError(f"Mobile camera frame timeout ({timeout}s)")
        finally:
            self._pending.pop(request_id, None)

    def deliver_frame(self, request_id: str, image_data: str) -> None:
        """Resolve a pending frame request with base64 JPEG data."""
        fut = self._pending.get(request_id)
        if fut and not fut.done():
            try:
                raw = base64.b64decode(image_data)
                fut.set_result(raw)
                logger.debug("Delivered frame (id=%s, %d bytes)", request_id, len(raw))
            except Exception as e:
                logger.error("Invalid frame data (id=%s): %s", request_id, e)
                fut.set_exception(RuntimeError(f"Invalid frame data: {e}"))
        else:
            logger.warning("deliver_frame: no pending future for id=%s", request_id)

    def deliver_error(self, request_id: str, error: str) -> None:
        """Resolve a pending frame request with an error."""
        fut = self._pending.get(request_id)
        if fut and not fut.done():
            fut.set_exception(RuntimeError(error))
            logger.warning("Mobile frame error (id=%s): %s", request_id, error)
        else:
            logger.warning("deliver_error: no pending future for id=%s", request_id)
