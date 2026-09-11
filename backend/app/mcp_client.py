"""Lifecycle management for the office document MCP server.

The server runs as a stdio subprocess owned by the backend. One process serves the
whole application: the sandbox root is the parent ``workspaces`` directory, and each
tool call carries a path relative to it (``<workspace_id>/file.xlsx``). That keeps
process count constant regardless of how many workspaces exist, while still pinning
every filesystem access inside the data directory.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import PROJECT_ROOT, Settings, get_settings

logger = logging.getLogger(__name__)

SERVER_NAME = "office"


class McpStartupError(RuntimeError):
    """Raised when the MCP subprocess cannot be started or initialized."""


@dataclass(frozen=True)
class ToolProfile:
    """A tool as advertised by the MCP server, with its approval semantics."""

    name: str
    description: str
    read_only: bool
    destructive: bool
    schema: dict[str, Any]

    @property
    def requires_approval(self) -> bool:
        """Writes are gated. Anything not explicitly read-only is treated as a write,
        so a tool added upstream without annotations fails safe rather than silently
        mutating a user's file."""
        return not self.read_only


def _default_server_command(settings: Settings) -> tuple[str, list[str]]:
    """Prefer an explicit command, else run the installed package with this interpreter.

    Using ``sys.executable`` guarantees the subprocess shares the backend's virtual
    environment, which matters because the server imports openpyxl and python-docx.
    """
    if settings.mcp_server_command:
        return settings.mcp_server_command, []
    return sys.executable, ["-m", "mcp_office_server.server"]


def build_server_params(
    settings: Settings | None = None, workspace_root: str | Path | None = None
) -> StdioServerParameters:
    """Describe how to launch the MCP subprocess.

    ``workspace_root`` overrides the configured directory so tests can point the
    sandbox at a temporary tree without mutating global settings.
    """
    settings = settings or get_settings()
    command, args = _default_server_command(settings)
    root = Path(workspace_root) if workspace_root else settings.workspaces_dir
    return StdioServerParameters(
        command=command,
        args=args,
        env={
            "WORKSPACE_ROOT": str(root.resolve()),
            # Keep the subprocess quiet and unicode-safe on Windows consoles.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
        cwd=str(PROJECT_ROOT),
    )


class McpOfficeClient:
    """Owns one long-lived MCP session and exposes its tools to LangGraph."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.workspace_root = workspace_root
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[BaseTool] = []
        self._profiles: dict[str, ToolProfile] = {}

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._session is not None:
            return

        stack = AsyncExitStack()
        try:
            params = build_server_params(self.settings, self.workspace_root)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = await load_mcp_tools(session)
        except Exception as exc:
            await stack.aclose()
            raise McpStartupError(
                f"failed to start MCP server '{params.command}': {exc}"
            ) from exc

        self._exit_stack = stack
        self._session = session
        self._tools = tools
        self._profiles = {tool.name: self._profile_from_tool(tool) for tool in tools}
        logger.info(
            "MCP server ready with %d tools (%d gated behind approval)",
            len(tools),
            sum(1 for profile in self._profiles.values() if profile.requires_approval),
        )

    async def stop(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None
        self._tools = []
        self._profiles = {}

    @property
    def started(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------------ #
    # tools
    # ------------------------------------------------------------------ #
    @staticmethod
    def _profile_from_tool(tool: BaseTool) -> ToolProfile:
        """Read approval metadata from the tool's MCP annotations.

        ``langchain-mcp-adapters`` attaches the raw MCP annotations under
        ``metadata["annotations"]``; ``readOnlyHint`` is mirrored onto
        ``metadata["readOnlyHint"]`` in newer versions, so both are checked.
        """
        metadata = tool.metadata or {}
        annotations = metadata.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = getattr(annotations, "__dict__", {}) or {}

        read_only = bool(
            metadata.get("readOnlyHint")
            or annotations.get("readOnlyHint")
            or annotations.get("read_only_hint")
        )
        destructive = bool(
            metadata.get("destructiveHint")
            or annotations.get("destructiveHint")
            or annotations.get("destructive_hint")
        )

        schema = getattr(tool, "args_schema", None)
        if hasattr(schema, "model_json_schema"):
            schema_json = schema.model_json_schema()
        elif isinstance(schema, dict):
            schema_json = schema
        else:
            schema_json = {}

        return ToolProfile(
            name=tool.name,
            description=tool.description or "",
            read_only=read_only,
            destructive=destructive,
            schema=schema_json,
        )

    def tools(self) -> list[BaseTool]:
        return list(self._tools)

    def profiles(self) -> dict[str, ToolProfile]:
        return dict(self._profiles)

    def tool(self, name: str) -> BaseTool | None:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def is_governed_tool(self, name: str) -> bool:
        """True when the tool comes from the MCP server and is therefore subject to
        the server's annotations. Locally defined tools (the knowledge-base search)
        are not governed by this policy."""
        return name in self._profiles

    def requires_approval(self, name: str) -> bool:
        profile = self._profiles.get(name)
        # Unknown tools are gated: failing closed is the only safe default for a
        # tool that can touch a user's files.
        return True if profile is None else profile.requires_approval


async def load_tools_once(settings: Settings | None = None) -> list[BaseTool]:
    """Convenience helper for scripts and tests: start, load, and tear down."""
    settings = settings or get_settings()
    params = build_server_params(settings)
    client = MultiServerMCPClient({SERVER_NAME: _params_to_config(params)})
    return await client.get_tools()


def _params_to_config(params: StdioServerParameters) -> dict[str, Any]:
    return {
        "transport": "stdio",
        "command": params.command,
        "args": list(params.args or []),
        "env": dict(params.env or {}),
        "cwd": params.cwd,
    }


def parse_tool_result(result: Any) -> dict[str, Any]:
    """Normalize an MCP tool result into our ``{"ok": bool, ...}`` envelope.

    ``langchain-mcp-adapters`` returns the raw MCP content blocks, i.e.
    ``[{"type": "text", "text": "<json>"}]``. Everything downstream wants the parsed
    payload, so unwrapping happens here exactly once.
    """
    text: str | None = None

    if isinstance(result, str):
        text = result
    elif isinstance(result, dict) and "text" in result:
        text = str(result["text"])
    elif isinstance(result, (list, tuple)):
        parts = [
            str(block.get("text", ""))
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not parts:
            parts = [
                str(block)
                for block in result
                if isinstance(block, str)
            ]
        text = "\n".join(parts) if parts else None
    else:
        text = str(result)

    if text is None:
        return {"ok": False, "error": {"code": "empty_result", "message": "tool returned no content"}}

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"ok": True, "data": {"raw": text}}

    if isinstance(parsed, dict):
        return parsed
    return {"ok": True, "data": parsed}
