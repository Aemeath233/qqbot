import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import event_payload

from qqbot.api import QQAPI, QQAPIError
from qqbot.dorm_state import DormState, UserContext, identity_tag
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


def set_binding(state, context, dormitory="33#4032", area="2"):
    state.request_binding(context, dormitory, area)
    request = state.latest_request(context)
    return state.finish_binding(context, request["request_id"], confirm=True)


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
    assert "modal" not in buttons[1]["action"]
    assert len({button["id"] for button in buttons}) == 3
    assert all(button["action"]["data"].startswith("electricity:v2:") for button in buttons)


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
    assert runtime.state.binding(user) is None
    prompt = Reply("", "", runtime.api.send_markdown.call_args.kwargs["keyboard"])
    await runtime.handle_event(interaction(action_data(prompt, 0), click_id="confirm-1"))
    await runtime.process(runtime.inbox.next())
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
        confirmation = state.request_binding(user, "33#2035", "2")
        assert "是否更换" in confirmation.text
        assert state.binding(user)["dormitory"] == "33#4032"
        request = state.latest_request(user)
        state.finish_binding(user, request["request_id"], confirm=True)
        assert state.binding(user)["dormitory"] == "33#2035"


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
    set_binding(runtime.state, UserContext("groups", "group-1", "sender-1"))
    reply = await runtime.assistant.generate(
        "chat", "查一下电费", context=UserContext("groups", "group-1", "other-user")
    )
    assert "还没有绑定" in reply
    runtime.assistant.electricity.query.assert_not_awaited()
    private = UserContext("users", "sender-1", "sender-1")
    assert runtime.state.binding(private)["dormitory"] == "33#4032"
    assert runtime.state.binding(UserContext("users", "different-id", "different-id")) is None


async def test_model_empty_dormitory_can_use_binding(runtime):
    user = UserContext("groups", "group-1", "sender-1")
    set_binding(runtime.state, user)
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


async def test_query_and_again_never_bind_in_either_group(runtime):
    first = UserContext("groups", "group-1", "sender-1")
    second = UserContext("groups", "group-2", "sender-1")
    runtime.assistant.model.complete.return_value = {
        "tool_calls": [
            {"function": {"name": "query_electricity", "arguments": '{"dormitory":"33#4032"}'}}
        ]
    }
    await runtime.assistant.generate("chat", "查33楼4032电费", context=first)
    reply = card(runtime)
    await runtime.handle_event(interaction(action_data(reply, 0), click_id="again-group-a"))
    await runtime.process(runtime.inbox.next())
    assert runtime.state.binding(first) is None and runtime.state.binding(second) is None
    assert runtime.state.latest_request(first) is None
    assert runtime.api.send_markdown.call_args.args[1] == "group-1"


def test_binding_is_shared_by_same_user_in_two_groups(runtime):
    first = UserContext("groups", "group-1", "sender-1")
    second = UserContext("groups", "group-2", "sender-1")
    set_binding(runtime.state, first, "33#4032")
    assert runtime.state.binding(second)["dormitory"] == "33#4032"
    set_binding(runtime.state, second, "33#2004")
    assert runtime.state.binding(first)["dormitory"] == "33#2004"
    assert runtime.state.binding(second)["dormitory"] == "33#2004"


async def test_cross_group_callback_cannot_query_or_bind(runtime):
    reply = card(runtime)
    await runtime.handle_event(interaction(action_data(reply, 0), group="group-2"))
    runtime.api.acknowledge_interaction.assert_awaited_once_with("click-1", 4)
    assert runtime.inbox.pending_count() == 0
    runtime.assistant.electricity.query.assert_not_awaited()
    runtime.api.send_markdown.assert_not_awaited()


