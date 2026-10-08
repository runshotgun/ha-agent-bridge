"""Session lifecycle for every assistant.

Rules: one session per assistant; a new session after 12 quiet hours or a
runtime change; one turn at a time per assistant; live processes close after
a short idle time and resume the same session on the next turn. Each turn runs
as its own task, so a dropped HTTP client never leaves a CLI mid-response.
Background tasks run in a copy of the conversation, in their own process, and their
results are delivered (delivery.py) and added to the assistant's next turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import logging
import time
import uuid

from .config import Config
from .delivery import Delivery
from .extensions import ExtensionStore
from .models import AssistantSpec, format_turn
from .runtimes import EXTENSIONS_SERVER, PROMPT_SERVER, LiveSession, SessionNotFound, session_signature
from .runtimes.claude import ClaudeSession
from .runtimes.codex import CodexAppServer, CodexSession
from .sessions import SessionRecord, SessionStore

_LOGGER = logging.getLogger(__name__)
_DONE = object()
BACKGROUND_LIMIT = 3
BACKGROUND_SECONDS = 30 * 60
BACKGROUND_PROMPT = """\
[Background task. The user is no longer waiting in this conversation. Your final reply is \
delivered to them later, spoken where they asked or as a phone notification, so it must \
stand alone. Do the whole task now: use the deep subagent for work that needs thinking, \
and to wait for a T3 thread use t3_wait_thread. End with only the result in one to three \
short plain sentences that start by naming what it is about, with no markdown.]
Task: {task}"""


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    live: LiveSession | None = None
    runtime: str | None = None
    last_active: float = 0.0


@dataclass
class TurnResult:
    session_id: str = ""
    new_session: bool = False


class AssistantManager:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._store = SessionStore(config.state_dir / "sessions.json")
        self._slots: dict[str, _Slot] = {}
        self._codex = CodexAppServer(config)
        self._reaper: asyncio.Task | None = None
        self._delivery = Delivery(config.ha_url, config.ha_token, config.notify_service,
                                  config.quiet_hours, config.timezone)
        self._specs: dict[str, AssistantSpec] = {}
        self._origins: dict[str, tuple[str, str]] = {}  # (satellite, conversation) of the latest turn
        self._notes: dict[str, list[str]] = {}  # background results for the next turn
        self._background_slots = asyncio.Semaphore(BACKGROUND_LIMIT)
        self._background: set[asyncio.Task] = set()
        self.extensions = (ExtensionStore(config.extensions_dir, {PROMPT_SERVER, EXTENSIONS_SERVER, "homeassistant"})
                           if config.extensions_dir else None)

    async def start(self) -> None:
        self._config.workdir.mkdir(parents=True, exist_ok=True)
        if self.extensions:
            self.extensions.ensure_repo()
        self._reaper = asyncio.create_task(self._reap_idle())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for slot in self._slots.values():
            if slot.live:
                await slot.live.close()
        await self._codex.close()

    def stream_turn(self, spec: AssistantSpec, text: str, context: dict[str, str],
                    satellite_id: str = "", conversation_id: str = "") -> tuple[asyncio.Queue, asyncio.Task]:
        """Run the turn in a task that feeds a queue; the HTTP side only reads the queue."""
        self._specs[spec.assistant_id] = spec
        self._origins[spec.assistant_id] = (satellite_id, conversation_id)
        prompt = format_turn(text, context)
        if notes := self._notes.pop(spec.assistant_id, []):
            prompt = "[Background results delivered since the last message]\n" + "\n".join(notes) + "\n\n" + prompt
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._run_turn(spec, prompt, queue))
        return queue, task

    def start_background(self, assistant_id: str, task: str, summary: str) -> None:
        """Queue a background task from inside a turn; it starts when that turn ends."""
        spec = self._specs.get(assistant_id)
        if spec is None:
            raise ValueError("No turn from this assistant yet")
        if len(self._background) >= BACKGROUND_LIMIT * 2:
            raise ValueError("Too many background tasks are waiting; try again later")
        job = asyncio.create_task(self._run_background(spec, self._origins.get(assistant_id, ("", "")), task, summary))
        self._background.add(job)
        job.add_done_callback(self._background.discard)

    async def _run_background(self, spec: AssistantSpec, origin: tuple[str, str], task: str, summary: str) -> None:
        slot = self._slots.setdefault(spec.assistant_id, _Slot())
        async with self._background_slots:
            async with slot.lock:  # wait for the turn that asked to end, so the copy includes it
                record = self._store.get(spec.assistant_id)
            fork = record is not None and record.runtime == spec.runtime
            if spec.runtime == "claude":
                live: LiveSession = ClaudeSession(self._config, spec, record.session_id if fork else str(uuid.uuid4()),
                                                  resume=fork, extensions=self.extensions, background=True)
            else:
                live = CodexSession(self._codex, self._config, spec, None, self.extensions, background=True)
            try:
                result = await asyncio.wait_for(self._collect(live, BACKGROUND_PROMPT.format(task=task)), BACKGROUND_SECONDS)
            except TimeoutError:
                result = f"I could not finish {summary} within 30 minutes."
            except Exception as err:  # noqa: BLE001 - the user must hear about every failure
                _LOGGER.exception("Background task failed for %s", spec.assistant_id)
                result = f"I could not finish {summary}: {str(err)[:150] or type(err).__name__}."
            finally:
                await live.close()
        result = result.strip() or f"{summary} finished without a result."
        await self._delivery.send(origin[0], result, conversation_id=origin[1], summary=summary)
        self._notes.setdefault(spec.assistant_id, []).append(f"- {summary} ({time.strftime('%H:%M')}): {result}")

    @staticmethod
    async def _collect(live: LiveSession, prompt: str) -> str:
        return "".join([delta async for delta in live.turn(prompt)])

    async def _run_turn(self, spec: AssistantSpec, prompt: str, queue: asyncio.Queue) -> TurnResult:
        slot = self._slots.setdefault(spec.assistant_id, _Slot())
        async with slot.lock:
            try:
                return await self._turn_locked(slot, spec, prompt, queue)
            finally:
                slot.last_active = time.monotonic()
                queue.put_nowait(_DONE)

    async def _turn_locked(self, slot: _Slot, spec: AssistantSpec, prompt: str, queue: asyncio.Queue) -> TurnResult:
        max_age = self._config.reset_after_hours * 3600
        record = self._store.current(spec.assistant_id, spec.runtime, max_age)
        result = TurnResult(new_session=record is None)
        for attempt in (1, 2):
            live = await self._live_session(slot, spec, record)
            emitted = False
            try:
                async for delta in live.turn(prompt):
                    emitted = True
                    queue.put_nowait(delta)
            except SessionNotFound:
                # The stored session is gone (deleted files, other machine). Start fresh once.
                if emitted or attempt == 2:
                    raise
                _LOGGER.warning("Session for %s not found; starting a new one", spec.assistant_id)
                await self._discard(slot, spec.assistant_id)
                record, result.new_session = None, True
                continue
            except BaseException:
                # A broken process must not serve the next turn.
                await self._discard(slot, None)
                raise
            previous_turns = record.turns if record else 0
            self._store.save(
                spec.assistant_id,
                SessionRecord(spec.runtime, live.session_id, time.time(), previous_turns + 1),
            )
            result.session_id = live.session_id
            return result
        raise RuntimeError("unreachable")

    async def _live_session(self, slot: _Slot, spec: AssistantSpec, record: SessionRecord | None) -> LiveSession:
        """Reuse the open process when it matches; otherwise open or resume one."""
        signature = session_signature(self._config, spec, self.extensions)
        live = slot.live
        same = live and record and slot.runtime == spec.runtime and live.session_id == record.session_id
        if same and live.signature == signature:
            return live
        if live:
            await live.close()
        if spec.runtime == "claude":
            session_id = record.session_id if record else str(uuid.uuid4())
            slot.live = ClaudeSession(self._config, spec, session_id, resume=record is not None, extensions=self.extensions)
        elif spec.runtime == "codex":
            slot.live = CodexSession(self._codex, self._config, spec, record.session_id if record else None, self.extensions)
        else:
            raise ValueError(f"Unknown runtime: {spec.runtime}")
        slot.runtime = spec.runtime
        return slot.live

    async def _discard(self, slot: _Slot, assistant_id: str | None) -> None:
        if slot.live:
            await slot.live.close()
            slot.live = None
        if assistant_id:
            self._store.drop(assistant_id)

    async def reset(self, assistant_id: str) -> bool:
        """Forget the session so the next message starts a new conversation."""
        slot = self._slots.setdefault(assistant_id, _Slot())
        async with slot.lock:
            had = self._store.get(assistant_id) is not None
            await self._discard(slot, assistant_id)
            return had

    def status(self) -> dict:
        now = time.time()
        return {
            assistant_id: {
                "runtime": record.runtime,
                "session_id": record.session_id,
                "turns": record.turns,
                "idle_seconds": round(now - record.last_used),
                "live": bool((slot := self._slots.get(assistant_id)) and slot.live),
            }
            for assistant_id, record in self._store.items()
        }

    async def _reap_idle(self) -> None:
        idle_s = self._config.idle_close_minutes * 60
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for assistant_id, slot in self._slots.items():
                if slot.live and not slot.lock.locked() and now - slot.last_active > idle_s:
                    _LOGGER.info("Closing idle session for %s", assistant_id)
                    await slot.live.close()
                    slot.live = None
            codex_busy = bool(self._background) or any(
                s.lock.locked() or (s.live and s.runtime == "codex") for s in self._slots.values())
            if self._codex.running and not codex_busy:
                await self._codex.close()


async def drain(queue: asyncio.Queue) -> AsyncIterator[str]:
    """Yield queued deltas until the turn task signals the end."""
    while (item := await queue.get()) is not _DONE:
        yield item
