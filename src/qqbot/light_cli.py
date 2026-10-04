"""QQ 电费机器人命令行入口。"""

import argparse
import asyncio
import logging
import sys

import aiohttp
from aiohttp import web

from qqbot.api import QQAPI
from qqbot.config import ConfigurationError, Settings
from qqbot.electricity import ElectricityClient, ElectricityError, format_electricity
from qqbot.light_server import create_app


async def check_qq(settings: Settings):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        user = await QQAPI(settings, session).me()
    print(
        f"QQ 鉴权成功，机器人：{user.get('username', '已连接')}。Webhook 可达性需在 QQ 平台验证。"
    )


async def query(settings: Settings, dormitory: str, area: str):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        result = await ElectricityClient(settings, session).query(dormitory, area=area)
    print(format_electricity(result))


def main():
    parser = argparse.ArgumentParser(description="只查询宿舍当前剩余电量的 QQ 机器人")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="启动 QQ Webhook 服务")
    serve.add_argument("--host", help="覆盖监听地址")
    serve.add_argument("--port", type=int, help="覆盖监听端口")
    sub.add_parser("check", help="测试 QQ AppID/AppSecret 鉴权")
    electricity = sub.add_parser("electricity", help="查询一个宿舍当前剩余电量")
    electricity.add_argument("dormitory", help="楼号#房号，例如 33#2035")
    electricity.add_argument("--area", default="", help="确认后的区域编号或名称")
    args = parser.parse_args()
    if args.command is None:
        args.command = "serve"
    try:
        settings = Settings.load(
            require_qq=args.command != "electricity",
            require_llm=args.command not in {"electricity", "check"},
        )
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if args.command == "check":
            asyncio.run(check_qq(settings))
        elif args.command == "electricity":
            asyncio.run(query(settings, args.dormitory, args.area))
        else:
            web.run_app(
                create_app(settings),
                host=args.host or settings.host,
                port=args.port or settings.port,
                access_log=None,
            )
    except KeyboardInterrupt:
        return
    except (
        ConfigurationError,
        ElectricityError,
        OSError,
        aiohttp.ClientError,
        TimeoutError,
    ) as exc:
        detail = (
            str(exc)
            if isinstance(exc, (ConfigurationError, ElectricityError, OSError))
            else type(exc).__name__
        )
        print(f"启动或查询失败：{detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
