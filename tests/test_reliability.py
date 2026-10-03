import asyncio
import json
import sqlite3
import time
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import event_payload, signed_payload
from test_toolpacks import archive

from qqbot.admin import AdminElectricity
from qqbot.api import QQAPIError
from qqbot.assistant import BotAssistant
from qqbot.commands import ReplyTask
from qqbot.config import Settings
from qqbot.electricity import ElectricityClient, ElectricityError
from qqbot.electricity_probe import ProbeError, RequestGate
from qqbot.inbox import INTERRUPTED_REPLY, Inbox, InboxFull
from qqbot.messages import Message
from qqbot.portal_store import PortalStore
from qqbot.server import RUNTIME, create_app
from qqbot.toolpacks import ToolPackStore
from qqbot.tools import FunctionTool, ToolRegistry
from qqbot.user_store import user_id


class Recorder:
    def __init__(self):
        self.calls = []
        self.called = asyncio.Event()

    async def send_text(self, *args):
        self.calls.append(args)
        self.called.set()


async def send(client, settings, text, identifier, *, group=False, author=None):
    payload = event_payload(content=text, message_id=identifier, group=group)
    if author:
        payload["d"]["author"]["member_openid" if group else "user_openid"] = author
    body, headers = signed_payload(settings, payload)
    assert (await client.post("/qqbot", data=body, headers=headers)).status == 200


async def eventually(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected asynchronous result was not produced")


async def test_restart_never_replays_a_real_mcp_side_effect(settings):
    source = '''from pathlib import Path
def increment() -> int:
    """Increase a persistent counter in this temporary tool package."""
    path = Path(__file__).with_name("counter.txt")
    value = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(value))
    return value
'''
    store = ToolPackStore(settings.toolpacks_dir)
    store.install("demo.zip", archive(source, config={"exports": ["increment"]}))
    store.enable("demo", True, True)
    settings = replace(settings, llm_enabled=True)
    after_tool = asyncio.Event()

    class Model:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "increment-once",
                            "type": "function",
                            "function": {"name": "mcp__demo__increment", "arguments": "{}"},
                        }
                    ],
                }
            after_tool.set()
            await asyncio.Event().wait()

    counter = settings.toolpacks_dir / "demo/counter.txt"
    async with aiohttp.ClientSession() as session:
        first_model = Model()
        assistant = BotAssistant(settings, session, model=first_model)
        async with TestClient(
            TestServer(create_app(settings, api=Recorder(), assistant=assistant))
        ) as client:
            await send(client, settings, "增加计数一次", "same-event")
            await asyncio.wait_for(after_tool.wait(), 15)
            row = client.app[RUNTIME].inbox.db.execute("SELECT * FROM replies").fetchone()
            assert row["generation_started"] == 1 and row["prepared"] == 0
            assert counter.read_text() == "1"
        second_model = Model()
        assistant = BotAssistant(settings, session, model=second_model)
        api = Recorder()
        async with TestClient(
            TestServer(create_app(settings, api=api, assistant=assistant))
        ) as client:
            await asyncio.wait_for(api.called.wait(), 3)
            assert api.calls[0][-1] == INTERRUPTED_REPLY
            assert second_model.calls == 0
            assert counter.read_text() == "1"
            await send(client, settings, "增加计数一次", "same-event")
            assert len(api.calls) == 1


def test_upgrade_treats_untracked_old_generations_as_unknown(settings):
    inbox = Inbox(settings.db_path)
    message = Message("users", "alice", "old-job", "write", time.time() + 60)
    inbox.add_task(message, ReplyTask("chat", "write"))
    inbox.close()
    with sqlite3.connect(settings.db_path) as db:
        db.execute("ALTER TABLE replies DROP COLUMN generation_started")
    inbox = Inbox(settings.db_path)
    assert inbox.next()["generation_started"] == 1
    inbox.close()


