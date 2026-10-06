# Assistants can edit their own prompt

## Decisions (Keven, 2026-10-06)

- Storage: the prompt field of the assistant in Home Assistant (one source of truth,
  visible and editable in the UI).
- When: on request, and on the agent's own initiative when it learns a lasting rule;
  it tells the user what it changed. Every old version is kept for undo.
- Scope: every Agent Bridge assistant (Wabadi, Claude, Codex); each edits only its own.

## Design

- HA integration owns the prompt. New admin service `agent_bridge.set_prompt`
  (entity_id, prompt) updates the subentry. Prompt-only changes do not reload the
  entry: the entity reads the live subentry each turn, so a change made during a
  turn cannot cut that turn off.
- Bridge: remembers each assistant's last spec (HA sends the prompt every turn),
  keeps history in `state/prompts/<assistant>.jsonl`, and writes through the HA
  service with its HA token. HTTP: `GET/POST /v1/assistants/{id}/prompt`,
  `GET .../prompt/history`, `POST .../prompt/undo`.
- Tools: a stdio MCP server (`python -m agent_bridge.prompt_mcp`) started for every
  session with that session's assistant id; tools `prompt_read`, `prompt_edit`,
  `prompt_add`, `prompt_history`, `prompt_undo`. Claude gets it in `mcp_servers`,
  Codex through the thread `config` overrides.
- The change applies from the next turn: a new prompt changes the session
  signature, and the bridge resumes the same conversation with it.

## Release

Bridge 0.2.0 (install.sh) and integration 0.2.0 (GitHub release, HACS update, HA
restart). Pushing and releasing need Keven's approval.
