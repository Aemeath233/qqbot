"""基础命令；新增业务功能从此处扩展。"""

import time
from datetime import datetime, timedelta, timezone

from qqbot import __version__
from qqbot.messages import Message

SHANGHAI = timezone(timedelta(hours=8))
HELP = (
    "可用命令：\n"
    "/帮助 或 /help：查看帮助\n"
    "/ping：检测机器人是否响应\n"
    "/复读 文本 或 /echo 文本：重复文本（最多 1000 字）\n"
    "/时间 或 /time：查看北京时间\n"
    "/状态 或 /status：查看服务运行状态\n"
    "群里先 @机器人，再输入命令；私聊直接输入即可。"
)


class CommandRouter:
    def __init__(self):
        self.started_at = time.monotonic()
        self.sent_count = 0

    def reply(self, message: Message) -> str | None:
        text = message.content.strip()
        if message.full_group and not text.startswith("/"):
            return None
        if text.startswith("/"):
            text = text[1:].lstrip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower() if parts else "help"
        argument = parts[1] if len(parts) == 2 else ""
        match command:
            case "help" | "帮助" | "菜单":
                return HELP
            case "ping":
                return "pong！机器人正在运行。"
            case "echo" | "复读":
                if not argument:
                    return "用法：/复读 你想让我重复的文字"
                if len(argument) > 1000:
                    return "复读内容最多 1000 字，请缩短后重试。"
                return argument
            case "time" | "时间":
                return f"北京时间：{datetime.now(SHANGHAI):%Y-%m-%d %H:%M:%S}"
            case "status" | "状态":
                seconds = int(time.monotonic() - self.started_at)
                return (
                    f"服务运行正常\n版本：{__version__}\n"
                    f"运行时间：{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟 "
                    f"{seconds % 60} 秒\n本次启动已发送回复：{self.sent_count} 条"
                )
            case _:
                if message.full_group:
                    return None
                return "暂不支持这个命令，发送 /帮助 查看可用命令。"
