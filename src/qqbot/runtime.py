"""QQ 消息持久化、去重和电费回复队列，两种连接共用。"""

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import replace

import aiohttp

from qqbot.api import QQAPIError
from qqbot.commands import ReplyTask
from qqbot.config import Settings
from qqbot.dorm_state import DormState, UserContext
from qqbot.inbox import INTERRUPTED_REPLY, Inbox, InboxFull
from qqbot.interactions import ButtonClick
from qqbot.light_commands import LightCommandRouter
from qqbot.messages import Message
from qqbot.presentation import Reply, help_reply
from qqbot.signing import WebhookSigner

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, api, assistant):
        self.settings, self.api, self.assistant = settings, api, assistant
        self.signer = WebhookSigner(settings.app_secret)
        self.router = LightCommandRouter()
        self.inbox = Inbox(settings.db_path)
        self.state = DormState(self.inbox.db, settings.app_id)
        if assistant is not None:
            assistant.state = self.state
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.active: dict[str, tuple[str, bool, asyncio.Task]] = {}

    def accept_event(self, payload):
        if payload.get("t") == "INTERACTION_CREATE":
            click = ButtonClick.parse(payload, self.settings.app_id)
            if click is None:
                return None
            context = UserContext(
                click.message.kind, click.message.target_id, click.message.sender_id
            )
            try:
                action = self.state.action(click.data, context)
            except PermissionError:
                return click.ack_id, 4
            if action is None:
                task = ReplyTask("text", "按钮已过期或不可用，请重新查询宿舍电量后再操作。")
                code = 1
            else:
                task = ReplyTask(
                    "button_" + action["action"],
                    json.dumps(
                        {
                            "dormitory": action["dormitory"],
                            "area": action["area"],
                        }
                    ),
                )
                code = 0
            try:
                added = self.inbox.add_task(click.message, task)
            except InboxFull:
                return click.ack_id, 2
            if added:
                self.wakeup.set()
            return click.ack_id, code
        message = Message.from_payload(
            payload, accept_full_group=self.settings.accept_group_messages
        )
        if message is not None:
            task = self.router.plan(message, llm_enabled=self.settings.llm_enabled)
            if task is not None and self.inbox.add_task(message, task):
                self.wakeup.set()

    async def handle_event(self, payload):
        ack = self.accept_event(payload)
        if ack is not None:
            try:
                async with asyncio.timeout(3):
                    await self.api.acknowledge_interaction(*ack)
            except (QQAPIError, aiohttp.ClientError, TimeoutError) as exc:
                logger.warning(
                    "按钮事件确认失败：%s",
                    str(exc) if isinstance(exc, QQAPIError) else type(exc).__name__,
                )

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
                        task = asyncio.create_task(
                            self.process(job), name="qqbot-electricity-reply"
                        )
                        self.active[job["key"]] = (job["conversation_key"], prepared, task)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
        finally:
            tasks = [item[2] for item in self.active.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def process(self, job):
        try:
            if time.time() >= job["expires_at"]:
                self.inbox.failed(job["key"], job["attempts"], retry=False)
                return
            content = Reply.loads(job["reply_json"]) if job["reply_json"] else job["content"]
            if not job["prepared"]:
                if job["generation_started"]:
                    content = INTERRUPTED_REPLY
                else:
                    self.inbox.start_generation(job["key"])
                    budget = max(0.1, min(90, job["expires_at"] - time.time()))
                    try:
                        async with asyncio.timeout(budget):
                            context = UserContext(job["kind"], job["target_id"], job["sender_id"])
                            kind = job["task_kind"]
                            if kind.startswith("button_"):
                                args = json.loads(job["task_payload"])
                                if kind == "button_query":
                                    content = await self.assistant.query_electricity(
                                        args["dormitory"], args["area"], context=context
                                    )
                                elif kind == "button_bind":
                                    content = self.state.bind_once(
                                        context, args["dormitory"], args["area"]
                                    )
                                elif kind == "button_help":
                                    content = help_reply()
                                else:
                                    content = "这个按钮暂不可用，请重新查询。"
                            else:
                                content = await self.assistant.generate(
                                    kind, job["task_payload"], context=context
                                )
                    except TimeoutError:
                        content = "处理超时，执行结果可能未知；本次不会自动重试，请先核对实际状态。"
                    except Exception as exc:
                        logger.warning("单条电费消息异常：%s", type(exc).__name__)
                        content = "电费处理失败，执行结果无法确认；如需继续，请发送新消息。"
                self.inbox.save_content(job["key"], content)
            if time.time() >= job["expires_at"]:
                self.inbox.failed(job["key"], job["attempts"], retry=False)
                return
            await self.send_reply(job, content)
        except (QQAPIError, aiohttp.ClientError, TimeoutError) as exc:
            attempts = job["attempts"] + 1
            retryable = not isinstance(exc, QQAPIError) or exc.retryable
            retry = retryable and attempts < 4
            self.inbox.failed(job["key"], attempts, retry=retry)
            logger.warning(
                "回复%s，尝试次数=%d：%s",
                "稍后重试" if retry else "失败",
                attempts,
                str(exc) if isinstance(exc, QQAPIError) else type(exc).__name__,
            )
        except Exception as exc:
            logger.error("后台任务异常：%s", type(exc).__name__)
            self.inbox.failed(job["key"], job["attempts"] + 1, retry=False)
        else:
            self.inbox.done(job["key"])
            logger.info("已回复一条 %s 电费消息", job["kind"])
        finally:
            self.active.pop(job["key"], None)
            self.wakeup.set()

    async def send_reply(self, job, content):
        reply = content if isinstance(content, Reply) else Reply(content)
        reference = job["reference"]
        while reply.markdown and getattr(self.api, "markdown_enabled", True):
            try:
                await self.api.send_markdown(
                    job["kind"],
                    job["target_id"],
                    job["message_id"],
                    reply.markdown,
                    keyboard=reply.keyboard,
                    reference=reference,
                )
                logger.info(
                    "电费回复使用 Markdown%s",
                    "和回调按钮" if reply.keyboard and self.api.buttons_enabled else "",
                )
                return
            except QQAPIError as exc:
                if exc.retryable:
                    raise
                if reply.keyboard and exc.code in {
                    "305007",
                    "40034029",
                    "40034106",
                    "40034108",
                    "40034109",
                }:
                    self.api.buttons_enabled = False
                    reply = replace(reply, keyboard=None)
                    logger.warning("QQ 拒绝按钮消息（code=%s），改为仅发送 Markdown", exc.code)
                elif exc.code in {
                    "304036",
                    "40034127",
                    "40034124",
                    "40034011",
                    "40034008",
                    "40034009",
                    "40034010",
                    "22006",
                    "304061",
                    "11253",
                }:
                    self.api.markdown_enabled = False
                    self.api.buttons_enabled = False
                    reply = Reply(reply.text)
                    logger.warning("QQ 拒绝 Markdown（code=%s），改为发送纯文本", exc.code)
                else:
                    raise
                self.inbox.save_content(job["key"], reply)
        options = {"reference": reference} if reference != "msg_id" else {}
        await self.api.send_text(
            job["kind"], job["target_id"], job["message_id"], reply.text, **options
        )
