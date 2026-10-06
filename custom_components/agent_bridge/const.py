"""Constants for the Agent Bridge integration."""

from typing import Final

DOMAIN: Final = "agent_bridge"

CONF_RUNTIME: Final = "runtime"
CONF_MODEL: Final = "model"
CONF_PROMPT: Final = "prompt"

RUNTIMES: Final = ("claude", "codex")
DEFAULT_URL: Final = "http://host.docker.internal:8318"
DEFAULT_MODELS: Final = {"claude": "claude-sonnet-5-5", "codex": "gpt-5.6-luna"}

# A CLI turn with tools can take a while; the stream itself keeps the socket busy.
TURN_TIMEOUT_S: Final = 300
REQUEST_TIMEOUT_S: Final = 15

SERVICE_RESET_SESSION: Final = "reset_session"
SERVICE_SET_PROMPT: Final = "set_prompt"