async def test_unstarted_generation_is_still_recovered(settings):
    inbox = Inbox(settings.db_path)
    message = Message("users", "alice", "unstarted", "write", time.time() + 60)
    inbox.add_task(message, ReplyTask("chat", "write"))
    inbox.close()

    class Assistant:
        llm_enabled = True

        async def generate(self, *args, **kwargs):
            return "ran once"

    api = Recorder()
    async with TestClient(TestServer(create_app(settings, api=api, assistant=Assistant()))):
        await asyncio.wait_for(api.called.wait(), 3)
        assert api.calls[0][-1] == "ran once"


@pytest.mark.parametrize("depth", [33, 3000])
async def test_deep_arguments_are_rejected_before_handler(depth):
    called = []

    async def handler(**values):
        called.append(values)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            "page",
            "page",
            {"type": "object"},
            handler,
            full_schema=True,
            arguments_limit=100000,
        )
    )
    result = await registry.execute("page", "[" * depth + "0" + "]" * depth)
    assert result["error"] == "invalid_arguments" and not called


async def test_html_braces_and_escaped_quotes_do_not_count_as_json_depth():
    async def handler(html):
        return {"ok": True, "html": html}

    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            "page",
            "page",
            {"type": "object", "properties": {"html": {"type": "string"}}},
            handler,
            full_schema=True,
            arguments_limit=100000,
        )
    )
    html = '<script>const data = "\\' + "[]{}" * 500 + '";</script>'
    assert (await registry.execute("page", json.dumps({"html": html})))["html"] == html


async def test_overflowing_json_numbers_never_reach_handler():
    async def handler(value):
        raise AssertionError("invalid number was executed")

    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            "number",
            "number",
            {"type": "object", "properties": {"value": {"type": "number"}}},
            handler,
            full_schema=True,
        )
    )
    assert (await registry.execute("number", '{"value":1e999}'))["error"] == "invalid_arguments"


async def test_unexpected_tool_failure_is_redacted_and_not_retried():
    calls = []

    async def handler():
        calls.append(True)
        raise RuntimeError("synthetic-private-token")

    registry = ToolRegistry()
    registry.register(
        FunctionTool("broken", "broken", {"type": "object"}, handler, full_schema=True)
    )
    result = await registry.execute("broken", "{}")
    assert result["error"] == "tool_failed" and "private" not in json.dumps(result)
    assert calls == [True]


async def test_unexpected_generation_error_does_not_kill_worker(settings, caplog):
    class Assistant:
        llm_enabled = True

        async def generate(self, *args, **kwargs):
            raise RuntimeError("synthetic-private-token")

    api = Recorder()
    app = create_app(settings, api=api, assistant=Assistant())
    async with TestClient(TestServer(app)) as client:
        await send(client, settings, "bad model", "bad")
        await asyncio.wait_for(api.called.wait(), 3)
        assert "处理失败" in api.calls[0][-1]
        await send(client, settings, "/ping", "good")
        await eventually(lambda: len(api.calls) == 2)
        assert "pong" in api.calls[1][-1]
        assert (await client.get("/healthz")).status == 200
    assert "synthetic-private-token" not in caplog.text


async def test_deep_webpage_tool_arguments_do_not_break_real_assistant(settings):
    settings = replace(
        settings, llm_enabled=True, portal_enabled=True, public_base_url="https://bot.example.test"
    )

    class Model:
        async def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                return {"role": "assistant", "content": "done"}
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "bad",
                        "type": "function",
                        "function": {
                            "name": "create_webpage",
                            "arguments": "[" * 3000 + "0" + "]" * 3000,
                        },
                    }
                ],
            }

    api = Recorder()
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=Model())
        async with TestClient(
            TestServer(create_app(settings, api=api, assistant=assistant))
        ) as client:
            await send(client, settings, "请生成网页", "bad-page")
            await asyncio.wait_for(api.called.wait(), 3)
            assert "有效" in api.calls[0][-1]
            await send(client, settings, "/ping", "good")
            await eventually(lambda: len(api.calls) == 2)
            assert "pong" in api.calls[1][-1]
            assert (await client.get("/healthz")).status == 200
    assert not assistant.portal_store.list_pages()


