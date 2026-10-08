"""Unit tests for the assistants' own skills, instructions, and MCP servers."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from agent_bridge.extensions import ExtensionError, ExtensionStore, check_server
from agent_bridge.models import AssistantSpec
from agent_bridge.runtimes import EXTENSIONS_SERVER, session_servers, session_signature
from test_core import _config

SKILL = "---\nname: tv\ndescription: Living room TV facts.\n---\n\nThe TV is a Chromecast.\n"
# A dependency-free stdio MCP server: answers initialize and tools/list.
SERVER = '''import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}}
    elif m.get("method") == "tools/list":
        r = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def _store(tmp_path: Path) -> ExtensionStore:
    store = ExtensionStore(tmp_path / "ext", {"homeassistant"})
    store.ensure_repo()
    # Tests run the server with this Python instead of `uv run --script`.
    store._launch = lambda name: {"command": sys.executable, "args": [str(store.root / "mcp" / name / "server.py")]}
    return store


async def test_skill_write_read_history_undo(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = store.revision()
    with pytest.raises(ExtensionError):
        await store.write("skill", "no front matter", "x", name="tv")
    await store.write("skill", SKILL, "learned the TV model", name="tv")
    assert store.list("skill") == [{"name": "tv", "description": "Living room TV facts."}]
    assert "Chromecast" in store.read("skill", "tv")
    assert store.revision() != start  # sessions reopen on the next turn
    last = store.history(1)[0]
    assert "learned the TV model" in last["reason"]
    await store.undo(last["commit"], "wrong")
    assert store.list("skill") == []


async def test_paths_stay_inside_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for name, file in (("../x", ""), ("Tv", ""), ("tv", "../../CLAUDE.md")):
        with pytest.raises(ExtensionError):
            await store.write("skill", SKILL, "x", name=name, file=file)


async def test_instructions_and_outside_edits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write("instructions", "Be brief.", "voice rules")
    (store.root / "CLAUDE.md").write_text("Edited by hand.")
    await store.write("skill", SKILL, "x", name="tv")
    reasons = [h["reason"] for h in store.history(3)]
    assert "Changes made outside the extension tools" in reasons  # kept apart from the agent's reason


async def test_mcp_needs_yes_and_a_working_server(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ExtensionError, match="yes"):
        await store.write("mcp", SERVER, "ping tool", name="pinger")
    with pytest.raises(ExtensionError, match="built-in"):
        await store.write("mcp", SERVER, "x", name="homeassistant", confirmed=True)
    with pytest.raises(ExtensionError, match="did not start"):
        await store.write("mcp", "import sys; sys.exit('broken')", "x", name="pinger", confirmed=True)
    assert store.list("mcp") == []  # a failed check leaves nothing behind
    assert "ping" in await store.write("mcp", SERVER, "ping tool", name="pinger", confirmed=True)
    servers = session_servers(_config(tmp_path), "a", "${T}", store)
    assert set(servers) == {"assistant_prompt", "assistant_tasks", EXTENSIONS_SERVER, "pinger"}
    with pytest.raises(ExtensionError, match="yes"):
        await store.delete("mcp", "pinger", "not needed")
    # A server written by hand is neither registered nor committed under another change.
    (store.root / "mcp" / "manual").mkdir()
    (store.root / "mcp" / "manual" / "server.py").write_text(SERVER)
    await store.write("skill", SKILL, "x", name="tv")
    assert "manual" not in store.mcp_servers()
    assert "?? mcp/" in store._git("status", "--porcelain")


def test_signature_follows_the_store(tmp_path: Path) -> None:
    spec = AssistantSpec("a", "claude", "m", "Be brief.")
    assert session_signature(_config(tmp_path), spec, None)[2] == ""
    store = _store(tmp_path)
    assert session_signature(_config(tmp_path), spec, store)[2] == store.revision()


async def test_check_ends_when_the_launcher_starts_a_child(tmp_path: Path) -> None:
    """`uv run` starts Python as a child; the check must not wait on that child's pipes."""
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    launch = {"command": "sh", "args": ["-c", f"{sys.executable} {server}; true"]}  # sh keeps a child
    assert await asyncio.wait_for(check_server(launch, 10), 15) == ["ping"]
