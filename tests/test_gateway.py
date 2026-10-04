import asyncio
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import event_payload

from qqbot.api import QQAPI
from qqbot.gateway import (
    GROUP_AND_C2C_INTENT,
    INTERACTION_INTENT,
    Gateway,
    GatewayFatalError,
    GatewayReconnect,
)
from qqbot.inbox import InboxFull
from qqbot.runtime import Runtime


def mock_app(handler):
    app = web.Application()

    async def token(request):
        return web.json_response({"access_token": "fake-access-token", "expires_in": 7200})

    async def gateway(request):
        return web.json_response({"url": f"wss://{request.host}/ws"})

    app.router.add_post("/app/getAppAccessToken", token)
    app.router.add_get("/gateway", gateway)
    app.router.add_get("/ws", handler)
    return app


def local_socket(session):
    # 仅测试把模拟网关的 WSS URL 换成本机 WS；生产连接始终校验 WSS。
    connect = session.ws_connect
    return patch.object(
        session,
        "ws_connect",
        side_effect=lambda url, **kw: connect(url.replace("wss:", "ws:"), **kw),
    )


async def handshake(request, *, interval=1000):
    socket = web.WebSocketResponse()
    await socket.prepare(request)
    await socket.send_json({"op": 10, "d": {"heartbeat_interval": interval}})
    auth = await socket.receive_json()
    assert auth["d"]["token"] == "QQBot fake-access-token"
    return socket, auth


async def send_ready(socket):
    await socket.send_json({"op": 0, "t": "READY", "s": 1, "d": {"session_id": "session-1"}})


async def test_identify_check_and_resume(settings):
    received = []

    async def handler(request):
        socket, auth = await handshake(request)
        received.append(auth)
        if auth["op"] == 2:
            await send_ready(socket)
        else:
            await socket.send_json({"op": 0, "t": "RESUMED", "s": 2, "d": {}})
        await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        gateway = Gateway(
            QQAPI(settings, session, base_url=str(server.make_url(""))), lambda p: None
        )
        with local_socket(session):
            await gateway.connect(check_only=True)
            await gateway.connect(check_only=True)
    assert received[0]["d"]["intents"] == GROUP_AND_C2C_INTENT | INTERACTION_INTENT
    assert received[0]["d"]["shard"] == [0, 1]
    assert received[1] == {
        "op": 6,
        "d": {"token": "QQBot fake-access-token", "session_id": "session-1", "seq": 1},
    }
    assert gateway.sequence == 2


async def test_group_and_private_events_replay_dedup(settings):
    async def handler(request):
        socket, _ = await handshake(request)
        await send_ready(socket)
        for number, group in enumerate((True, True, False), start=2):
            payload = event_payload(group=group, content="查一下33号楼2035室还有多少电")
            payload["s"] = number
            await socket.send_json(payload)
        await socket.send_json({"op": 7})
        await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        runtime = Runtime(settings, api, None)
        gateway = Gateway(api, runtime.accept_event)
        try:
            with local_socket(session), pytest.raises(GatewayReconnect, match="要求重连"):
                await gateway.connect()
            assert runtime.inbox.pending_count() == 2
            assert gateway.sequence == 4
        finally:
            runtime.inbox.close()


@pytest.mark.parametrize("ack", [True, False])
async def test_heartbeat_ack_and_missing_ack(settings, ack):
    beats = []

    async def handler(request):
        socket, _ = await handshake(request, interval=40)
        await send_ready(socket)
        beats.append(await socket.receive_json())
        if ack:
            await socket.send_json({"op": 11})
            beats.append(await socket.receive_json())
            await socket.send_json({"op": 11})
            await socket.send_json({"op": 7})
        await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        gateway = Gateway(
            QQAPI(settings, session, base_url=str(server.make_url(""))), lambda p: None
        )
        match = "要求重连" if ack else "未收到心跳 ACK"
        with local_socket(session), pytest.raises(GatewayReconnect, match=match):
            async with asyncio.timeout(3):
                await gateway.connect()
    assert beats == [{"op": 1, "d": 1}] * (2 if ack else 1)


async def test_inbox_full_does_not_advance_sequence(settings):
    def reject(payload):
        raise InboxFull

    async def handler(request):
        socket, _ = await handshake(request)
        await send_ready(socket)
        await socket.send_json({**event_payload(), "s": 2})
        await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        gateway = Gateway(QQAPI(settings, session, base_url=str(server.make_url(""))), reject)
        with local_socket(session), pytest.raises(InboxFull):
            await gateway.connect()
    assert gateway.sequence == 1


