"""电量台账与余额差额计算：只看当前用户、同一电表、实际观测区间。"""

import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from qqbot.commands import SHANGHAI
from qqbot.dorms import DormDirectory, DormError
from qqbot.electricity import ElectricityError, parse_electricity_quantity
from qqbot.user_store import UserDataError, UserStore

if TYPE_CHECKING:
    from qqbot.config import Settings


def meter_key(school: str, project: int, area: str, roomverify: str) -> str:
    return hashlib.sha256(json.dumps([school, project, area, roomverify]).encode()).hexdigest()


def display_time(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


class HistoryAccess:
    def __init__(
        self, settings: "Settings", store: UserStore, context: str, profile: dict, *, request_id=""
    ):
        self.settings, self.store, self.context, self.profile = settings, store, context, profile
        self.request_id = request_id or str(time.time_ns())
        self.query_index = 0

    def record(self, result: dict) -> dict:
        if not self.settings.electricity_history_enabled or self.settings.dry_run:
            return self.public(result)
        try:
            amount = parse_electricity_quantity(
                {
                    "returncode": "SUCCESS",
                    "businessData": {"quantity": result["remaining_kwh"], "quantityunit": "度"},
                }
            )
            room = DormDirectory.load(self.settings.electricity_map_path).resolve(
                result["dormitory"],
                result.get("area", "") or self.settings.electricity_default_area,
            )
            stamp = result.get("_observed_at")
            if stamp is None:
                stamp = (
                    datetime.strptime(result["queried_at"], "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=SHANGHAI)
                    .timestamp()
                )
            identifier = meter_key(
                self.settings.electricity_school_code,
                self.settings.electricity_pay_project,
                room.area,
                room.roomverify,
            )
            event = hashlib.sha256(
                f"{self.request_id}:{self.query_index}:{identifier}".encode()
            ).hexdigest()
            self.query_index += 1
            self.store.add_reading(
                self.context,
                {
                    "meter_key": identifier,
                    "dormitory": room.label,
                    "area": room.area,
                    "area_name": room.area_name,
                    "quantity": amount,
                    "observed_at": stamp,
                    "cached": bool(result.get("cached")),
                },
                event,
                self.settings.electricity_history_retention_days,
            )
        except (UserDataError, DormError, ElectricityError, ValueError, KeyError, OverflowError):
            result = {**result, "history_warning": "电量查询成功，但本次个人历史未能保存。"}
        return self.public(result)

    @staticmethod
    def public(result: dict) -> dict:
        return {key: value for key, value in result.items() if not key.startswith("_")}

    def _rows(self, days: int, dormitory: str, area: str) -> list[dict]:
        if not self.settings.electricity_history_enabled:
            raise UserDataError("电费历史功能尚未启用，请联系管理员。")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise UserDataError("查询天数应为1～365，例如：/用电统计 3。")
        target = dormitory or self.profile.get("dormitory", "")
        selected = area or (self.profile.get("area", "") if not dormitory else "")
        identifier = ""
        if target:
            try:
                room = DormDirectory.load(self.settings.electricity_map_path).resolve(
                    target, selected or self.settings.electricity_default_area
                )
            except DormError as exc:
                raise UserDataError(str(exc)) from None
            identifier = meter_key(
                self.settings.electricity_school_code,
                self.settings.electricity_pay_project,
                room.area,
                room.roomverify,
            )
        rows = self.store.readings(self.context, days, meter_key=identifier)
        if not target and len({row["meter_key"] for row in rows}) > 1:
            raise UserDataError("你查过多间宿舍，请先绑定宿舍，或在命令中指定楼号#房号和区域。")
        return rows

    async def history(self, days: int = 3, dormitory: str = "", area: str = "") -> dict:
        rows = self._rows(days, dormitory, area)
        return {
            "ok": True,
            "kind": "history",
            "days": days,
            "query_count": len(rows),
            "records": [
                {
                    "dormitory": row["dormitory"],
                    "area_name": row["area_name"],
                    "remaining_kwh": row["quantity"],
                    "observed_at": display_time(row["observed_at"]),
                    "requested_at": display_time(row["requested_at"]),
                    "cached": bool(row["cached"]),
                }
                for row in rows[:10]
            ],
        }

    async def usage(self, days: int = 3, dormitory: str = "", area: str = "") -> dict:
        rows = self._rows(days, dormitory, area)
        # 缓存访问是查询日志，不是新的独立读数。
        points = sorted(
            {(row["observed_at"], row["quantity"]) for row in rows}, key=lambda item: item[0]
        )
        if len({point[0] for point in points}) != len(points):
            return {"ok": False, "message": "同一读数时间出现不同电量，暂时不能可靠计算。"}
        if len(points) < 2 or points[-1][0] <= points[0][0]:
            return {
                "ok": False,
                "kind": "usage",
                "message": (
                    "有效读数不足两次，暂时不能计算。以后每次成功查询都会记录，不会补造以前的数据。"
                ),
            }
        first, last = points[0], points[-1]
        change = Decimal(first[1]) - Decimal(last[1])
        increases = sum(
            Decimal(b[1]) > Decimal(a[1]) for a, b in zip(points, points[1:], strict=False)
        )
        return {
            "ok": True,
            "kind": "usage",
            "days": days,
            "sample_count": len(points),
            "dormitory": rows[0]["dormitory"],
            "area_name": rows[0]["area_name"],
            "first_at": display_time(first[0]),
            "last_at": display_time(last[0]),
            "covered_hours": round((last[0] - first[0]) / 3600, 2),
            "first_kwh": first[1],
            "last_kwh": last[1],
            "net_decrease_kwh": format(change, "f"),
            "possible_recharge": bool(increases),
            "consumption_estimate_kwh": format(change, "f")
            if not increases and change >= 0
            else None,
            "note": "仅在期间未充值、且无电表余额修正时，净减少值可作为耗电估算。",
        }


def format_history(result: dict) -> str:
    if not result.get("ok"):
        return result.get("message", "历史查询暂时不可用。")
    if result["kind"] == "history":
        if not result["records"]:
            return "这个区间还没有你的电费查询记录。成功查询后才会产生历史数据。"
        lines = [f"最近{result['days']}天的个人查询记录（最多展示10次，北京时间）："]
        for row in result["records"]:
            source = f"（缓存读数，原读数时间{row['observed_at']}）" if row["cached"] else ""
            lines.append(
                f"{row['requested_at']} · {row['dormitory']} · {row['remaining_kwh']}度{source}"
            )
        return "\n".join(lines)
    change = Decimal(result["net_decrease_kwh"])
    direction = "净减少" if change >= 0 else "净增加"
    lines = [
        f"宿舍{result['dormitory']}，最近{result['days']}天内有{result['sample_count']}次独立读数。",
        f"实际覆盖：{result['first_at']} → {result['last_at']}"
        f"（{result['covered_hours']}小时，北京时间）。",
        f"剩余电量：{result['first_kwh']} → {result['last_kwh']}度；"
        f"余额{direction}{format(abs(change), 'f')}度。",
    ]
    if result["possible_recharge"]:
        lines.append("期间检测到电量上升，可能有充值或电表修正，不能据此给出准确耗电量。")
    else:
        lines.append(
            "按期间未充值、无余额修正估算，耗电量为" + result["consumption_estimate_kwh"] + "度。"
        )
    lines.append("数据只覆盖上述查询点之间；未记录的充值无法从余额变化中识别。")
    return "\n".join(lines)
