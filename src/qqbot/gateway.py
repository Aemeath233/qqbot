"""QQ WebSocket：Access Token 鉴权、心跳、会话恢复和限速重连。"""

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import aiohttp

from qqbot.api import QQAPI, QQAPIError
from qqbot.inbox import InboxFull

logger = logging.getLogger(__name__)
GROUP_AND_C2C_INTENT = 1 << 25
FATAL_CLOSE_CODES = {
    4001: "网关拒绝了操作码",
    4002: "网关拒绝了消息格式",
    4004: "QQ 鉴权失败，请核对 AppID 和 AppSecret",
    4010: "网关拒绝了分片参数",
    4011: "该机器人需要多分片连接，当前精简版只支持单分片",
    4012: "网关拒绝了协议版本",
    4013: "网关拒绝了事件订阅参数",
    4014: "机器人没有群聊或单聊事件权限，请检查 QQ 平台权限",
    4914: "机器人未上线，请检查 QQ 平台的测试环境和机器人状态",
    4915: "机器人已被平台封禁，请先在 QQ 平台处理",
}


class GatewayFatalError(Exception):
    """凭证、权限或协议错误，需要修正后再启动。"""


class GatewayReconnect(Exception):
    def __init__(self, reason: str, *, delay: float = 0):
        super().__init__(reason)
        self.delay = delay


