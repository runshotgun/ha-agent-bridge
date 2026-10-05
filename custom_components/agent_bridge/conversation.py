"""Conversation entities: one per assistant subentry.

History lives in the CLI session on the bridge, not in Home Assistant's chat
log, so each turn sends only the new text plus a short context line (time,
person, device, area). The reply streams into the chat log so TTS can start
before the turn ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging
from typing import Any, Literal, override

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import AgentBridgeConfigEntry
from .client import BridgeError
from .const import CONF_MODEL, CONF_PROMPT, CONF_RUNTIME, DOMAIN

_LOGGER = logging.getLogger(__name__)
_MAKER = {"claude": "Anthropic (Claude Code)", "codex": "OpenAI (Codex)"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AgentBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == "conversation":
            async_add_entities(
                [AgentBridgeConversationEntity(entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )


class AgentBridgeConversationEntity(conversation.ConversationEntity, conversation.AbstractConversationAgent):
    """An assistant whose memory is one CLI session on the bridge."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: AgentBridgeConfigEntry, subentry: ConfigSubentry) -> None:
        self.entry = entry
        self.subentry = subentry
        runtime = subentry.data[CONF_RUNTIME]
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer=_MAKER.get(runtime, runtime),
            model=subentry.data[CONF_MODEL],
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    @override
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    @override
    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    @override
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        payload = {
            "assistant_id": self.subentry.subentry_id,
            "runtime": self.subentry.data[CONF_RUNTIME],
            "model": self.subentry.data[CONF_MODEL],
            "instructions": self.subentry.data.get(CONF_PROMPT, ""),
            "text": user_input.text,
            "context": await self._turn_context(user_input),
        }
        async for _content in chat_log.async_add_delta_content_stream(self.entity_id, self._stream(payload)):
            pass
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[conversation.AssistantContentDeltaDict]:
        yield {"role": "assistant"}
        try:
            async for event in self.entry.runtime_data.turn(payload):
                if event["type"] == "delta":
                    yield {"content": event["text"]}
                elif event["type"] == "error":
                    raise HomeAssistantError(f"The assistant failed: {event.get('message')}")
        except BridgeError as err:
            _LOGGER.error("Agent Bridge request failed: %s", err)
            raise HomeAssistantError(f"Could not reach Agent Bridge: {err}") from err

    async def _turn_context(self, user_input: conversation.ConversationInput) -> dict[str, str]:
        """Who is speaking, from where, and when: the facts a CLI cannot know."""
        context = {
            "time": dt_util.now().strftime("%A %Y-%m-%d %H:%M %Z"),
            "language": user_input.language,
        }
        if user_input.context.user_id and (user := await self.hass.auth.async_get_user(user_input.context.user_id)):
            context["person"] = user.name or ""
        if user_input.device_id and (device := dr.async_get(self.hass).async_get(user_input.device_id)):
            context["device"] = device.name_by_user or device.name or ""
            if device.area_id and (area := ar.async_get(self.hass).async_get_area(device.area_id)):
                context["area"] = area.name
        if user_input.extra_system_prompt:
            context["note"] = user_input.extra_system_prompt
        return context