async def test_send_retry_keeps_own_order_without_blocking_other_context(settings):
    class RetryAPI(Recorder):
        async def send_text(self, *args):
            self.calls.append(args)
            self.called.set()
            if len(self.calls) == 1:
                raise QQAPIError(503)

    api = RetryAPI()
    async with TestClient(TestServer(create_app(settings, api=api))) as client:
        await send(client, settings, "/ping", "retry", author="alice")
        await asyncio.wait_for(api.called.wait(), 1)
        await send(client, settings, "/ping", "same", author="alice")
        await send(client, settings, "/ping", "other", group=True)
        await eventually(lambda: len(api.calls) == 2)
        assert [call[2] for call in api.calls] == ["retry", "other"]
        inbox = client.app[RUNTIME].inbox
        with inbox.db:
            inbox.db.execute("UPDATE replies SET next_try_at=0 WHERE message_id='retry'")
        client.app[RUNTIME].wakeup.set()
        await eventually(lambda: len(api.calls) == 4)
        assert [call[2] for call in api.calls] == ["retry", "other", "retry", "same"]


async def test_qq_electricity_429_blocks_all_clients_and_test_entry(settings):
    calls = []

    async def upstream(request):
        calls.append(await request.json())
        return web.Response(status=429, headers={"Retry-After": "1800"})

    app = web.Application()
    app.router.add_post("/trade", upstream)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        settings = replace(
            settings, electricity_enabled=True, electricity_endpoint=str(server.make_url("/trade"))
        )
        clients = [
            ElectricityClient(settings, session),
            AdminElectricity(settings, session, settings.electricity_cooldown_path),
        ]
        with pytest.raises(ElectricityError, match="限流"):
            await clients[0].query("33#2035")
        for client in clients:
            with pytest.raises(ElectricityError) as error:
                await client.query("33#4032")
            assert error.value.code == "cooldown"
        gate = RequestGate(settings.electricity_cooldown_path)
        try:
            with pytest.raises(ProbeError):
                gate.reserve()
            assert (
                gate.db.execute("SELECT next_allowed FROM cooldown").fetchone()[0]
                > time.time() + 1790
            )
        finally:
            gate.close()
    assert len(calls) == 1


async def test_success_cache_is_free_but_different_room_and_new_client_are_limited(settings):
    calls = []

    async def upstream(request):
        calls.append(True)
        return web.json_response(
            {"returncode": "SUCCESS", "businessData": {"quantity": "47.93", "quantityunit": "度"}}
        )

    app = web.Application()
    app.router.add_post("/trade", upstream)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        settings = replace(
            settings, electricity_enabled=True, electricity_endpoint=str(server.make_url("/trade"))
        )
        client = ElectricityClient(settings, session)
        await client.query("33#2035")
        assert (await client.query("33#2035"))["cached"]
        for other, dorm in [(client, "33#4032"), (ElectricityClient(settings, session), "33#2035")]:
            with pytest.raises(ElectricityError) as error:
                await other.query(dorm)
            assert error.value.code == "cooldown"
    assert len(calls) == 1


async def test_broken_cooldown_database_fails_before_request(settings, monkeypatch):
    settings.electricity_cooldown_path.write_bytes(b"not a SQLite database")

    async def forbidden(*args):
        raise AssertionError("request should not start")

    async with aiohttp.ClientSession() as session:
        client = ElectricityClient(replace(settings, electricity_enabled=True), session)
        monkeypatch.setattr(client, "_query", forbidden)
        with pytest.raises(ElectricityError) as error:
            await client.query("33#2035")
        assert error.value.code == "cooldown_unavailable"


