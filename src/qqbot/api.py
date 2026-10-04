"""QQ OpenAPI：统一域名、Access Token 缓存、明确的业务错误处理。"""

import asyncio
import base64
import logging
import math
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from qqbot.config import Settings

API_BASE = "https://api.bot.qq.com"
logger = logging.getLogger(__name__)


class QQAPIError(Exception):
    def __init__(self, status: int, code: Any = None, trace_id: Any = None):
        self.status = status
        self.code = str(code)[:32] if code is not None else None
        self.trace_id = str(trace_id)[:128] if trace_id else "-"
        self.retryable = status == 429 or status >= 500 or self.code == "100001"
        # 不输出原始响应、请求头或凭证，避免服务端错误意外带出密钥。
        super().__init__(
            f"QQ API 调用失败：HTTP={status}, code={self.code}, trace_id={self.trace_id}"
        )


class QQAPI:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession, *, base_url=API_BASE):
        self.settings = settings
        self.session = session
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()

    async def _decode(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            data = await response.json(content_type=None)
        except (ValueError, UnicodeError):
            raise QQAPIError(response.status, "invalid_json") from None
        if not isinstance(data, dict):
            raise QQAPIError(response.status, "invalid_response")
        code = next((data[k] for k in ("err_code", "code") if str(data.get(k, 0)) != "0"), None)
        if response.status >= 400 or code is not None:
            raise QQAPIError(
                response.status,
                code,
                data.get("trace_id") or response.headers.get("X-Tps-trace-ID"),
            )
        return data

    async def access_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._refresh_at:
                return self._token
            started_at = time.monotonic()
            async with self.session.post(
                f"{self.base_url}/app/getAppAccessToken",
                json={"appId": self.settings.app_id, "clientSecret": self.settings.app_secret},
            ) as response:
                data = await self._decode(response)
            token = data.get("access_token")
            try:
                expires_in = float(data["expires_in"])
            except (KeyError, ValueError, TypeError):
                raise QQAPIError(200, "invalid_token_response") from None
            if (
                not isinstance(token, str)
                or not token
                or not math.isfinite(expires_in)
                or expires_in <= 0
            ):
                raise QQAPIError(200, "invalid_token_response")
            self._token = token
            self._refresh_at = started_at + expires_in - min(30, expires_in / 2)
            return token

    async def request(self, method: str, path: str, *, payload=None) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.access_token()
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"QQBot {token}"},
                json=payload,
            ) as response:
                if response.status == 401 and attempt == 0:
                    async with self._lock:
                        if self._token == token:
                            self._refresh_at = 0
                    continue
                return await self._decode(response)
        raise AssertionError("unreachable")

    async def send_text(
        self, kind: str, target_id: str, message_id: str, content: str, *, msg_seq: int = 1
    ):
        if kind not in {"users", "groups"}:
            raise ValueError("不支持的消息场景")
        return await self.request(
            "POST",
            f"/v2/{kind}/{quote(target_id, safe='')}/messages",
            payload={"msg_type": 0, "content": content, "msg_id": message_id, "msg_seq": msg_seq},
        )

    async def send_image(
        self, kind: str, target_id: str, message_id: str, image: bytes, *, msg_seq: int = 2
    ):
        """上传 PNG，再以 QQ 富媒体消息发送；群与私聊使用各自的上传端点。"""
        if kind not in {"users", "groups"}:
            raise ValueError("不支持的图片发送场景")
        if not image or len(image) > 4 * 1024 * 1024:
            raise ValueError("图片为空或超过 4 MiB")
        target = quote(target_id, safe="")
        uploaded = await self.request(
            "POST",
            f"/v2/{kind}/{target}/files",
            payload={"file_type": 1, "file_data": base64.b64encode(image).decode("ascii"), "srv_send_msg": False},
        )
        file_info = uploaded.get("file_info")
        if not isinstance(file_info, str) or not file_info:
            raise QQAPIError(200, "invalid_media_response")
        return await self.request(
            "POST",
            f"/v2/{kind}/{target}/messages",
            payload={
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_id": message_id,
                "msg_seq": msg_seq,
            },
        )

    async def me(self):
        return await self.request("GET", "/users/@me")


class DryRunAPI:
    async def send_text(
        self, kind: str, target_id: str, message_id: str, content: str, *, msg_seq: int = 1
    ):
        logger.info("[本地模拟] %s 回复：%s", kind, content)
        return {"id": "dry-run"}

    async def send_image(
        self, kind: str, target_id: str, message_id: str, image: bytes, *, msg_seq: int = 2
    ):
        logger.info("[本地模拟] %s 发送曲线图：%d bytes", kind, len(image))
        return {"id": "dry-run"}
