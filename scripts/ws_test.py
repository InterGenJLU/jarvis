#!/usr/bin/env python3
"""WebSocket test client for JARVIS web service.

Sends a message, collects the streamed response, and prints it.
Used by Claude Code sessions to verify routing, tool usage, and
response quality without needing a browser.

Usage:
    python3 scripts/ws_test.py "has Nvidia announced any new GPUs recently?"
    python3 scripts/ws_test.py "what time is it" --timeout 15
    python3 scripts/ws_test.py "tell me about black holes" --verbose
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import websockets


def load_auth_token() -> str:
    """Load the web auth token from config."""
    from core.config import load_config
    config = load_config()
    return config.get("web.auth_token", "")


async def send_and_collect(message: str, timeout: float = 30,
                           verbose: bool = False,
                           host: str = "127.0.0.1",
                           port: int = 8088) -> dict:
    """Send a message via WebSocket and collect the full response.

    Returns a dict with:
        response: str — the full JARVIS response text
        chunks: list — individual streamed chunks
        tool_calls: list — any tool calls made
        duration_ms: float — total time from send to final chunk
        metadata: dict — any metadata received (routing, model, etc.)
    """
    token = load_auth_token()
    uri = f"ws://{host}:{port}/ws?token={token}"

    result = {
        "message": message,
        "response": "",
        "chunks": [],
        "tool_calls": [],
        "duration_ms": 0,
        "metadata": {},
        "all_messages": [],
    }

    t0 = time.time()

    async with websockets.connect(uri) as ws:
        # Consume initial messages (history, session_list, system_stats)
        # with a short timeout — they arrive immediately on connect
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                data = json.loads(raw)
                msg_type = data.get("type", "")
                if verbose:
                    print(f"  [init] {msg_type}", file=sys.stderr)
                if msg_type in ("history", "session_list", "system_stats"):
                    continue
                else:
                    # Unexpected message during init — put it back mentally
                    break
        except asyncio.TimeoutError:
            pass  # No more init messages

        # Send the user message
        await ws.send(json.dumps({
            "type": "message",
            "content": message,
        }))

        if verbose:
            print(f"  [sent] {message}", file=sys.stderr)

        # Collect response chunks until stream_end or timeout
        response_parts = []
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                data = json.loads(raw)
                msg_type = data.get("type", "")
                result["all_messages"].append(data)

                if verbose:
                    if msg_type in ("chunk", "stream_token"):
                        content = data.get("content", data.get("token", ""))
                        # Don't spam individual tokens, just note them
                        pass
                    elif msg_type == "tool_call":
                        print(f"  [tool] {data.get('name', '?')}: "
                              f"{data.get('args', {})}", file=sys.stderr)
                    elif msg_type == "info":
                        info = data.get("content", "")
                        if info:
                            print(f"  [info] {info}", file=sys.stderr)
                    else:
                        print(f"  [{msg_type}]", file=sys.stderr)

                if msg_type in ("chunk", "stream_token"):
                    content = data.get("content", data.get("token", ""))
                    response_parts.append(content)
                    result["chunks"].append(content)
                elif msg_type == "response":
                    # Full response (non-streaming path)
                    result["response"] = data.get("content", "")
                    break
                elif msg_type == "stream_end":
                    result["metadata"] = data.get("metadata", {})
                    break
                elif msg_type == "tool_call":
                    result["tool_calls"].append({
                        "name": data.get("name"),
                        "args": data.get("args"),
                    })
                elif msg_type == "info":
                    # Tool status updates (e.g. "Searching...")
                    info = data.get("content", "")
                    if info and verbose:
                        pass  # already printed above
                    result.setdefault("info", []).append(info)
                elif msg_type == "error":
                    result["response"] = f"ERROR: {data.get('content', '')}"
                    break

        except asyncio.TimeoutError:
            if verbose:
                print(f"  [timeout] after {timeout}s", file=sys.stderr)

    result["duration_ms"] = (time.time() - t0) * 1000
    if not result["response"] and response_parts:
        result["response"] = "".join(response_parts)

    return result


def main():
    parser = argparse.ArgumentParser(description="JARVIS WebSocket test client")
    parser.add_argument("message", help="Message to send to JARVIS")
    parser.add_argument("--timeout", type=float, default=30,
                        help="Response timeout in seconds (default: 30)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show streaming details")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--json", action="store_true",
                        help="Output full result as JSON")
    args = parser.parse_args()

    result = asyncio.run(send_and_collect(
        args.message,
        timeout=args.timeout,
        verbose=args.verbose,
        host=args.host,
        port=args.port,
    ))

    if args.json:
        # Remove all_messages for cleaner output unless verbose
        if not args.verbose:
            result.pop("all_messages", None)
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'─' * 60}")
        print(f"Query:    {result['message']}")
        print(f"Response: {result['response'][:500]}")
        if result["tool_calls"]:
            tools = ", ".join(t["name"] for t in result["tool_calls"])
            print(f"Tools:    {tools}")
        print(f"Time:     {result['duration_ms']:.0f}ms")
        print(f"{'─' * 60}")


if __name__ == "__main__":
    main()
