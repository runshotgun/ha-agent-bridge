# Agent Bridge

Use Claude Code and Codex as Home Assistant assistants. Each assistant is a
real CLI session, so it has your skills, your MCP servers, and memory of the
conversation.

The project has two parts:

- **Bridge service** (`bridge/`): runs on the computer that has the `claude`
  and `codex` CLIs. It keeps one CLI session for each assistant and sends
  model traffic through a proxy such as
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI).
- **Home Assistant integration** (`custom_components/agent_bridge`): installs
  through HACS. Each assistant is a conversation agent that you can use in a
  voice pipeline.

## How a conversation works

1. Home Assistant sends the user's words and a short context line (time,
   person, device, area) to the bridge.
2. The bridge sends the words to the assistant's CLI session and streams the
   reply back. Home Assistant can start speaking before the reply is complete.
3. The session continues across messages. After 12 hours with no message, the
   next message starts a new session. The `agent_bridge.reset_session` action
   starts a new session at once.
4. The CLI process closes after 15 idle minutes. The next message resumes the
   same session.

Claude sessions use the Claude Agent SDK (`claude` in stream-json mode).
Codex sessions use `codex app-server`. Both load the user's settings, skills,
and MCP servers.

## Safety

- The bridge listens on `127.0.0.1` only and requires a bearer token.
- Voice sessions can use MCP tools. They cannot run shell commands or edit
  files unless you set `allow_shell = true`.
- Secrets are files with mode 0600. They go to the CLIs through environment
  variables, never through command-line arguments.

## Install the bridge (macOS)

Requirements: [uv](https://docs.astral.sh/uv/), the `claude` and `codex` CLIs,
and a proxy that serves Claude and OpenAI models.

1. Run `bridge/install.sh` from your login shell.
2. Put the proxy client key in
   `~/Library/Application Support/AgentBridge/secrets/proxy.key`.
3. Optional: to give sessions Home Assistant tools, enable the Home Assistant
   **Model Context Protocol Server** integration. Put a long-lived access
   token in `secrets/ha-mcp.token` and set `mcp_url` in `config.toml`.
4. Run `bridge/install.sh` again to start the LaunchAgent.

The log is `~/Library/Application Support/AgentBridge/logs/bridge.log`.

## Install the integration

1. In HACS, add this repository as a custom repository (type: Integration).
2. Install **Agent Bridge** and restart Home Assistant.
3. Add the integration. URL: `http://host.docker.internal:8318` for Home
   Assistant in Docker Desktop on the same computer. Token: the contents of
   `secrets/bridge.token`.
4. Add an assistant: choose Claude Code or Codex, a model, and optional
   instructions.
5. Select the assistant as the conversation agent of a voice pipeline.

## Bridge API

All routes require `Authorization: Bearer <token>`.

| Route | Purpose |
| --- | --- |
| `GET /v1/health` | Version and status |
| `GET /v1/models` | Proxy models grouped by CLI |
| `GET /v1/sessions` | Stored sessions and whether a process is live |
| `POST /v1/turn` | Run one turn; streams NDJSON `delta`, then `done` or `error` |
| `POST /v1/assistants/{id}/reset` | Forget the assistant's session |

## Development

```sh
cd bridge
uv run --python 3.12 --group dev pytest -q
```
