"""stdio MCP server with one tool: run long work in the background and report back.

A thin client of the bridge's HTTP API; the bridge forks the assistant's session after
the current turn, runs the task, and delivers the result (delivery.py).
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mcp.server import MCPServer

BRIDGE = os.environ["AGENT_BRIDGE_URL"].rstrip("/")
ASSISTANT = os.environ["AGENT_BRIDGE_ASSISTANT_ID"]
HEADERS = {"Authorization": f"Bearer {os.environ['AGENT_BRIDGE_TOKEN']}", "Content-Type": "application/json"}

mcp = MCPServer(
    "assistant_tasks",
    instructions=(
        "Work that takes more than about 15 seconds (an investigation, waiting for a T3 thread, "
        "a multi-step check) runs in the background with background_task, so the user does not wait. "
        "The result reaches the user by itself: spoken where they asked, or as a phone notification."
    ),
)


@mcp.tool()
def background_task(task: str, summary: str) -> str:
    """Run a long task in the background; its result is delivered to the user when done.
    task: the whole job, self-contained (what to do, which tools, the T3 thread id to
    wait for, what the user wants to know). It runs in a copy of this conversation.
    summary: a few words that name it ("today's personal emails"). After this call, tell
    the user in one short sentence that you are on it and will report back, then stop."""
    request = Request(f"{BRIDGE}/v1/assistants/{ASSISTANT}/background", method="POST", headers=HEADERS,
                      data=json.dumps({"task": task, "summary": summary}).encode())
    try:
        with urlopen(request, timeout=30) as response:
            json.load(response)
    except HTTPError as err:
        raise RuntimeError(json.loads(err.read() or b"{}").get("error") or str(err)) from err
    return "Started. Say in one short sentence that you are on it and will report back, then end your turn."


if __name__ == "__main__":
    mcp.run()
