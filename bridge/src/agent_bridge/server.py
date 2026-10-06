"""HTTP API for the Home Assistant integration.

POST /v1/turn streams NDJSON events: {"type":"delta"}, then {"type":"done"} or
{"type":"error"}. Every route needs the bridge bearer token. The service binds
to loopback; Home Assistant in Docker reaches it as host.docker.internal.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from . import __version__
from .config import Config
from .manager import AssistantManager, drain
from .models import RUNTIMES, AssistantSpec
from .prompts import PromptError, PromptStore

_LOGGER = logging.getLogger(__name__)
CONFIG_KEY = web.AppKey("config", Config)
MANAGER_KEY = web.AppKey("manager", AssistantManager)
PROMPTS_KEY = web.AppKey("prompts", PromptStore)


@web.middleware
async def _auth(request: web.Request, handler):
    expected = f"Bearer {request.app[CONFIG_KEY].bridge_token}"
    if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
    return await handler(request)


def _spec_from(body: dict[str, Any]) -> AssistantSpec:
    runtime = body.get("runtime")
    if runtime not in RUNTIMES:
        raise web.HTTPBadRequest(text=f"runtime must be one of {RUNTIMES}")
    if not body.get("assistant_id") or not body.get("model"):
        raise web.HTTPBadRequest(text="assistant_id and model are required")
    return AssistantSpec(body["assistant_id"], runtime, body["model"], body.get("instructions", ""),
                         body.get("entity_id", ""))


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": __version__})


async def models(request: web.Request) -> web.Response:
    """List proxy models grouped by the CLI that can use them."""
    config = request.app[CONFIG_KEY]
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        async with session.get(
            f"{config.proxy_base_url}/v1/models",
            headers={"Authorization": f"Bearer {config.proxy_key}"},
        ) as response:
            response.raise_for_status()
            data = await response.json()
    ids = sorted(item["id"] for item in data.get("data", []))
    return web.json_response({
        "claude": [m for m in ids if m.startswith("claude-")],
        "codex": [m for m in ids if m.startswith("gpt-") and not m.startswith("gpt-image")],
    })


async def turn(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    spec = _spec_from(body)
    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="text is required")
    context = {str(k): str(v) for k, v in (body.get("context") or {}).items()}
    request.app[PROMPTS_KEY].remember(spec.assistant_id, spec.entity_id, spec.instructions)

    queue, task = request.app[MANAGER_KEY].stream_turn(spec, text, context)
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await response.prepare(request)

    async def send(event: dict[str, Any]) -> None:
        await response.write((json.dumps(event) + "\n").encode())

    try:
        async for delta in drain(queue):
            await send({"type": "delta", "text": delta})
        result = await task
        await send({"type": "done", "session_id": result.session_id, "new_session": result.new_session})
    except (ConnectionResetError, ConnectionError) as err:
        # Client left. The turn task keeps running to the end on its own.
        _LOGGER.info("Client disconnected during turn for %s: %s", spec.assistant_id, err)
        return response
    except Exception as err:  # noqa: BLE001 - report every turn failure to the caller
        _LOGGER.exception("Turn failed for %s", spec.assistant_id)
        await send({"type": "error", "message": str(err) or type(err).__name__})
    await response.write_eof()
    return response


async def reset(request: web.Request) -> web.Response:
    assistant_id = request.match_info["assistant_id"]
    had_session = await request.app[MANAGER_KEY].reset(assistant_id)
    return web.json_response({"assistant_id": assistant_id, "had_session": had_session})


def _prompt_error(err: PromptError) -> web.Response:
    return web.json_response({"error": str(err)}, status=409)


async def prompt_get(request: web.Request) -> web.Response:
    try:
        return web.json_response({"prompt": request.app[PROMPTS_KEY].read(request.match_info["assistant_id"])})
    except PromptError as err:
        return _prompt_error(err)


async def prompt_change(request: web.Request) -> web.Response:
    """Body: {"op": "add"|"edit"|"undo", "text", "old", "reason"}."""
    body = await request.json()
    try:
        prompt = await request.app[PROMPTS_KEY].change(
            request.match_info["assistant_id"], body.get("op", ""), body.get("reason", ""),
            text=body.get("text", ""), old=body.get("old", ""))
    except PromptError as err:
        return _prompt_error(err)
    return web.json_response({"prompt": prompt})


async def prompt_history(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "5"))
    return web.json_response({"history": request.app[PROMPTS_KEY].history(request.match_info["assistant_id"], limit)})


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[MANAGER_KEY].status())


def build_app(config: Config) -> web.Application:
    app = web.Application(middlewares=[_auth])
    app[CONFIG_KEY] = config
    app[MANAGER_KEY] = AssistantManager(config)
    app[PROMPTS_KEY] = PromptStore(config.state_dir, config.ha_url, config.ha_token)

    async def lifecycle(app: web.Application):
        await app[MANAGER_KEY].start()
        yield
        await app[MANAGER_KEY].stop()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/v1/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/v1/sessions", status)
    app.router.add_post("/v1/turn", turn)
    app.router.add_post("/v1/assistants/{assistant_id}/reset", reset)
    app.router.add_get("/v1/assistants/{assistant_id}/prompt", prompt_get)
    app.router.add_post("/v1/assistants/{assistant_id}/prompt", prompt_change)
    app.router.add_get("/v1/assistants/{assistant_id}/prompt/history", prompt_history)
    return app