class Gateway:
    def __init__(self, api: QQAPI, on_event: Callable[[dict], None]):
        self.api = api
        self.on_event = on_event
        self.session_id: str | None = None
        self.sequence: int | None = None
        self.ready = False
        self._backoff = 5.0

    def reset_session(self):
        self.session_id = None
        self.sequence = None

    async def run(self):
        while True:
            self.ready = False
            minimum_delay = 0.0
            try:
                await self.connect()
            except GatewayReconnect as exc:
                reason, minimum_delay = str(exc), exc.delay
            except QQAPIError as exc:
                if not exc.retryable:
                    raise GatewayFatalError(
                        f"无法获取 QQ WebSocket 接入凭证或地址：{exc}。"
                        "请检查应用凭证、服务器 IP 白名单和平台连接权限。"
                    ) from None
                reason = str(exc)
                minimum_delay = 60 if exc.status == 429 else 0
            except aiohttp.WSServerHandshakeError as exc:
                reason = f"WebSocket 握手 HTTP={exc.status}"
                minimum_delay = 60 if exc.status == 429 else 0
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                # 不记录原始异常、URL、帧或 Token，避免平台数据带出密钥。
                reason = type(exc).__name__
            except InboxFull:
                reason = "消息队列已满，保留序号等待恢复"
                minimum_delay = 15
            finally:
                self.ready = False
            delay = max(minimum_delay, self._backoff) + random.uniform(0, 1)
            logger.warning("QQ WebSocket 断开（%s），%.1f 秒后重连", reason, delay)
            await asyncio.sleep(delay)
            self._backoff = min(self._backoff * 2, 60)

    async def connect(self, *, check_only: bool = False):
        self.ready = False
        gateway = await self.api.gateway()
        url = gateway.get("url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            parsed = None
        if (
            not parsed
            or parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.fragment
        ):
            raise GatewayFatalError("QQ 返回了无效的 WSS 地址，未发送鉴权数据。")
        token = await self.api.access_token()
        try:
            await self.connect_socket(url, token, check_only=check_only)
        except aiohttp.WSServerHandshakeError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise GatewayFatalError(
                    f"QQ 拒绝了 WebSocket 握手（HTTP={exc.status}）；"
                    "请检查应用连接权限、IP 白名单及平台是否要求 Webhook。"
                ) from None
            raise

    async def connect_socket(self, url, token, *, check_only):
        async with self.api.session.ws_connect(
            url,
            timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5),
            max_msg_size=1024 * 1024,
        ) as socket:
            hello = await self.receive(socket, wait_seconds=15)
            data = hello.get("d")
            interval_ms = data.get("heartbeat_interval") if isinstance(data, dict) else None
            if (
                hello["op"] != 10
                or type(interval_ms) not in {int, float}
                or not math.isfinite(interval_ms)
                or not 0 < interval_ms <= 300_000
            ):
                raise GatewayReconnect("未收到有效的 Hello 心跳周期")
            interval = interval_ms / 1000
            resuming = bool(self.session_id and self.sequence is not None)
            if resuming:
                await socket.send_json(
                    {
                        "op": 6,
                        "d": {
                            "token": f"QQBot {token}",
                            "session_id": self.session_id,
                            "seq": self.sequence,
                        },
                    }
                )
            else:
                await socket.send_json(
                    {
                        "op": 2,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": GROUP_AND_C2C_INTENT,
                            "shard": [0, 1],
                        },
                    }
                )
            logger.info("QQ WebSocket 已建立连接，等待%s", "恢复会话" if resuming else "鉴权")
            next_heartbeat = time.monotonic() + interval
            ack_deadline = None
            auth_deadline = time.monotonic() + 20
            while True:
                now = time.monotonic()
                if not self.ready and now >= auth_deadline:
                    raise GatewayReconnect("QQ 鉴权响应超时")
                if ack_deadline is not None and now >= ack_deadline:
                    raise GatewayReconnect("未收到心跳 ACK")
                if now >= next_heartbeat:
                    await socket.send_json({"op": 1, "d": self.sequence})
                    ack_deadline = now + interval
                    next_heartbeat = now + interval
                deadlines = [next_heartbeat]
                if ack_deadline is not None:
                    deadlines.append(ack_deadline)
                if not self.ready:
                    deadlines.append(auth_deadline)
                try:
                    payload = await self.receive(
                        socket, wait_seconds=max(0.001, min(deadlines) - time.monotonic())
                    )
                except TimeoutError:
                    continue
                op = payload["op"]
                if op == 11:
                    ack_deadline = None
                elif op == 1:
                    await socket.send_json({"op": 1, "d": self.sequence})
                    if ack_deadline is None:
                        ack_deadline = time.monotonic() + interval
                elif op == 7:
                    raise GatewayReconnect("QQ 要求重连")
                elif op == 9:
                    self.reset_session()
                    if resuming:
                        raise GatewayReconnect("会话失效，将重新鉴权")
                    raise GatewayFatalError(
                        "QQ 拒绝了 WebSocket 鉴权；请检查应用凭证、群聊/单聊权限，"
                        "以及该应用是否已切换为仅 Webhook 接收。"
                    )
                elif op == 0:
                    event = payload.get("t")
                    if event == "READY":
                        ready_data = payload.get("d")
                        session_id = (
                            ready_data.get("session_id") if isinstance(ready_data, dict) else None
                        )
                        if not isinstance(session_id, str) or not session_id:
                            raise GatewayReconnect("READY 缺少会话 ID")
                        self.session_id = session_id
                        self.ready = True
                        self._backoff = 5.0
                        logger.info("QQ WebSocket 鉴权成功，已订阅群 @ 消息和私聊消息")
                    elif event == "RESUMED":
                        self.ready = True
                        self._backoff = 5.0
                        logger.info("QQ WebSocket 会话已恢复")
                    else:
                        try:
                            self.on_event(payload)
                        except (ValueError, TypeError):
                            logger.warning("忽略格式无效的 QQ 消息事件")
                    # 消息持久化成功后才推进序号；队列满时留给 RESUME 补发。
                    sequence = payload.get("s")
                    if type(sequence) is int and sequence >= 0:
                        self.sequence = sequence
                    if check_only and self.ready:
                        return

    async def receive(self, socket, *, wait_seconds):
        async with asyncio.timeout(wait_seconds):
            frame = await socket.receive()
        if frame.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
            try:
                payload = json.loads(frame.data)
            except (ValueError, UnicodeError, RecursionError):
                raise GatewayReconnect("网关数据不是有效 JSON") from None
            if not isinstance(payload, dict) or type(payload.get("op")) is not int:
                raise GatewayReconnect("网关消息格式无效")
            return payload
        code = socket.close_code
        if code in FATAL_CLOSE_CODES:
            raise GatewayFatalError(f"{FATAL_CLOSE_CODES[code]}（网关关闭码 {code}）。")
        if code in {4006, 4007} or (code is not None and 4900 <= code <= 4913):
            self.reset_session()
        raise GatewayReconnect(f"网关连接关闭，code={code}", delay=30 if code == 4008 else 0)
