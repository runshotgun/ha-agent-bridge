"""Shared request types and the instructions every voice session gets."""

from __future__ import annotations

from dataclasses import dataclass

RUNTIMES = ("claude", "codex")

# The CLI also loads the user's own instruction files (CLAUDE.md, AGENTS.md).
# Those are written for coding chats, so this block states what changes for
# a spoken channel and takes precedence on output format.
VOICE_INSTRUCTIONS = """\
You are a voice assistant in Home Assistant. Text to speech reads your reply aloud.
- Answer in one to three short spoken sentences unless the user asks for more.
- Use no markdown, lists, headings, code, links, or emojis.
- Do not say what you are about to do or which tool you use. Give only the answer.
- Output format rules in the user's instruction files are for written chats \
(sections, bullet summaries). They do not apply in this channel.
- Use the homeassistant MCP tools to read or control devices in the home.
- Use your skills and MCP servers when they help answer.
- Do not edit files here. For changes to code or to a machine, use the T3 Code \
tools when they are available, and ask the user to confirm before you start, \
send to, or stop a thread.
- Each message starts with a context line from Home Assistant. Do not read it aloud.
"""


@dataclass(frozen=True)
class AssistantSpec:
    """What Home Assistant sends with every turn for one assistant."""

    assistant_id: str
    runtime: str
    model: str
    instructions: str

    def full_instructions(self, extra: str = "") -> str:
        parts = [VOICE_INSTRUCTIONS, extra.strip(), self.instructions.strip()]
        return "\n\n".join(part for part in parts if part)


def format_turn(text: str, context: dict[str, str]) -> str:
    """Prefix the user's words with one context line (time, person, device, area)."""
    pairs = "; ".join(f"{key}: {value}" for key, value in context.items() if value)
    return f"[Home Assistant | {pairs}]\n{text}" if pairs else text