@pytest.mark.parametrize("code,reset,fatal", [(4006, True, False), (4014, False, True)])
async def test_close_codes(settings, code, reset, fatal):
    async def handler(request):
        socket, _ = await handshake(request)
        await send_ready(socket)
        await socket.close(code=code, message=b"fake-access-token must not be logged")
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        gateway = Gateway(
            QQAPI(settings, session, base_url=str(server.make_url(""))), lambda p: None
        )
        gateway.api.buttons_enabled = False
        with (
            local_socket(session),
            pytest.raises(GatewayFatalError if fatal else GatewayReconnect) as caught,
        ):
            await gateway.connect()
    assert (gateway.session_id is None) == reset
    assert "fake-access-token" not in str(caught.value)


async def test_button_permission_failure_falls_back_to_basic_events(settings):
    intents = []

    async def handler(request):
        socket, auth = await handshake(request)
        intents.append(auth["d"]["intents"])
        if len(intents) == 1:
            await socket.close(code=4014)
        else:
            await send_ready(socket)
            await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        gateway = Gateway(api, lambda p: None)
        with local_socket(session):
            with pytest.raises(GatewayReconnect, match="按钮事件权限不足"):
                await gateway.connect()
            await gateway.connect(check_only=True)
    assert intents == [GROUP_AND_C2C_INTENT | INTERACTION_INTENT, GROUP_AND_C2C_INTENT]


async def test_invalid_session_resumes_then_identifies(settings):
    attempts = []

    async def handler(request):
        socket, auth = await handshake(request)
        attempts.append(auth["op"])
        if auth["op"] == 6:
            await socket.send_json({"op": 9, "d": False})
        else:
            await send_ready(socket)
        await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        gateway = Gateway(
            QQAPI(settings, session, base_url=str(server.make_url(""))), lambda p: None
        )
        gateway.session_id, gateway.sequence = "old-session", 42
        with local_socket(session):
            with pytest.raises(GatewayReconnect, match="会话失效"):
                await gateway.connect()
            await gateway.connect(check_only=True)
    assert attempts == [6, 2]


async def test_generated_reply_completes_without_task_error(settings):
    class FakeAPI:
        async def send_text(self, *args):
            sent.append(args)

    sent = []
    runtime = Runtime(settings, FakeAPI(), None)
    try:
        runtime.accept_event(event_payload(content="/help"))
        await runtime.process(runtime.inbox.next())
        assert len(sent) == 1
        assert runtime.inbox.pending_count() == 0
    finally:
        runtime.inbox.close()


async def test_run_reconnects_and_resumes(settings):
    attempts, events, delays = [], [], []
    finished = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    def on_event(payload):
        events.append(payload)
        finished.set()

    async def handler(request):
        socket, auth = await handshake(request)
        attempts.append(auth)
        if len(attempts) == 1:
            await send_ready(socket)
            await socket.send_json({"op": 7})
        else:
            await socket.send_json({"op": 0, "t": "RESUMED", "s": 2, "d": {}})
            await socket.send_json({**event_payload(), "s": 3})
        await socket.receive()
        return socket

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        gateway = Gateway(api, on_event)
        with local_socket(session), patch("qqbot.gateway.asyncio.sleep", new=fast_sleep):
            task = asyncio.create_task(gateway.run())
            try:
                await asyncio.wait_for(finished.wait(), timeout=3)
                assert gateway.sequence == 3
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert [attempt["op"] for attempt in attempts] == [2, 6]
    assert attempts[1]["d"]["seq"] == 1
    assert len(events) == len(delays) == 1
    assert 5 <= delays[0] <= 6


async def test_rejected_handshake_is_actionable_and_hides_response(settings):
    async def handler(request):
        return web.Response(status=403, text="fake-access-token")

    async with TestServer(mock_app(handler)) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        with local_socket(session), pytest.raises(GatewayFatalError, match="HTTP=403") as caught:
            await Gateway(api, lambda p: None).run()
    assert "fake-access-token" not in str(caught.value)


@pytest.mark.parametrize("url", ["ws://example.com", "wss://user:secret@example.com", None])
async def test_invalid_gateway_url_never_sends_token(url):
    class FakeAPI:
        async def gateway(self):
            return {"url": url}

        async def access_token(self):
            pytest.fail("invalid gateway must not receive a token")

    with pytest.raises(GatewayFatalError, match="无效的 WSS"):
        await Gateway(FakeAPI(), lambda p: None).connect()
