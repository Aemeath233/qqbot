import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qqbot.api import QQAPI, QQAPIError


async def test_token_cache_and_group_private_routes(settings):
    token_calls = []
    messages = []

    async def token(request):
        token_calls.append(await request.json())
        return web.json_response({"access_token": "access-secret", "expires_in": "7200"})

    async def send(request):
        assert request.headers["Authorization"] == "QQBot access-secret"
        messages.append((request.path, await request.json()))
        return web.json_response({"id": "reply-1"})

    app = web.Application()
    app.router.add_post("/app/getAppAccessToken", token)
    app.router.add_post("/v2/{kind}/{target}/messages", send)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        tokens = await asyncio.gather(*(api.access_token() for _ in range(10)))
        assert tokens == ["access-secret"] * 10
        await api.send_text("groups", "group-1", "msg-group", "群回复")
        await api.send_text("users", "user-1", "msg-user", "私聊回复")
    assert len(token_calls) == 1
    assert token_calls[0] == {"appId": settings.app_id, "clientSecret": settings.app_secret}
    assert messages[0][0] == "/v2/groups/group-1/messages"
    assert messages[1][0] == "/v2/users/user-1/messages"
    assert messages[0][1] == {
        "msg_type": 0,
        "content": "群回复",
        "msg_id": "msg-group",
        "msg_seq": 1,
    }


@pytest.mark.parametrize("error_key", ["code", "err_code"])
async def test_business_error_even_with_http_200(settings, error_key):
    async def token(request):
        return web.json_response({error_key: 100016, "message": settings.app_secret})

    app = web.Application()
    app.router.add_post("/app/getAppAccessToken", token)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        with pytest.raises(QQAPIError) as caught:
            await api.access_token()
        assert caught.value.code == "100016"
        assert settings.app_secret not in str(caught.value)
        assert not caught.value.retryable


async def test_401_refreshes_token_once(settings):
    token_calls = 0
    api_calls = 0

    async def token(request):
        nonlocal token_calls
        token_calls += 1
        return web.json_response({"access_token": f"access-{token_calls}", "expires_in": 7200})

    async def me(request):
        nonlocal api_calls
        api_calls += 1
        if api_calls == 1:
            return web.json_response({"code": 11243}, status=401)
        assert request.headers["Authorization"] == "QQBot access-2"
        return web.json_response({"username": "测试机器人"})

    app = web.Application()
    app.router.add_post("/app/getAppAccessToken", token)
    app.router.add_get("/users/@me", me)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        assert (await api.me())["username"] == "测试机器人"
    assert token_calls == api_calls == 2


async def test_token_expiry_and_invalid_response(settings):
    calls = 0

    async def token(request):
        nonlocal calls
        calls += 1
        if calls == 3:
            return web.Response(text=json.dumps({"access_token": "token", "expires_in": "nan"}))
        return web.json_response({"access_token": f"token-{calls}", "expires_in": 7200})

    app = web.Application()
    app.router.add_post("/app/getAppAccessToken", token)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        assert await api.access_token() == "token-1"
        api._refresh_at = 0
        assert await api.access_token() == "token-2"
        api._refresh_at = 0
        with pytest.raises(QQAPIError, match="invalid_token_response"):
            await api.access_token()
