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
from homeassistant.helpers.typing import ConfigType

from .client import AgentBridgeClient, BridgeAuthError, BridgeError
from .const import DOMAIN, SERVICE_RESET_SESSION

PLATFORMS = [Platform.CONVERSATION]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AgentBridgeConfigEntry = ConfigEntry[AgentBridgeClient]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the reset service once for all entries."""

    async def reset_session(call: ServiceCall) -> ServiceResponse:
        entity = er.async_get(hass).async_get(call.data[ATTR_ENTITY_ID])
        if entity is None or entity.platform != DOMAIN or entity.config_entry_id is None:
            raise ServiceValidationError("Not an Agent Bridge assistant")
        entry: AgentBridgeConfigEntry | None = hass.config_entries.async_get_entry(entity.config_entry_id)
        if entry is None or not hasattr(entry, "runtime_data"):
            raise ServiceValidationError("Agent Bridge entry is not loaded")
        try:
            return await entry.runtime_data.reset(entity.unique_id)
        except BridgeError as err:
            raise ServiceValidationError(f"Bridge error: {err}") from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_SESSION,
        reset_session,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AgentBridgeConfigEntry) -> bool:
    client = AgentBridgeClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    try:
        await client.health()
    except BridgeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except BridgeError as err:
        raise ConfigEntryNotReady(f"Agent Bridge is not reachable: {err}") from err
    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AgentBridgeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: AgentBridgeConfigEntry) -> None:
    """Reload so added, changed, or removed assistants take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
