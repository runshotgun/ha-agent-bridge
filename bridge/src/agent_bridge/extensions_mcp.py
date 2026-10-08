"""stdio MCP server that lets the assistants maintain their own skills, instructions
file, and MCP servers. A thin client of the bridge's HTTP API (extensions.py does the
work, keeps the git history, and refuses MCP changes without the user's yes).
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from mcp.server import MCPServer

BRIDGE = os.environ["AGENT_BRIDGE_URL"].rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.environ['AGENT_BRIDGE_TOKEN']}"}

mcp = MCPServer(
    "assistant_extensions",
    instructions=(
        "Your own skills, instructions file (CLAUDE.md), and MCP servers. kind is skill, "
        "instructions, or mcp. Write a skill when you learn a procedure worth reusing, and change "
        "skills and the instructions file on your own when the user asks or you learn a lasting "
        "rule; say in your reply what you changed. MCP servers run code: before you create, change, "
        "delete, or undo one, say what it will do and ask for a yes; call with user_confirmed=true "
        "only after the user said yes. An MCP server is one file, server.py, that starts with a "
        "PEP 723 block listing its dependencies (include \"mcp>=2.3,<3\"), uses "
        "`from mcp.server import MCPServer`, declares tools with @mcp.tool(), and ends with "
        "mcp.run(). The bridge starts it and checks its tools before saving. Changes apply from "
        "the next message."
    ),
)


def _call(method: str, path: str = "", body: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> Any:
    url = f"{BRIDGE}/v1/extensions{path}" + (f"?{urlencode(query)}" if query else "")
    request = Request(url, method=method, headers={**HEADERS, "Content-Type": "application/json"},
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(request, timeout=240) as response:  # an MCP check can download dependencies
            return json.load(response)
    except HTTPError as err:
        raise RuntimeError(json.loads(err.read() or b"{}").get("error") or str(err)) from err


@mcp.tool()
def extension_list(kind: str) -> list[dict[str, str]]:
    """Your skills (with descriptions), MCP servers, or instructions file. kind: skill, mcp, or instructions."""
    return _call("GET", "", query={"kind": kind})["items"]


@mcp.tool()
def extension_read(kind: str, name: str = "", file: str = "") -> str:
    """Read SKILL.md, server.py, or CLAUDE.md (or another file of that skill or server)."""
    return _call("GET", f"/{quote(kind)}", query={"name": name, "file": file})["text"]


@mcp.tool()
def extension_write(kind: str, content: str, reason: str, name: str = "", file: str = "",
                    user_confirmed: bool = False) -> str:
    """Create or replace a file: a skill's SKILL.md (front matter with name and description
    first), another file in it (file="reference.md"), an MCP server's server.py, or the
    instructions file (kind="instructions", no name). Read before you replace. reason: why,
    in a few words. user_confirmed: only for kind="mcp", after the user said yes."""
    return _call("POST", "", {"op": "write", "kind": kind, "name": name, "file": file, "content": content,
                              "reason": reason, "confirmed": user_confirmed})["result"]


@mcp.tool()
def extension_delete(kind: str, name: str, reason: str, user_confirmed: bool = False) -> str:
    """Delete a skill or an MCP server (user_confirmed after a yes, for MCP)."""
    return _call("POST", "", {"op": "delete", "kind": kind, "name": name, "reason": reason,
                              "confirmed": user_confirmed})["result"]


@mcp.tool()
def extension_history(limit: int = 10) -> list[dict[str, str]]:
    """Recent changes, newest first: commit id, time, and reason."""
    return _call("GET", "/history", query={"limit": limit})["history"]


@mcp.tool()
def extension_undo(commit: str, reason: str, user_confirmed: bool = False) -> str:
    """Undo one change by its commit id (user_confirmed after a yes when it touched an MCP server)."""
    return _call("POST", "", {"op": "undo", "commit": commit, "reason": reason,
                              "confirmed": user_confirmed})["result"]


if __name__ == "__main__":
    mcp.run()
