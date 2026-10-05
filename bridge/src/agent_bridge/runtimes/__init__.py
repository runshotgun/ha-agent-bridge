"""CLI runtimes. Each one keeps a live process and streams text deltas per turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class SessionNotFound(Exception):
    """The CLI could not resume the stored session; the caller starts a new one."""


class LiveSession(Protocol):
    """One open CLI conversation for one assistant."""

    session_id: str
    signature: tuple

    def turn(self, prompt: str) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
