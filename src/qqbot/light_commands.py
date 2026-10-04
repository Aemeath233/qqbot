"""把 QQ 消息送给唯一的电量查询助手。"""

from qqbot.commands import ReplyTask
from qqbot.messages import Message

HELP = (
    "自然语言询问宿舍当前剩余电量即可，例如：\n"
    "“查一下33号楼2035室还有多少电”\n"
    "也可以发送 /电费 33#2035。\n"
    "机器人只查当前剩余电量，不保存个人房间设置。"
)


class LightCommandRouter:
    def plan(self, message: Message, *, llm_enabled=False):
        text = message.content.strip()
        if text.lower() in {"/help", "/帮助", "/菜单"}:
            return ReplyTask("text", HELP)
        if text.startswith("/"):
            if not text.lower().startswith(("/电费", "/electricity")):
                return ReplyTask("text", "当前只支持查询宿舍当前剩余电量。发送 /帮助 查看用法。")
            _, _, argument = text.partition(" ")
            text = "查询宿舍当前剩余电量 " + argument.strip()
        if message.full_group and not llm_enabled:
            return None
        return ReplyTask("chat", text)
