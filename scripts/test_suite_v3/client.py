"""
WebSocket client for JARVIS Test Suite V3.

Handles all communication with the JARVIS web service.
Captures exhaustive per-turn data in the TurnLog dataclass.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
import yaml


@dataclass
class TurnLog:
    """Exhaustive per-turn data capture."""
    # Request
    turn_num: int = 0
    conversation_id: str = ""
    user_input: str = ""
    user_id: str = "primary_user"
    timestamp_sent: str = ""

    # Response
    response_text: str = ""
    response_tokens: list[str] = field(default_factory=list)
    timestamp_received: str = ""

    # Routing metadata (from stats message)
    routing_layer: str = ""
    skill_name: str = ""
    handler: str = ""
    confidence: float = 0.0
    llm_model: str = ""
    llm_tokens: int = 0
    input_tokens: int = 0
    synthesis_category: str = ""
    synthesis_temperature: float = 0.0
    total_ms: int = 0
    llm_calls: int = 0              # Number of LLM calls in pipeline
    llm_provider: str = ""          # "qwen-small" or "qwen" (synthesis model)
    llm_routing_model: str = ""     # Model used for tool routing (if different)
    routing_ttft_ms: float = 0      # TTFT for routing/tool-selection call
    synthesis_ttft_ms: float = 0    # TTFT for synthesis call
    raw_stats: dict = field(default_factory=dict)

    # Tool calls (from info messages)
    tools_called: list[str] = field(default_factory=list)
    tool_outputs: dict = field(default_factory=dict)  # tool_name -> raw output
    info_messages: list[str] = field(default_factory=list)

    # Derived
    word_count: int = 0
    is_empty: bool = True
    has_error: bool = False
    error_text: str = ""

    def to_dict(self) -> dict:
        return {
            "turn_num": self.turn_num,
            "conversation_id": self.conversation_id,
            "user_input": self.user_input,
            "user_id": self.user_id,
            "timestamp_sent": self.timestamp_sent,
            "response_text": self.response_text,
            "timestamp_received": self.timestamp_received,
            "routing_layer": self.routing_layer,
            "skill_name": self.skill_name,
            "handler": self.handler,
            "confidence": self.confidence,
            "llm_model": self.llm_model,
            "llm_tokens": self.llm_tokens,
            "input_tokens": self.input_tokens,
            "synthesis_category": self.synthesis_category,
            "synthesis_temperature": self.synthesis_temperature,
            "total_ms": self.total_ms,
            "llm_calls": self.llm_calls,
            "llm_provider": self.llm_provider,
            "llm_routing_model": self.llm_routing_model,
            "routing_ttft_ms": self.routing_ttft_ms,
            "synthesis_ttft_ms": self.synthesis_ttft_ms,
            "raw_stats": self.raw_stats,
            "tools_called": self.tools_called,
            "tool_outputs": self.tool_outputs,
            "info_messages": self.info_messages,
            "word_count": self.word_count,
            "is_empty": self.is_empty,
            "has_error": self.has_error,
            "error_text": self.error_text,
        }


def load_config() -> dict:
    """Load auth token and connection details from config.yaml."""
    config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
    config_path = os.path.abspath(config_path)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    web = config.get('web', {})
    tls = web.get('tls', {})

    token = web.get('auth_token', '')
    tls_enabled = tls.get('enabled', False)
    port = tls.get('port', 8443) if tls_enabled else web.get('port', 8088)
    scheme = 'wss' if tls_enabled else 'ws'

    return {
        'url': f'{scheme}://localhost:{port}/ws',
        'token': token,
        'tls_enabled': tls_enabled,
    }


class JarvisClient:
    """WebSocket client for JARVIS web endpoint."""

    def __init__(self, url: str, token: str, tls: bool = False):
        self.url = f"{url}?token={token}"
        self.tls = tls
        self.ws = None
        self.session = None

    async def connect(self):
        """Connect to JARVIS WebSocket endpoint."""
        ssl_ctx = None
        if self.tls:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(
            self.url, ssl=ssl_ctx, heartbeat=30
        )
        await self._consume_handshake()

    async def _consume_handshake(self):
        """Consume connection handshake: session_list, history, system_stats."""
        seen = set()
        expected = {'session_list', 'history', 'system_stats'}
        deadline = time.time() + 10

        while seen != expected and time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=5)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    seen.add(data.get('type', ''))
            except asyncio.TimeoutError:
                break

    async def set_user(self, user_id: str):
        """Switch active user (sends set_user WS message, consumes handshake)."""
        await self.ws.send_json({"type": "set_user", "user_id": user_id})
        await self._consume_handshake()

    async def send_turn(self, content: str, conversation_id: str = "",
                        turn_num: int = 0, user_id: str = "primary_user") -> TurnLog:
        """Send user message, collect full response + metadata."""
        timestamp_sent = datetime.now(timezone.utc).isoformat()
        await self.ws.send_json({"type": "message", "content": content})

        tokens = []
        response_text = ""
        stats_data = {}
        info_messages = []
        tool_outputs = {}
        got_terminal = False
        has_error = False
        error_text = ""

        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=120)
            except asyncio.TimeoutError:
                if not response_text and tokens:
                    response_text = ''.join(tokens)
                has_error = True
                error_text = "Timeout waiting for response"
                break

            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    has_error = True
                    error_text = f"WebSocket {msg.type.name}"
                    break
                continue

            data = json.loads(msg.data)
            mtype = data.get('type', '')

            if mtype == 'stream_token':
                tokens.append(data.get('token', ''))
            elif mtype == 'stream_end':
                response_text = data.get('full_response', ''.join(tokens))
                got_terminal = True
            elif mtype == 'response':
                response_text = data.get('content', '')
                got_terminal = True
            elif mtype == 'stats':
                stats_data = data.get('data', {})
            elif mtype == 'tool_output':
                tool_outputs[data.get('tool', '')] = data.get('content', '')
            elif mtype == 'info':
                info_messages.append(data.get('content', ''))
            elif mtype == 'system_stats':
                if got_terminal or stats_data:
                    break
            elif mtype == 'error':
                response_text = data.get('content', '')
                has_error = True
                error_text = response_text
                break
            # Ignore: announcement, doc_status, stream_start, health_report, voice_status

        timestamp_received = datetime.now(timezone.utc).isoformat()
        word_count = len(response_text.split()) if response_text else 0

        # Extract tool names from info messages
        tools_called = []
        for info in info_messages:
            if info.startswith("Searching:"):
                tools_called.append("web_search")
            elif info.startswith("Running:"):
                tools_called.append(info.replace("Running:", "").strip())

        return TurnLog(
            turn_num=turn_num,
            conversation_id=conversation_id,
            user_input=content,
            user_id=user_id,
            timestamp_sent=timestamp_sent,
            response_text=response_text,
            response_tokens=tokens,
            timestamp_received=timestamp_received,
            routing_layer=stats_data.get('layer', ''),
            skill_name=stats_data.get('skill_name', ''),
            handler=stats_data.get('handler', ''),
            confidence=stats_data.get('confidence', 0.0),
            llm_model=stats_data.get('llm_model', ''),
            llm_tokens=stats_data.get('llm_tokens', 0),
            input_tokens=stats_data.get('input_tokens', 0),
            synthesis_category=stats_data.get('synthesis_category', ''),
            synthesis_temperature=stats_data.get('synthesis_temperature', 0.0),
            total_ms=stats_data.get('total_ms', 0),
            llm_calls=stats_data.get('llm_calls', 0),
            llm_provider=stats_data.get('llm_provider', ''),
            llm_routing_model=stats_data.get('llm_routing_model', ''),
            routing_ttft_ms=stats_data.get('routing_ttft_ms', 0),
            synthesis_ttft_ms=stats_data.get('synthesis_ttft_ms', 0),
            raw_stats=stats_data,
            tools_called=tools_called,
            tool_outputs=tool_outputs,
            info_messages=info_messages,
            word_count=word_count,
            is_empty=(word_count == 0),
            has_error=has_error,
            error_text=error_text,
        )

    async def close(self):
        """Close WebSocket and session."""
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session and not self.session.closed:
            await self.session.close()
