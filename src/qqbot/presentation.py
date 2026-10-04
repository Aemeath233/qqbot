"""电量结果的 Markdown 和可持久化回复。"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from qqbot.electricity import format_electricity

LOW_POWER_NOTICE = "电量已经不多了，记得及时充值电费，避免停电。"
HELP_TEXT = (
    "电费查询使用帮助\n\n"
    "• 查询宿舍当前剩余电量：例如“看看33楼4032宿舍电费”。\n"
    "• 点击“再次查询”，可直接查询同一宿舍，无需重新输入房号。\n"
    "• 点击“绑定此宿舍”，再点“是”保存；点“否”不修改。绑定后直接说“查一下电费”即可。\n"
    "• 可以更换：说“更换绑定宿舍为33楼2004室”，或查询新宿舍后点击绑定，再确认。\n"
    "• 每人一个默认宿舍，在不同群共用。普通查询和“再次查询”不会修改绑定。\n"
    "• 电量低于50度时，会附带充值提醒。\n"
    "• 群聊请@机器人，私聊直接发送。短时间重复查询可能使用缓存或提示等待。\n"
    "只查当前剩余电量，单位是度；不提供历史分析、缴费充值或其他问答。"
)


@dataclass(frozen=True)
class Reply:
    text: str
    markdown: str | None = None
    keyboard: dict | None = None

    def dumps(self):
        return json.dumps(
            {"text": self.text, "markdown": self.markdown, "keyboard": self.keyboard},
            ensure_ascii=False,
        )

    @classmethod
    def loads(cls, payload):
        data = json.loads(payload)
        if not isinstance(data.get("text"), str):
            raise ValueError("回复格式错误")
        return cls(**data)


def escape_markdown(value):
    return re.sub(r"([\\`*_{}\[\]()#>|])", r"\\\1", str(value))


def electricity_reply(result):
    text = format_electricity(result)
    if not result.get("ok"):
        return Reply(text)
    remaining = Decimal(str(result["remaining_kwh"]))
    low = remaining.is_finite() and remaining < 50
    if low:
        text += "\n" + LOW_POWER_NOTICE
    building, room = result["dormitory"].split("#")
    lines = [
        "# 宿舍电量",
        f"宿舍：**{building}楼{room}室**",
        f"剩余电量：**{escape_markdown(result['remaining_kwh'])} 度**",
    ]
    if result.get("area_name"):
        lines.append("区域：" + escape_markdown(result["area_name"]))
    lines.append("查询时间：" + escape_markdown(result["queried_at"]) + "（北京时间）")
    if result.get("cached"):
        lines.append("本次使用30秒内的查询缓存。")
    if low:
        lines.append("**" + LOW_POWER_NOTICE + "**")
    return Reply(text, "\n\n".join(lines))


def help_reply():
    return Reply(HELP_TEXT, "# 电费查询帮助\n\n" + HELP_TEXT.split("\n\n", 1)[1])
