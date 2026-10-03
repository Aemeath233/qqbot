"""完美校园剩余电量查询；请求目标与学校配置均由服务器决定。"""

import asyncio
import json
import secrets
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import aiohttp

from qqbot.commands import SHANGHAI
from qqbot.config import Settings
from qqbot.dorms import DormDirectory, DormError

ENDPOINT = Settings.electricity_endpoint
ORIGIN = "https://cloudpaygateway.59wanmei.com:8087"


def generate_idserial() -> str:
    return datetime.now(SHANGHAI).strftime("%y%m%d") + str(secrets.randbelow(900000) + 100000)


class ElectricityError(Exception):
    def __init__(
        self,
        message: str,
        code: str = "query_failed",
        candidates: list[dict] | None = None,
        *,
        retry_after_seconds: float = 0,
    ):
        self.code = code
        self.candidates = candidates or []
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class ElectricityClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession, *, endpoint=None):
        self.settings = settings
        self.session = session
        self.endpoint = endpoint or settings.electricity_endpoint
        self.directory: DormDirectory | None = None
        self.cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def query(self, dormitory: str, area: str = "") -> dict:
        if not self.settings.electricity_enabled or self.settings.dry_run:
            raise ElectricityError("电费查询尚未启用，请联系管理员。", "disabled")
        try:
            if self.directory is None:
                self.directory = DormDirectory.load(self.settings.electricity_map_path)
            room = self.directory.resolve(dormitory, area or self.settings.electricity_default_area)
        except DormError as exc:
            raise ElectricityError(str(exc), exc.code, exc.candidates) from None
        cache_key = json.dumps([room.area, room.roomverify])
        async with self._lock:
            now = time.monotonic()
            self.cache = {key: value for key, value in self.cache.items() if now - value[0] < 30}
            if cache_key in self.cache:
                return {**self.cache[cache_key][1], "cached": True}
            from qqbot.electricity_probe import ProbeError, RequestGate

            gate = None
            try:
                gate = RequestGate(self.settings.electricity_cooldown_path)
                try:
                    gate.reserve()
                except ProbeError as exc:
                    raise ElectricityError(str(exc), "cooldown") from None
                try:
                    result = await self._query(room.label, room.roomverify)
                except ElectricityError as exc:
                    if exc.code == "rate_limited":
                        gate.defer(exc.retry_after_seconds)
                    raise
            except (OSError, sqlite3.Error):
                raise ElectricityError(
                    "电费请求冷却记录暂时不可用，已停止继续查询。", "cooldown_unavailable"
                ) from None
            finally:
                if gate is not None:
                    gate.close()
            result["area"] = room.area
            result["area_name"] = room.area_name
            self.cache[cache_key] = (time.monotonic(), result)
            return dict(result)

    async def _query(self, dormitory: str, roomverify: str) -> dict:
        inner = {
            "payproid": self.settings.electricity_pay_project,
            "schoolcode": self.settings.electricity_school_code,
            "roomverify": roomverify,
            "businesstype": 2,
            "idserial": generate_idserial(),
        }
        body = {
            "timestamp": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            "method": "samllProgramGetRoomState",
            "bizcontent": json.dumps(inner, ensure_ascii=False, separators=(",", ":")),
            "sourceId": 1,
        }
        referer = f"{ORIGIN}/pay/index.html"
        if self.settings.electricity_token:
            referer += "?" + urlencode({"token": self.settings.electricity_token})
        headers = {
            "Origin": ORIGIN,
            "Referer": referer,
            "Accept": "application/json",
            "X-Requested-With": "com.eg.android.AlipayGphone",
        }
        if self.settings.electricity_cookie:
            headers["Cookie"] = self.settings.electricity_cookie
        if self.settings.electricity_tapp_id:
            headers["x-mass-tappid"] = self.settings.electricity_tapp_id
        try:
            async with self.session.post(
                self.endpoint,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    from qqbot.electricity_probe import throttle_delay

                    raise ElectricityError(
                        "电费接口正在限流，请等待冷却后重试。",
                        "rate_limited",
                        retry_after_seconds=throttle_delay(response.headers.get("Retry-After")),
                    )
                if response.status in {401, 403}:
                    raise ElectricityError(
                        "电费查询会话失效，请联系管理员检查登录状态。", "auth_failed"
                    )
                if response.status != 200:
                    raise ElectricityError("电费服务暂时不可用，请稍后重试。", "upstream_error")
                try:
                    data = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    raise ElectricityError(
                        "电费服务返回异常，请稍后重试。", "invalid_response"
                    ) from None
        except (aiohttp.ClientError, TimeoutError):
            raise ElectricityError(
                "电费查询网络超时或连接失败，请稍后重试。", "network_error"
            ) from None
        return {
            "ok": True,
            "dormitory": dormitory,
            "remaining_kwh": parse_electricity_quantity(data),
            "unit": "度",
            "queried_at": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            "cached": False,
            "_observed_at": time.time(),
        }


def parse_electricity_quantity(data: object) -> str:
    """机器人与诊断脚本共用的业务状态、单位和电量校验。"""
    if not isinstance(data, dict) or data.get("returncode") != "SUCCESS":
        raise ElectricityError("电费服务查询失败，请核对宿舍或联系管理员。", "business_error")
    business = data.get("businessData")
    if not isinstance(business, dict) or business.get("quantityunit") != "度":
        raise ElectricityError("电费服务返回的电量格式异常，请联系管理员。", "invalid_quantity")
    quantity = business.get("quantity")
    try:
        if isinstance(quantity, bool) or not isinstance(quantity, (str, int, float)):
            raise ValueError
        if len(str(quantity)) > 32:
            raise ValueError
        amount = Decimal(str(quantity))
        if (
            not amount.is_finite()
            or not 0 <= amount <= 10000000
            or abs(amount.as_tuple().exponent) > 32
        ):
            raise ValueError
    except (ValueError, InvalidOperation):
        raise ElectricityError(
            "电费服务返回的电量数值异常，请联系管理员。", "invalid_quantity"
        ) from None
    return format(amount, "f")


def format_electricity(result: dict) -> str:
    if not result.get("ok"):
        return result.get("message", "电费查询失败，请稍后重试。")
    source = "（30 秒内缓存）" if result.get("cached") else ""
    return (
        f"宿舍 {result['dormitory']} 剩余电量：{result['remaining_kwh']} 度{source}\n"
        + (f"区域：{result['area_name']}\n" if result.get("area_name") else "")
        + f"查询时间：{result['queried_at']}（北京时间）"
        + ("\n" + result["history_warning"] if result.get("history_warning") else "")
    )
