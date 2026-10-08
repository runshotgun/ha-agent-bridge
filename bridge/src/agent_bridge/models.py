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
- Each message starts with a context line from Home Assistant. Do not read it aloud.
- Work that takes more than about 15 seconds (an investigation, waiting for a T3 \
thread) goes to background_task: say in one short sentence that you are on it and \
will report back, then stop. The result reaches the user by itself, and you see it \
at the start of their next message.
- Your own instructions from Home Assistant are yours to maintain with the \
assistant_prompt tools. Change them when the user asks, or when you learn a lasting \
rule (a preference, or a mistake not to repeat). Keep changes small and say what you \
changed. They apply from the next message.
"""

# allow_shell decides which of these joins the voice block. A spoken "yes" is the
# approval: on a voice channel the agent cannot know who else is in the room.
FILES_BLOCKED = """\
- Do not edit files here. For changes to code or to a machine, use the T3 Code \
tools when they are available, and ask the user to confirm before you start, \
send to, or stop a thread."""
FILES_ALLOWED = """\
- You can read and change files and run commands on this computer. Before any change \
(a file edit, a command that changes something, an install, a restart), say in one \
sentence what you will do and ask for a yes. Act only after the user says yes in \
the next message. Reading and looking up need no confirmation. For code projects \
on Keven's other machines, use the T3 Code tools."""


@dataclass(frozen=True)
class AssistantSpec:
    """What Home Assistant sends with every turn for one assistant."""

    assistant_id: str
    runtime: str
    model: str
    instructions: str
    entity_id: str = ""

    def full_instructions(self, extra: str = "", allow_shell: bool = False) -> str:
        files = FILES_ALLOWED if allow_shell else FILES_BLOCKED
        parts = [VOICE_INSTRUCTIONS.rstrip() + "\n" + files, extra.strip(), self.instructions.strip()]
        return "\n\n".join(part for part in parts if part)


def format_turn(text: str, context: dict[str, str]) -> str:
    """Prefix the user's words with one context line (time, person, device, area)."""
    pairs = "; ".join(f"{key}: {value}" for key, value in context.items() if value)
    return f"[Home Assistant | {pairs}]\n{text}" if pairs else text
