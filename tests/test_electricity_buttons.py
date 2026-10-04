import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import event_payload

from qqbot.api import QQAPI, QQAPIError
from qqbot.dorm_state import UserContext
from qqbot.interactions import ButtonClick
from qqbot.light_assistant import LightAssistant
from qqbot.presentation import Reply, electricity_reply
from qqbot.runtime import Runtime


def result(quantity="10.47"):
    return {
        "ok": True,
        "dormitory": "33#4032",
        "remaining_kwh": quantity,
        "area": "2",
        "area_name": "7—10、30—33号楼",
        "queried_at": "2026-10-04 17:25:00",
    }


def interaction(data, *, sender="sender-1", group="group-1", click_id="click-1"):
    return {
        "op": 0,
        "t": "INTERACTION_CREATE",
        "id": "INTERACTION_CREATE:" + click_id,
        "d": {
            "id": click_id,
            "type": 11,
            "scene": "group",
            "application_id": "test-app",
            "group_openid": group,
            "group_member_openid": sender,
            "data": {"type": 11, "resolved": {"button_data": data}},
        },
    }


@pytest.fixture
def runtime(settings):
    configured = replace(settings, llm_enabled=True, electricity_enabled=True)
    assistant = LightAssistant(configured, None)
    assistant.electricity.query = AsyncMock(return_value=result())
    assistant.model.complete = AsyncMock(
        return_value={
            "tool_calls": [{"function": {"name": "query_electricity", "arguments": "{}"}}]
        }
    )
    api = AsyncMock()
    api.markdown_enabled = api.buttons_enabled = True
    bot = Runtime(configured, api, assistant)
    yield bot
    bot.inbox.close()


def card(runtime):
    return runtime.state.card(result(), UserContext("groups", "group-1", "sender-1"))


def action_data(reply, index):
    return reply.keyboard["content"]["rows"][0]["buttons"][index]["action"]["data"]


@pytest.mark.parametrize(
    "quantity,low", [("49.99", True), ("50", False), ("50.01", False), ("0", True)]
)
def test_markdown_and_exact_low_threshold(quantity, low):
    reply = electricity_reply(result(quantity))
    assert "**" in reply.markdown
    assert ("电量已经不多了" in reply.text) == low
    assert ("电量已经不多了" in reply.markdown) == low


def test_buttons_are_callbacks_with_owner_permission_and_bind_confirmation(runtime):
    reply = card(runtime)
    buttons = reply.keyboard["content"]["rows"][0]["buttons"]
    assert [b["render_data"]["label"] for b in buttons] == ["再次查询", "绑定此宿舍", "使用帮助"]
    assert all(b["action"]["type"] == 1 for b in buttons)
    assert all(
        b["action"]["permission"] == {"type": 0, "specify_user_ids": ["sender-1"]} for b in buttons
    )
    assert "不可更换" in buttons[1]["action"]["modal"]["content"]
    assert len(buttons[1]["action"]["modal"]["content"]) <= 40


async def test_again_queries_without_model_and_replies_using_event_id(runtime):
    click = interaction(action_data(card(runtime), 0))
    await runtime.handle_event(click)
    runtime.api.acknowledge_interaction.assert_awaited_once_with("click-1", 0)
    await runtime.process(runtime.inbox.next())
    runtime.assistant.model.complete.assert_not_awaited()
    runtime.assistant.electricity.query.assert_awaited_once_with("33#4032", area="2")
    assert runtime.api.send_markdown.call_args.kwargs["reference"] == "event_id"
    assert runtime.api.send_markdown.call_args.args[2] == "INTERACTION_CREATE:click-1"
    assert runtime.inbox.pending_count() == 0


async def test_binding_then_natural_query_uses_default_and_persists(runtime):
    await runtime.handle_event(interaction(action_data(card(runtime), 1)))
    await runtime.process(runtime.inbox.next())
    user = UserContext("groups", "group-1", "sender-1")
    assert runtime.state.binding(user)["dormitory"] == "33#4032"
    reply = await runtime.assistant.generate("chat", "查一下电费", context=user)
    assert isinstance(reply, Reply)
    runtime.assistant.electricity.query.assert_awaited_once_with("33#4032", area="2")
    prompt = runtime.assistant.model.complete.call_args.args[0][0]["content"]
    assert "已绑定默认宿舍：33#4032" in prompt
    # 用同一个数据库重新加载，不会覆盖既有绑定。
    from qqbot.dorm_state import DormState

    with sqlite3.connect(runtime.settings.db_path) as db:
        db.row_factory = sqlite3.Row
        state = DormState(db, "test-app")
        denied = state.bind_once(user, "33#2035", "2")
        assert "不能更换" in denied.text
        assert state.binding(user)["dormitory"] == "33#4032"


