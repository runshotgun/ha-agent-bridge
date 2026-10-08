"""Unit tests for background tasks: delivery choice, task tool wiring, results in the next turn."""

from __future__ import annotations

import asyncio
from datetime import time
from pathlib import Path

from test_core import _config

from agent_bridge import manager as manager_module
from agent_bridge.delivery import choose, in_quiet_hours
from agent_bridge.manager import AssistantManager
from agent_bridge.models import AssistantSpec
from agent_bridge.runtimes import TASKS_SERVER, session_servers

SAT = "assist_satellite.kitchen"


def test_quiet_hours_wrap_midnight() -> None:
    assert in_quiet_hours(time(23, 0), "22:00-07:00") and in_quiet_hours(time(6, 59), "22:00-07:00")
    assert not in_quiet_hours(time(7, 0), "22:00-07:00") and not in_quiet_hours(time(12, 0), None)


def test_delivery_choice() -> None:
    assert choose(SAT, time(12, 0), "22:00-07:00", "notify.phone") == ("assist_satellite.announce", {"entity_id": SAT})
    assert choose(SAT, time(23, 0), "22:00-07:00", "notify.phone")[0] == "notify.phone"  # quiet hours
    assert choose("", time(12, 0), None, "notify.phone")[0] == "notify.phone"  # typed in the app
    assert choose("", time(12, 0), None, None)[0] == "persistent_notification.create"


def test_background_sessions_cannot_start_more(tmp_path: Path) -> None:
    assert TASKS_SERVER in session_servers(_config(tmp_path), "a", "t", None)
    assert TASKS_SERVER not in session_servers(_config(tmp_path), "a", "t", None, background=True)


class _FakeLive:
    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    async def turn(self, prompt: str):
        assert "Task: check the mail" in prompt
        yield "Today's mail: two messages."

    async def close(self) -> None:
        pass


async def test_result_is_delivered_and_reaches_the_next_turn(tmp_path: Path, monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(manager_module, "ClaudeSession", _FakeLive)
    manager = AssistantManager(_config(tmp_path))

    async def send(origin: str, message: str, conversation_id: str = "", summary: str = "") -> None:
        sent.append((origin, message))
    manager._delivery.send = send
    spec = AssistantSpec("a", "claude", "m", "")
    manager._specs["a"], manager._origins["a"] = spec, (SAT, "conv")
    manager.start_background("a", "check the mail", "today's mail")
    await asyncio.gather(*manager._background)
    assert sent == [(SAT, "Today's mail: two messages.")]
    manager._run_turn = lambda spec, prompt, queue: asyncio.sleep(0, prompt)  # capture the prompt only
    _, task = manager.stream_turn(spec, "what did it find?", {})
    assert "Today's mail: two messages." in await task
    _, task = manager.stream_turn(spec, "again", {})
    assert "Today's mail" not in await task  # a result is added once


async def test_unclaimed_result_falls_back_to_notify(monkeypatch) -> None:
    from agent_bridge.delivery import Delivery
    calls: list[str] = []
    d = Delivery("http://ha", "t", "notify.phone", None)

    async def call(service: str, data: dict) -> bool:
        calls.append(service)
        return True

    async def claimed(*args) -> bool:
        return claim
    d._call, d._claimed = call, claimed
    claim = True
    await d.send("", "msg", conversation_id="c")
    assert calls == []  # a client spoke it
    claim = False
    await d.send("", "msg", conversation_id="c")
    assert calls == ["notify.phone"]
    calls.clear()
    await d.send(SAT, "msg", conversation_id="c")
    assert calls == ["assist_satellite.announce"]  # satellites never wait for a client
