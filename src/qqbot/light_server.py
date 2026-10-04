"""仅提供 QQ Webhook 和健康检查的轻量服务。"""

import asyncio
import json
import logging
import re
from contextlib import suppress

import aiohttp
from aiohttp import web

from qqbot.api import QQAPI
from qqbot.config import Settings
from qqbot.inbox import InboxFull
from qqbot.light_assistant import LightAssistant
from qqbot.runtime import Runtime

logger = logging.getLogger(__name__)
CHALLENGE_TOKEN = re.compile(r"[A-Za-z0-9_+/=\-]{1,1024}")


RUNTIME = web.AppKey("electricity_runtime", Runtime)


async def health(request: web.Request):
    runtime = request.app[RUNTIME]
    healthy = runtime.worker is not None and not runtime.worker.done()
    return web.json_response(
        {"status": "ok" if healthy else "error"},
        status=200 if healthy else 503,
    )


async def webhook(request: web.Request):
    runtime = request.app[RUNTIME]
    if request.headers.get("X-Bot-Appid") != runtime.settings.app_id:
        raise web.HTTPUnauthorized(text="AppID mismatch")
    body = await request.read()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    verified = runtime.signer.verify(body, timestamp, signature)
    if (signature or timestamp) and not verified:
        raise web.HTTPUnauthorized(text="Invalid signature")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise web.HTTPBadRequest(text="Invalid JSON") from None
    if not isinstance(payload, dict) or type(payload.get("op")) is not int:
        raise web.HTTPBadRequest(text="Invalid payload")
    if payload["op"] == 13:
        data = payload.get("d")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Invalid challenge")
        token, event_ts = data.get("plain_token"), data.get("event_ts")
        if not (
            isinstance(token, str)
            and CHALLENGE_TOKEN.fullmatch(token)
            and isinstance(event_ts, str)
            and 0 < len(event_ts) <= 32
            and event_ts.isascii()
            and event_ts.isdigit()
        ):
            raise web.HTTPBadRequest(text="Invalid challenge")
        return web.json_response(runtime.signer.challenge(token, event_ts))
    if not verified:
        raise web.HTTPUnauthorized(text="Signature required")
    if payload["op"] != 0:
        raise web.HTTPBadRequest(text="Unsupported opcode")
    if runtime.worker is None or runtime.worker.done():
        raise web.HTTPServiceUnavailable(text="Worker unavailable")
    try:
        await runtime.handle_event(payload)
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid message event") from None
    except InboxFull:
        raise web.HTTPServiceUnavailable(text="Inbox full") from None
    return web.json_response({"op": 12, "d": 0})


def create_app(settings: Settings):
    app = web.Application(client_max_size=1024 * 1024)

    async def lifespan(application):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            active_api = QQAPI(settings, session)
            active_assistant = LightAssistant(settings, session)
            runtime = Runtime(settings, active_api, active_assistant)
            application[RUNTIME] = runtime
            runtime.worker = asyncio.create_task(runtime.work(), name="qqbot-electricity-worker")
            logger.info("电费机器人已启动，Webhook /qqbot，健康检查 /healthz")
            try:
                yield
            finally:
                runtime.worker.cancel()
                with suppress(asyncio.CancelledError):
                    await runtime.worker
                runtime.inbox.close()

    app.cleanup_ctx.append(lifespan)
    app.router.add_get("/healthz", health)
    app.router.add_post("/qqbot", webhook)
    return app
