"""独立的本机管理服务；保存 .env 后由操作者重启机器人应用配置。"""

import argparse
import asyncio
import getpass
import json
import logging
import secrets
import sqlite3
import sys
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from qqbot import __version__
from qqbot.admin_auth import LoginLimit, PasswordStore, Sessions
from qqbot.admin_config import ConfigConflict, ConfigStore
from qqbot.api import QQAPI, QQAPIError
from qqbot.config import ConfigurationError, Settings, env_bool
from qqbot.electricity_probe import (
    ProbeConfig,
    ProbeError,
    RequestGate,
    make_plan,
    probe,
)
from qqbot.llm import ChatCompletionsClient, LLMError

logger = logging.getLogger(__name__)
COOKIE = "qqbot_admin"


class AdminState:
    def __init__(self, root: Path, *, environ=None, checks=None):
        self.root = root
        self.config = ConfigStore(root / ".env", environ=environ)
        self.password = PasswordStore(root / "data/admin/password.json")
        self.sessions = Sessions()
        self.login_limit = LoginLimit()
        self.save_lock = asyncio.Lock()
        self.test_locks = {key: asyncio.Lock() for key in ("qq", "llm", "electricity")}
        self.checks = checks or {"qq": test_qq, "llm": test_llm, "electricity": test_electricity}
        self.session: aiohttp.ClientSession | None = None
        values = self.config.values()
        self.hosts = {
            host.strip().casefold()
            for host in values.get("ADMIN_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",")
            if host.strip()
        }
        if not self.hosts:
            raise ConfigurationError("ADMIN_ALLOWED_HOSTS 不能为空。")
        self.secure_cookie = env_bool("ADMIN_COOKIE_SECURE", values=values)


STATE = web.AppKey("admin_state", AdminState)
# aiohttp 3.14 提供 RequestKey；兼容已有 3.12/3.13 安装。
CSRF = getattr(web, "RequestKey", web.AppKey)("admin_csrf", str)


def same_origin(request: web.Request) -> bool:
    try:
        origin = urlsplit(request.headers.get("Origin", ""))
        return (
            origin.scheme in {"http", "https"}
            and origin.netloc.casefold() == request.host.casefold()
        )
    except ValueError:
        return False


@web.middleware
async def security(request: web.Request, handler):
    state = request.app[STATE]
    try:
        hostname = urlsplit("//" + request.host).hostname
        if not hostname or hostname.casefold() not in state.hosts:
            raise web.HTTPForbidden(text="管理域名未获允许，请检查 ADMIN_ALLOWED_HOSTS。")
        if request.path.startswith("/api/"):
            if request.method == "POST" and not same_origin(request):
                raise web.HTTPForbidden(text="请求来源校验失败。")
            if request.path != "/api/login":
                fingerprint = state.password.fingerprint()
                csrf = state.sessions.get(request.cookies.get(COOKIE, ""), fingerprint)
                if csrf is None:
                    raise web.HTTPUnauthorized(text="请先登录管理页。")
                if request.method == "POST" and not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), csrf
                ):
                    raise web.HTTPForbidden(text="会话校验失败，请重新登录。")
                request[CSRF] = csrf
        response = await handler(request)
    except web.HTTPException as exc:
        response = web.json_response({"message": exc.text}, status=exc.status)
    except ConfigConflict as exc:
        response = web.json_response({"message": str(exc)}, status=409)
    except (ConfigurationError, ProbeError) as exc:
        response = web.json_response({"message": str(exc)}, status=400)
    except (OSError, sqlite3.Error):
        response = web.json_response({"message": "本地配置或数据目录无法读写。"}, status=503)
    except (ValueError, KeyError, TypeError):
        response = web.json_response({"message": "请求或配置格式错误。"}, status=400)
    except Exception as exc:
        logger.error("管理请求失败：%s", type(exc).__name__)
        response = web.json_response({"message": "管理服务暂时无法完成请求。"}, status=500)
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
        }
    )
    return response


async def login(request: web.Request):
    state = request.app[STATE]
    public = urlsplit("//" + request.host).hostname not in {"localhost", "127.0.0.1", "::1"}
    https = urlsplit(request.headers.get("Origin", "")).scheme == "https"
    if public and not https:
        raise web.HTTPForbidden(text="公网管理页请通过 HTTPS 访问。")
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("password"), str):
        raise web.HTTPBadRequest(text="请输入管理密码。")
    if not state.login_limit.reserve():
        raise web.HTTPTooManyRequests(text="登录尝试过多，请等待 5 分钟再试。")
    # 同一次读取的指纹与密码验证绑定，重设密码后旧会话立即失效。
    fingerprint = state.password.fingerprint()
    valid = await asyncio.to_thread(state.password.verify, payload["password"])
    if not valid or fingerprint != state.password.fingerprint():
        raise web.HTTPUnauthorized(text="管理密码不正确。")
    state.login_limit.attempts.clear()
    token, csrf = state.sessions.issue(fingerprint)
    response = web.json_response({"csrf": csrf})
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="Strict",
        secure=state.secure_cookie or public or https,
        path="/",
        max_age=8 * 3600,
    )
    return response


async def logout(request: web.Request):
    request.app[STATE].sessions.items.pop(request.cookies.get(COOKIE, ""), None)
    response = web.json_response({"message": "已退出。"})
    response.del_cookie(COOKIE, path="/")
    return response


async def get_settings(request: web.Request):
    return web.json_response({**request.app[STATE].config.public(), "csrf": request[CSRF]})


async def save_settings(request: web.Request):
    state = request.app[STATE]
    payload = await request.json()
    async with state.save_lock:
        settings = state.config.save(payload)
    return web.json_response(
        {
            **settings,
            "message": "配置已保存。请重启机器人，让新配置生效。",
            "restart_required": True,
        }
    )


