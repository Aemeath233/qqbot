"""一次运行只发一个只读请求；跨进程冷却，无重试、重定向或目录遍历。"""

import argparse
import asyncio
import json
import math
import os
import secrets
import socket
import sqlite3
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import getproxies

import aiohttp
from dotenv import load_dotenv

from qqbot.commands import SHANGHAI
from qqbot.dorms import DormDirectory, DormError
from qqbot.electricity import ORIGIN, ElectricityError, parse_electricity_quantity

COOLDOWN_SECONDS = 60
THROTTLED_SECONDS = 600
TIMEOUT_SECONDS = 12
MAX_RESPONSE_BYTES = 65536
DATA_DIR = Path("data/electricity-test")


class ProbeError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeConfig:
    school_code: str = "1402"
    pay_project: int = 953
    map_path: Path | None = None
    area: str = ""
    token: str = field(default="", repr=False)
    cookie: str = field(default="", repr=False)
    tapp_id: str = field(default="", repr=False)

    @classmethod
    def load(cls):
        # 不检查 QQ/LLM 配置，也不要求开启机器人的 ELECTRICITY_ENABLED。
        load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig")
        school = os.getenv("ELECTRICITY_SCHOOL_CODE", "1402").strip()
        try:
            project = int(os.getenv("ELECTRICITY_PAY_PROJECT", "953"))
        except ValueError:
            raise ProbeError("ELECTRICITY_PAY_PROJECT 必须是正整数。") from None
        if not school.isascii() or not school.isdigit() or len(school) > 20 or project <= 0:
            raise ProbeError("请检查学校代码和缴费项目编号。")
        map_path = os.getenv("ELECTRICITY_MAP_PATH", "").strip()
        credentials = [
            os.getenv(key, "").strip()
            for key in ("ELECTRICITY_TOKEN", "ELECTRICITY_COOKIE", "ELECTRICITY_TAPP_ID")
        ]
        if any("\r" in value or "\n" in value or len(value) > 16384 for value in credentials):
            raise ProbeError("电费会话配置不能包含换行或超长请求头。")
        return cls(
            school,
            project,
            Path(map_path) if map_path else None,
            os.getenv("ELECTRICITY_DEFAULT_AREA", "").strip(),
            *credentials,
        )


@dataclass(frozen=True)
class ProbePlan:
    endpoint: str
    method: str
    payload: dict = field(repr=False)
    headers: dict = field(repr=False)
    proxy_mode: str = "direct"
    dormitory: str = ""


def make_plan(config: ProbeConfig, args: argparse.Namespace) -> ProbePlan:
    origin = "https://cloudpaygateway.59wanmei.com"
    if args.port == 8087:
        origin += ":8087"
    endpoint = origin + "/paygateway/smallpaygateway/trade"
    serial = datetime.now(SHANGHAI).strftime("%y%m%d") + str(secrets.randbelow(900000) + 100000)
    label = ""
    if args.dormitory:
        if config.school_code != "1402" and config.map_path is None:
            raise ProbeError("其他学校查询宿舍必须配置 ELECTRICITY_MAP_PATH。")
        room = DormDirectory.load(config.map_path).resolve(args.dormitory, args.area or config.area)
        label = room.label
        method = "samllProgramGetRoomState"
        inner = {
            "payproid": config.pay_project,
            "schoolcode": config.school_code,
            "roomverify": room.roomverify,
            "businesstype": 2,
            "idserial": serial,
        }
    else:
        if args.area:
            raise ProbeError("--area 只能与 --dormitory 一起使用。")
        method = "samllProgramGetRoom"
        inner = {
            "schoolno": config.school_code,
            "optype": "1",
            "payproid": config.pay_project,
            "areaid": "0",
            "buildid": "0",
            "unitid": "0",
            "levelid": "0",
            "businesstype": "2",
            "idserial": serial,
        }
    referer = ORIGIN + "/pay/index.html"
    if config.token:
        referer += "?" + urlencode({"token": config.token})
    headers = {
        "Origin": ORIGIN,
        "Referer": referer,
        "Accept": "application/json",
        "X-Requested-With": "com.eg.android.AlipayGphone",
    }
    if config.cookie:
        headers["Cookie"] = config.cookie
    if config.tapp_id:
        headers["x-mass-tappid"] = config.tapp_id
    return ProbePlan(
        endpoint,
        method,
        {
            "timestamp": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "bizcontent": json.dumps(inner, ensure_ascii=False, separators=(",", ":")),
            "sourceId": 1,
        },
        headers,
        args.proxy,
        label,
    )


