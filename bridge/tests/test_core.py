"""Unit tests for the session rules, the turn format, and the Codex approval policy."""

from __future__ import annotations

from pathlib import Path
import time

from agent_bridge.config import Config, RuntimePaths
from agent_bridge.models import AssistantSpec, format_turn
from agent_bridge.runtimes.codex import CodexAppServer
from agent_bridge.sessions import SessionRecord, SessionStore


def _config(tmp_path: Path, allow_shell: bool = False) -> Config:
    return Config(
        host="127.0.0.1", port=0, bridge_token="t", proxy_base_url="http://proxy", proxy_key="k",
        ha_mcp_url=None, ha_token=None, ha_url=None, workdir=tmp_path, state_dir=tmp_path,
        reset_after_hours=12, idle_close_minutes=15, allow_shell=allow_shell,
        paths=RuntimePaths("claude", "codex"),
    )


def test_session_expires_after_quiet_period(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.json")
    store.save("a", SessionRecord("claude", "s1", time.time() - 13 * 3600))
    assert store.current("a", "claude", 12 * 3600) is None
    store.save("a", SessionRecord("claude", "s1", time.time() - 3600))
    assert store.current("a", "claude", 12 * 3600).session_id == "s1"


def test_runtime_change_starts_new_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.json")
    store.save("a", SessionRecord("claude", "s1", time.time()))
    assert store.current("a", "codex", 12 * 3600) is None


def test_store_survives_restart(tmp_path: Path) -> None:
    SessionStore(tmp_path / "s.json").save("a", SessionRecord("codex", "t1", time.time(), 4))
    record = SessionStore(tmp_path / "s.json").get("a")
    assert (record.session_id, record.turns) == ("t1", 4)


def test_format_turn_skips_empty_context() -> None:
    assert format_turn("hi", {}) == "hi"
    assert format_turn("hi", {"person": "Alex", "area": ""}) == "[Home Assistant | person: Alex]\nhi"


def test_instructions_order() -> None:
    text = AssistantSpec("a", "claude", "m", "Be brief.").full_instructions("Extra.")
    assert text.index("voice assistant") < text.index("Extra.") < text.index("Be brief.")
    assert "Do not edit files" in text


def test_shell_access_needs_a_spoken_yes() -> None:
    text = AssistantSpec("a", "claude", "m", "").full_instructions(allow_shell=True)
    assert "ask for a yes" in text and "Do not edit files" not in text


def test_codex_policy_runs_mcp_and_blocks_shell(tmp_path: Path) -> None:
    server = CodexAppServer(_config(tmp_path))
    mcp = {"method": "mcpServer/elicitation/request", "params": {"_meta": {"codex_approval_kind": "mcp_tool_call"}}}
    elicit = {"method": "mcpServer/elicitation/request", "params": {"_meta": {}}}
    shell = {"method": "item/commandExecution/requestApproval", "params": {}}
    other = {"method": "item/tool/requestUserInput", "params": {}}
    assert server._answer(mcp) == {"result": {"action": "accept"}}
    assert server._answer(elicit) == {"result": {"action": "decline"}}
    assert server._answer(shell) == {"result": {"decision": "decline"}}
    assert "error" in server._answer(other)
    assert CodexAppServer(_config(tmp_path, allow_shell=True))._answer(shell) == {"result": {"decision": "accept"}}