async def test_unbound_model_omission_asks_for_room(runtime):
    reply = await runtime.assistant.generate(
        "chat", "查电费", context=UserContext("groups", "group-1", "sender-1")
    )
    assert "还没有绑定" in reply
    runtime.assistant.electricity.query.assert_not_awaited()


async def test_help_does_not_query_or_bind(runtime):
    await runtime.handle_event(interaction(action_data(card(runtime), 2)))
    await runtime.process(runtime.inbox.next())
    runtime.assistant.electricity.query.assert_not_awaited()
    runtime.assistant.model.complete.assert_not_awaited()
    assert "只查当前剩余电量" in runtime.api.send_markdown.call_args.args[3]


@pytest.mark.parametrize("sender,group", [("other-user", "group-1"), ("sender-1", "other-group")])
async def test_foreign_user_or_group_cannot_use_buttons(runtime, sender, group):
    await runtime.handle_event(
        interaction(action_data(card(runtime), 1), sender=sender, group=group)
    )
    runtime.api.acknowledge_interaction.assert_awaited_once_with("click-1", 4)
    assert runtime.inbox.pending_count() == 0
    assert runtime.state.binding(UserContext("groups", "group-1", "sender-1")) is None


async def test_duplicate_button_delivery_does_not_repeat_query(runtime):
    event = interaction(action_data(card(runtime), 0))
    await runtime.handle_event(event)
    await runtime.handle_event(event)
    assert runtime.inbox.pending_count() == 1
    await runtime.process(runtime.inbox.next())
    await runtime.handle_event(event)
    assert runtime.inbox.pending_count() == 0
    runtime.assistant.electricity.query.assert_awaited_once()


def test_expired_button_is_not_used(runtime):
    reply = card(runtime)
    runtime.inbox.db.execute("UPDATE electricity_buttons SET expires_at=0")
    runtime.inbox.db.commit()
    assert (
        runtime.state.action(action_data(reply, 0), UserContext("groups", "group-1", "sender-1"))
        is None
    )


async def test_markdown_permission_failure_falls_back_to_plaintext_without_requery(runtime):
    runtime.api.send_markdown.side_effect = QQAPIError(200, "40034127")
    runtime.accept_event(event_payload(group=True, content="查电费"))
    job = runtime.inbox.next()
    await runtime.send_reply(job, card(runtime))
    runtime.api.send_text.assert_awaited_once()
    assert "10.47" in runtime.api.send_text.call_args.args[3]
    assert runtime.api.markdown_enabled is False
    row = runtime.inbox.next()
    assert Reply.loads(row["reply_json"]).markdown is None
    runtime.assistant.electricity.query.assert_not_awaited()


async def test_keyboard_rejection_keeps_markdown(runtime):
    runtime.api.send_markdown.side_effect = [QQAPIError(200, "305007"), {}]
    runtime.accept_event(event_payload(group=True, content="查电费"))
    await runtime.send_reply(runtime.inbox.next(), card(runtime))
    assert runtime.api.send_markdown.await_count == 2
    assert runtime.api.send_markdown.call_args.kwargs["keyboard"] is None
    runtime.api.send_text.assert_not_awaited()


async def test_uncertain_network_failure_never_falls_back_to_duplicate_send(runtime):
    runtime.api.send_markdown.side_effect = TimeoutError()
    runtime.accept_event(event_payload(group=True, content="查电费"))
    with pytest.raises(TimeoutError):
        await runtime.send_reply(runtime.inbox.next(), card(runtime))
    runtime.api.send_text.assert_not_awaited()


async def test_wire_markdown_and_interaction_ack(settings):
    bodies = []

    async def token(request):
        return web.json_response({"access_token": "test-token", "expires_in": 7200})

    async def send(request):
        bodies.append(await request.json())
        return web.json_response({"id": "sent"})

    async def ack(request):
        assert request.path == "/interactions/click-1"
        assert await request.json() == {"code": 0}
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/app/getAppAccessToken", token)
    app.router.add_post("/v2/groups/group-1/messages", send)
    app.router.add_put("/interactions/click-1", ack)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        api = QQAPI(settings, session, base_url=str(server.make_url("")))
        await api.send_markdown(
            "groups",
            "group-1",
            "event-1",
            "**10.47 度**",
            keyboard={"content": {"rows": []}},
            reference="event_id",
        )
        await api.acknowledge_interaction("click-1")
    assert bodies[0]["msg_type"] == 2
    assert "content" not in bodies[0] and "msg_id" not in bodies[0]
    assert bodies[0]["event_id"] == "event-1"
    assert "keyboard" in bodies[0]


