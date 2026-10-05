"""Persistent map from assistant ID to its CLI session.

One assistant has one session at a time. A session expires after a quiet
period (default 12 hours since the last message) or when the runtime changes.
The file is rewritten atomically so a crash never leaves half a record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import time


@dataclass
class SessionRecord:
    runtime: str
    session_id: str
    last_used: float
    turns: int = 0


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, SessionRecord] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._records = {key: SessionRecord(**value) for key, value in data.items()}

    def current(self, assistant_id: str, runtime: str, max_age_s: float) -> SessionRecord | None:
        """Return the live session, or None when it is missing, stale, or for another runtime."""
        record = self._records.get(assistant_id)
        if record is None or record.runtime != runtime:
            return None
        if time.time() - record.last_used > max_age_s:
            return None
        return record

    def save(self, assistant_id: str, record: SessionRecord) -> None:
        self._records[assistant_id] = record
        self._flush()

    def drop(self, assistant_id: str) -> SessionRecord | None:
        record = self._records.pop(assistant_id, None)
        self._flush()
        return record

    def get(self, assistant_id: str) -> SessionRecord | None:
        return self._records.get(assistant_id)

    def items(self) -> list[tuple[str, SessionRecord]]:
        return list(self._records.items())

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._records.items()}, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)
