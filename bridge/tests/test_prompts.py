"""Unit tests for assistants editing their own prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.models import AssistantSpec
from agent_bridge.prompts import PromptError, PromptStore, apply_edit
from agent_bridge.runtimes import PROMPT_SERVER, prompt_server
from agent_bridge.runtimes.codex import CodexSession
from test_core import _config


def test_apply_edit_add_and_replace() -> None:
    assert apply_edit("Be brief.", "add", "Music goes to the TV.") == "Be brief.\n\nMusic goes to the TV."
    assert apply_edit("A.\n\nB.\n\nC.", "edit", "", "B.") == "A.\n\nC."
    with pytest.raises(PromptError):
        apply_edit("A. A.", "edit", "x", "A.")  # ambiguous
    with pytest.raises(PromptError):
        apply_edit("A.", "edit", "x", "missing")


class _FakeHA(PromptStore):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, "http://ha", "token")
        self.writes: list[tuple[str, str]] = []

    async def _write_to_ha(self, entity_id: str, prompt: str) -> None:
        self.writes.append((entity_id, prompt))


async def test_change_logs_history_and_undoes(tmp_path: Path) -> None:
    store = _FakeHA(tmp_path)
    with pytest.raises(PromptError):
        store.read("a")  # nothing known before a turn
    store.remember("a", "conversation.wabadi", "Be brief.")
    new = await store.change("a", "add", "user asked", text="Music goes to the TV.")
    assert store.writes[-1] == ("conversation.wabadi", new)
    assert store.history("a")[-1]["before"] == "Be brief."
    assert await store.change("a", "undo", "oops") == "Be brief."
    store.remember("a", "conversation.wabadi", "Edited in the UI.")
    with pytest.raises(PromptError):
        await store.change("a", "undo", "too late")  # never undo over someone else's edit


def test_every_session_gets_its_own_prompt_tool(tmp_path: Path) -> None:
    server = prompt_server(_config(tmp_path), "a", "${TOKEN}")
    assert server["args"] == ["-m", "agent_bridge.prompt_mcp"]
    assert server["env"]["AGENT_BRIDGE_ASSISTANT_ID"] == "a"
    session = CodexSession(None, _config(tmp_path), AssistantSpec("a", "codex", "m", ""), None)
    assert f"mcp_servers.{PROMPT_SERVER}" in session._thread_params()["config"]
