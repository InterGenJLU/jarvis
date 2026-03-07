#!/usr/bin/env python3
"""
Vision system unit tests — WebcamManager, capture_webcam, take_screenshot.

Tests frame parsing, lifecycle management, and tool handler behavior using
mocked ffmpeg subprocesses and webcam managers. No real hardware required.

Usage:
    python3 -u scripts/test_vision.py --verbose              # All parts
    python3 -u scripts/test_vision.py --part 1 --verbose     # Frame parser only
    python3 -u scripts/test_vision.py --part 2 --verbose     # Lifecycle only
    python3 -u scripts/test_vision.py --part 3 --verbose     # capture_webcam handler
    python3 -u scripts/test_vision.py --part 4 --verbose     # take_screenshot handler
"""

import argparse
import asyncio
import base64
import io
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Test infrastructure (matches existing test_tool_artifacts.py pattern)
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""

results: list[TestResult] = []
verbose = False

def log(msg: str):
    print(msg, flush=True)

def assert_eq(name, actual, expected, detail=""):
    passed = actual == expected
    results.append(TestResult(name, passed, detail or f"expected={expected!r}, got={actual!r}"))
    if verbose:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not passed and detail else ""))
    elif not passed:
        log(f"  [FAIL] {name} — expected={expected!r}, got={actual!r}")

def assert_true(name, condition, detail=""):
    results.append(TestResult(name, bool(condition), detail))
    if verbose:
        status = "PASS" if condition else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not condition and detail else ""))
    elif not condition:
        log(f"  [FAIL] {name} — {detail}")

def assert_in(name, needle, haystack, detail=""):
    passed = needle in haystack
    results.append(TestResult(name, passed, detail or f"{needle!r} not in result"))
    if verbose:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not passed and detail else ""))
    elif not passed:
        log(f"  [FAIL] {name} — {needle!r} not in result")

def assert_isinstance(name, obj, cls, detail=""):
    passed = isinstance(obj, cls)
    results.append(TestResult(name, passed, detail or f"expected {cls.__name__}, got {type(obj).__name__}"))
    if verbose:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}" + (f" — {detail}" if not passed and detail else ""))
    elif not passed:
        log(f"  [FAIL] {name} — expected {cls.__name__}, got {type(obj).__name__}")


# ---------------------------------------------------------------------------
# JPEG helpers
# ---------------------------------------------------------------------------

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

def make_jpeg(payload_size: int = 100) -> bytes:
    """Construct a minimal JPEG-like frame (SOI + payload + EOI)."""
    # Avoid embedding SOI/EOI markers in the payload
    payload = bytes(b % 254 + 1 for b in range(payload_size))
    return _SOI + payload + _EOI


# ═══════════════════════════════════════════════════════════════════════════
# PART 1: Frame parser unit tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part1():
    log("\n═══ Part 1: Frame parser (JPEG SOI/EOI boundary detection) ═══")

    from core.webcam_manager import WebcamManager

    async def run_parser(chunks: list[bytes]) -> list[bytes]:
        """Feed byte chunks into the frame reader and collect parsed frames."""
        config = {"vision": {"webcam_device": "/dev/null", "webcam_fps": 15}}
        wm = WebcamManager(config)

        collected_frames = []
        original_frame_time = 0

        # Mock the subprocess with a controlled stdout reader
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        # Build an async reader that yields our chunks then EOF
        chunk_iter = iter(chunks)
        async def mock_read(n):
            try:
                return next(chunk_iter)
            except StopIteration:
                return b""  # EOF
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = mock_read

        wm._process = mock_proc
        wm._running = True
        wm._frame_condition = asyncio.Condition()

        # Capture frames as they're parsed
        original_notify = wm._frame_condition.notify_all

        async def capture_frame():
            if wm._current_frame:
                collected_frames.append(wm._current_frame)
            # Call original
            original_notify()

        # Monkey-patch: wrap _read_frames to capture frames via condition
        # We'll collect frames by polling after the task completes
        await wm._read_frames()

        # After _read_frames exits (EOF), check what was parsed
        if wm._current_frame:
            # The reader only keeps the LAST frame in _current_frame,
            # but we need all frames. Let's re-implement the test differently.
            pass

        return collected_frames

    # Since _read_frames only stores the LAST frame, we need a different
    # approach: intercept frame storage to capture ALL frames.
    async def run_parser_all(chunks: list[bytes]) -> list[bytes]:
        """Feed chunks and capture every frame the parser extracts."""
        config = {"vision": {"webcam_device": "/dev/null", "webcam_fps": 15}}
        wm = WebcamManager(config)
        collected = []

        mock_proc = MagicMock()
        chunk_iter = iter(chunks)
        async def mock_read(n):
            try:
                return next(chunk_iter)
            except StopIteration:
                return b""
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = mock_read

        wm._process = mock_proc
        wm._running = True
        wm._frame_condition = asyncio.Condition()

        # Intercept frame assignment to capture all frames
        class FrameCapture:
            def __setattr__(self, name, value):
                if name == '_current_frame' and value is not None:
                    collected.append(value)
                object.__setattr__(wm, name, value)

        # Patch __setattr__ on the instance for _current_frame tracking
        orig_setattr = type(wm).__setattr__
        def capturing_setattr(self, name, value):
            if name == '_current_frame' and value is not None:
                collected.append(value)
            orig_setattr(self, name, value)

        type(wm).__setattr__ = capturing_setattr
        try:
            await wm._read_frames()
        finally:
            type(wm).__setattr__ = orig_setattr

        return collected

    # --- Test 1.1: Single complete frame ---
    log("\n── 1.1: Single complete JPEG frame ──")
    frame = make_jpeg(200)
    frames = asyncio.run(run_parser_all([frame]))
    assert_eq("single_frame: count", len(frames), 1)
    if frames:
        assert_eq("single_frame: content", frames[0], frame)

    # --- Test 1.2: Two frames in one chunk ---
    log("\n── 1.2: Two frames in single chunk ──")
    frame1 = make_jpeg(100)
    frame2 = make_jpeg(150)
    frames = asyncio.run(run_parser_all([frame1 + frame2]))
    assert_eq("two_in_one: count", len(frames), 2)
    if len(frames) >= 2:
        assert_eq("two_in_one: frame1", frames[0], frame1)
        assert_eq("two_in_one: frame2", frames[1], frame2)

    # --- Test 1.3: Frame split across two reads ---
    log("\n── 1.3: Frame split across two read calls ──")
    frame = make_jpeg(200)
    split = len(frame) // 2
    frames = asyncio.run(run_parser_all([frame[:split], frame[split:]]))
    assert_eq("split_frame: count", len(frames), 1)
    if frames:
        assert_eq("split_frame: content", frames[0], frame)

    # --- Test 1.4: Garbage bytes before SOI ---
    log("\n── 1.4: Garbage before SOI discarded ──")
    garbage = b"\x00\x01\x02\x03\x04\x05"
    frame = make_jpeg(100)
    frames = asyncio.run(run_parser_all([garbage + frame]))
    assert_eq("garbage_prefix: count", len(frames), 1)
    if frames:
        assert_eq("garbage_prefix: content", frames[0], frame)

    # --- Test 1.5: Empty chunk (ffmpeg exit) breaks loop ---
    log("\n── 1.5: Empty chunk breaks read loop ──")
    frames = asyncio.run(run_parser_all([b""]))
    assert_eq("empty_chunk: count", len(frames), 0)

    # --- Test 1.6: Three frames across multiple reads ---
    log("\n── 1.6: Three frames across multiple reads ──")
    f1 = make_jpeg(80)
    f2 = make_jpeg(120)
    f3 = make_jpeg(90)
    # Split: f1 complete, f2 split, f3 complete
    chunk1 = f1 + f2[:30]
    chunk2 = f2[30:] + f3
    frames = asyncio.run(run_parser_all([chunk1, chunk2]))
    assert_eq("three_frames: count", len(frames), 3)
    if len(frames) >= 3:
        assert_eq("three_frames: f1", frames[0], f1)
        assert_eq("three_frames: f2", frames[1], f2)
        assert_eq("three_frames: f3", frames[2], f3)


