"""stdio MCP server that lets one assistant read and edit its own prompt.

The bridge starts it for every session with that session's assistant id, so an
assistant can only change its own instructions. It is a thin client of the bridge's
HTTP API (prompts.py does the work and keeps the undo history).
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server import MCPServer

BRIDGE = os.environ["AGENT_BRIDGE_URL"].rstrip("/")
ASSISTANT = os.environ["AGENT_BRIDGE_ASSISTANT_ID"]
HEADERS = {"Authorization": f"Bearer {os.environ['AGENT_BRIDGE_TOKEN']}"}

mcp = MCPServer(
    "assistant_prompt",
    instructions=(
        "Your own instructions (the prompt Home Assistant gives you). Change them when the user "
        "asks, or when you learn a lasting rule: a preference, or a mistake not to repeat. Keep "
        "each change small and say in your reply what you changed. Changes apply from the next message."
    ),
)


def _call(method: str, path: str, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> Any:
    url = f"{BRIDGE}/v1/assistants/{ASSISTANT}/prompt{path}" + (f"?{urlencode(query)}" if query else "")
    request = Request(url, method=method, headers={**HEADERS, "Content-Type": "application/json"},
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as err:
        raise RuntimeError(json.loads(err.read() or b"{}").get("error") or str(err)) from err


@mcp.tool()
def prompt_read() -> str:
    """Your current instructions, exactly as stored in Home Assistant."""
    return _call("GET", "")["prompt"]


@mcp.tool()
def prompt_add(text: str, reason: str) -> str:
    """Add a rule at the end of your instructions. reason: why, in a few words."""
    return _call("POST", "", {"op": "add", "text": text, "reason": reason})["prompt"]


@mcp.tool()
def prompt_edit(old_text: str, new_text: str, reason: str) -> str:
    """Replace text in your instructions; old_text must match exactly one place (read
    first). An empty new_text removes it. reason: why, in a few words."""
    return _call("POST", "", {"op": "edit", "old": old_text, "text": new_text, "reason": reason})["prompt"]


@mcp.tool()
def prompt_history(limit: int = 5) -> list[dict[str, Any]]:
    """Your latest prompt changes, oldest first: time, edit, reason, before, after."""
    return _call("GET", "/history", query={"limit": limit})["history"]


@mcp.tool()
def prompt_undo(reason: str) -> str:
    """Undo your last prompt change."""
    return _call("POST", "", {"op": "undo", "reason": reason})["prompt"]


if __name__ == "__main__":
    mcp.run()
