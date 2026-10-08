"""Deliver a background task's result to the user through Home Assistant.

Spoken on the satellite the request came from (not in quiet hours). Without a satellite,
the HA event `agent_bridge_result` lets a client that started the conversation (a
push-to-talk app) speak it; it must answer `agent_bridge_result_ack` within ACK_SECONDS.
Everything else, and every failure, goes to the notify service, so a result is never
lost silently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time
import logging
import uuid
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)
ACK_SECONDS = 15


def in_quiet_hours(now: time, quiet: str | None) -> bool:
    """quiet is "HH:MM-HH:MM"; a range past midnight (22:00-07:00) wraps."""
    if not quiet:
        return False
    start, end = (time.fromisoformat(part.strip()) for part in quiet.split("-"))
    return start <= now < end if start <= end else now >= start or now < end


def choose(satellite_id: str, now: time, quiet: str | None, notify_service: str | None) -> tuple[str, dict]:
    """(service, data) for one result: announce on the satellite, else notify."""
    if satellite_id and not in_quiet_hours(now, quiet):
        return "assist_satellite.announce", {"entity_id": satellite_id}
    if notify_service:
        return notify_service, {"title": "Wabadi"}
    return "persistent_notification.create", {"title": "Wabadi"}


@dataclass
class Delivery:
    ha_url: str | None
    ha_token: str | None
    notify_service: str | None
    quiet_hours: str | None
    timezone: str = "UTC"

    async def send(self, satellite_id: str, message: str, conversation_id: str = "", summary: str = "") -> None:
        now = datetime.now(ZoneInfo(self.timezone)).time()
        service, data = choose(satellite_id, now, self.quiet_hours, self.notify_service)
        if service == "assist_satellite.announce" and await self._call(service, {**data, "message": message}):
            return
        if not satellite_id and conversation_id and await self._claimed(conversation_id, summary, message):
            return
        fallback, data = choose("", now, self.quiet_hours, self.notify_service)
        await self._call(fallback, {**data, "message": message})

    async def _claimed(self, conversation_id: str, summary: str, message: str) -> bool:
        """Fire agent_bridge_result; True when a client acknowledges it in time."""
        if not (self.ha_url and self.ha_token):
            return False
        result_id = uuid.uuid4().hex
        try:
            async with ClientSession(timeout=ClientTimeout(total=ACK_SECONDS + 15)) as session, session.ws_connect(
                f"{self.ha_url}/api/websocket"
            ) as ws:
                await ws.receive_json()
                await ws.send_json({"type": "auth", "access_token": self.ha_token})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    return False
                await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "agent_bridge_result_ack"})
                await ws.send_json({"id": 2, "type": "fire_event", "event_type": "agent_bridge_result", "event_data": {
                    "result_id": result_id, "conversation_id": conversation_id, "summary": summary, "message": message}})
                async with asyncio.timeout(ACK_SECONDS):
                    while True:
                        event = (await ws.receive_json()).get("event") or {}
                        if (event.get("data") or {}).get("result_id") == result_id:
                            _LOGGER.info("Result %s claimed by a client", result_id)
                            return True
        except TimeoutError:
            _LOGGER.info("No client claimed result %s; using the fallback", result_id)
        except Exception:  # noqa: BLE001 - fall back to notify on any failure
            _LOGGER.exception("Could not offer result %s to clients", result_id)
        return False

    async def _call(self, service: str, data: dict) -> bool:
        if not (self.ha_url and self.ha_token):
            _LOGGER.error("No Home Assistant URL and token; result not delivered: %s", data.get("message"))
            return False
        domain, name = service.split(".", 1)
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session, session.post(
                f"{self.ha_url}/api/services/{domain}/{name}",
                headers={"Authorization": f"Bearer {self.ha_token}"}, json=data,
            ) as response:
                if response.status < 400:
                    return True
                _LOGGER.warning("%s refused the result (%s): %s", service, response.status, (await response.text())[:200])
        except Exception:  # noqa: BLE001 - delivery must try the fallback, never raise
            _LOGGER.exception("Delivery through %s failed", service)
        return False