async def status(request: web.Request):
    state = request.app[STATE]
    values = state.config.values()
    service = "unavailable"
    try:
        settings = Settings.from_values(values, require_qq=False)
        async with state.session.get(
            f"http://127.0.0.1:{settings.port}/healthz",
            timeout=aiohttp.ClientTimeout(total=2),
            allow_redirects=False,
        ) as response:
            body = json.loads(await response.content.read(4096))
            if response.status == 200 and body.get("status") == "ok":
                service = (
                    body.get("mode") if body.get("mode") in {"live", "dry-run"} else "unavailable"
                )
    except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError):
        pass
    return web.json_response(
        {
            "version": __version__,
            "service": service,
            "qq_configured": bool(values.get("QQ_APP_ID") and values.get("QQ_APP_SECRET")),
            "llm_enabled": values.get("LLM_ENABLED", "false").casefold() == "true",
            "electricity_enabled": values.get("ELECTRICITY_ENABLED", "false").casefold() == "true",
        }
    )


async def test_qq(state: AdminState) -> dict:
    settings = Settings.from_values(state.config.values())
    async with asyncio.timeout(15):
        await QQAPI(settings, state.session).me()
    return {
        "ok": True,
        "message": "QQ 鉴权及机器人资料读取成功。未发送消息；回调还需在开放平台验证。",
    }


async def test_llm(state: AdminState) -> dict:
    values = {**state.config.values(), "LLM_ENABLED": "true"}
    settings = Settings.from_values(values, require_qq=False)
    message = await ChatCompletionsClient(settings, state.session).complete(
        [{"role": "user", "content": "请只回复 OK。"}],
        [],
    )
    if not message.get("content"):
        raise LLMError("模型服务没有返回文本，请检查模型配置。")
    return {"ok": True, "message": "模型服务连接成功，已收到有效文本。此次测试不验证函数工具能力。"}


async def test_electricity(state: AdminState) -> dict:
    values = {**state.config.values(), "ELECTRICITY_ENABLED": "true"}
    settings = Settings.from_values(values, require_qq=False)
    config = ProbeConfig(
        school_code=settings.electricity_school_code,
        pay_project=settings.electricity_pay_project,
        token=settings.electricity_token,
        cookie=settings.electricity_cookie,
        tapp_id=settings.electricity_tapp_id,
    )
    plan = make_plan(config, argparse.Namespace(port=443, proxy="direct", dormitory="", area=""))
    # 管理员明确保存的地址与机器人的运行配置一致；不扫描端口或自动尝试其他地址。
    plan = replace(plan, endpoint=settings.electricity_endpoint)
    gate = RequestGate(state.root / "data/electricity-test/cooldown.sqlite3")
    try:
        gate.reserve()
        result = await probe(plan, state.session)
        if result.get("category") == "rate_limited":
            gate.defer(result["cooldown_seconds"])
        return result
    finally:
        gate.close()


async def test_connection(request: web.Request):
    state = request.app[STATE]
    service = request.match_info["service"]
    if service not in state.checks:
        raise web.HTTPNotFound(text="未知服务。")
    lock = state.test_locks[service]
    if lock.locked():
        raise web.HTTPTooManyRequests(text="该服务正在测试，请等待结果。")
    async with lock:
        try:
            result = await state.checks[service](state)
        except QQAPIError as exc:
            result = {
                "ok": False,
                "message": f"QQ 鉴权失败（HTTP {exc.status}），请核对凭据及权限。",
            }
        except LLMError as exc:
            result = {"ok": False, "message": str(exc)}
        except (aiohttp.ClientError, TimeoutError):
            result = {"ok": False, "message": "连接失败或超时，请检查网络和服务配置。"}
    return web.json_response(result)


def create_admin_app(root: Path, *, environ=None, checks=None) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=65536)
    state = AdminState(root.resolve(), environ=environ, checks=checks)
    if not state.password.path.exists():
        raise ConfigurationError("请先执行 uv run qqbot admin --set-password 设置管理密码。")
    app[STATE] = state

    async def lifespan(application):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            state.session = session
            yield

    app.cleanup_ctx.append(lifespan)
    for route, filename, content_type in (
        ("/", "index.html", "text/html"),
        ("/admin.js", "admin.js", "text/javascript"),
        ("/admin.css", "admin.css", "text/css"),
    ):
        content = files("qqbot").joinpath("resources/admin", filename).read_text(encoding="utf-8")

        async def asset(request, text=content, mime=content_type):
            return web.Response(text=text, content_type=mime)

        app.router.add_get(route, asset)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", save_settings)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/test/{service}", test_connection)
    return app


def run_admin(*, port: int = 8081, set_password: bool = False):
    if not 1 <= port <= 65535:
        raise ConfigurationError("管理页端口必须在 1～65535 之间。")
    root = Path.cwd()
    passwords = PasswordStore(root / "data/admin/password.json")
    if set_password or not passwords.path.exists():
        if not sys.stdin.isatty():
            raise ConfigurationError("请在交互终端运行 uv run qqbot admin --set-password。")
        first = getpass.getpass("设置管理密码（至少8位，输入不显示）：")
        second = getpass.getpass("再次输入管理密码：")
        if first != second:
            raise ConfigurationError("两次密码不一致，未修改。")
        passwords.set(first)
        print("管理密码已设置；密码哈希保存在本机 data/admin/，不会提交 Git。")
        if set_password:
            return
    print(f"管理页：http://127.0.0.1:{port}；配置保存后请重启机器人。")
    web.run_app(create_admin_app(root), host="127.0.0.1", port=port, access_log=None)
