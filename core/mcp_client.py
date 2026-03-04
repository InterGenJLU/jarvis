"""MCP Client Bridge — consumes external MCP servers as native JARVIS tools.

Manages MCP server subprocess connections, discovers their tools, and registers
sync handler wrappers into tool_registry so the voice/console pipeline treats
them identically to core/tools/*.py handlers.

Architecture:
    User query → semantic pruner scores virtual skill → LLM gets MCP tool schemas
    → LLM calls tool → sync handler → MCPBridge → async event loop → MCP server
    → result flows back through LLM → TTS speaks it
"""

import asyncio
from contextlib import AsyncExitStack
import logging
import os
import threading

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

from core.tool_registry import register_external_tool

logger = logging.getLogger("jarvis.mcp_client")


class MCPBridge:
    """Bridges external MCP servers into JARVIS's sync tool pipeline.

    Runs a background asyncio event loop in a daemon thread.  Each configured
    MCP server is started as a subprocess, its tools discovered and registered
    into tool_registry with sync handler closures that bridge to the async
    MCP SDK calls.
    """

    def __init__(self, skill_manager=None):
        self._loop = None           # asyncio event loop (background thread)
        self._thread = None         # daemon thread running the loop
        self._sessions = {}         # server_name → ClientSession
        self._exit_stack = None     # AsyncExitStack for lifecycle
        self._skill_manager = skill_manager
        self._server_tools = {}     # server_name → [tool_name, ...]

    def start(self, mcp_config: dict):
        """Start background event loop, connect to all configured servers.

        Args:
            mcp_config: Dict from config.yaml 'mcp_servers' section.
                        Keys are server names, values are dicts with
                        'command', 'args', 'env', 'intent_examples', etc.
        """
        if not mcp_config:
            return

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-bridge"
        )
        self._thread.start()

        # Create the exit stack on the event loop thread
        future = asyncio.run_coroutine_threadsafe(
            self._init_exit_stack(), self._loop
        )
        future.result(timeout=5)

        for name, cfg in mcp_config.items():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._connect_server(name, cfg), self._loop
                )
                future.result(timeout=30)
                tools = self._server_tools.get(name, [])
                logger.info(f"MCP server '{name}' connected ({len(tools)} tools: {tools})")
            except Exception as e:
                logger.warning(f"MCP server '{name}' failed to connect: {e}")

    async def _init_exit_stack(self):
        """Initialize the AsyncExitStack on the event loop thread."""
        self._exit_stack = AsyncExitStack()

    async def _connect_server(self, name: str, cfg: dict):
        """Connect to one MCP server, discover tools, register them."""
        # Resolve env vars (support ${VAR} syntax from config.yaml)
        env = dict(os.environ)
        for key, val in cfg.get("env", {}).items():
            if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                env_var = val[2:-1]
                env[key] = os.environ.get(env_var, "")
            else:
                env[key] = str(val)

        # Start server subprocess via stdio
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=env,
        )
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            stdio_client(params)
        )
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions[name] = session

        # Discover tools
        response = await session.list_tools()
        tool_names = []
        shared_rule = cfg.get("system_prompt_rule", "")

        for tool in response.tools:
            # Convert MCP schema → OpenAI function-calling format
            schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {
                        "type": "object", "properties": {}
                    },
                },
            }

            handler = self._make_sync_handler(name, tool.name)
            rule = shared_rule or f"Use {tool.name} for {tool.description}"

            register_external_tool(
                name=tool.name,
                schema=schema,
                handler=handler,
                system_prompt_rule=rule,
                skill_name=f"mcp_{name}",
            )
            tool_names.append(tool.name)

        self._server_tools[name] = tool_names

        # Register virtual skill for semantic pruning
        if self._skill_manager and cfg.get("intent_examples"):
            self._skill_manager.register_virtual_skill(
                name=f"mcp_{name}",
                intent_examples=cfg["intent_examples"],
            )

    def _make_sync_handler(self, server_name: str, tool_name: str):
        """Create sync handler closure bridging to async MCP call."""
        def handler(args: dict) -> str:
            future = asyncio.run_coroutine_threadsafe(
                self._call_tool(server_name, tool_name, args),
                self._loop,
            )
            return future.result(timeout=30)
        return handler

    async def _call_tool(self, server_name: str, tool_name: str, args: dict) -> str:
        """Call an MCP tool and return the text result."""
        session = self._sessions.get(server_name)
        if not session:
            return f"Error: MCP server '{server_name}' not connected"

        result = await session.call_tool(tool_name, args)

        if result.isError:
            error_text = (
                result.content[0].text
                if result.content and hasattr(result.content[0], "text")
                else "unknown error"
            )
            return f"Error from {tool_name}: {error_text}"

        # Concatenate all text content blocks
        texts = [
            block.text for block in result.content
            if hasattr(block, "text")
        ]
        return "\n".join(texts) if texts else "No output"

    def stop(self):
        """Clean shutdown: close all sessions, stop event loop."""
        if not self._loop or not self._loop.is_running():
            return

        if self._exit_stack:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._exit_stack.aclose(), self._loop
                )
                future.result(timeout=10)
            except Exception as e:
                # anyio cancel scope cross-task warning is expected and harmless
                logger.debug(f"MCP bridge shutdown (non-fatal): {e}")

        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

        self._sessions.clear()
        self._server_tools.clear()
        logger.info("MCP bridge stopped")
