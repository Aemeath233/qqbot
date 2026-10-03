"""aiohttp Webhook 服务：验签、持久化确认、后台回复。"""

import asyncio
import json
import logging
import re
import time
from contextlib import suppress

import aiohttp
from aiohttp import web

from qqbot.api import QQAPI, DryRunAPI, QQAPIError
from qqbot.assistant import BotAssistant
from qqbot.commands import CommandRouter
from qqbot.config import Settings
from qqbot.inbox import Inbox, InboxFull
from qqbot.messages import Message
from qqbot.public_portal import register_portal
from qqbot.signing import WebhookSigner

logger = logging.getLogger(__name__)
CHALLENGE_TOKEN = re.compile(r"[A-Za-z0-9_+/=\-]{1,1024}")


class Runtime:
    def __init__(self, settings: Settings, api, assistant):
        self.settings = settings
        self.api = api
        self.assistant = assistant
        self.signer = WebhookSigner(settings.app_secret)
        self.router = CommandRouter()
        self.inbox = Inbox(settings.db_path)
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task | None = None

    async def work(self):
        while True:
            self.wakeup.clear()
            job = self.inbox.next()
            if job is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
                continue
            try:
                content = job["content"]
                if not job["prepared"]:
                    budget = max(0.1, min(90, job["expires_at"] - time.time()))
                    try:
                        async with asyncio.timeout(budget):
                            content = await self.assistant.generate(
                                job["task_kind"],
                                job["task_payload"],
                                job["conversation_key"],
                                request_id=job["key"],
                            )
                    except TimeoutError:
                        content = "查询或 AI 回复超时，请稍后再试。"
                    self.inbox.save_content(job["key"], content)
                if time.time() >= job["expires_at"]:
                    self.inbox.failed(job["key"], job["attempts"], retry=False)
                    continue
                await self.api.send_text(job["kind"], job["target_id"], job["message_id"], content)
            except (QQAPIError, aiohttp.ClientError, TimeoutError) as exc:
                attempts = job["attempts"] + 1
                retryable = not isinstance(exc, QQAPIError) or exc.retryable
                retry = retryable and attempts < 4
                self.inbox.failed(job["key"], attempts, retry=retry)
                detail = str(exc) if isinstance(exc, QQAPIError) else type(exc).__name__
                logger.warning(
                    "回复%s，尝试次数=%d：%s", "稍后重试" if retry else "失败", attempts, detail
                )
            else:
                self.inbox.done(job["key"])
                self.router.sent_count += 1
                logger.info("已回复一条 %s 消息", job["kind"])


RUNTIME = web.AppKey("runtime", Runtime)


async def health(request: web.Request):
    runtime = request.app[RUNTIME]
    healthy = runtime.worker is not None and not runtime.worker.done()
    return web.json_response(
        {
            "status": "ok" if healthy else "error",
            "mode": "dry-run" if runtime.settings.dry_run else "live",
        },
        status=200 if healthy else 503,
    )


async def webhook(request: web.Request):
    runtime = request.app[RUNTIME]
    if request.headers.get("X-Bot-Appid") != runtime.settings.app_id:
        raise web.HTTPUnauthorized(text="AppID mismatch")
    body = await request.read()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    # 有签名头时先验签；官方的 op=13 地址验证示例本身不带签名头。
    verified = runtime.signer.verify(body, timestamp, signature)
    if (signature or timestamp) and not verified:
        raise web.HTTPUnauthorized(text="Invalid signature")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest(text="Invalid JSON") from None
    if not isinstance(payload, dict) or type(payload.get("op")) is not int:
        raise web.HTTPBadRequest(text="Invalid payload")
    if payload["op"] == 13:
        data = payload.get("d")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Invalid challenge")
        plain_token, event_ts = data.get("plain_token"), data.get("event_ts")
        if not (
            isinstance(plain_token, str)
            # 无签名地址验证仅接受随机 token，防止借此接口为任意 JSON 事件签名。
            and CHALLENGE_TOKEN.fullmatch(plain_token) is not None
            and isinstance(event_ts, str)
            and 0 < len(event_ts) <= 32
            and event_ts.isascii()
            and event_ts.isdigit()
        ):
            raise web.HTTPBadRequest(text="Invalid challenge")
        return web.json_response(runtime.signer.challenge(plain_token, event_ts))
    if not verified:
        raise web.HTTPUnauthorized(text="Signature required")
    if payload["op"] != 0:
        raise web.HTTPBadRequest(text="Unsupported opcode")
    if runtime.worker is None or runtime.worker.done():
        raise web.HTTPServiceUnavailable(text="Worker unavailable")
    try:
        message = Message.from_payload(
            payload, accept_full_group=runtime.settings.accept_group_messages
        )
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid message event") from None
    if message is not None:
        task = runtime.router.plan(message, llm_enabled=runtime.assistant.llm_enabled)
        if task is not None:
            try:
                added = runtime.inbox.add_task(message, task)
            except InboxFull:
                raise web.HTTPServiceUnavailable(text="Inbox full") from None
            if added:
                runtime.wakeup.set()
    # 在远程发消息前确认回调，避免 API 网络延迟阻塞 QQ 的事件推送。
    return web.json_response({"op": 12, "d": 0})


def create_app(settings: Settings, *, api=None, assistant=None) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)

    async def lifespan(application: web.Application):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            active_api = (
                api
                if api is not None
                else (DryRunAPI() if settings.dry_run else QQAPI(settings, session))
            )
            active_assistant = (
                assistant if assistant is not None else BotAssistant(settings, session)
            )
            if callable(getattr(active_assistant, "start", None)):
                await active_assistant.start()
            runtime = Runtime(settings, active_api, active_assistant)
            application[RUNTIME] = runtime
            runtime.worker = asyncio.create_task(runtime.work(), name="qqbot-replies")
            logger.info("Webhook 服务已启动，回调路径 /qqbot，健康检查 /healthz")
            if settings.dry_run:
                logger.info("本地模拟模式：收到的事件只在终端输出回复")
            try:
                yield
            finally:
                runtime.worker.cancel()
                try:
                    with suppress(asyncio.CancelledError):
                        await runtime.worker
                finally:
                    runtime.inbox.close()
                    if callable(getattr(active_assistant, "close", None)):
                        await active_assistant.close()

    app.cleanup_ctx.append(lifespan)
    app.router.add_get("/healthz", health)
    app.router.add_post("/qqbot", webhook)
    register_portal(app, settings)
    return app
