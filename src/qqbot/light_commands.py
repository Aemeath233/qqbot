"""电费机器人唯一的 QQ 命令入口。"""

import json
from dataclasses import dataclass

from qqbot.messages import Message

HELP = (
    "我只提供宿舍电费查询和用电分析。\n"
    "/电费 33#2035 [区域]：查询剩余电量\n"
    "/绑定宿舍 33#2035 [区域]：绑定自己的宿舍\n"
    "/用电统计 [天数]、/电费历史 [天数]：分析或查看自己的查询记录\n"
    "/用电曲线 [天数]：将自己的记录绘成图片\n"
    "/我的宿舍、/解绑宿舍、/忘记我。\n"
    "每次成功查询会记录实际电量；没有历史数据时不会编造用电量。"
)


@dataclass(frozen=True)
class ReplyTask:
    kind: str
    content: str


class LightCommandRouter:
    def plan(self, message: Message, *, llm_enabled=False):
        text = message.content.strip()
        if message.full_group and not text.startswith("/") and not llm_enabled:
            return None
        if not text.startswith("/"):
            if llm_enabled and len(text) <= 2000:
                return ReplyTask("chat", text)
            return ReplyTask("text", "我只支持宿舍电费查询和用电分析。发送 /帮助 查看用法。")

        command, _, argument = text[1:].lstrip().partition(" ")
        command, argument = command.lower(), argument.strip()
        if command in {"帮助", "help", "菜单"}:
            return ReplyTask("text", HELP)
        if command == "ping":
            return ReplyTask("text", "电费机器人运行正常。")
        if command in {"电费", "electricity"}:
            return ReplyTask("electricity", argument)
        if command in {"绑定宿舍", "绑定", "bind"}:
            return ReplyTask("bind", argument)
        if command in {"我的宿舍", "myroom"}:
            return ReplyTask("profile", "")
        if command in {"解绑宿舍", "解绑", "unbind"}:
            return ReplyTask("unbind", "")
        if command in {"忘记我", "forget"}:
            return ReplyTask("forget", "")
        if command in {"电费历史", "history", "历史"}:
            return self._history("history", argument)
        if command in {"用电统计", "用电分析", "usage", "分析"}:
            return self._history("usage", argument)
        if command in {"用电曲线", "电量曲线", "curve"}:
            return self._history("curve", argument)
        if command in {"聊天", "chat"}:
            if not llm_enabled:
                return ReplyTask("text", "自然语言查询尚未配置模型；发送 /电费 楼号#房号 查询。")
            if argument and len(argument) <= 2000:
                return ReplyTask("chat", argument)
            return ReplyTask("text", "用法：/聊天 询问电费或用电情况。")
        if not message.full_group and llm_enabled and not text.startswith("//"):
            return ReplyTask("chat", text[:2000])
        return ReplyTask("text", "暂不支持这个指令。发送 /帮助 查看电费功能。")

    @staticmethod
    def _history(kind: str, argument: str) -> ReplyTask:
        parts = argument.split(maxsplit=2)
        if parts and not parts[0].isdigit():
            return ReplyTask("text", "天数应为 1～365，例如：/用电统计 7。")
        days = int(parts[0]) if parts else 7
        if not 1 <= days <= 365:
            return ReplyTask("text", "天数应为 1～365。")
        if kind == "curve" and len(parts) > 1:
            return ReplyTask("text", "用法：/用电曲线 [天数]。房间使用 /绑定宿舍 设置。")
        return ReplyTask(
            kind,
            json.dumps(
                {
                    "days": days,
                    "dormitory": parts[1] if len(parts) > 1 else "",
                    "area": parts[2] if len(parts) > 2 else "",
                },
                ensure_ascii=False,
            ),
        )


def help_text() -> str:
    return HELP
