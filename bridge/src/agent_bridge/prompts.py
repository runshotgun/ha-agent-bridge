"""Assistants editing their own Home Assistant prompt.

Home Assistant owns the prompt (the assistant's subentry). HA sends it with every
turn, so the bridge knows the current text; an edit is written back through HA's
`agent_bridge.set_prompt` service and applies from the next turn (a new prompt
changes the session signature, and the same conversation resumes with it).
Every change is appended to state/prompts/<assistant>.jsonl, which is also the undo log.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout


class PromptError(Exception):
    """A prompt edit the bridge refuses; the message is for the agent."""


def apply_edit(prompt: str, op: str, text: str = "", old: str = "") -> str:
    """New prompt for an edit: add a line at the end, or replace exact text (empty new text removes)."""
    if op == "add":
        if not text.strip():
            raise PromptError("Nothing to add")
        return f"{prompt.rstrip()}\n\n{text.strip()}".strip()
    if op == "edit":
        count = prompt.count(old) if old else 0
        if count != 1:
            raise PromptError("old_text must match exactly one place in the prompt; read it first")
        return re.sub(r"\n{3,}", "\n\n", prompt.replace(old, text)).strip()
    raise PromptError(f"Unknown edit: {op}")


@dataclass
class _Known:
    entity_id: str
    prompt: str


class PromptStore:
    def __init__(self, state_dir: Path, ha_url: str | None, ha_token: str | None) -> None:
        self._dir = state_dir / "prompts"
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._known: dict[str, _Known] = {}

    def remember(self, assistant_id: str, entity_id: str | None, prompt: str) -> None:
        """Record what HA sent with a turn; that is the prompt in force."""
        if entity_id:
            self._known[assistant_id] = _Known(entity_id, prompt)

    def read(self, assistant_id: str) -> str:
        return self._get(assistant_id).prompt

    def history(self, assistant_id: str, limit: int = 5) -> list[dict[str, Any]]:
        path = self._dir / f"{assistant_id}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [json.loads(line) for line in lines[-max(1, limit):]]

    async def change(self, assistant_id: str, op: str, reason: str, text: str = "", old: str = "") -> str:
        known = self._get(assistant_id)
        if op == "undo":
            last = self.history(assistant_id, 1)
            if not last or last[0]["after"] != known.prompt:
                raise PromptError("Nothing to undo: the prompt changed outside these tools since the last edit")
            new = last[0]["before"]
        else:
            new = apply_edit(known.prompt, op, text, old)
        if new == known.prompt:
            raise PromptError("The prompt already says that")
        await self._write_to_ha(known.entity_id, new)
        self._log(assistant_id, {"time": time.time(), "op": op, "reason": reason, "before": known.prompt, "after": new})
        known.prompt = new
        return new

    def _get(self, assistant_id: str) -> _Known:
        known = self._known.get(assistant_id)
        if known is None:
            raise PromptError("This assistant's prompt is not known yet; the Home Assistant integration may need an update")
        return known

    async def _write_to_ha(self, entity_id: str, prompt: str) -> None:
        if not (self._ha_url and self._ha_token):
            raise PromptError("The bridge has no Home Assistant URL and token, so it cannot save prompts")
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.post(
                f"{self._ha_url}/api/services/agent_bridge/set_prompt",
                headers={"Authorization": f"Bearer {self._ha_token}"},
                json={"entity_id": entity_id, "prompt": prompt},
            ) as response:
                if response.status >= 400:
                    raise PromptError(f"Home Assistant refused the prompt change ({response.status}): {(await response.text())[:200]}")

    def _log(self, assistant_id: str, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with (self._dir / f"{assistant_id}.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
