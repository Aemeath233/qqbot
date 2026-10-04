"""QQ 电费机器人命令行入口。"""

import argparse
import asyncio
import logging
import sys

import aiohttp
from aiohttp import web

from qqbot.api import QQAPI, QQAPIError
from qqbot.config import ConfigurationError, Settings
from qqbot.electricity import ElectricityClient, ElectricityError, format_electricity
from qqbot.gateway import Gateway, GatewayFatalError, GatewayReconnect
from qqbot.light_server import create_app
from qqbot.websocket_server import serve_websocket


async def check_qq(settings: Settings, *, websocket: bool = False):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        api = QQAPI(settings, session)
        if websocket:
            async with asyncio.timeout(45):
                await Gateway(api, lambda payload: None).connect(check_only=True)
            print("QQ WebSocket 鉴权成功，群 @ 消息和私聊订阅成功；未查询电费。")
            return
        user = await api.me()
    print(
        f"QQ API 鉴权成功，机器人：{user.get('username', '已连接')}。"
        "可用 check --websocket 检查消息连接。"
    )


async def query(settings: Settings, dormitory: str, area: str):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        result = await ElectricityClient(settings, session).query(dormitory, area=area)
    print(format_electricity(result))


def main():
    parser = argparse.ArgumentParser(description="只查询宿舍当前剩余电量的 QQ 机器人")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="启动机器人，默认使用 WebSocket")
    serve.add_argument("--transport", choices=("websocket", "webhook"), help="覆盖连接方式")
    serve.add_argument("--host", help="Webhook 模式：覆盖监听地址")
    serve.add_argument("--port", type=int, help="Webhook 模式：覆盖监听端口")
    check = sub.add_parser("check", help="测试 QQ AppID/AppSecret 鉴权")
    check.add_argument("--websocket", action="store_true", help="实际连接网关检查消息订阅")
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
            asyncio.run(check_qq(settings, websocket=args.websocket))
        elif args.command == "electricity":
            asyncio.run(query(settings, args.dormitory, args.area))
        else:
            transport = getattr(args, "transport", None) or settings.transport
            if transport == "websocket":
                asyncio.run(serve_websocket(settings))
            else:
                web.run_app(
                    create_app(settings),
                    host=getattr(args, "host", None) or settings.host,
                    port=getattr(args, "port", None) or settings.port,
                    access_log=None,
                )
    except GatewayFatalError as exc:
        print(f"QQ WebSocket 启动失败：{exc}", file=sys.stderr)
        raise SystemExit(78) from None
    except KeyboardInterrupt:
        return
    except (
        ConfigurationError,
        ElectricityError,
        GatewayReconnect,
        QQAPIError,
        OSError,
        aiohttp.ClientError,
        TimeoutError,
    ) as exc:
        detail = (
            str(exc)
            if isinstance(exc, (ConfigurationError, ElectricityError, GatewayReconnect, QQAPIError))
            else type(exc).__name__
        )
        print(f"启动或查询失败：{detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
