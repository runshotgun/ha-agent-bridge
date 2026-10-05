"""Claude Code runtime through the Claude Agent SDK.

One ClaudeSDKClient is one live `claude` process in stream-json mode. It loads
the user's settings, skills, and MCP servers (setting_sources=["user"]), and
talks to the model through the proxy (ANTHROPIC_BASE_URL). Secrets go through
the environment only, never the command line.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    StreamEvent,
)

from ..config import Config
from ..models import AssistantSpec
from . import SessionNotFound

_LOGGER = logging.getLogger(__name__)

# Tools that write files or run commands. Voice sessions route such work to T3.
WRITE_TOOLS = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"]
_NOT_FOUND = ("no conversation found", "session not found")


class ClaudeSession:
    def __init__(self, config: Config, spec: AssistantSpec, session_id: str, resume: bool) -> None:
        self.session_id = session_id
        self.signature = (spec.model, spec.full_instructions(config.extra_instructions))
        env = {
            "ANTHROPIC_BASE_URL": config.proxy_base_url,
            "ANTHROPIC_AUTH_TOKEN": config.proxy_key,
            "ANTHROPIC_API_KEY": "",
        }
        mcp_servers: dict = {}
        if config.ha_mcp_url and config.ha_token:
            # Claude expands ${VAR} in MCP headers, so the token stays out of argv.
            env["AGENT_BRIDGE_HA_TOKEN"] = config.ha_token
            mcp_servers["homeassistant"] = {
                "type": "http",
                "url": config.ha_mcp_url,
                "headers": {"Authorization": "Bearer ${AGENT_BRIDGE_HA_TOKEN}"},
            }
        self._options = ClaudeAgentOptions(
            cli_path=config.paths.claude_cli,
            cwd=str(config.workdir),
            model=spec.model,
            resume=session_id if resume else None,
            session_id=None if resume else session_id,
            setting_sources=["user"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": self.signature[1]},
            mcp_servers=mcp_servers,
            env=env,
            permission_mode="bypassPermissions",
            disallowed_tools=[] if config.allow_shell else WRITE_TOOLS,
            include_partial_messages=True,
        )
        self._client: ClaudeSDKClient | None = None

    async def _ensure_connected(self) -> ClaudeSDKClient:
        if self._client is None:
            client = ClaudeSDKClient(options=self._options)
            try:
                await client.connect()
            except ClaudeSDKError as err:
                raise _translate(err) from err
            self._client = client
        return self._client

    async def turn(self, prompt: str) -> AsyncIterator[str]:
        """Send one user message and yield reply text as it streams."""
        client = await self._ensure_connected()
        streamed = False
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, StreamEvent):
                    event = message.event
                    if event.get("type") == "content_block_start" and streamed:
                        if event.get("content_block", {}).get("type") == "text":
                            yield "\n\n"
                    elif event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            streamed = True
                            yield delta["text"]
                elif isinstance(message, ResultMessage):
                    if message.session_id:
                        self.session_id = message.session_id
                    if message.is_error:
                        raise RuntimeError(f"Claude turn failed: {message.result or message.subtype}")
                    if not streamed and message.result:
                        yield message.result
        except ClaudeSDKError as err:
            raise _translate(err) from err

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - closing must never raise
                _LOGGER.debug("Claude disconnect failed", exc_info=True)


def _translate(err: Exception) -> Exception:
    """Map the CLI's 'cannot resume' failure to SessionNotFound."""
    text = str(err).lower()
    return SessionNotFound(str(err)) if any(marker in text for marker in _NOT_FOUND) else err
