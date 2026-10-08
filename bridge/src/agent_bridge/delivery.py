"""Deliver a background task's result to the user through Home Assistant.

Spoken on the satellite the request came from; requests without a satellite (typed in
the app) and quiet hours go to a notify service instead, and a failed announcement
falls back to it too, so a result is never lost silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
import logging
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)


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

    async def send(self, satellite_id: str, message: str) -> None:
        now = datetime.now(ZoneInfo(self.timezone)).time()
        service, data = choose(satellite_id, now, self.quiet_hours, self.notify_service)
        if await self._call(service, {**data, "message": message}) or service == self.notify_service:
            return
        fallback, data = choose("", now, self.quiet_hours, self.notify_service)
        await self._call(fallback, {**data, "message": message})

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
