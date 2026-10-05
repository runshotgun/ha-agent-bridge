"""HTTP client for the Agent Bridge service."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT_S, TURN_TIMEOUT_S


class BridgeError(Exception):
    """The bridge could not be reached or returned an error."""


class BridgeAuthError(BridgeError):
    """The bridge rejected the token."""


class AgentBridgeClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str) -> Any:
        try:
            async with self._session.get(
                f"{self._url}{path}",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as response:
                if response.status == 401:
                    raise BridgeAuthError("Invalid bridge token")
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BridgeError(str(err)) from err

    async def health(self) -> dict[str, Any]:
        return await self._get("/v1/health")

    async def models(self) -> dict[str, list[str]]:
        return await self._get("/v1/models")

    async def reset(self, assistant_id: str) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{self._url}/v1/assistants/{assistant_id}/reset",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as response:
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BridgeError(str(err)) from err

    async def turn(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Post one turn and yield the NDJSON events as they arrive."""
        try:
            async with self._session.post(
                f"{self._url}/v1/turn",
                headers=self._headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=TURN_TIMEOUT_S, sock_connect=REQUEST_TIMEOUT_S),
            ) as response:
                if response.status == 401:
                    raise BridgeAuthError("Invalid bridge token")
                if response.status != 200:
                    raise BridgeError(f"Bridge returned {response.status}: {await response.text()}")
                async for line in response.content:
                    if line.strip():
                        yield json.loads(line)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BridgeError(str(err)) from err
