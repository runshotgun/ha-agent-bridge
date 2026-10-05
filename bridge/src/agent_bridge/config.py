"""Bridge configuration loaded from a TOML file.

Secrets (bridge token, proxy key, HA token) live in separate files with mode
0600. The TOML file holds only their paths, so it is safe to show or diff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


def _read_secret(path: Path) -> str:
    """Read a one-line secret file and fail early if it is empty."""
    value = path.expanduser().read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret file is empty: {path}")
    return value


@dataclass(frozen=True)
class RuntimePaths:
    claude_cli: str
    codex_cli: str


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    bridge_token: str
    proxy_base_url: str
    proxy_key: str
    ha_mcp_url: str | None
    ha_token: str | None
    workdir: Path
    state_dir: Path
    reset_after_hours: float
    idle_close_minutes: float
    allow_shell: bool
    paths: RuntimePaths
    extra_instructions: str = field(default="")


def load_config(path: Path) -> Config:
    """Parse the TOML file and resolve every secret file it names."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    server, proxy, session = raw["server"], raw["proxy"], raw.get("session", {})
    ha, runtimes, policy = raw.get("homeassistant", {}), raw.get("runtimes", {}), raw.get("policy", {})
    state_dir = Path(session.get("state_dir", "~/Library/Application Support/AgentBridge/state")).expanduser()
    return Config(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8318)),
        bridge_token=_read_secret(Path(server["token_file"])),
        proxy_base_url=proxy.get("base_url", "http://127.0.0.1:8317").rstrip("/"),
        proxy_key=_read_secret(Path(proxy["api_key_file"])),
        ha_mcp_url=ha.get("mcp_url"),
        ha_token=_read_secret(Path(ha["token_file"])) if ha.get("token_file") else None,
        workdir=Path(session.get("workdir", "~/Library/Application Support/AgentBridge/workspace")).expanduser(),
        state_dir=state_dir,
        reset_after_hours=float(session.get("reset_after_hours", 12)),
        idle_close_minutes=float(session.get("idle_close_minutes", 15)),
        allow_shell=bool(policy.get("allow_shell", False)),
        paths=RuntimePaths(
            claude_cli=runtimes.get("claude_cli", "claude"),
            codex_cli=runtimes.get("codex_cli", "codex"),
        ),
        extra_instructions=policy.get("extra_instructions", ""),
    )
