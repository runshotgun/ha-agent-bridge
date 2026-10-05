"""Codex runtime through `codex app-server` (JSON-RPC over stdio).

One app-server process serves every Codex assistant; each assistant owns one
thread in it. The proxy is a custom model provider whose key comes from the
environment (env_key), so no secret appears on the command line. Approval and
input requests are answered by policy at once: voice sessions never wait on them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
import itertools
import json
import logging
import os
from typing import Any

from .. import __version__
from ..config import Config
from ..models import AssistantSpec
from . import SessionNotFound

_LOGGER = logging.getLogger(__name__)
_PROVIDER = "agent_bridge_proxy"
_NOT_FOUND = ("not found", "no rollout", "no such thread", "unknown thread")


class CodexRpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", str(error)))
        self.error = error


class CodexAppServer:
    """Owns the app-server process, routes responses by ID and events by thread."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._threads: dict[str, asyncio.Queue] = {}
        self._start_lock = asyncio.Lock()
        # Increases on every process start; threads loaded in an older process must resume again.
        self.generation = 0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _command(self) -> list[str]:
        cfg = self._config
        overrides = [
            f'model_providers.{_PROVIDER}={{name="CLIProxyAPI",base_url="{cfg.proxy_base_url}/v1",'
            f'env_key="AGENT_BRIDGE_PROXY_KEY",wire_api="responses"}}',
            f'model_provider="{_PROVIDER}"',
        ]
        if cfg.ha_mcp_url and cfg.ha_token:
            overrides.append(
                f'mcp_servers.homeassistant={{url="{cfg.ha_mcp_url}",'
                'bearer_token_env_var="AGENT_BRIDGE_HA_TOKEN"}'
            )
        args = [cfg.paths.codex_cli, "app-server"]
        for override in overrides:
            args += ["-c", override]
        return args

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            env = {**os.environ, "AGENT_BRIDGE_PROXY_KEY": self._config.proxy_key}
            if self._config.ha_token:
                env["AGENT_BRIDGE_HA_TOKEN"] = self._config.ha_token
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self._config.workdir),
                env=env,
                limit=64 * 1024 * 1024,
            )
            self._reader = asyncio.create_task(self._read_loop(self._proc))
            self.generation += 1
            await self.request(
                "initialize",
                {"clientInfo": {"name": "agent_bridge", "title": "Agent Bridge", "version": __version__}},
            )
            await self._send({"method": "initialized"})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float = 120) -> Any:
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        return self._threads.setdefault(thread_id, asyncio.Queue())

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        async for line in proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message and "method" not in message:
                future = self._pending.get(message["id"])
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(CodexRpcError(message["error"]))
                    else:
                        future.set_result(message.get("result"))
            elif "id" in message:
                await self._send({"id": message["id"], **self._answer(message)})
            else:
                thread_id = (message.get("params") or {}).get("threadId")
                if thread_id in self._threads:
                    self._threads[thread_id].put_nowait(message)
        # Process ended: fail every waiter so no turn hangs.
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("codex app-server exited"))
        for queue in self._threads.values():
            queue.put_nowait({"method": "bridge/exited"})

    def _answer(self, message: dict[str, Any]) -> dict[str, Any]:
        """Policy for app-server requests: run MCP tools, gate shell and file work.

        Same rule as the Claude runtime: MCP tool calls run; commands and file
        changes run only when allow_shell is on. Everything else (questions,
        real elicitations, permission escalation) is refused so a turn never waits.
        """
        method, params = message.get("method"), message.get("params") or {}
        allow = self._config.allow_shell
        if method == "mcpServer/elicitation/request":
            approval = (params.get("_meta") or {}).get("codex_approval_kind") == "mcp_tool_call"
            return {"result": {"action": "accept" if approval else "decline"}}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"result": {"decision": "accept" if allow else "decline"}}
        if method in ("execCommandApproval", "applyPatchApproval"):
            return {"result": {"decision": "approved" if allow else "abort"}}
        _LOGGER.info("Refusing app-server request %s", method)
        return {"error": {"code": -32601, "message": "Not supported by Agent Bridge"}}

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 10)
            if proc.returncode is None:
                proc.kill()
        if self._reader:
            self._reader.cancel()
        self._threads.clear()


class CodexSession:
    """One assistant's thread inside the shared app-server."""

    def __init__(self, server: CodexAppServer, config: Config, spec: AssistantSpec, session_id: str | None) -> None:
        self._server = server
        self._config = config
        self._spec = spec
        self.session_id = session_id or ""
        self.signature = (spec.model, spec.full_instructions(config.extra_instructions))
        self._loaded_generation = -1

    def _thread_params(self) -> dict[str, Any]:
        return {
            "model": self._spec.model,
            "cwd": str(self._config.workdir),
            "approvalPolicy": "on-request",
            "sandbox": "danger-full-access" if self._config.allow_shell else "read-only",
            "developerInstructions": self.signature[1],
        }

    async def _ensure_thread(self) -> None:
        await self._server.ensure_started()
        if self.session_id and self._loaded_generation == self._server.generation:
            return
        try:
            if self.session_id:
                result = await self._server.request("thread/resume", {"threadId": self.session_id, **self._thread_params()})
            else:
                result = await self._server.request("thread/start", self._thread_params())
        except CodexRpcError as err:
            if self.session_id and any(marker in str(err).lower() for marker in _NOT_FOUND):
                raise SessionNotFound(str(err)) from err
            raise
        self.session_id = result["thread"]["id"]
        self._loaded_generation = self._server.generation

    async def turn(self, prompt: str) -> AsyncIterator[str]:
        """Start one turn and yield agent message deltas until the turn completes."""
        await self._ensure_thread()
        queue = self._server.subscribe(self.session_id)
        while not queue.empty():
            queue.get_nowait()
        await self._server.request(
            "turn/start",
            {"threadId": self.session_id, "input": [{"type": "text", "text": prompt}]},
        )
        last_item: str | None = None
        while True:
            message = await queue.get()
            method, params = message.get("method"), message.get("params") or {}
            if method == "item/agentMessage/delta":
                if last_item is not None and params["itemId"] != last_item:
                    yield "\n\n"
                last_item = params["itemId"]
                yield params["delta"]
            elif method == "turn/completed":
                turn = params["turn"]
                if turn.get("status") == "failed":
                    raise RuntimeError(f"Codex turn failed: {(turn.get('error') or {}).get('message')}")
                return
            elif method == "bridge/exited":
                raise ConnectionError("codex app-server exited during the turn")

    async def close(self) -> None:
        # The thread stays on disk; the next turn resumes it.
        self._loaded_generation = -1