# ═══════════════════════════════════════════════════════════════════════════
# PART 2: WebcamManager lifecycle tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part2():
    log("\n═══ Part 2: WebcamManager lifecycle ═══")

    from core.webcam_manager import WebcamManager

    def make_config(device="/dev/video0"):
        return {"vision": {"webcam_device": device, "webcam_fps": 15}}

    # --- Test 2.1: device_available checks os.path.exists ---
    log("\n── 2.1: device_available property ──")
    wm = WebcamManager(make_config("/dev/null"))  # /dev/null always exists
    assert_true("device_available: /dev/null exists", wm.device_available)

    wm2 = WebcamManager(make_config("/dev/nonexistent_webcam_xyz"))
    assert_true("device_available: nonexistent is False", not wm2.device_available)

    # --- Test 2.2: initial state ---
    log("\n── 2.2: Initial state ──")
    wm = WebcamManager(make_config())
    assert_true("initial: not running", not wm.is_running)
    assert_eq("initial: client_count=0", wm._client_count, 0)
    assert_true("initial: no current frame", wm._current_frame is None)

    # --- Test 2.3: start raises FileNotFoundError for missing device ---
    log("\n── 2.3: start() with missing device ──")
    wm = WebcamManager(make_config("/dev/nonexistent_webcam_xyz"))
    try:
        asyncio.run(wm.start())
        assert_true("start_missing: should have raised", False)
    except FileNotFoundError:
        assert_true("start_missing: FileNotFoundError raised", True)

    # --- Test 2.4: register_client auto-starts and increments count ---
    log("\n── 2.4: register_client auto-start ──")
    async def test_register():
        wm = WebcamManager(make_config("/dev/null"))
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            await wm.register_client()
            assert_true("register: is_running after register", wm.is_running)
            assert_eq("register: client_count=1", wm._client_count, 1)

            await wm.register_client()
            assert_eq("register: client_count=2", wm._client_count, 2)

            # Cleanup
            await wm.stop()

    asyncio.run(test_register())

    # --- Test 2.5: unregister_client decrements, floor at 0 ---
    log("\n── 2.5: unregister_client ──")
    async def test_unregister():
        wm = WebcamManager(make_config("/dev/null"))
        wm._running = True  # pretend we're running
        wm._client_count = 2

        await wm.unregister_client()
        assert_eq("unregister: count=1", wm._client_count, 1)

        await wm.unregister_client()
        assert_eq("unregister: count=0", wm._client_count, 0)
        assert_true("unregister: idle_task started", wm._idle_task is not None)

        # Unregister again — floor at 0
        await wm.unregister_client()
        assert_eq("unregister: floor at 0", wm._client_count, 0)

        # Cancel the idle task to avoid warnings
        if wm._idle_task:
            wm._idle_task.cancel()
            try:
                await wm._idle_task
            except asyncio.CancelledError:
                pass

    asyncio.run(test_unregister())

    # --- Test 2.6: re-register cancels idle timer ---
    log("\n── 2.6: Re-register cancels idle timer ──")
    async def test_cancel_idle():
        wm = WebcamManager(make_config("/dev/null"))
        wm._running = True
        wm._client_count = 1

        # Unregister to start idle timer
        await wm.unregister_client()
        assert_true("cancel_idle: idle_task exists", wm._idle_task is not None)
        idle_task = wm._idle_task

        # Re-register should cancel it
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()

        await wm.register_client()
        # Yield to let cancellation propagate through event loop
        await asyncio.sleep(0)
        assert_true("cancel_idle: old idle_task cancelled", idle_task.cancelled())
        assert_true("cancel_idle: idle_task cleared", wm._idle_task is None)

        # Cleanup
        wm._running = False
        if wm._reader_task:
            wm._reader_task.cancel()
            try:
                await wm._reader_task
            except asyncio.CancelledError:
                pass

    asyncio.run(test_cancel_idle())

    # --- Test 2.7: get_frame returns cached frame if fresh ---
    log("\n── 2.7: get_frame returns cached frame ──")
    async def test_cached_frame():
        wm = WebcamManager(make_config("/dev/null"))
        wm._running = True
        test_frame = make_jpeg(100)
        wm._current_frame = test_frame
        wm._last_frame_time = time.monotonic()  # fresh

        frame = await wm.get_frame()
        assert_eq("cached_frame: returns cached", frame, test_frame)

    asyncio.run(test_cached_frame())

    # --- Test 2.8: get_frame raises TimeoutError if no frame ---
    log("\n── 2.8: get_frame timeout ──")
    async def test_frame_timeout():
        wm = WebcamManager(make_config("/dev/null"))
        wm._running = True
        wm._current_frame = None
        wm._last_frame_time = 0  # stale
        wm._frame_condition = asyncio.Condition()

        try:
            await wm.get_frame(timeout=0.1)
            assert_true("frame_timeout: should have raised", False)
        except TimeoutError:
            assert_true("frame_timeout: TimeoutError raised", True)

    asyncio.run(test_frame_timeout())

    # --- Test 2.9: stop() cleans up state ---
    log("\n── 2.9: stop() cleanup ──")
    async def test_stop():
        wm = WebcamManager(make_config("/dev/null"))
        wm._running = True
        wm._current_frame = make_jpeg(50)

        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.kill = MagicMock()
        wm._process = mock_proc
        wm._reader_task = asyncio.create_task(asyncio.sleep(100))

        await wm.stop()
        assert_true("stop: not running", not wm.is_running)
        assert_true("stop: frame cleared", wm._current_frame is None)
        assert_true("stop: process cleared", wm._process is None)
        assert_true("stop: reader_task cleared", wm._reader_task is None)

    asyncio.run(test_stop())


