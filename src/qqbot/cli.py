"""uv run qqbot / uv run qqbot demo / uv run qqbot check。"""

import argparse
import asyncio
import logging
import sys
import time

import aiohttp
from aiohttp import web

from qqbot.api import QQAPI, QQAPIError
from qqbot.commands import CommandRouter
from qqbot.config import ConfigurationError, Settings
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
    args = parser.parse_args()
    if args.command == "demo":
        demo()
        return
    try:
        settings = Settings.load(dry_run=getattr(args, "dry_run", False))
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if args.command == "check":
            asyncio.run(check(settings))
        else:
            web.run_app(
                create_app(settings), host=settings.host, port=settings.port, access_log=None
            )
    except KeyboardInterrupt:
        pass
    except (ConfigurationError, QQAPIError, OSError, aiohttp.ClientError, TimeoutError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, (ConfigurationError, QQAPIError, OSError))
            else type(exc).__name__
        )
        print(f"启动或检查失败：{detail}", file=sys.stderr)
        raise SystemExit(1) from None