def test_interaction_scene_and_ack_id_validation():
    click = ButtonClick.parse(interaction("electricity:token"), "test-app")
    assert click.ack_id == "click-1"
    assert click.message.reference == "event_id"
    bad = interaction("electricity:token")
    bad["d"]["application_id"] = "different-app"
    with pytest.raises(ValueError):
        ButtonClick.parse(bad, "test-app")


async def test_default_binding_does_not_leak_to_other_users(runtime):
    runtime.state.bind_once(UserContext("groups", "group-1", "sender-1"), "33#4032", "2")
    reply = await runtime.assistant.generate(
        "chat", "查一下电费", context=UserContext("groups", "group-1", "other-user")
    )
    assert "还没有绑定" in reply
    runtime.assistant.electricity.query.assert_not_awaited()
    private = UserContext("users", "sender-1", "sender-1")
    assert runtime.state.binding(private) is None


async def test_model_empty_dormitory_can_use_binding(runtime):
    user = UserContext("groups", "group-1", "sender-1")
    runtime.state.bind_once(user, "33#4032", "2")
    runtime.assistant.model.complete.return_value = {
        "tool_calls": [{"function": {"name": "query_electricity", "arguments": '{"dormitory":""}'}}]
    }
    await runtime.assistant.generate("chat", "查一下电费", context=user)
    runtime.assistant.electricity.query.assert_awaited_once_with("33#4032", area="2")


async def test_markdown_send_retry_reuses_generated_reply_and_buttons(runtime):
    runtime.assistant.model.complete.return_value = {
        "tool_calls": [
            {"function": {"name": "query_electricity", "arguments": '{"dormitory":"33#4032"}'}}
        ]
    }
    runtime.api.send_markdown.side_effect = [QQAPIError(500), {}]
    runtime.accept_event(event_payload(group=True, content="看看33楼4032宿舍电费"))
    await runtime.process(runtime.inbox.next())
    first_buttons = runtime.api.send_markdown.call_args.kwargs["keyboard"]
    runtime.inbox.db.execute("UPDATE replies SET next_try_at=0")
    runtime.inbox.db.commit()
    await runtime.process(runtime.inbox.next())
    runtime.assistant.electricity.query.assert_awaited_once()
    runtime.assistant.model.complete.assert_awaited_once()
    assert runtime.api.send_markdown.call_args.kwargs["keyboard"] == first_buttons
    assert runtime.inbox.pending_count() == 0


async def test_websocket_button_to_ack_query_and_markdown_wire(runtime):
    from test_gateway import handshake, local_socket, mock_app, send_ready

    from qqbot.gateway import Gateway, GatewayReconnect

    sent = asyncio.Event()
    acks, bodies = [], []
    button = action_data(card(runtime), 0)

    async def handler(request):
        socket, _ = await handshake(request)
        await send_ready(socket)
        await socket.send_json({**interaction(button), "s": 2})
        await sent.wait()
        await socket.send_json({"op": 7})
        await socket.receive()
        return socket

    async def acknowledge(request):
        acks.append((request.path, await request.json()))
        return web.Response(status=204)

    async def send(request):
        bodies.append(await request.json())
        sent.set()
        return web.json_response({"id": "reply"})

    app = mock_app(handler)
    app.router.add_put("/interactions/click-1", acknowledge)
    app.router.add_post("/v2/groups/group-1/messages", send)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        api = QQAPI(runtime.settings, session, base_url=str(server.make_url("")))
        runtime.api = api
        worker = asyncio.create_task(runtime.work())
        runtime.worker = worker
        try:
            with local_socket(session), pytest.raises(GatewayReconnect, match="要求重连"):
                async with asyncio.timeout(3):
                    await Gateway(api, runtime.handle_event).connect()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    assert acks == [("/interactions/click-1", {"code": 0})]
    assert len(bodies) == 1
    assert bodies[0]["event_id"] == "INTERACTION_CREATE:click-1"
    assert "**10.47 度**" in bodies[0]["markdown"]["content"]
    assert len(bodies[0]["keyboard"]["content"]["rows"][0]["buttons"]) == 3
    runtime.assistant.model.complete.assert_not_awaited()
    runtime.assistant.electricity.query.assert_awaited_once()
