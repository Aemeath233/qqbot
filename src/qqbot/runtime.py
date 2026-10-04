"""QQ 消息持久化、去重和电费回复队列，两种连接共用。"""

import asyncio
import logging
import time
from contextlib import suppress

import aiohttp

from qqbot.api import QQAPIError
from qqbot.config import Settings
from qqbot.inbox import INTERRUPTED_REPLY, Inbox
from qqbot.light_commands import LightCommandRouter
from qqbot.messages import Message
from qqbot.signing import WebhookSigner

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, api, assistant):
        self.settings, self.api, self.assistant = settings, api, assistant
        self.signer = WebhookSigner(settings.app_secret)
        self.router = LightCommandRouter()
        self.inbox = Inbox(settings.db_path)
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.active: dict[str, tuple[str, bool, asyncio.Task]] = {}

    def accept_event(self, payload):
        message = Message.from_payload(
            payload, accept_full_group=self.settings.accept_group_messages
        )
        if message is not None:
            task = self.router.plan(message, llm_enabled=self.settings.llm_enabled)
            if task is not None and self.inbox.add_task(message, task):
                self.wakeup.set()

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
            await self.api.send_text(job["kind"], job["target_id"], job["message_id"], content)
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
