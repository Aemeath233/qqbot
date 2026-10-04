"""主动连接 QQ，不监听 HTTP 端口，不需要公网回调地址。"""

import asyncio
import logging

import aiohttp

from qqbot.api import QQAPI
from qqbot.gateway import Gateway
from qqbot.light_assistant import LightAssistant
from qqbot.runtime import Runtime

logger = logging.getLogger(__name__)


async def serve_websocket(settings):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        api = QQAPI(settings, session)
        runtime = Runtime(settings, api, LightAssistant(settings, session))
        gateway = Gateway(api, runtime.handle_event)
        runtime.worker = asyncio.create_task(runtime.work(), name="qqbot-electricity-worker")
        connection = asyncio.create_task(gateway.run(), name="qqbot-websocket")
        logger.info("电费机器人使用 WebSocket，无需配置域名或消息回调")
        tasks = {runtime.worker, connection}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            raise RuntimeError("QQ 消息工作任务意外结束")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            runtime.inbox.close()