async def test_dry_run_forget_delete_and_revoke_leave_live_portal_intact(tmp_path):
    values = {
        "QQ_APP_ID": "same-app",
        "QQ_APP_SECRET": "synthetic-secret",
        "QQ_DB_PATH": str(tmp_path / "inbox.sqlite3"),
        "PORTAL_DB_PATH": str(tmp_path / "portal.sqlite3"),
        "SKILLS_DIR": str(tmp_path / "skills"),
        "TOOLPACKS_DIR": str(tmp_path / "toolpacks"),
        "ACCESS_POLICY_PATH": str(tmp_path / "access.json"),
        "PORTAL_ENABLED": "true",
        "PUBLIC_BASE_URL": "https://bot.example.test",
    }
    settings = Settings.from_values(values, dry_run=True)
    assert settings.db_path.name == "inbox.dry-run.sqlite3"
    assert settings.portal_db_path.name == "portal.dry-run.sqlite3"
    assert not settings.portal_enabled
    context = json.dumps(["users", "alice", "alice"])
    owner = user_id(context, settings.app_id)
    store = PortalStore(tmp_path / "portal.sqlite3")
    page = store.draft(owner, "live page", "<p>live</p>", "live")
    store.publish(owner, page, "public")
    link, token = store.link(owner, "curve", "meter", {"days": 3})
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session)
        for kind, payload in [
            ("portal_action", json.dumps({"action": "delete", "identifier": page})),
            ("portal_action", '{"action":"revoke_curve"}'),
            ("profile", '{"action":"forget"}'),
        ]:
            await assistant.generate(kind, payload, context)
        await assistant.close()
    assert store.page(page, public=True)["title"] == "live page"
    assert store.authorize(link, token, "curve")["owner"] == owner


async def test_slow_chat_does_not_block_other_group_ping_but_own_order_is_preserved(settings):
    entered, release = asyncio.Event(), asyncio.Event()

    class Assistant:
        llm_enabled = True

        async def generate(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return "slow done"

    api = Recorder()
    async with TestClient(
        TestServer(create_app(settings, api=api, assistant=Assistant()))
    ) as client:
        await send(client, settings, "slow", "slow", author="alice")
        await entered.wait()
        await send(client, settings, "/ping", "same-chat", author="alice")
        await send(client, settings, "/ping", "other-group", group=True)
        await asyncio.wait_for(api.called.wait(), 1)
        assert [call[2] for call in api.calls] == ["other-group"]
        release.set()
        await eventually(lambda: len(api.calls) == 3)
        assert [call[2] for call in api.calls] == ["other-group", "slow", "same-chat"]


async def test_generation_concurrency_is_bounded_and_fast_lane_remains_available(settings):
    release = asyncio.Event()
    running = []

    class Assistant:
        llm_enabled = True

        async def generate(self, *args, **kwargs):
            running.append(args[2])
            await release.wait()
            return "done"

    api = Recorder()
    app = create_app(settings, api=api, assistant=Assistant())
    async with TestClient(TestServer(app)) as client:
        for index in range(6):
            await send(client, settings, "slow", str(index), author=f"user-{index}")
        await eventually(lambda: len(running) == 4)
        assert len(app[RUNTIME].active) == 4
        await send(client, settings, "/ping", "ping", group=True)
        await asyncio.wait_for(api.called.wait(), 1)
        assert api.calls[0][2] == "ping" and len(running) == 4
        release.set()
        await eventually(lambda: len(api.calls) == 7)


def test_pending_limits_preserve_dedup_and_protect_user_and_group(settings):
    inbox = Inbox(settings.db_path)
    try:
        for index in range(8):
            msg = Message("users", "alice", str(index), "/ping", time.time() + 60)
            assert inbox.add(msg, "pong")
        assert not inbox.add(msg, "pong")
        with pytest.raises(InboxFull):
            inbox.add(replace(msg, message_id="too-many"), "pong")
        for index in range(32):
            msg = Message(
                "groups", "group", str(index), "/ping", time.time() + 60, sender_id=f"user-{index}"
            )
            assert inbox.add(msg, "pong")
        with pytest.raises(InboxFull):
            inbox.add(replace(msg, message_id="too-many", sender_id="different"), "pong")
    finally:
        inbox.close()