async def test_button_id_mismatch_cannot_turn_query_into_bind(runtime):
    reply = card(runtime)
    buttons = reply.keyboard["content"]["rows"][0]["buttons"]
    event = interaction(action_data(reply, 1))
    event["d"]["data"]["resolved"]["button_id"] = buttons[0]["id"]
    await runtime.handle_event(event)
    runtime.api.acknowledge_interaction.assert_awaited_once_with("click-1", 4)
    assert runtime.state.latest_request(UserContext("groups", "group-1", "sender-1")) is None
    assert runtime.inbox.pending_count() == 0


def test_buttons_have_unique_ids_across_groups_and_messages(runtime):
    first = card(runtime)
    second = runtime.state.card(result(), UserContext("groups", "group-2", "sender-1"))
    third = card(runtime)
    ids = [
        b["id"]
        for reply in (first, second, third)
        for b in reply.keyboard["content"]["rows"][0]["buttons"]
    ]
    assert len(ids) == len(set(ids)) == 9


async def test_bind_other_room_requires_yes_and_no_keeps_old_binding(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    set_binding(runtime.state, context, "33#4032")
    prompt = runtime.state.request_binding(context, "33#2004", "2")
    assert "是否更换" in prompt.text
    assert runtime.state.binding(context)["dormitory"] == "33#4032"
    await runtime.handle_event(interaction(action_data(prompt, 1), click_id="say-no"))
    await runtime.process(runtime.inbox.next())
    assert runtime.state.binding(context)["dormitory"] == "33#4032"
    assert "已取消" in runtime.api.send_text.call_args.args[3]
    # 旧确认卡的“是”在已取消后也不能修改。
    await runtime.handle_event(interaction(action_data(prompt, 0), click_id="late-yes"))
    await runtime.process(runtime.inbox.next())
    assert runtime.state.binding(context)["dormitory"] == "33#4032"


async def test_natural_language_rebind_only_proposes_and_exact_yes_commits(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    set_binding(runtime.state, context, "33#4032")
    runtime.assistant.model.complete.return_value = {
        "tool_calls": [
            {"function": {"name": "request_dorm_binding", "arguments": '{"dormitory":"33#2004"}'}}
        ]
    }
    prompt = await runtime.assistant.generate("chat", "更换绑定宿舍为33楼2004室", context=context)
    assert [
        b["render_data"]["label"] for b in prompt.keyboard["content"]["rows"][0]["buttons"]
    ] == ["是", "否"]
    assert runtime.state.binding(context)["dormitory"] == "33#4032"
    runtime.assistant.electricity.query.assert_not_awaited()
    runtime.accept_event(event_payload(group=True, content="确认更换", message_id="explicit-yes"))
    await runtime.process(runtime.inbox.next())
    assert runtime.state.binding(context)["dormitory"] == "33#2004"


async def test_model_binding_request_cannot_write_without_confirmation(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    runtime.assistant.model.complete.return_value = {
        "tool_calls": [
            {"function": {"name": "request_dorm_binding", "arguments": '{"dormitory":"33#2004"}'}}
        ]
    }
    await runtime.assistant.generate("chat", "查33楼2004电费", context=context)
    assert runtime.state.binding(context) is None


def test_old_confirmation_cannot_overwrite_newer_binding(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(context, "33#4032", "2")
    old = runtime.state.latest_request(context)["request_id"]
    runtime.state.request_binding(context, "33#2004", "2")
    new = runtime.state.latest_request(context)["request_id"]
    runtime.state.finish_binding(context, new, confirm=True)
    stale = runtime.state.finish_binding(context, old, confirm=True)
    assert "失效" in stale.text
    assert runtime.state.binding(context)["dormitory"] == "33#2004"


def test_confirmation_is_scoped_to_user_and_group(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(context, "33#4032", "2")
    request_id = runtime.state.latest_request(context)["request_id"]
    for wrong in (
        UserContext("groups", "group-2", "sender-1"),
        UserContext("groups", "group-1", "other"),
    ):
        runtime.state.finish_binding(wrong, request_id, confirm=True)
        assert runtime.state.binding(wrong) is None
    assert runtime.state.binding(context) is None


def test_legacy_user_binding_is_migrated_and_shared_without_overwriting(runtime):
    import json

    old = json.dumps(["test-app", "groups", "sender-1"], separators=(",", ":"))
    runtime.inbox.db.execute("INSERT INTO dorm_bindings VALUES (?,?,?,?)", (old, "21#2011", "1", 0))
    runtime.inbox.db.commit()
    from qqbot.dorm_state import DormState

    state = DormState(runtime.inbox.db, "test-app")
    assert state.binding(UserContext("groups", "group-1", "sender-1"))["dormitory"] == "21#2011"
    assert state.binding(UserContext("groups", "group-2", "sender-1"))["dormitory"] == "21#2011"
    set_binding(state, UserContext("groups", "group-2", "sender-1"), "33#2004")
    again = DormState(runtime.inbox.db, "test-app")
    assert again.binding(UserContext("groups", "group-1", "sender-1"))["dormitory"] == "33#2004"


async def test_invalid_task_origin_never_sends_to_another_group(runtime):
    import json

    from qqbot.commands import ReplyTask
    from qqbot.messages import Message

    message = Message.from_payload(event_payload(group=True, content="query"))
    payload = {
        "version": 2,
        "action": "query",
        "origin_kind": "groups",
        "origin_target": "group-2",
        "origin_sender": "sender-1",
        "dormitory": "33#4032",
        "area": "2",
    }
    runtime.inbox.add_task(message, ReplyTask("button_query", json.dumps(payload)))
    await runtime.process(runtime.inbox.next())
    runtime.api.send_markdown.assert_not_awaited()
    runtime.api.send_text.assert_not_awaited()
    runtime.assistant.electricity.query.assert_not_awaited()


def test_restart_discards_unverified_legacy_button_tasks(runtime):
    from qqbot.commands import ReplyTask
    from qqbot.messages import Message

    message = Message.from_payload(event_payload(group=True, content="bind"))
    runtime.inbox.add_task(message, ReplyTask("button_bind", '{"dormitory":"21#2011","area":"1"}'))
    replacement = Runtime(runtime.settings, runtime.api, None)
    try:
        assert replacement.inbox.pending_count() == 0
        assert replacement.state.binding(UserContext("groups", "group-1", "sender-1")) is None
    finally:
        replacement.inbox.close()


def test_legacy_private_binding_can_be_safely_migrated(runtime):
    import json

    from qqbot.dorm_state import DormState

    old = json.dumps(["test-app", "users", "private-user"], separators=(",", ":"))
    runtime.inbox.db.execute("INSERT INTO dorm_bindings VALUES (?,?,?,?)", (old, "33#4032", "2", 0))
    runtime.inbox.db.commit()
    state = DormState(runtime.inbox.db, "test-app")
    assert (
        state.binding(UserContext("users", "private-user", "private-user"))["dormitory"]
        == "33#4032"
    )
    assert state.binding(UserContext("groups", "group-1", "private-user"))["dormitory"] == "33#4032"


async def test_card_from_group_a_is_never_sent_to_group_b(runtime):
    from qqbot.commands import ReplyTask
    from qqbot.messages import Message

    first_card = card(runtime)
    payload = event_payload(group=True, content="query")
    payload["d"]["group_openid"] = "group-2"
    message = Message.from_payload(payload)
    runtime.inbox.add_task(message, ReplyTask("text", "reply"))
    with pytest.raises(PermissionError):
        await runtime.send_reply(runtime.inbox.next(), first_card)
    runtime.api.send_markdown.assert_not_awaited()
    runtime.api.send_text.assert_not_awaited()


def test_binding_confirmation_does_not_coerce_non_boolean_values(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(context, "33#4032", "2")
    request = runtime.state.latest_request(context)
    with pytest.raises(ValueError):
        runtime.state.finish_binding(context, request["request_id"], confirm="false")
    assert runtime.state.binding(context) is None


async def test_same_user_queries_shared_binding_in_group_b_but_reply_stays_in_b(runtime):
    first = UserContext("groups", "group-1", "sender-1")
    set_binding(runtime.state, first, "21#2011", "1")
    event = event_payload(group=True, content="帮我查一下电费", message_id="group-b-query")
    event["d"]["group_openid"] = "group-2"
    runtime.accept_event(event)
    await runtime.process(runtime.inbox.next())
    runtime.assistant.electricity.query.assert_awaited_once_with("21#2011", area="1")
    assert runtime.api.send_markdown.call_args.args[1] == "group-2"
    assert runtime.state.binding(first)["dormitory"] == "21#2011"
    stages = [
        r[0]
        for r in runtime.inbox.db.execute(
            "SELECT stage FROM bot_operations WHERE trace=? ORDER BY id",
            (
                identity_tag(
                    runtime.inbox.db.execute(
                        "SELECT key FROM replies WHERE message_id='group-b-query'"
                    ).fetchone()[0]
                ),
            ),
        )
    ]
    assert stages == [
        "message_received",
        "task_queued",
        "task_started",
        "model_started",
        "tool_selected",
        "query_started",
        "query_finished",
        "buttons_created",
        "reply_attempt",
        "reply_sent",
        "task_done",
    ]


@pytest.mark.parametrize("word", ["是", "yes", "确认", "确定"])
async def test_casual_confirmation_word_never_saves_pending_binding(runtime, word):
    user = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(user, "33#4032", "2")
    runtime.assistant.model.complete.return_value = {"content": "请点击是，或回复确认绑定。"}
    runtime.accept_event(event_payload(group=True, content=word))
    await runtime.process(runtime.inbox.next())
    assert runtime.state.binding(user) is None
    assert runtime.state.latest_request(user) is not None
    runtime.assistant.model.complete.assert_awaited_once()


async def test_text_confirmation_in_other_group_cannot_confirm_group_a_prompt(runtime):
    first = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(first, "33#4032", "2")
    event = event_payload(group=True, content="确认绑定")
    event["d"]["group_openid"] = "group-2"
    runtime.assistant.model.complete.return_value = {"content": "请在发起绑定的群确认。"}
    runtime.accept_event(event)
    await runtime.process(runtime.inbox.next())
    assert runtime.state.binding(first) is None
    assert runtime.state.latest_request(first) is not None


async def test_duplicate_callback_with_changed_envelope_id_queries_once(runtime):
    event = interaction(action_data(card(runtime), 0))
    await runtime.handle_event(event)
    await runtime.process(runtime.inbox.next())
    event["id"] = "a-different-delivery-envelope"
    await runtime.handle_event(event)
    assert runtime.inbox.pending_count() == 0
    runtime.assistant.electricity.query.assert_awaited_once()


async def test_callback_id_reused_for_another_group_or_action_is_rejected_and_logged(runtime):
    first = card(runtime)
    second = runtime.state.card(result(), UserContext("groups", "group-2", "sender-1"))
    await runtime.handle_event(interaction(action_data(first, 0)))
    await runtime.process(runtime.inbox.next())
    await runtime.handle_event(interaction(action_data(second, 1), group="group-2"))
    runtime.api.acknowledge_interaction.assert_awaited_with("click-1", 4)
    assert runtime.inbox.pending_count() == 0
    assert runtime.state.latest_request(UserContext("groups", "group-2", "sender-1")) is None
    row = runtime.inbox.db.execute(
        "SELECT details FROM bot_operations WHERE stage='button_rejected' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row[0])["reason"] == "callback_id_reused_with_changed_fields"


async def test_raw_callback_evidence_is_logged_before_id_validation_without_tokens(runtime, caplog):
    reply = card(runtime)
    buttons = reply.keyboard["content"]["rows"][0]["buttons"]
    event = interaction(action_data(reply, 1))
    event["d"]["data"]["resolved"]["button_id"] = buttons[0]["id"]
    with caplog.at_level(logging.INFO):
        await runtime.handle_event(event)
    row = runtime.inbox.db.execute(
        "SELECT details FROM bot_operations WHERE stage='button_received' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    details = json.loads(row[0])
    assert details["data_action"] == details["stored_action"] == "bind"
    assert details["button"] == identity_tag(buttons[0]["id"])
    assert details["stored_button"] == identity_tag(buttons[1]["id"])
    assert details["source_group"] == identity_tag("group-1")
    assert "group-1" not in caplog.text and "sender-1" not in caplog.text
    assert action_data(reply, 1) not in caplog.text
    assert buttons[0]["id"] not in caplog.text
    assert runtime.state.binding(UserContext("groups", "group-1", "sender-1")) is None


def test_binding_and_durable_audit_roll_back_together(runtime):
    context = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(context, "33#4032", "2")
    request_id = runtime.state.latest_request(context)["request_id"]
    runtime.inbox.db.execute("""CREATE TRIGGER fail_binding_audit BEFORE INSERT ON bot_operations
        WHEN NEW.stage='binding_saved' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    runtime.inbox.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        runtime.state.finish_binding(context, request_id, confirm=True)
    assert runtime.state.binding(context) is None
    assert runtime.state.latest_request(context)["request_id"] == request_id
    assert not runtime.inbox.db.execute(
        "SELECT 1 FROM bot_operations WHERE stage='binding_saved'"
    ).fetchone()


def test_latest_legacy_group_binding_migrates_once_to_shared_user_binding(runtime):
    for group, room, stamp in [("group-1", "33#4032", 10), ("group-2", "33#2004", 20)]:
        runtime.inbox.db.execute(
            "INSERT INTO dorm_bindings VALUES (?,?,?,?)",
            (json.dumps(["test-app", "groups", group, "legacy-user"]), room, "2", stamp),
        )
    runtime.inbox.db.commit()
    state = DormState(runtime.inbox.db, "test-app", audit=runtime.audit)
    for group in ("group-1", "group-2"):
        assert state.binding(UserContext("groups", group, "legacy-user"))["dormitory"] == "33#2004"
    other_app = DormState(runtime.inbox.db, "other-app")
    assert other_app.binding(UserContext("groups", "group-1", "legacy-user")) is None
    set_binding(state, UserContext("groups", "group-1", "legacy-user"), "33#4032")
    reloaded = DormState(runtime.inbox.db, "test-app")
    assert (
        reloaded.binding(UserContext("groups", "group-2", "legacy-user"))["dormitory"] == "33#4032"
    )


def test_cancelled_binding_is_persisted_and_logged_after_commit(runtime, caplog):
    context = UserContext("groups", "group-1", "sender-1")
    runtime.state.request_binding(context, "33#4032", "2")
    request_id = runtime.state.latest_request(context)["request_id"]
    with caplog.at_level(logging.INFO):
        runtime.state.finish_binding(context, request_id, confirm=False)
    assert "binding_cancelled" in caplog.text
    assert runtime.inbox.db.execute(
        "SELECT 1 FROM bot_operations WHERE stage='binding_cancelled'"
    ).fetchone()
    assert runtime.state.binding(context) is None


async def test_audit_storage_failure_stops_worker_instead_of_silently_losing_operations(runtime):
    runtime.accept_event(event_payload(group=True, content="查电费"))
    runtime.inbox.db.execute("""CREATE TRIGGER fail_all_audit BEFORE INSERT ON bot_operations
        BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    runtime.inbox.db.commit()
    async with asyncio.timeout(2):
        with pytest.raises(RuntimeError, match="停止服务"):
            await runtime.work()
    assert isinstance(runtime.worker_error, sqlite3.IntegrityError)
    runtime.assistant.model.complete.assert_not_awaited()
    runtime.api.send_markdown.assert_not_awaited()
    runtime.api.send_text.assert_not_awaited()
