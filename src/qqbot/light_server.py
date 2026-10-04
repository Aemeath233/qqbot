"""仅提供 QQ Webhook 和健康检查的轻量服务。"""

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from pathlib import Path

import aiohttp
from aiohttp import web

from qqbot.api import DryRunAPI, QQAPI, QQAPIError
from qqbot.config import Settings
from qqbot.inbox import INTERRUPTED_REPLY, Inbox, InboxFull
from qqbot.light_assistant import IMAGE_REPLY, LightAssistant
from qqbot.light_commands import LightCommandRouter
from qqbot.messages import Message
from qqbot.signing import WebhookSigner

logger = logging.getLogger(__name__)
CHALLENGE_TOKEN = re.compile(r"[A-Za-z0-9_+/=\-]{1,1024}")


class Runtime:
    def __init__(self, settings: Settings, api, assistant):
        self.settings, self.api, self.assistant = settings, api, assistant
        self.signer = WebhookSigner(settings.app_secret)
        self.router = LightCommandRouter()
        self.inbox = Inbox(settings.db_path)
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.active: dict[str, tuple[str, bool, asyncio.Task]] = {}

    async def work(self):
        try:
            while True:
                self.wakeup.clear()
                for prepared, limit in ((True, 2), (False, 4)):
                    while sum(item[1] == prepared for item in self.active.values()) < limit:
                        contexts = [item[0] for item in self.active.values()]
                        job = self.inbox.next(excluded_contexts=contexts, prepared=prepared)
                        if job is None:
                            break
                        task = asyncio.create_task(self.process(job), name="qqbot-electricity-reply")
                        self.active[job["key"]] = (job["conversation_key"], prepared, task)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
        finally:
            tasks = [item[2] for item in self.active.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _chart_path(self, content: str):
        try:
            reply = json.loads(content)
        except (ValueError, TypeError):
            reply = None
        if not isinstance(reply, dict) or reply.get("type") != IMAGE_REPLY:
            return None
        name = reply.get("path")
        if not isinstance(name, str):
            raise ValueError("曲线文件路径异常")
        root = Path("data/charts").resolve()
        image_path = Path(name).resolve()
        try:
            image_path.relative_to(root)
        except ValueError:
            raise ValueError("曲线文件路径异常") from None
        if image_path.suffix.lower() != ".png":
            raise ValueError("曲线文件路径异常")
        return image_path

    async def _send(self, job, content: str):
        try:
            reply = json.loads(content)
        except (ValueError, TypeError):
            reply = None
        if not isinstance(reply, dict) or reply.get("type") != IMAGE_REPLY:
            await self.api.send_text(job["kind"], job["target_id"], job["message_id"], content)
            return None
        text = reply.get("text")
        name = reply.get("path")
        if not isinstance(text, str) or len(text) > 1500 or not isinstance(name, str):
            raise ValueError("曲线回复格式异常")
        image_path = self._chart_path(content)
        if image_path.suffix.lower() != ".png" or not image_path.is_file():
            raise ValueError("曲线图片不存在")
        image = image_path.read_bytes()
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 4 * 1024 * 1024:
            raise ValueError("曲线图片无效或超过 4 MiB")
        await self.api.send_text(job["kind"], job["target_id"], job["message_id"], text)
        await self.api.send_image(
            job["kind"], job["target_id"], job["message_id"], image, msg_seq=2
        )
        return image_path

    async def process(self, job):
        image_path = None
        keep_image_for_retry = False
        try:
            if time.time() >= job["expires_at"]:
                self.inbox.failed(job["key"], job["attempts"], retry=False)
                return
            content = job["content"]
            if not job["prepared"]:
                if job["generation_started"]:
                    content = INTERRUPTED_REPLY
                else:
                    self.inbox.start_generation(job["key"])
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
                        content = "处理超时，执行结果可能未知；本次不会自动重试，请先核对实际状态。"
                    except Exception as exc:
                        logger.warning("单条电费消息异常：%s", type(exc).__name__)
                        content = "电费处理失败，执行结果无法确认；如需继续，请发送新消息。"
                self.inbox.save_content(job["key"], content)
            image_path = self._chart_path(content)
            if time.time() >= job["expires_at"]:
                self.inbox.failed(job["key"], job["attempts"], retry=False)
                return
            await self._send(job, content)
        except (QQAPIError, aiohttp.ClientError, TimeoutError) as exc:
            attempts = job["attempts"] + 1
            retryable = not isinstance(exc, QQAPIError) or exc.retryable
            retry = retryable and attempts < 4
            keep_image_for_retry = retry
            self.inbox.failed(job["key"], attempts, retry=retry)
            logger.warning("回复%s，尝试次数=%d：%s", "稍后重试" if retry else "失败", attempts,
                           str(exc) if isinstance(exc, QQAPIError) else type(exc).__name__)
        except Exception as exc:
            logger.error("后台任务异常：%s", type(exc).__name__)
            self.inbox.failed(job["key"], job["attempts"] + 1, retry=False)
        else:
            self.inbox.done(job["key"])
            self.router.sent_count += 1
            logger.info("已回复一条 %s 电费消息", job["kind"])
        finally:
            if image_path is not None and not keep_image_for_retry:
                with suppress(OSError):
                    image_path.unlink()
            self.active.pop(job["key"], None)
            self.wakeup.set()


RUNTIME = web.AppKey("electricity_runtime", Runtime)


async def health(request: web.Request):
    runtime = request.app[RUNTIME]
    healthy = runtime.worker is not None and not runtime.worker.done()
    return web.json_response(
        {"status": "ok" if healthy else "error", "mode": "dry-run" if runtime.settings.dry_run else "live"},
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
            isinstance(token, str) and CHALLENGE_TOKEN.fullmatch(token)
            and isinstance(event_ts, str) and 0 < len(event_ts) <= 32
            and event_ts.isascii() and event_ts.isdigit()
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
        message = Message.from_payload(
            payload, accept_full_group=runtime.settings.accept_group_messages
        )
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid message event") from None
    if message is not None:
        task = runtime.router.plan(message, llm_enabled=runtime.settings.llm_enabled)
        if task is not None:
            try:
                added = runtime.inbox.add_task(message, task)
            except InboxFull:
                raise web.HTTPServiceUnavailable(text="Inbox full") from None
            if added:
                runtime.wakeup.set()
    return web.json_response({"op": 12, "d": 0})


def create_app(settings: Settings, *, api=None, assistant=None):
    app = web.Application(client_max_size=1024 * 1024)

    async def lifespan(application):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            active_api = api if api is not None else (
                DryRunAPI() if settings.dry_run else QQAPI(settings, session)
            )
            active_assistant = assistant if assistant is not None else LightAssistant(settings, session)
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
