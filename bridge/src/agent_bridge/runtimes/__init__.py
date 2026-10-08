"""CLI runtimes. Each one keeps a live process and streams text deltas per turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
import sys
from typing import Any, Protocol

from ..config import Config

PROMPT_SERVER = "assistant_prompt"
EXTENSIONS_SERVER = "assistant_extensions"
TASKS_SERVER = "assistant_tasks"


def prompt_server(config: Config, assistant_id: str, token: str) -> dict[str, Any]:
    """stdio MCP server that edits only this assistant's prompt (prompt_mcp.py). It runs on
    the bridge's own Python and talks to the bridge over loopback. token is the bridge
    token, or a ${VAR} reference when the CLI expands it (keeps it out of argv)."""
    return {
        "command": sys.executable,
        "args": ["-m", "agent_bridge.prompt_mcp"],
        "env": {
            "AGENT_BRIDGE_URL": f"http://127.0.0.1:{config.port}",
            "AGENT_BRIDGE_ASSISTANT_ID": assistant_id,
            "AGENT_BRIDGE_TOKEN": token,
        },
    }


def extensions_server(config: Config, token: str) -> dict[str, Any]:
    """stdio MCP server for the assistants' own skills and MCP servers (extensions_mcp.py);
    same transport and token handling as prompt_server."""
    return {
        "command": sys.executable,
        "args": ["-m", "agent_bridge.extensions_mcp"],
        "env": {"AGENT_BRIDGE_URL": f"http://127.0.0.1:{config.port}", "AGENT_BRIDGE_TOKEN": token},
    }


def tasks_server(config: Config, assistant_id: str, token: str) -> dict[str, Any]:
    """stdio MCP server for background tasks (tasks_mcp.py); same transport as prompt_server."""
    server = prompt_server(config, assistant_id, token)
    return {**server, "args": ["-m", "agent_bridge.tasks_mcp"]}


def session_servers(config: Config, assistant_id: str, token: str, extensions: Any,
                    background: bool = False) -> dict[str, dict[str, Any]]:
    """The bridge's MCP servers for one session: the prompt tool, the background-task tool
    (not inside a background task, so one cannot start another), and with an extension
    store, its tool server and every server stored in it."""
    servers = {PROMPT_SERVER: prompt_server(config, assistant_id, token)}
    if not background:
        servers[TASKS_SERVER] = tasks_server(config, assistant_id, token)
    if extensions is not None:
        servers[EXTENSIONS_SERVER] = extensions_server(config, token)
        servers.update(extensions.mcp_servers())
    return servers


def session_signature(config: Config, spec: Any, extensions: Any) -> tuple:
    """What a live session was opened with. A new prompt, model, or extension revision
    reopens it on the next turn, resuming the same conversation."""
    revision = extensions.revision() if extensions is not None else ""
    return (spec.model, spec.full_instructions(config.extra_instructions, config.allow_shell), revision)


class SessionNotFound(Exception):
    """The CLI could not resume the stored session; the caller starts a new one."""


class LiveSession(Protocol):
    """One open CLI conversation for one assistant."""

    session_id: str
    signature: tuple

    def turn(self, prompt: str) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
