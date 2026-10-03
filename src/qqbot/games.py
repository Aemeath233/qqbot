"""小互动由 Python 产生结果，回复和重试复用同一次结果。"""

import re
import secrets


class GameError(ValueError):
    pass


def roll_dice(expression: str = "1d6") -> dict:
    match = re.fullmatch(r"([0-9]{1,2})[dD]([0-9]{1,4})", expression.strip() or "1d6")
    if match is None:
        raise GameError("用法：/掷骰子 或 /掷骰子 2d6（最多20枚，每枚2～1000面）。")
    count, sides = map(int, match.groups())
    if not 1 <= count <= 20 or not 2 <= sides <= 1000:
        raise GameError("骰子数量应为1～20，每枚骰子应有2～1000面。")
    values = [secrets.randbelow(sides) + 1 for _ in range(count)]
    return {
        "ok": True,
        "kind": "dice",
        "expression": f"{count}d{sides}",
        "values": values,
        "total": sum(values),
    }


def draw_lots(options: str = "") -> dict:
    if not options.strip():
        items = [
            "顺风签：先完成一件小事，今天就算开了个好头。",
            "摸鱼签：忙完手头的事，给自己留一点休息时间。",
            "相逢签：今天适合跟老朋友打个招呼。",
            "稳稳签：按自己的节奏来，不必急着追赶。",
            "灵感签：先把脑中的点子记下来，再慢慢实现。",
            "早睡签：今晚试试早点收工，明天再续。",
        ]
        return {"ok": True, "kind": "fortune", "selected": secrets.choice(items)}
    items = [item.strip() for item in re.split(r"[|｜,，、\n]+", options) if item.strip()]
    if not 2 <= len(items) <= 20 or any(
        len(item) > 40 or any(ord(c) < 32 for c in item) for item in items
    ):
        raise GameError("请提供2～20个选项，用 | 分隔；每项最多40字。例如：/抽签 面条|米饭|饺子")
    return {"ok": True, "kind": "choice", "selected": secrets.choice(items)}


def format_game(result: dict) -> str:
    if not result.get("ok"):
        return result.get("message", "互动暂时无法完成。")
    if result["kind"] == "dice":
        values = "、".join(map(str, result["values"]))
        return f"🎲 {result['expression']}：{values}；合计 {result['total']}。"
    if result["kind"] == "fortune":
        return "今日随机抽签（娱乐）：" + result["selected"]
    return "帮你随机选了：" + result["selected"]
