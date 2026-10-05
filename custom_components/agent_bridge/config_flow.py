"""Config flow: one entry per bridge, one subentry per assistant.

The assistant form has two steps: pick the runtime (Claude or Codex), then a
model from the list the bridge reads from the proxy. Free text is allowed, so
a new model works before the list knows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME, CONF_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import AgentBridgeClient, BridgeAuthError, BridgeError
from .const import CONF_MODEL, CONF_PROMPT, CONF_RUNTIME, DEFAULT_MODELS, DEFAULT_URL, DOMAIN, RUNTIMES


async def _validate(hass, url: str, token: str) -> dict[str, str]:
    try:
        await AgentBridgeClient(async_get_clientsession(hass), url, token).health()
    except BridgeAuthError:
        return {"base": "invalid_auth"}
    except BridgeError:
        return {"base": "cannot_connect"}
    return {}


class AgentBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_URL: user_input[CONF_URL]})
            errors = await _validate(self.hass, user_input[CONF_URL], user_input[CONF_TOKEN])
            if not errors:
                return self.async_create_entry(title="Agent Bridge", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_URL, default=DEFAULT_URL): str,
                vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate(self.hass, entry.data[CONF_URL], user_input[CONF_TOKEN])
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {"conversation": AssistantSubentryFlow}


class AssistantSubentryFlow(ConfigSubentryFlow):
    """Add or change one assistant."""

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_runtime(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        self._data = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_runtime(user_input)

    async def async_step_runtime(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        if user_input is not None:
            if user_input[CONF_RUNTIME] != self._data.get(CONF_RUNTIME):
                self._data.pop(CONF_MODEL, None)
            self._data.update(user_input)
            return await self.async_step_options()
        schema: dict[Any, Any] = {}
        if self._is_new:
            schema[vol.Required(CONF_NAME)] = str
        schema[vol.Required(CONF_RUNTIME, default=self._data.get(CONF_RUNTIME, "claude"))] = SelectSelector(
            SelectSelectorConfig(options=list(RUNTIMES), translation_key="runtime", mode=SelectSelectorMode.LIST)
        )
        return self.async_show_form(step_id="runtime", data_schema=vol.Schema(schema))

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        runtime = self._data[CONF_RUNTIME]
        if user_input is not None:
            self._data.update(user_input)
            name = self._data.pop(CONF_NAME, None)
            if self._is_new:
                return self.async_create_entry(title=name, data=self._data)
            return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(), data=self._data)
        try:
            models = (await self._get_entry().runtime_data.models()).get(runtime, [])
        except BridgeError:
            return self.async_abort(reason="cannot_connect")
        default_model = self._data.get(CONF_MODEL) or DEFAULT_MODELS[runtime]
        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema({
                vol.Required(CONF_MODEL, default=default_model): SelectSelector(
                    SelectSelectorConfig(options=models or [default_model], custom_value=True, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(CONF_PROMPT, default=self._data.get(CONF_PROMPT, "")): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }),
        )
