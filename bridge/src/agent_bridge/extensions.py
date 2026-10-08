"""Skills, an instructions file, and MCP servers that the assistants maintain themselves.

They live in one folder (`[session] extensions_dir`) that is a git repository: every
change is a commit with the agent's reason, so history and undo come from git.
  CLAUDE.md               instructions (the deployment links it as ~/.claude/CLAUDE.md)
  skills/<name>/SKILL.md  skills (the deployment links skills/ as ~/.claude/skills)
  mcp/<name>/server.py    stdio MCP servers, run by `uv run --script` (PEP 723 deps)
The bridge registers the MCP servers in every session, and the store revision is part of
the session signature, so a change applies from the next message. MCP servers run code,
so every change to them needs the user's yes (`confirmed`).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from typing import Any

from . import __version__

KINDS = ("skill", "mcp", "instructions")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_DEFAULT_FILE = {"skill": "SKILL.md", "mcp": "server.py"}
_FOLDER = {"skill": "skills", "mcp": "mcp"}
_CHECK_SECONDS = 180  # the first start of a new server downloads its dependencies


class ExtensionError(Exception):
    """A change the store refuses; the message is for the agent."""


class ExtensionStore:
    def __init__(self, root: Path, reserved: set[str] | None = None) -> None:
        self.root = root
        self._reserved = reserved or set()  # MCP names the bridge itself registers
        self._lock = asyncio.Lock()

    # Repository

    def ensure_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init", "-q", "-b", "main")
            self._git("config", "user.name", "Agent Bridge")
            self._git("config", "user.email", "agent-bridge@localhost")
            self._commit("Start the extension store", self.root, allow_empty=True)

    def revision(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def history(self, limit: int = 10) -> list[dict[str, str]]:
        out = self._git("log", f"-n{max(1, limit)}", "--format=%h%x1f%cI%x1f%s")
        return [dict(zip(("commit", "time", "reason"), line.split("\x1f"))) for line in out.splitlines()]

    # Reading

    def list(self, kind: str) -> list[dict[str, str]]:
        _kind(kind)
        if kind == "instructions":
            return [{"name": "CLAUDE.md", "exists": str((self.root / "CLAUDE.md").exists()).lower()}]
        folder = self.root / _FOLDER[kind]
        found = []
        for item in sorted(folder.iterdir()) if folder.exists() else []:
            main = item / _DEFAULT_FILE[kind]
            if item.is_dir() and main.exists():
                entry = {"name": item.name}
                if kind == "skill":
                    entry["description"] = _frontmatter(main.read_text(encoding="utf-8")).get("description", "")
                found.append(entry)
        return found

    def read(self, kind: str, name: str = "", file: str = "") -> str:
        """One file (default: SKILL.md or server.py), with the folder's other files named."""
        path = self._path(kind, name, file or _DEFAULT_FILE.get(kind, ""))
        if not path.is_file():
            raise ExtensionError(f"{path.relative_to(self.root)} does not exist")
        text = path.read_text(encoding="utf-8")
        if kind != "instructions":
            others = [str(p.relative_to(path.parent)) for p in sorted(path.parent.rglob("*")) if p.is_file() and p != path]
            text += f"\n\n[Other files: {', '.join(others)}]" if others else ""
        return text

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Launch settings for every committed MCP server (same shape as the bridge's own).
        A server.py that only exists on disk (written by hand, or left by a failed write)
        never runs: servers enter the store only through a checked, confirmed write."""
        committed = self._git("ls-tree", "--name-only", "HEAD", "mcp/").split()
        return {Path(p).name: self._launch(Path(p).name) for p in committed
                if (self.root / p / "server.py").exists()}

    def _launch(self, name: str) -> dict[str, Any]:
        return {"command": shutil.which("uv") or "uv",
                "args": ["run", "--quiet", "--script", str(self.root / "mcp" / name / "server.py")]}

    # Changes

    async def write(self, kind: str, content: str, reason: str, name: str = "", file: str = "",
                    confirmed: bool = False) -> str:
        path = self._path(kind, name, file or _DEFAULT_FILE.get(kind, ""))
        self._need_reason(reason)
        if kind == "mcp":
            self._need_yes(confirmed)
            if name in self._reserved:
                raise ExtensionError(f"{name} is a built-in server name; choose another")
        if kind == "skill" and path.name == "SKILL.md":
            meta = _frontmatter(content)
            if meta.get("name") != name or not meta.get("description"):
                raise ExtensionError(f"SKILL.md must start with front matter: name: {name}, and a description")
        async with self._lock:
            self._commit_outside_changes()
            before = path.read_text(encoding="utf-8") if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if kind == "mcp":
                try:
                    if not (self.root / "mcp" / name / "server.py").exists():
                        raise ExtensionError("Write server.py first")
                    tools = await check_server(self._launch(name))
                except ExtensionError:
                    if before is None:
                        path.unlink()
                        if not any(path.parent.iterdir()):
                            path.parent.rmdir()
                    else:
                        path.write_text(before, encoding="utf-8")
                    raise
                reason = f"{reason} (tools: {', '.join(tools)})"
            if not self._commit(f"{kind} {name or 'CLAUDE.md'}: {reason}", path):
                raise ExtensionError("No change: the file already says that")
            return reason

    async def delete(self, kind: str, name: str, reason: str, confirmed: bool = False) -> None:
        if kind == "instructions":
            raise ExtensionError("The instructions file cannot be deleted; write a shorter one")
        path = self._path(kind, name, "")
        self._need_reason(reason)
        if kind == "mcp":
            self._need_yes(confirmed)
        if not path.exists():
            raise ExtensionError(f"No {kind} called {name}")
        async with self._lock:
            self._commit_outside_changes()
            shutil.rmtree(path)
            self._commit(f"{kind} {name}: delete: {reason}", path)

    async def undo(self, commit: str, reason: str, confirmed: bool = False) -> None:
        """Revert one commit; one that touches an MCP server needs the user's yes."""
        self._need_reason(reason)
        if not re.fullmatch(r"[0-9a-f]{4,40}", commit):
            raise ExtensionError("commit must be a commit id from extension_history")
        try:
            touched = self._git("show", "--name-only", "--format=", commit).split()
        except subprocess.CalledProcessError as err:
            raise ExtensionError(f"No commit {commit}") from err
        if any(p.startswith("mcp/") for p in touched):
            self._need_yes(confirmed)
        async with self._lock:
            self._commit_outside_changes()
            try:
                self._git("revert", "--no-edit", "--no-commit", commit)
            except subprocess.CalledProcessError as err:
                self._git("revert", "--abort")
                raise ExtensionError("Later changes touch the same lines; undo those first") from err
            self._commit(f"undo {commit}: {reason}", None)  # revert staged its own changes

    # Helpers

    def _path(self, kind: str, name: str, file: str) -> Path:
        _kind(kind)
        if kind == "instructions":
            return self.root / "CLAUDE.md"
        if not _NAME.fullmatch(name or ""):
            raise ExtensionError("name must be lowercase letters, digits, and dashes")
        base = self.root / _FOLDER[kind] / name
        if not file:
            return base  # the whole extension (delete)
        path = (base / file).resolve()
        if base.resolve() not in path.parents:
            raise ExtensionError("file must be a path inside the extension's folder")
        return path

    def _need_reason(self, reason: str) -> None:
        if not reason.strip():
            raise ExtensionError("Give a reason in a few words")

    @staticmethod
    def _need_yes(confirmed: bool) -> None:
        if not confirmed:
            raise ExtensionError("MCP servers run code: say what you will change, ask the user for a yes, "
                                 "and call again with user_confirmed only after they said yes")

    def _commit_outside_changes(self) -> None:
        """Keep hand edits of skills and CLAUDE.md apart from the agent's next change.
        mcp/ is left out: unchecked code must not slip in under another change."""
        self._git("add", "-A", "--", ".", ":(exclude)mcp")
        if self._staged():
            self._git("commit", "-q", "-m", "Changes made outside the extension tools")

    def _commit(self, message: str, path: Path | None, allow_empty: bool = False) -> bool:
        """Commit only path (a file or folder), or what is already staged when None."""
        if path is not None:
            self._git("add", "-A", "--", str(path))
        if not allow_empty and not self._staged():
            return False
        self._git("commit", "-q", *(["--allow-empty"] if allow_empty else []), "-m", message)
        return True

    def _staged(self) -> bool:
        return subprocess.run(["git", "-C", str(self.root), "diff", "--cached", "--quiet"]).returncode != 0

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout


def _kind(kind: str) -> None:
    if kind not in KINDS:
        raise ExtensionError(f"kind must be one of {', '.join(KINDS)}")


def _frontmatter(text: str) -> dict[str, str]:
    """name and description from a SKILL.md front matter block (simple key: value lines)."""
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    pairs = (line.split(":", 1) for line in (match.group(1).splitlines() if match else []) if ":" in line)
    return {key.strip(): value.strip().strip("\"'") for key, value in pairs}


async def check_server(launch: dict[str, Any], seconds: float = _CHECK_SECONDS) -> list[str]:
    """Start an MCP server, run the MCP handshake, and return its tool names. A server that
    does not start or answer is refused before it is registered. It runs in its own process
    group: `uv run` starts Python as a child, and killing only uv would leave that child
    holding the pipes open."""
    proc = await asyncio.create_subprocess_exec(
        launch["command"], *launch["args"], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)

    def stop() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def ask(rpc_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()
        while line := await proc.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue  # stray output; a stdio server should not print it, but it is not fatal
            if message.get("id") == rpc_id:
                if "error" in message:
                    raise ExtensionError(f"The server refused {method}: {message['error']}")
                return message["result"]
        raise ExtensionError("The server stopped")

    async def handshake() -> list[str]:
        await ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "agent-bridge", "version": __version__}})
        proc.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        return [tool["name"] for tool in (await ask(2, "tools/list", {}))["tools"]]

    try:
        tools = await asyncio.wait_for(handshake(), seconds)
    except (ExtensionError, TimeoutError) as err:
        stop()
        try:
            stderr = (await asyncio.wait_for(proc.stderr.read(), 5)).decode(errors="replace")[-1500:]
        except TimeoutError:
            stderr = "(no output)"
        raise ExtensionError(f"The MCP server did not start ({err or 'timed out'}). Its output:\n{stderr}") from err
    finally:
        stop()
        await proc.wait()
    if not tools:
        raise ExtensionError("The MCP server started but has no tools")
    return tools
