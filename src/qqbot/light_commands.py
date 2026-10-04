"""把 QQ 消息送给唯一的电量查询助手。"""

from qqbot.commands import ReplyTask
from qqbot.messages import Message
from qqbot.presentation import HELP_TEXT

HELP = HELP_TEXT


class LightCommandRouter:
    def plan(self, message: Message, *, llm_enabled=False):
        text = message.content.strip()
        if text.lower() in {"/help", "/帮助", "/菜单"}:
            return ReplyTask("text", HELP)
        if message.full_group and not llm_enabled:
            return None
        return ReplyTask("chat", text)
