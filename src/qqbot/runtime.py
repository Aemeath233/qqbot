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
from qqbot.dorm_state import DormState, UserContext, identity_tag
from qqbot.inbox import INTERRUPTED_REPLY, Inbox, InboxFull
from qqbot.interactions import ButtonClick
from qqbot.light_commands import LightCommandRouter
from qqbot.messages import Message
from qqbot.operation_log import OperationLog, error_location, operation_trace
from qqbot.presentation import Reply, help_reply
from qqbot.signing import WebhookSigner

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, api, assistant):
        self.settings, self.api, self.assistant = settings, api, assistant
        self.signer = WebhookSigner(settings.app_secret)
        self.router = LightCommandRouter()
        self.inbox = Inbox(settings.db_path)
        self.audit = OperationLog(self.inbox.db)
        self.state = DormState(self.inbox.db, settings.app_id, audit=self.audit)
        discarded = self.inbox.discard_legacy_button_jobs()
        if discarded:
            logger.info("已停止 %d 个旧版按钮任务，请使用新查询结果的按钮", discarded)
        if assistant is not None:
            assistant.state = self.state
            assistant.audit = self.audit
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.worker_error: Exception | None = None
        self.active: dict[str, tuple[str, bool, asyncio.Task]] = {}
        from qqbot import __version__

        self.audit.record(
            "startup",
            version=__version__,
            app=identity_tag(settings.app_id),
            transport=settings.transport,
            llm_enabled=settings.llm_enabled,
            markdown=settings.markdown_enabled,
            buttons=settings.buttons_enabled,
        )

    def record(self, stage, item, **details):
        if isinstance(item, Message):
            context = UserContext(item.kind, item.target_id, item.sender_id)
            key = item.key
        else:
            context = UserContext(item["kind"], item["target_id"], item["sender_id"])
            key = item["key"]
        return self.audit.record(stage, context, trace=identity_tag(key), **details)

    def accept_event(self, payload):
        if payload.get("t") == "INTERACTION_CREATE":
            try:
                click = ButtonClick.parse(payload, self.settings.app_id)
            except (ValueError, TypeError):
                self.audit.record("event_invalid", event="interaction")
                raise
            if click is None:
                return None
            context = UserContext(
                click.message.kind, click.message.target_id, click.message.sender_id
            )
            parts = click.data.split(":")
            hint = parts[2] if len(parts) == 4 and parts[:2] == ["electricity", "v2"] else ""
            if hint not in {"query", "bind", "help", "confirm_bind", "cancel_bind"}:
                hint = "unknown"
            self.record(
                "button_received",
                click.message,
                callback=identity_tag(click.ack_id),
                button=identity_tag(click.button_id) if click.button_id else "missing",
                data=identity_tag(click.data),
                data_action=hint,
                platform_time=click.occurred_at,
                **self.state.button_evidence(click.data),
            )
            receipt = self.audit.remember_callback(click)
            self.record("callback_receipt", click.message, result=receipt)
            if receipt == "conflict":
                self.record(
                    "button_rejected",
                    click.message,
                    reason="callback_id_reused_with_changed_fields",
                    code=4,
                )
                return click.ack_id, 4
            try:
                action = self.state.action(click.data, context, button_id=click.button_id)
            except PermissionError:
                self.record(
                    "button_rejected", click.message, reason="origin_or_id_mismatch", code=4
                )
                logger.warning(
                    "拒绝不匹配按钮：群=%s，用户=%s",
                    identity_tag(context.target_id),
                    identity_tag(context.sender_id),
                )
                return click.ack_id, 4
            if action is None:
                # 无法验证来源时仅确认失败，不在未经验证的群里执行或发送绑定结果。
                self.record("button_rejected", click.message, reason="expired_or_unknown", code=1)
                return click.ack_id, 1
            task = ReplyTask(
                "button_" + action["action"],
                json.dumps(
                    {
                        "version": 2,
                        "action": action["action"],
                        "dormitory": action["dormitory"],
                        "area": action["area"],
                        "request_id": action["request_id"],
                        "origin_kind": action["kind"],
                        "origin_target": action["target_id"],
                        "origin_sender": context.sender_id,
                    }
                ),
            )
            logger.info(
                "收到按钮：动作=%s，群=%s，用户=%s，按钮=%s",
                action["action"],
                identity_tag(context.target_id),
                identity_tag(context.sender_id),
                identity_tag(action["button_id"]),
            )
            code = 0
            try:
                added = self.inbox.add_task(click.message, task)
            except InboxFull:
                self.record("queue_full", click.message, action=action["action"])
                return click.ack_id, 2
            self.record(
                "task_queued" if added else "task_not_queued",
                click.message,
                task=task.kind,
                reason=""
                if added
                else ("expired" if click.message.expires_at <= time.time() else "duplicate"),
                dormitory=action["dormitory"],
                request=identity_tag(action["request_id"]) if action["request_id"] else "",
            )
            if added:
                self.wakeup.set()
            return click.ack_id, code if added or click.message.expires_at > time.time() else 1
        try:
            message = Message.from_payload(
                payload, accept_full_group=self.settings.accept_group_messages
            )
        except (ValueError, TypeError):
            self.audit.record("event_invalid", event="message")
            raise
        if message is not None:
            context = UserContext(message.kind, message.target_id, message.sender_id)
            pending = self.state.latest_request(context)
            decision = message.content.strip().casefold()
            # 不把群里随口说的“是/确认”当成有副作用的绑定授权。
            yes = {"确认更换", "确认绑定"}
            no = {"否", "no", "取消", "取消更换", "取消绑定", "不更换"}
            if pending is not None and decision in yes | no:
                task = ReplyTask(
                    "binding_decision",
                    json.dumps(
                        {
                            "request_id": pending["request_id"],
                            "confirm": decision in yes,
                        }
                    ),
                )
            else:
                task = self.router.plan(message, llm_enabled=self.settings.llm_enabled)
            logger.info(
                "收到消息：场景=%s，群=%s，用户=%s",
                message.kind,
                identity_tag(message.target_id),
                identity_tag(message.sender_id),
            )
            self.record("message_received", message, event=payload["t"])
            if task is not None:
                try:
                    added = self.inbox.add_task(message, task)
                except InboxFull:
                    self.record("queue_full", message, task=task.kind)
                    raise
                self.record(
                    "task_queued" if added else "task_not_queued",
                    message,
                    task=task.kind,
                    reason=""
                    if added
                    else ("expired" if message.expires_at <= time.time() else "duplicate"),
                )
                if added:
                    self.wakeup.set()

    async def handle_event(self, payload):
        ack = self.accept_event(payload)
        if ack is not None:
            click = ButtonClick.parse(payload, self.settings.app_id)
            trace = identity_tag(click.message.key)
            try:
                async with asyncio.timeout(3):
                    await self.api.acknowledge_interaction(*ack)
                self.audit.record(
                    "interaction_ack", trace=trace, callback=identity_tag(ack[0]), code=ack[1]
                )
            except (QQAPIError, aiohttp.ClientError, TimeoutError) as exc:
                self.audit.record(
                    "interaction_ack_failed",
                    trace=trace,
                    callback=identity_tag(ack[0]),
                    error=type(exc).__name__,
                    code=exc.code if isinstance(exc, QQAPIError) else None,
                )
                logger.warning(
                    "按钮事件确认失败：%s",
                    str(exc) if isinstance(exc, QQAPIError) else type(exc).__name__,
                )

    async def work(self):
        try:
            while True:
                if self.worker_error is not None:
                    raise RuntimeError("消息任务异常退出，停止服务以避免漏记操作") from None
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
                        task.add_done_callback(self.task_finished)
                        self.active[job["key"]] = (job["conversation_key"], prepared, task)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
        finally:
            tasks = [item[2] for item in self.active.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def task_finished(self, task):
        if not task.cancelled() and (exc := task.exception()) is not None:
            self.worker_error = exc
            logger.error("消息任务未正常完成，停止工作队列：%s", type(exc).__name__)
            self.wakeup.set()

    async def process(self, job):
        trace_token = operation_trace.set(identity_tag(job["key"]))
        try:
            self.record(
                "task_started",
                job,
                task=job["task_kind"],
                attempt=job["attempts"] + 1,
                prepared=bool(job["prepared"]),
            )
            if time.time() >= job["expires_at"]:
                self.inbox.failed(job["key"], job["attempts"], retry=False)
                self.record("task_expired", job)
                return
            content = Reply.loads(job["reply_json"]) if job["reply_json"] else job["content"]
            if not job["prepared"]:
                if job["generation_started"]:
                    self.record("generation_not_replayed", job)
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
                                valid_origin = (
                                    isinstance(args, dict)
                                    and args.get("version") == 2
                                    and args.get("origin_kind") == context.kind
                                    and args.get("origin_target") == context.target_id
                                    and args.get("origin_sender") == context.sender_id
                                    and isinstance(args.get("action"), str)
                                    and "button_" + args["action"] == kind
                                )
                                if not valid_origin:
                                    self.inbox.failed(job["key"], job["attempts"] + 1, retry=False)
                                    logger.error("按钮任务来源不一致，未执行或发送回复")
                                    self.record("task_rejected", job, reason="task_origin_mismatch")
                                    return
                                if kind == "button_query":
                                    content = await self.assistant.query_electricity(
                                        args["dormitory"], args["area"], context=context
                                    )
                                elif kind == "button_bind":
                                    content = self.state.request_binding(
                                        context, args["dormitory"], args["area"]
                                    )
                                elif kind in {"button_confirm_bind", "button_cancel_bind"}:
                                    content = self.state.finish_binding(
                                        context,
                                        args["request_id"],
                                        confirm=kind == "button_confirm_bind",
                                    )
                                elif kind == "button_help":
                                    content = help_reply()
                                else:
                                    content = "这个按钮暂不可用，请重新查询。"
                            elif kind == "binding_decision":
                                args = json.loads(job["task_payload"])
                                content = self.state.finish_binding(
                                    context, args["request_id"], confirm=args["confirm"]
                                )
                            else:
                                content = await self.assistant.generate(
                                    kind, job["task_payload"], context=context
                                )
                    except TimeoutError:
                        self.record("generation_failed", job, error="TimeoutError")
                        content = "处理超时，执行结果可能未知；本次不会自动重试，请先核对实际状态。"
                    except Exception as exc:
                        self.record(
                            "generation_failed",
                            job,
                            error=type(exc).__name__,
                            location=error_location(exc),
                        )
                        logger.warning("单条电费消息异常：%s", type(exc).__name__)
                        content = "电费处理失败，执行结果无法确认；如需继续，请发送新消息。"
                self.inbox.save_content(job["key"], content)
            if time.time() >= job["expires_at"]:
                self.inbox.failed(job["key"], job["attempts"], retry=False)
                self.record("task_expired", job)
                return
            await self.send_reply(job, content)
        except (QQAPIError, aiohttp.ClientError, TimeoutError) as exc:
            attempts = job["attempts"] + 1
            retryable = not isinstance(exc, QQAPIError) or exc.retryable
            retry = retryable and attempts < 4
            self.inbox.failed(job["key"], attempts, retry=retry)
            self.record(
                "reply_failed",
                job,
                error=type(exc).__name__,
                retry=retry,
                attempt=attempts,
                code=exc.code if isinstance(exc, QQAPIError) else None,
                http=exc.status if isinstance(exc, QQAPIError) else None,
            )
            logger.warning(
                "回复%s，尝试次数=%d：%s",
                "稍后重试" if retry else "失败",
                attempts,
                str(exc) if isinstance(exc, QQAPIError) else type(exc).__name__,
            )
        except Exception as exc:
            logger.error("后台任务异常：%s", type(exc).__name__)
            self.inbox.failed(job["key"], job["attempts"] + 1, retry=False)
            self.record(
                "task_failed",
                job,
                error=type(exc).__name__,
                location=error_location(exc),
            )
        else:
            self.inbox.done(job["key"])
            self.record("task_done", job, task=job["task_kind"])
            logger.info("已回复一条 %s 电费消息", job["kind"])
        finally:
            operation_trace.reset(trace_token)
            self.active.pop(job["key"], None)
            self.wakeup.set()

    async def send_reply(self, job, content):
        reply = content if isinstance(content, Reply) else Reply(content)
        if reply.keyboard:
            context = UserContext(job["kind"], job["target_id"], job["sender_id"])
            buttons = [
                button for row in reply.keyboard["content"]["rows"] for button in row["buttons"]
            ]
            try:
                valid = all(
                    self.state.action(button["action"]["data"], context, button_id=button["id"])
                    is not None
                    for button in buttons
                )
            except PermissionError:
                self.record("reply_rejected", job, reason="keyboard_origin_mismatch")
                logger.error("回复按钮来源不符，未发送：群=%s", identity_tag(context.target_id))
                raise
            if not valid:
                self.record("reply_keyboard_removed", job, reason="expired_or_unknown")
                reply = replace(reply, keyboard=None)
                self.inbox.save_content(job["key"], reply)
        reference = job["reference"]
        while reply.markdown and getattr(self.api, "markdown_enabled", True):
            try:
                keyboard = reply.keyboard if self.api.buttons_enabled else None
                self.record(
                    "reply_attempt",
                    job,
                    format="markdown",
                    keyboard=bool(keyboard),
                    reference=job["reference"],
                    buttons=[
                        identity_tag(button["id"])
                        for row in keyboard["content"]["rows"]
                        for button in row["buttons"]
                    ]
                    if keyboard
                    else [],
                )
                response = await self.api.send_markdown(
                    job["kind"],
                    job["target_id"],
                    job["message_id"],
                    reply.markdown,
                    keyboard=keyboard,
                    reference=reference,
                )
                sent_id = response.get("id") if isinstance(response, dict) else None
                self.record(
                    "reply_sent",
                    job,
                    format="markdown",
                    message=identity_tag(sent_id) if isinstance(sent_id, str) else "",
                    keyboard=bool(keyboard),
                )
                logger.info(
                    "电费回复使用 Markdown%s：群=%s，用户=%s",
                    "和回调按钮" if reply.keyboard and self.api.buttons_enabled else "",
                    identity_tag(job["target_id"]),
                    identity_tag(job["sender_id"]),
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
                self.record("reply_fallback", job, code=exc.code, markdown=bool(reply.markdown))
        options = {"reference": reference} if reference != "msg_id" else {}
        self.record("reply_attempt", job, format="text", reference=job["reference"])
        response = await self.api.send_text(
            job["kind"], job["target_id"], job["message_id"], reply.text, **options
        )
        sent_id = response.get("id") if isinstance(response, dict) else None
        self.record(
            "reply_sent",
            job,
            format="text",
            message=identity_tag(sent_id) if isinstance(sent_id, str) else "",
        )
        logger.info(
            "电费文本回复：群=%s，用户=%s",
            identity_tag(job["target_id"]),
            identity_tag(job["sender_id"]),
        )
