"""uv run qqbot / uv run qqbot demo / uv run qqbot check。"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from qqbot.admin import run_admin
from qqbot.api import QQAPI, QQAPIError
from qqbot.assistant import BotAssistant
from qqbot.commands import CommandRouter
from qqbot.config import ConfigurationError, Settings
from qqbot.dorms import DormDirectory
from qqbot.electricity import ElectricityClient, ElectricityError, format_electricity
from qqbot.messages import Message
from qqbot.server import create_app


def demo():
    router = CommandRouter()
    print("本地命令演示：输入 /帮助、/ping、/复读 你好；输入 exit 或按 Ctrl+C 退出。")
    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return
        if text.lower() in {"exit", "quit", "退出"}:
            return
        message = Message("users", "demo", "demo", text, time.time() + 3600)
        print(f"机器人：{router.reply(message)}")
        router.sent_count += 1


async def check(settings: Settings):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        api = QQAPI(settings, session)
        me = await api.me()
        name = me.get("username") or "未返回名称"
        print(f"鉴权成功，机器人：{name}。Webhook 是否可达还需在开放平台验证。")


async def query_electricity(settings: Settings, dormitory: str, area: str = ""):
    async with aiohttp.ClientSession() as session:
        result = await ElectricityClient(settings, session).query(dormitory, area=area)
        print(format_electricity(result))


async def chat(settings: Settings, prompt: str | None):
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session)
        router = CommandRouter()

        async def respond(text: str):
            message = Message("users", "local-console", "console", text, time.time() + 3600)
            task = router.plan(message, llm_enabled=assistant.llm_enabled)
            return (
                task.content
                if task.kind == "text"
                else await assistant.generate(task.kind, task.content, "local-console")
            )

        if prompt is not None:
            print(await respond(prompt))
            return
        print("AI 对话测试：可输入自然语言；exit 退出。不会给 QQ 用户发送消息。")
        while True:
            try:
                text = await asyncio.to_thread(input, "你：")
            except (EOFError, KeyboardInterrupt):
                return
            if text.strip().lower() in {"exit", "quit", "退出"}:
                return
            print("机器人：" + await respond(text))


def main():
    # Windows 的重定向管道默认编码可能是 GBK；PowerShell 7 和日志采集使用 UTF-8。
    if sys.platform == "win32":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure") and not stream.isatty():
                stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="QQ 群聊与私聊机器人（官方 Webhook API）")
    subcommands = parser.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="启动 Webhook 服务（默认命令）")
    serve.add_argument("--dry-run", action="store_true", help="本机模拟服务，不向 QQ 发消息")
    subcommands.add_parser("demo", help="无须凭证，交互体验基础命令")
    subcommands.add_parser("check", help="检查配置和 QQ 鉴权，不发送消息")
    electric = subcommands.add_parser("electricity", help="直接测试电量查询，无须 QQ 凭证")
    electric.add_argument("dormitory", help="楼号#房号，例如33#2035")
    electric.add_argument("--area", default="", help="主菜单/区域编号或名称")
    ai_chat = subcommands.add_parser("chat", help="测试自然语言对话及工具调用，无须 QQ 凭证")
    ai_chat.add_argument("prompt", nargs="?", help="省略时进入交互对话")
    export = subcommands.add_parser("export-dorms", help="导出宿舍 JSON 目录，便于维护房号别名")
    export.add_argument("output", type=Path, help="例如 data/room_catalog.json，已有文件不覆盖")
    admin = subcommands.add_parser("admin", help="启动本机管理网页，无须预先配置 QQ/LLM")
    admin.add_argument("--port", type=int, default=8081, help="管理端口，默认8081")
    admin.add_argument("--set-password", action="store_true", help="设置或重设管理密码后退出")
    args = parser.parse_args()
    if args.command == "demo":
        demo()
        return
    if args.command == "export-dorms":
        try:
            directory = DormDirectory.load()
            directory.export(args.output)
            print(f"已导出 {len(directory.rooms)} 条宿舍记录；在 aliases 中配置日常房号。")
        except (ValueError, OSError) as exc:
            print(f"导出失败：{exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return
    try:
        if args.command == "admin":
            run_admin(port=args.port, set_password=args.set_password)
            return
        settings = Settings.load(
            dry_run=getattr(args, "dry_run", False),
            require_qq=args.command not in {"electricity", "chat"},
        )
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if args.command == "check":
            asyncio.run(check(settings))
        elif args.command == "electricity":
            asyncio.run(query_electricity(settings, args.dormitory, args.area))
        elif args.command == "chat":
            asyncio.run(chat(settings, args.prompt))
        else:
            web.run_app(
                create_app(settings), host=settings.host, port=settings.port, access_log=None
            )
    except KeyboardInterrupt:
        pass
    except (
        ConfigurationError,
        QQAPIError,
        ElectricityError,
        OSError,
        aiohttp.ClientError,
        TimeoutError,
    ) as exc:
        detail = (
            str(exc)
            if isinstance(exc, (ConfigurationError, QQAPIError, ElectricityError, OSError))
            else type(exc).__name__
        )
        print(f"启动或检查失败：{detail}", file=sys.stderr)
        raise SystemExit(1) from None
