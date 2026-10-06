"""CLI runtimes. Each one keeps a live process and streams text deltas per turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
import sys
from typing import Any, Protocol

from ..config import Config

PROMPT_SERVER = "assistant_prompt"


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


class SessionNotFound(Exception):
    """The CLI could not resume the stored session; the caller starts a new one."""


class LiveSession(Protocol):
    """One open CLI conversation for one assistant."""

    session_id: str
    signature: tuple

    def turn(self, prompt: str) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