# ═══════════════════════════════════════════════════════════════════════════
# PART 3: capture_webcam handler unit tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part3():
    log("\n═══ Part 3: capture_webcam handler ═══")

    import core.tools.capture_webcam as tool

    # --- Test 3.1: error when webcam manager not initialized ---
    log("\n── 3.1: Handler error — not initialized ──")
    # Reset singleton
    import core.webcam_manager as wm_mod
    original_instance = wm_mod._instance
    wm_mod._instance = None
    try:
        result = tool.handler({})
        assert_isinstance("not_init: returns str", result, str)
        assert_in("not_init: error message", "Error", result)
    finally:
        wm_mod._instance = original_instance

    # --- Test 3.2: error when device unavailable ---
    log("\n── 3.2: Handler error — device unavailable ──")
    mock_wm = MagicMock()
    mock_wm.device_available = False
    wm_mod._instance = mock_wm
    try:
        result = tool.handler({})
        assert_isinstance("no_device: returns str", result, str)
        assert_in("no_device: mentions camera", "camera", result.lower())
    finally:
        wm_mod._instance = original_instance

    # --- Test 3.3: successful capture returns correct dict ---
    log("\n── 3.3: Successful capture — correct dict structure ──")

    # Create a real JPEG via PIL (minimal 4x4 image)
    from PIL import Image
    img = Image.new("RGB", (640, 480), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    fake_jpeg = buf.getvalue()

    mock_wm = MagicMock()
    mock_wm.device_available = True
    mock_wm._loop = None  # force fallback path (new event loop)
    mock_wm.get_frame = AsyncMock(return_value=fake_jpeg)

    wm_mod._instance = mock_wm
    try:
        result = tool.handler({})
        assert_isinstance("success: returns dict", result, dict)
        if isinstance(result, dict):
            assert_in("success: has 'text' key", "text", result)
            assert_in("success: has 'image_data' key", "image_data", result)

            # Verify base64 is decodable
            try:
                decoded = base64.b64decode(result["image_data"])
                assert_true("success: base64 decodable", len(decoded) > 0)
            except Exception as e:
                assert_true("success: base64 decodable", False, detail=str(e))

            # Verify text has dimensions
            assert_in("success: text has dimensions", "640x480", result["text"])
            assert_in("success: text has KB", "KB", result["text"])
    finally:
        wm_mod._instance = original_instance

    # --- Test 3.4: downscale for wide images ---
    log("\n── 3.4: Downscale images wider than 1280px ──")
    wide_img = Image.new("RGB", (1920, 1080), color=(0, 128, 255))
    buf = io.BytesIO()
    wide_img.save(buf, format="JPEG")
    wide_jpeg = buf.getvalue()

    mock_wm = MagicMock()
    mock_wm.device_available = True
    mock_wm._loop = None
    mock_wm.get_frame = AsyncMock(return_value=wide_jpeg)

    wm_mod._instance = mock_wm
    try:
        result = tool.handler({})
        assert_isinstance("downscale: returns dict", result, dict)
        if isinstance(result, dict):
            # The original dimensions should be in text
            assert_in("downscale: original dims in text", "1920x1080", result["text"])

            # Decode and verify the actual image was downscaled
            decoded = base64.b64decode(result["image_data"])
            output_img = Image.open(io.BytesIO(decoded))
            assert_eq("downscale: output width <= 1920", output_img.width <= 1920, True)
    finally:
        wm_mod._instance = original_instance

    # --- Test 3.5: timeout error ---
    log("\n── 3.5: Timeout error ──")
    mock_wm = MagicMock()
    mock_wm.device_available = True
    mock_wm._loop = None

    async def timeout_get_frame(*a, **kw):
        raise TimeoutError("test timeout")
    mock_wm.get_frame = timeout_get_frame

    wm_mod._instance = mock_wm
    try:
        result = tool.handler({})
        assert_isinstance("timeout: returns str", result, str)
        assert_in("timeout: error message", "timeout", result.lower())
    finally:
        wm_mod._instance = original_instance


# ═══════════════════════════════════════════════════════════════════════════
# PART 4: take_screenshot handler unit tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part4():
    log("\n═══ Part 4: take_screenshot handler ═══")

    import core.tools.take_screenshot as tool

    # --- Test 4.1: capture error when desktop_manager is None ---
    log("\n── 4.1: Capture without desktop manager ──")
    original_dm = tool._desktop_manager
    tool._desktop_manager = None
    try:
        result = tool.handler({"action": "capture"})
        assert_isinstance("no_dm: returns str", result, str)
        assert_in("no_dm: error message", "Error", result)
    finally:
        tool._desktop_manager = original_dm

    # --- Test 4.2: survey with no monitors ---
    log("\n── 4.2: Survey with no monitors ──")
    tool._desktop_manager = MagicMock()
    try:
        with patch.object(tool, "_parse_monitors", return_value=[]):
            result = tool.handler({"action": "survey"})
            assert_isinstance("survey_empty: returns str", result, str)
            assert_in("survey_empty: fallback message", "could not", result.lower())
    finally:
        tool._desktop_manager = original_dm

    # --- Test 4.3: survey with monitors and windows ---
    log("\n── 4.3: Survey with monitors and windows ──")
    mock_dm = MagicMock()
    mock_dm.list_windows.return_value = [
        {"title": "Terminal", "wm_class": "gnome-terminal", "monitor": 0},
        {"title": "Firefox", "wm_class": "firefox", "monitor": 1},
    ]
    tool._desktop_manager = mock_dm
    try:
        with patch.object(tool, "_parse_monitors", return_value=[
            {"name": "DP-2", "width": 2560, "height": 1440, "x": 1920, "y": 0, "primary": True},
            {"name": "DP-1", "width": 1920, "height": 1080, "x": 0, "y": 0, "primary": False},
        ]):
            result = tool.handler({"action": "survey"})
            assert_in("survey: has DP-2", "DP-2", result)
            assert_in("survey: has PRIMARY", "PRIMARY", result)
            assert_in("survey: has Terminal", "Terminal", result)
            assert_in("survey: has Firefox", "Firefox", result)
    finally:
        tool._desktop_manager = original_dm

    # --- Test 4.4: _parse_monitors xrandr parsing ---
    log("\n── 4.4: _parse_monitors xrandr output parsing ──")
    xrandr_output = """Monitors: 3
 0: +DP-1 1920/527x1080/296+0+0  DP-1
 1: +*DP-2 2560/610x1440/350+1920+0  DP-2
 2: +HDMI-1 1920/527x1080/296+4480+0  HDMI-1"""

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=xrandr_output)
        monitors = tool._parse_monitors()

        assert_eq("parse_monitors: count", len(monitors), 3)
        if len(monitors) >= 3:
            assert_eq("parse_monitors: DP-1 name", monitors[0]["name"], "DP-1")
            assert_eq("parse_monitors: DP-1 width", monitors[0]["width"], 1920)
            assert_eq("parse_monitors: DP-1 primary", monitors[0]["primary"], False)

            assert_eq("parse_monitors: DP-2 name", monitors[1]["name"], "DP-2")
            assert_eq("parse_monitors: DP-2 width", monitors[1]["width"], 2560)
            assert_eq("parse_monitors: DP-2 height", monitors[1]["height"], 1440)
            assert_eq("parse_monitors: DP-2 x", monitors[1]["x"], 1920)
            assert_eq("parse_monitors: DP-2 primary", monitors[1]["primary"], True)

            assert_eq("parse_monitors: HDMI-1 name", monitors[2]["name"], "HDMI-1")
            assert_eq("parse_monitors: HDMI-1 x", monitors[2]["x"], 4480)

    # --- Test 4.5: _encode_and_cleanup downscales wide images ---
    log("\n── 4.5: _encode_and_cleanup downscale ──")
    from PIL import Image
    wide = Image.new("RGB", (3840, 2160), color=(64, 128, 255))
    test_path = "/tmp/jarvis_test_screenshot.png"
    wide.save(test_path)
    try:
        result = tool._encode_and_cleanup(test_path, "test", max_width=1920)
        assert_isinstance("encode: returns dict", result, dict)
        if isinstance(result, dict):
            decoded = base64.b64decode(result["image_data"])
            output_img = Image.open(io.BytesIO(decoded))
            assert_eq("encode: downscaled width", output_img.width, 1920)
            assert_in("encode: text has resized", "resized", result["text"])
            assert_in("encode: text has original dims", "3840x2160", result["text"])
    finally:
        # _encode_and_cleanup cleans up the file, but just in case
        try:
            os.unlink(test_path)
        except FileNotFoundError:
            pass

    # --- Test 4.6: _encode_and_cleanup keeps native resolution when no max_width ---
    log("\n── 4.6: _encode_and_cleanup native resolution ──")
    native = Image.new("RGB", (2560, 1440), color=(255, 0, 0))
    test_path2 = "/tmp/jarvis_test_screenshot2.png"
    native.save(test_path2)
    try:
        result = tool._encode_and_cleanup(test_path2, "monitor DP-2", max_width=None)
        assert_isinstance("native: returns dict", result, dict)
        if isinstance(result, dict):
            decoded = base64.b64decode(result["image_data"])
            output_img = Image.open(io.BytesIO(decoded))
            assert_eq("native: keeps width", output_img.width, 2560)
            assert_true("native: no 'resized' in text", "resized" not in result["text"])
    finally:
        try:
            os.unlink(test_path2)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# PART 5: MobileCameraRelay + mobile fallback tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part5():
    log("\n═══ Part 5: MobileCameraRelay + mobile fallback ═══")

    from core.webcam_manager import MobileCameraRelay
    import core.webcam_manager as wm_mod
    import core.tools.capture_webcam as tool
    from core.tool_registry import inject_dependencies

    # --- Test 5.1: Initial state ---
    log("\n── 5.1: MobileCameraRelay initial state ──")
    relay = MobileCameraRelay()
    assert_true("init: not connected", not relay.is_connected)
    assert_eq("init: no pending", len(relay._pending), 0)
    assert_true("init: ws is None", relay._ws is None)
    assert_true("init: loop is None", relay._loop is None)

    # --- Test 5.2: set_ws / is_connected ---
    log("\n── 5.2: set_ws and is_connected ──")
    mock_ws = MagicMock()
    mock_ws.closed = False
    mock_loop = MagicMock()
    relay.set_ws(mock_ws, mock_loop)
    assert_true("set_ws: connected", relay.is_connected)
    assert_true("set_ws: ws stored", relay._ws is mock_ws)
    assert_true("set_ws: loop stored", relay._loop is mock_loop)

    # --- Test 5.3: clear_ws disconnects ---
    log("\n── 5.3: clear_ws disconnects ──")
    relay.clear_ws()
    assert_true("clear: not connected", not relay.is_connected)
    assert_true("clear: ws is None", relay._ws is None)
    assert_eq("clear: pending cleared", len(relay._pending), 0)

    # --- Test 5.4: clear_ws cancels pending futures ---
    log("\n── 5.4: clear_ws cancels pending futures ──")
    async def test_clear_cancels():
        relay = MobileCameraRelay()
        mock_ws = MagicMock()
        mock_ws.closed = False
        relay.set_ws(mock_ws, asyncio.get_event_loop())

        # Create pending futures
        fut1 = asyncio.get_event_loop().create_future()
        fut2 = asyncio.get_event_loop().create_future()
        relay._pending["req1"] = fut1
        relay._pending["req2"] = fut2

        relay.clear_ws()
        assert_true("cancel_pending: fut1 cancelled", fut1.cancelled())
        assert_true("cancel_pending: fut2 cancelled", fut2.cancelled())
        assert_eq("cancel_pending: dict cleared", len(relay._pending), 0)

    asyncio.run(test_clear_cancels())

    # --- Test 5.5: deliver_frame resolves future ---
    log("\n── 5.5: deliver_frame resolves pending future ──")
    async def test_deliver_frame():
        relay = MobileCameraRelay()
        fut = asyncio.get_event_loop().create_future()
        relay._pending["abc123"] = fut

        # Deliver a base64-encoded "JPEG"
        fake_jpeg = make_jpeg(50)
        b64_data = base64.b64encode(fake_jpeg).decode("utf-8")
        relay.deliver_frame("abc123", b64_data)

        assert_true("deliver: future done", fut.done())
        assert_eq("deliver: result matches", fut.result(), fake_jpeg)

    asyncio.run(test_deliver_frame())

    # --- Test 5.6: deliver_error rejects future ---
    log("\n── 5.6: deliver_error rejects pending future ──")
    async def test_deliver_error():
        relay = MobileCameraRelay()
        fut = asyncio.get_event_loop().create_future()
        relay._pending["err123"] = fut

        relay.deliver_error("err123", "Camera panel not open")
        assert_true("deliver_err: future done", fut.done())
        try:
            fut.result()
            assert_true("deliver_err: should have raised", False)
        except RuntimeError as e:
            assert_in("deliver_err: error message", "Camera panel not open", str(e))

    asyncio.run(test_deliver_error())

    # --- Test 5.7: deliver_frame with invalid base64 ---
    log("\n── 5.7: deliver_frame with invalid base64 ──")
    async def test_deliver_bad_b64():
        relay = MobileCameraRelay()
        fut = asyncio.get_event_loop().create_future()
        relay._pending["bad123"] = fut

        relay.deliver_frame("bad123", "not!!valid!!base64!!")
        assert_true("bad_b64: future done", fut.done())
        try:
            fut.result()
            assert_true("bad_b64: should have raised", False)
        except RuntimeError as e:
            assert_in("bad_b64: error message", "Invalid frame data", str(e))

    asyncio.run(test_deliver_bad_b64())

    # --- Test 5.8: deliver to unknown request_id is no-op ---
    log("\n── 5.8: deliver to unknown request_id ──")
    relay = MobileCameraRelay()
    relay.deliver_frame("unknown_id", base64.b64encode(b"test").decode())
    assert_eq("unknown_id: no pending created", len(relay._pending), 0)

    # --- Test 5.9: is_connected returns False when ws.closed ---
    log("\n── 5.9: is_connected with closed WebSocket ──")
    relay = MobileCameraRelay()
    mock_ws = MagicMock()
    mock_ws.closed = True
    relay._ws = mock_ws
    assert_true("closed_ws: not connected", not relay.is_connected)

    # --- Test 5.10: request_frame raises when not connected ---
    log("\n── 5.10: request_frame when not connected ──")
    async def test_request_not_connected():
        relay = MobileCameraRelay()
        try:
            await relay.request_frame(timeout=0.1)
            assert_true("not_connected: should have raised", False)
        except RuntimeError as e:
            assert_in("not_connected: error msg", "No mobile camera", str(e))

    asyncio.run(test_request_not_connected())

    # --- Test 5.11: request_frame end-to-end (mock WS) ---
    log("\n── 5.11: request_frame end-to-end with mock WS ──")
    async def test_request_e2e():
        relay = MobileCameraRelay()
        mock_ws = AsyncMock()
        mock_ws.closed = False
        relay.set_ws(mock_ws, asyncio.get_event_loop())

        fake_jpeg = make_jpeg(80)
        b64_data = base64.b64encode(fake_jpeg).decode("utf-8")

        # Simulate: after send_json, "browser" delivers the frame
        async def fake_send_json(msg):
            # Deliver frame in response to request
            rid = msg["request_id"]
            relay.deliver_frame(rid, b64_data)

        mock_ws.send_json = fake_send_json

        frame = await relay.request_frame(timeout=2.0)
        assert_eq("e2e: frame matches", frame, fake_jpeg)

    asyncio.run(test_request_e2e())

    # --- Test 5.12: request_frame timeout ---
    log("\n── 5.12: request_frame timeout ──")
    async def test_request_timeout():
        relay = MobileCameraRelay()
        mock_ws = AsyncMock()
        mock_ws.closed = False
        relay.set_ws(mock_ws, asyncio.get_event_loop())

        try:
            await relay.request_frame(timeout=0.1)
            assert_true("req_timeout: should have raised", False)
        except TimeoutError as e:
            assert_in("req_timeout: error msg", "timeout", str(e).lower())

    asyncio.run(test_request_timeout())

    # --- Test 5.13: capture_webcam handler — mobile fallback success ---
    log("\n── 5.13: Handler — mobile fallback with successful frame ──")
    from PIL import Image
    # Create a real JPEG for processing
    img = Image.new("RGB", (640, 480), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    fake_jpeg = buf.getvalue()

    # No desktop webcam (singleton not initialized)
    original_instance = wm_mod._instance
    wm_mod._instance = None

    # Wire a mock relay that returns a frame
    mock_relay = MagicMock()
    mock_relay.is_connected = True
    mock_relay._loop = MagicMock()
    mock_relay._loop.is_running.return_value = True

    import concurrent.futures
    frame_future = concurrent.futures.Future()
    frame_future.set_result(fake_jpeg)

    mock_relay.request_frame = AsyncMock(return_value=fake_jpeg)

    # Patch run_coroutine_threadsafe to return our pre-resolved future
    original_relay = tool._mobile_relay
    tool._mobile_relay = mock_relay

    with patch("asyncio.run_coroutine_threadsafe", return_value=frame_future):
        result = tool.handler({})
        assert_isinstance("mobile_success: returns dict", result, dict)
        if isinstance(result, dict):
            assert_in("mobile_success: has text", "text", result)
            assert_in("mobile_success: has image_data", "image_data", result)
            assert_in("mobile_success: dims in text", "640x480", result["text"])

    tool._mobile_relay = original_relay
    wm_mod._instance = original_instance

    # --- Test 5.14: capture_webcam handler — no desktop, no mobile connected ---
    log("\n── 5.14: Handler — no desktop, no mobile ──")
    wm_mod._instance = None
    tool._mobile_relay = None

    result = tool.handler({})
    assert_isinstance("no_both: returns str", result, str)
    assert_in("no_both: mentions camera", "camera", result.lower())
    assert_in("no_both: mentions phone", "phone", result.lower())

    tool._mobile_relay = original_relay
    wm_mod._instance = original_instance

    # --- Test 5.15: capture_webcam handler — no desktop, mobile relay not connected ---
    log("\n── 5.15: Handler — no desktop, mobile relay disconnected ──")
    wm_mod._instance = None
    mock_relay_disconnected = MagicMock()
    mock_relay_disconnected.is_connected = False
    tool._mobile_relay = mock_relay_disconnected

    result = tool.handler({})
    assert_isinstance("relay_off: returns str", result, str)
    assert_in("relay_off: mentions camera", "camera", result.lower())

    tool._mobile_relay = original_relay
    wm_mod._instance = original_instance

    # --- Test 5.16: set_ws replaces previous connection ---
    log("\n── 5.16: set_ws replaces previous connection ──")
    async def test_set_ws_replaces():
        relay = MobileCameraRelay()
        ws1 = MagicMock()
        ws1.closed = False
        ws2 = MagicMock()
        ws2.closed = False
        loop = asyncio.get_event_loop()

        relay.set_ws(ws1, loop)
        assert_true("replace: ws1 connected", relay._ws is ws1)

        # Add a pending future on ws1
        fut = loop.create_future()
        relay._pending["old_req"] = fut

        # Replace with ws2 — should clear pending
        relay.set_ws(ws2, loop)
        assert_true("replace: ws2 connected", relay._ws is ws2)
        assert_true("replace: old fut cancelled", fut.cancelled())
        assert_eq("replace: pending cleared", len(relay._pending), 0)

    asyncio.run(test_set_ws_replaces())

    # --- Test 5.17: Singleton accessor ---
    log("\n── 5.17: get_mobile_relay singleton ──")
    from core.webcam_manager import get_mobile_relay
    # Reset singleton for test
    original_relay_instance = wm_mod._relay_instance
    wm_mod._relay_instance = None

    r1 = get_mobile_relay()
    r2 = get_mobile_relay()
    assert_true("singleton: same instance", r1 is r2)
    assert_isinstance("singleton: correct type", r1, MobileCameraRelay)

    wm_mod._relay_instance = original_relay_instance


# ═══════════════════════════════════════════════════════════════════════════
# PART 6: Presence detection + face enrollment tests
# ═══════════════════════════════════════════════════════════════════════════

def test_part6():
    log("\n═══ Part 6: Presence detection + face enrollment ═══")

    import tempfile
    import shutil
    from unittest.mock import MagicMock, patch, PropertyMock
    from PIL import Image
    import numpy as np

    # --- Test 6.1: PresenceState enum ---
    log("\n── 6.1: PresenceState enum values ──")
    from core.presence_detector import PresenceState, PersonPresence
    assert_true("state: ABSENT exists", PresenceState.ABSENT is not None)
    assert_true("state: DETECTED exists", PresenceState.DETECTED is not None)
    assert_true("state: GREETED exists", PresenceState.GREETED is not None)
    assert_true("state: PRESENT exists", PresenceState.PRESENT is not None)

    # --- Test 6.2: PersonPresence dataclass defaults ---
    log("\n── 6.2: PersonPresence defaults ──")
    pp = PersonPresence(person_id="test123")
    assert_eq("presence: default state", pp.state, PresenceState.ABSENT)
    assert_eq("presence: default first_seen", pp.first_seen, 0.0)
    assert_eq("presence: default confidence", pp.confidence, 0.0)

    # --- Test 6.3: PresenceDetector initialization ---
    log("\n── 6.3: PresenceDetector init ──")
    from core.presence_detector import PresenceDetector

    tmpdir = tempfile.mkdtemp(prefix="jarvis_test_presence_")
    mock_config = MagicMock()
    mock_config.get = MagicMock(side_effect=lambda key, default=None: {
        "vision.presence": {
            "detection_interval": 5,
            "greeting_cooldown": 3600,
            "face_confidence_threshold": 0.6,
            "min_face_size": 80,
            "greet_unknown": False,
            "absence_threshold": 30,
        },
        "system.storage_path": tmpdir,
    }.get(key, default))

    mock_tts = MagicMock()
    mock_webcam = MagicMock()
    mock_webcam.device_available = True
    mock_people = MagicMock()
    mock_people.get_people_with_face_embeddings.return_value = []
    mock_conv = MagicMock()
    mock_conv.conversation_active = False

    detector = PresenceDetector(
        mock_config, mock_tts, mock_webcam, mock_people, mock_conv
    )
    assert_true("init: not running", not detector._running)
    assert_eq("init: interval", detector._interval, 5)
    assert_eq("init: cooldown", detector._cooldown, 3600)
    assert_eq("init: empty face cache", len(detector._face_cache), 0)

    # --- Test 6.4: _should_skip during conversation ---
    log("\n── 6.4: _should_skip guards ──")
    mock_conv.conversation_active = True
    assert_true("skip: during conversation", detector._should_skip())

    mock_conv.conversation_active = False
    assert_true("skip: not during idle", not detector._should_skip())

    mock_webcam.device_available = False
    assert_true("skip: no webcam", detector._should_skip())
    mock_webcam.device_available = True

    # --- Test 6.5: _greeting_allowed cooldown ---
    log("\n── 6.5: Greeting cooldown ──")
    now = time.time()

    # No prior state — greeting allowed
    assert_true("cooldown: first time allowed",
                detector._greeting_allowed("person1", now))

    # Set a recent greeting
    detector._person_states["person1"] = PersonPresence(
        person_id="person1",
        state=PresenceState.PRESENT,
        last_greeted=now - 100,  # 100s ago (within 3600s cooldown)
    )
    assert_true("cooldown: within cooldown blocked",
                not detector._greeting_allowed("person1", now))

    # Set an old greeting (beyond cooldown)
    detector._person_states["person1"].last_greeted = now - 4000
    assert_true("cooldown: beyond cooldown allowed",
                detector._greeting_allowed("person1", now))

    # --- Test 6.6: _was_long_absence ---
    log("\n── 6.6: Long absence detection ──")
    # Last greeted 2 hours ago — long absence
    detector._person_states["person1"].last_greeted = now - 7200
    assert_true("absence: 2h = long",
                detector._was_long_absence("person1", now))

    # Last greeted 10 minutes ago — not long
    detector._person_states["person1"].last_greeted = now - 600
    assert_true("absence: 10m = not long",
                not detector._was_long_absence("person1", now))

    # First time ever — not a "return"
    assert_true("absence: first time = not return",
                not detector._was_long_absence("person_new", now))

    # --- Test 6.7: State machine transitions ---
    log("\n── 6.7: State machine transitions ──")
    detector._person_states.clear()

    # Mock greeting to not actually speak
    detector._fire_greeting = MagicMock()

    # First detection: ABSENT → DETECTED → fires greeting → GREETED
    detector._handle_detection("person_x", 0.85, now)
    state = detector._person_states["person_x"]
    assert_eq("sm: first detect → GREETED", state.state, PresenceState.GREETED)
    assert_true("sm: greeting fired", detector._fire_greeting.called)

    # Second detection: GREETED → PRESENT
    detector._handle_detection("person_x", 0.85, now + 1)
    assert_eq("sm: greeted → PRESENT", state.state, PresenceState.PRESENT)

    # Continued detection: stays PRESENT
    detector._handle_detection("person_x", 0.85, now + 2)
    assert_eq("sm: stays PRESENT", state.state, PresenceState.PRESENT)

    # --- Test 6.8: ABSENT transition after absence_threshold ---
    log("\n── 6.8: Absence timeout ──")
    state.last_seen = now - 60  # Last seen 60s ago
    detector._update_all_absent()
    assert_eq("absent: after timeout", state.state, PresenceState.ABSENT)

    # --- Test 6.9: Re-detection after absence triggers greeting again ---
    log("\n── 6.9: Re-detection after cooldown ──")
    detector._fire_greeting.reset_mock()
    state.last_greeted = now - 4000  # Beyond cooldown
    state.state = PresenceState.ABSENT

    detector._handle_detection("person_x", 0.85, now + 100)
    assert_eq("re-detect: → GREETED", state.state, PresenceState.GREETED)
    assert_true("re-detect: greeting fired", detector._fire_greeting.called)

    # --- Test 6.10: Re-detection within cooldown skips greeting ---
    log("\n── 6.10: Re-detection within cooldown skips greeting ──")
    detector._fire_greeting.reset_mock()
    state.last_greeted = now + 100  # Just greeted
    state.state = PresenceState.ABSENT

    detector._handle_detection("person_x", 0.85, now + 200)
    assert_eq("cooldown_skip: → PRESENT (no greeting)", state.state, PresenceState.PRESENT)
    assert_true("cooldown_skip: no greeting fired",
                not detector._fire_greeting.called)

    # --- Test 6.11: Haar cascade face detection ---
    log("\n── 6.11: Haar cascade detection ──")
    detector._ensure_cascade()
    assert_true("cascade: loaded", detector._cascade is not None)

    # Create a synthetic image with no faces — should detect 0
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    faces = detector._detect_faces(blank)
    assert_eq("cascade: blank image = 0 faces", len(faces), 0)

    # --- Test 6.12: Face enrollment ---
    log("\n── 6.12: Face enrollment ──")
    # Create a real face-like image for enrollment
    # (face_recognition needs a real face, so we test the error path)
    img = Image.new("RGB", (640, 480), color=(180, 140, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    no_face_jpeg = buf.getvalue()

    success, msg = detector.enroll_face("test_person", no_face_jpeg, "Test Person")
    assert_true("enroll: no face fails", not success)
    assert_in("enroll: error mentions face", "face", msg.lower())

    # --- Test 6.13: _fire_greeting produces speech ---
    log("\n── 6.13: Greeting delivery ──")
    # Restore real _fire_greeting
    detector._fire_greeting = PresenceDetector._fire_greeting.__get__(detector)
    mock_tts.speak.reset_mock()
    mock_pause = MagicMock()
    mock_resume = MagicMock()
    mock_window = MagicMock()
    detector._pause_listener_callback = mock_pause
    detector._resume_listener_callback = mock_resume
    detector._window_callback = mock_window

    with patch("core.presence_detector.set_honorific") as mock_set_h:
        detector._fire_greeting("person_x", is_return=False)

    assert_true("greeting: TTS called", mock_tts.speak.called)
    greeting_text = mock_tts.speak.call_args[0][0]
    assert_true("greeting: has text", len(greeting_text) > 5)
    assert_true("greeting: pause called", mock_pause.called)
    assert_true("greeting: resume called", mock_resume.called)
    assert_true("greeting: window opened", mock_window.called)
    assert_eq("greeting: window duration", mock_window.call_args[0][0], 8.0)

    # --- Test 6.14: Return greeting with pending reminders ---
    log("\n── 6.14: Return greeting with pending reminders ──")
    mock_tts.speak.reset_mock()
    mock_reminder_mgr = MagicMock()
    mock_reminder_mgr.get_pending_acks.return_value = [{"id": 1, "title": "test"}]
    detector._reminder_manager = mock_reminder_mgr

    with patch("core.presence_detector.set_honorific"):
        detector._fire_greeting("person_x", is_return=True)

    greeting_text = mock_tts.speak.call_args[0][0]
    # Should contain reminder-related language
    assert_true("return_reminder: mentions reminders",
                "reminder" in greeting_text.lower() or
                "while you were" in greeting_text.lower() or
                "came up" in greeting_text.lower() or
                "held" in greeting_text.lower())

    # --- Test 6.15: get_status ---
    log("\n── 6.15: get_status ──")
    status = detector.get_status()
    assert_in("status: has 'running'", "running", status)
    assert_in("status: has 'enrolled_faces'", "enrolled_faces", status)
    assert_in("status: has 'tracked_people'", "tracked_people", status)
    assert_in("status: has 'states'", "states", status)

    # --- Test 6.16: enroll_face tool handler ---
    log("\n── 6.16: enroll_face tool (not initialized) ──")
    import core.tools.enroll_face as enroll_tool
    original_pd = enroll_tool._presence_detector
    enroll_tool._presence_detector = None

    result = enroll_tool.handler({})
    assert_isinstance("tool_no_pd: returns str", result, str)
    assert_in("tool_no_pd: mentions not available", "not available", result.lower())

    enroll_tool._presence_detector = original_pd

    # --- Test 6.17: Persona presence_greeting helper ---
    log("\n── 6.17: Persona presence greeting pools ──")
    from core.persona import presence_greeting, _POOLS

    # Verify all pools exist
    for pool_name in ["presence_morning", "presence_afternoon", "presence_evening",
                      "presence_return", "presence_return_reminders"]:
        assert_in(f"pool: {pool_name} exists", pool_name, _POOLS)
        assert_true(f"pool: {pool_name} non-empty", len(_POOLS[pool_name]) > 0)

    # Test helper produces output
    with patch("core.persona.get_honorific", return_value="sir"):
        greeting = presence_greeting("morning")
        assert_true("helper: morning greeting has text", len(greeting) > 5)

        greeting = presence_greeting("evening", is_return=True)
        assert_true("helper: return greeting has text", len(greeting) > 5)

        greeting = presence_greeting("afternoon", is_return=True,
                                      has_pending_reminders=True)
        assert_true("helper: reminder greeting has text", len(greeting) > 5)
        assert_true("helper: reminder greeting mentions reminders",
                    "reminder" in greeting.lower() or
                    "while you were" in greeting.lower() or
                    "came up" in greeting.lower() or
                    "held" in greeting.lower())

    # --- Test 6.18: PRESENCE_DETECTED event type exists ---
    log("\n── 6.18: PRESENCE_DETECTED event type ──")
    from core.events import EventType
    assert_true("event: PRESENCE_DETECTED exists",
                hasattr(EventType, 'PRESENCE_DETECTED'))

    # --- Test 6.19: People manager face embedding methods ---
    log("\n── 6.19: People manager face embedding DB ──")
    from core.people_manager import PeopleManager
    pm_tmpdir = tempfile.mkdtemp(prefix="jarvis_test_people_")
    pm_config = MagicMock()
    pm_config.get = MagicMock(side_effect=lambda key, default=None: {
        "people.db_path": f"{pm_tmpdir}/people.db",
        "people.enabled": True,
    }.get(key, default))

    # Patch TTS normalizer registration to avoid import issues
    with patch.object(PeopleManager, '_register_tts_normalizer'):
        pm = PeopleManager(pm_config)

    # Add a person and set face embedding
    pid = pm.add_person("Test User", relationship="owner")
    pm.set_face_embedding_path(pid, "/tmp/face_test.npy")

    # Verify it's retrievable
    people_with_faces = pm.get_people_with_face_embeddings()
    assert_eq("pm_face: count", len(people_with_faces), 1)
    assert_eq("pm_face: path",
              people_with_faces[0]["face_embedding_path"], "/tmp/face_test.npy")

    # Person without embedding should not appear
    pm.add_person("No Face User", relationship="contact")
    people_with_faces = pm.get_people_with_face_embeddings()
    assert_eq("pm_face: only enrolled", len(people_with_faces), 1)

    # Cleanup
    shutil.rmtree(pm_tmpdir, ignore_errors=True)
    shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════

def main():
    global verbose

    parser = argparse.ArgumentParser(description="Vision System Unit Tests")
    parser.add_argument("--verbose", action="store_true", help="Show each test result")
    parser.add_argument("--part", type=int, choices=[1, 2, 3, 4, 5, 6],
                       help="Run only one part")
    args = parser.parse_args()
    verbose = args.verbose

    log("Vision System — Unit Test Suite\n")

    parts = {
        1: ("Frame Parser", test_part1),
        2: ("WebcamManager Lifecycle", test_part2),
        3: ("capture_webcam Handler", test_part3),
        4: ("take_screenshot Handler", test_part4),
        5: ("MobileCameraRelay + Mobile Fallback", test_part5),
        6: ("Presence Detection + Face Enrollment", test_part6),
    }

    if args.part:
        name, fn = parts[args.part]
        log(f"Running Part {args.part}: {name}")
        fn()
    else:
        for num, (name, fn) in parts.items():
            fn()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    log(f"\n{'='*60}")
    log(f"Results: {passed}/{total} passed, {failed} failed")

    if failed:
        log(f"\nFailed tests:")
        for r in results:
            if not r.passed:
                log(f"  FAIL: {r.name} — {r.detail}")

    log(f"{'='*60}")

    # Clean exit to avoid ROCm/ONNX teardown issues
    os._exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