class RequestGate:
    """用 SQLite 事务阻止同时运行及切换端口/代理后连续发请求。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cooldown "
            "(id INTEGER PRIMARY KEY CHECK(id=1), next_allowed REAL NOT NULL)"
        )

    def reserve(self, now: float | None = None):
        now = time.time() if now is None else now
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT next_allowed FROM cooldown WHERE id=1").fetchone()
            if row and row[0] > now:
                wait = math.ceil(row[0] - now)
                raise ProbeError(f"请求冷却中，请至少等待 {wait} 秒再运行；本次未发送请求。")
            self.db.execute(
                "INSERT OR REPLACE INTO cooldown VALUES (1, ?)", (now + COOLDOWN_SECONDS,)
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def defer(self, seconds: float):
        self.db.execute(
            "UPDATE cooldown SET next_allowed=MAX(next_allowed, ?) WHERE id=1",
            (time.time() + seconds,),
        )
        self.db.commit()

    def close(self):
        self.db.close()


def throttle_delay(retry_after: str | None, *, now: float | None = None) -> float:
    now = time.time() if now is None else now
    delay = 0.0
    if retry_after and len(retry_after) <= 128:
        try:
            if retry_after.strip().isdigit():
                delay = float(retry_after)
            else:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is not None:
                    delay = parsed.timestamp() - now
        except (ValueError, OverflowError, TypeError):
            pass
    return max(THROTTLED_SECONDS, delay) if math.isfinite(delay) else THROTTLED_SECONDS


def network_failure(exc: Exception) -> dict:
    chain = []
    pending = [exc]
    seen = set()
    while pending and len(chain) < 8:
        error = pending.pop(0)
        if id(error) in seen:
            continue
        seen.add(id(error))
        chain.append(error)
        pending.extend(
            child
            for child in (error.__cause__, error.__context__, getattr(error, "os_error", None))
            if isinstance(child, BaseException)
        )
    if any(
        isinstance(e, (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError))
        for e in chain
    ):
        category, message = "tls_certificate", "TLS 证书校验失败；没有关闭证书验证。"
    elif any(isinstance(e, (ssl.SSLError, aiohttp.ClientSSLError)) for e in chain):
        category, message = "tls", "TLS 握手失败，尚未取得业务响应。"
    elif any(isinstance(e, socket.gaierror) for e in chain):
        category, message = "dns", "域名解析失败，尚未取得业务响应。"
    elif any(isinstance(e, TimeoutError) for e in chain):
        category, message = "timeout", "连接或读取超时，没有自动重试。"
    elif any(isinstance(e, aiohttp.ClientPayloadError) for e in chain):
        category, message = "response_read", "HTTP 响应读取中断，没有自动重试。"
    else:
        category, message = "network", "网络或代理连接失败，尚未取得完整响应。"
    return {
        "ok": False,
        "category": category,
        "message": message,
        # 不保存异常文本，它可能带有代理地址、请求头或登录信息。
        "error_types": list(dict.fromkeys(type(e).__name__ for e in chain)),
    }


async def probe(plan: ProbePlan, session: aiohttp.ClientSession) -> dict:
    started = time.monotonic()
    result = {
        "tested_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "endpoint": plan.endpoint,
        "method": plan.method,
        "proxy_mode": plan.proxy_mode,
        "http_status": None,
        "max_requests": 1,
        "automatic_retries": 0,
    }
    try:
        async with session.post(
            plan.endpoint,
            json=plan.payload,
            headers=plan.headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
        ) as response:
            result["http_status"] = response.status
            if response.status == 429:
                result.update(
                    ok=False,
                    category="rate_limited",
                    message="接口返回 HTTP 429，已停止；请等待冷却后再测试。",
                    cooldown_seconds=throttle_delay(response.headers.get("Retry-After")),
                )
            elif response.status in {401, 403}:
                result.update(
                    ok=False,
                    category="access_denied",
                    message="访问被拒绝；请核对登录状态或接口访问限制。没有重试。",
                )
            elif response.status != 200:
                result.update(
                    ok=False,
                    category="http",
                    message=f"接口返回 HTTP {response.status}；没有重试或跟随重定向。",
                )
            else:
                try:
                    raw = await response.content.readexactly(MAX_RESPONSE_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    raw = exc.partial
                if len(raw) > MAX_RESPONSE_BYTES:
                    result.update(
                        ok=False,
                        category="response_too_large",
                        message="响应超过 64 KiB，已停止读取；没有保存原始内容。",
                    )
                else:
                    result.update(parse_response(raw, plan))
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        result.update(network_failure(exc))
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def parse_response(raw: bytes, plan: ProbePlan) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return {
            "ok": False,
            "category": "invalid_response",
            "message": "HTTP 已连接，但响应不是有效 JSON；可能返回了网页或拦截页。",
        }
    if plan.dormitory:
        try:
            quantity = parse_electricity_quantity(data)
        except ElectricityError as exc:
            return {"ok": False, "category": exc.code, "message": str(exc)}
        return {
            "ok": True,
            "category": "quantity",
            "message": "取得有效的剩余电量。",
            "dormitory": plan.dormitory,
            "remaining_kwh": quantity,
            "unit": "度",
        }
    if not isinstance(data, dict) or data.get("returncode") != "SUCCESS":
        return {
            "ok": False,
            "category": "business_error",
            "message": "HTTP 已连接，但区域查询未返回 SUCCESS；请检查当前会话或业务参数。",
        }
    areas = data.get("businessData")
    if not isinstance(areas, list) or any(
        not isinstance(item, dict)
        or item.get("id") in (None, "")
        or not isinstance(item.get("name"), str)
        for item in areas
    ):
        return {
            "ok": False,
            "category": "invalid_response",
            "message": "区域列表格式异常；没有将原始响应写入报告。",
        }
    return {
        "ok": True,
        "category": "directory",
        "area_count": len(areas),
        "message": f"区域接口查询成功，返回 {len(areas)} 个区域；未继续读取楼栋或房间。",
    }


async def run_probe(plan: ProbePlan) -> dict:
    async with aiohttp.ClientSession(trust_env=plan.proxy_mode == "env") as session:
        return await probe(plan, session)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="低频电费诊断：默认只请求一次区域列表，不遍历宿舍。"
    )
    parser.add_argument(
        "--port",
        type=int,
        choices=(443, 8087),
        default=443,
        help="选择接口端口，默认 443；不使用 ELECTRICITY_ENDPOINT",
    )
    parser.add_argument(
        "--proxy",
        choices=("direct", "env"),
        default="direct",
        help="direct 忽略显式代理；env 使用 Python 识别的代理。均可能被 TUN 接管",
    )
    parser.add_argument("--dormitory", default="", help="可选，改为只查一间宿舍，如33#2035")
    parser.add_argument("--area", default="", help="宿舍查询的区域编号或名称")
    parser.add_argument(
        "--dry-run", action="store_true", help="检查配置及请求计划，不联网、不占冷却"
    )
    args = parser.parse_args()
    gate = None
    try:
        config = ProbeConfig.load()
        plan = make_plan(config, args)
        print(f"接口：{plan.endpoint}")
        print(
            f"查询：{'宿舍电量 ' + plan.dormitory if plan.dormitory else '区域列表（不遍历宿舍）'}"
        )
        proxies = getproxies()
        print(
            f"代理模式：{plan.proxy_mode}；Python 检测到 HTTP(S) 代理："
            f"{'有' if proxies.get('https') or proxies.get('http') else '无'}"
        )
        print("direct 不绕过 TUN，请手动关闭 TUN 后再对比。TLS 证书验证保持开启。")
        print(
            f"会话配置：Token={'已设置' if config.token else '未设置'}，"
            f"Cookie={'已设置' if config.cookie else '未设置'}，"
            f"TappID={'已设置' if config.tapp_id else '未设置'}（不显示值）"
        )
        if args.dry_run:
            print("配置检查完成；本次发送 0 个请求。实际运行最多 1 个请求，无重试。")
            return
        gate = RequestGate(DATA_DIR / "cooldown.sqlite3")
        gate.reserve()
        print("开始一次只读请求，超时 12 秒；两次请求开始时间至少间隔 60 秒。")
        result = asyncio.run(run_probe(plan))
        if result.get("category") == "rate_limited":
            gate.defer(result["cooldown_seconds"])
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f")
        path = DATA_DIR / f"report-{stamp}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"结果 [{result['category']}]：{result['message']}")
        if result.get("remaining_kwh") is not None:
            print(f"宿舍 {result['dormitory']}：{result['remaining_kwh']} 度")
        if result.get("error_types"):
            print("异常类型：" + " → ".join(result["error_types"]))
        if result.get("category") == "rate_limited":
            print(f"至少等待 {math.ceil(result['cooldown_seconds'])} 秒后再测试。")
        print(f"脱敏报告：{path.resolve()}")
        raise SystemExit(0 if result["ok"] else 1)
    except KeyboardInterrupt:
        print("已中止，不会继续发请求。", file=sys.stderr)
        raise SystemExit(130) from None
    except (ProbeError, DormError) as exc:
        print(f"未发送请求：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, sqlite3.Error):
        print("本地冷却记录或报告文件无法读写；已停止，请检查 data 目录权限。", file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        if gate is not None:
            gate.close()


if __name__ == "__main__":
    main()
