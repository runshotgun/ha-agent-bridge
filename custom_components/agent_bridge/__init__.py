"""Agent Bridge: Home Assistant assistants backed by Claude Code and Codex CLI sessions.

Each conversation subentry is one assistant. The bridge service on the host
keeps that assistant's CLI session; this integration only forwards each turn
and streams the reply into the chat log.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .client import AgentBridgeClient, BridgeAuthError, BridgeError
from .const import CONF_MODEL, CONF_PROMPT, CONF_RUNTIME, DOMAIN, SERVICE_RESET_SESSION, SERVICE_SET_PROMPT

PLATFORMS = [Platform.CONVERSATION]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AgentBridgeConfigEntry = ConfigEntry[AgentBridgeClient]


def _assistant(hass: HomeAssistant, entity_id: str) -> tuple[AgentBridgeConfigEntry, str]:
    """The loaded entry and subentry id behind an Agent Bridge conversation entity."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != DOMAIN or entity.config_entry_id is None:
        raise ServiceValidationError("Not an Agent Bridge assistant")
    entry: AgentBridgeConfigEntry | None = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None or not hasattr(entry, "runtime_data"):
        raise ServiceValidationError("Agent Bridge entry is not loaded")
    return entry, entity.unique_id


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once for all entries."""

    async def reset_session(call: ServiceCall) -> ServiceResponse:
        entry, assistant_id = _assistant(hass, call.data[ATTR_ENTITY_ID])
        try:
            return await entry.runtime_data.reset(assistant_id)
        except BridgeError as err:
            raise ServiceValidationError(f"Bridge error: {err}") from err

    async def set_prompt(call: ServiceCall) -> None:
        """Replace an assistant's prompt. The bridge calls this when an assistant edits its
        own instructions; admin-only because the prompt steers every tool it has."""
        entry, assistant_id = _assistant(hass, call.data[ATTR_ENTITY_ID])
        subentry = entry.subentries[assistant_id]
        hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, CONF_PROMPT: call.data[CONF_PROMPT]})

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SESSION,
        reset_session,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_PROMPT,
        set_prompt,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id, vol.Required(CONF_PROMPT): cv.string}),
    )
    return True


def _structure(entry: AgentBridgeConfigEntry) -> tuple:
    """Everything that needs a reload: connection and the assistants' identity and model.
    Prompts are left out; the entities read them live each turn."""
    assistants = sorted(
        (sid, sub.subentry_type, sub.title, sub.data.get(CONF_RUNTIME), sub.data.get(CONF_MODEL))
        for sid, sub in entry.subentries.items()
    )
    return (tuple(sorted(entry.data.items())), tuple(assistants))


_loaded_structure: dict[str, tuple] = {}


async def async_setup_entry(hass: HomeAssistant, entry: AgentBridgeConfigEntry) -> bool:
    client = AgentBridgeClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    try:
        await client.health()
    except BridgeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except BridgeError as err:
        raise ConfigEntryNotReady(f"Agent Bridge is not reachable: {err}") from err
    entry.runtime_data = client
    _loaded_structure[entry.entry_id] = _structure(entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AgentBridgeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: AgentBridgeConfigEntry) -> None:
    """Reload so added, changed, or removed assistants take effect. A prompt-only change
    skips the reload, so an assistant can edit its prompt in the middle of a turn."""
    if _loaded_structure.get(entry.entry_id) != _structure(entry):
        await hass.config_entries.async_reload(entry.entry_id)
