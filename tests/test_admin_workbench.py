import asyncio
import json
import time
from dataclasses import replace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from qqbot import admin
from qqbot.admin_auth import PasswordStore
from qqbot.electricity import ElectricityClient, ElectricityError
from qqbot.electricity_probe import RequestGate

PASSWORD = "workbench-test-password"


@pytest.fixture
async def client(tmp_path):
    PasswordStore(tmp_path / "data/admin/password.json").set(PASSWORD)
    app = admin.create_admin_app(tmp_path, environ={})
    async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        yield client


async def login(client):
    client.session.headers["Origin"] = str(client.make_url("/")).rstrip("/")
    response = await client.post("/api/login", json={"password": PASSWORD})
    assert response.status == 200
    client.session.headers["X-CSRF-Token"] = (await response.json())["csrf"]


async def test_web_chat_requires_auth_and_does_not_accept_another_context(client):
    assert (await client.post("/api/chat", json={"message": "hello"})).status == 403
    await login(client)
    assert (
        await client.post("/api/chat", json={"message": "/人设", "context": "other-user"})
    ).status == 400
    assert (await client.post("/api/chat", json={"message": "x" * 2001})).status == 400
    client.session.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/chat", json={"message": "/人设"})).status == 403


async def test_web_chat_persona_dice_and_memory_work_without_model_or_qq(client):
    await login(client)
    for text, expected in [
        ("/人设", "猫猫"),
        ("/掷骰子 2d6", "合计"),
        ("以后叫我网页阿明", "网页阿明"),
    ]:
        response = await client.post("/api/chat", json={"message": text})
        assert response.status == 200 and expected in (await response.json())["reply"]
    state = client.app[admin.STATE]
    users = state.chat_assistant().users
    assert users.profile(admin.WEB_CONTEXT)["nickname"] == "网页阿明"
    qq_context = json.dumps(["users", "qq-user", "qq-user"])
    assert users.profile(qq_context) == {}
    response = await client.post("/api/chat/reset", json={})
    assert response.status == 200
    assert users.profile(admin.WEB_CONTEXT)["nickname"] == "网页阿明"


async def test_web_chat_uses_new_saved_persona_and_game_switch(client):
    await login(client)
    settings = await (await client.get("/api/settings")).json()
    response = await client.post(
        "/api/settings",
        json={
            "revision": settings["revision"],
            "values": {"BOT_PERSONA": "friend", "BOT_NAME": "小明", "GAMES_ENABLED": False},
        },
    )
    assert response.status == 200
    response = await client.post("/api/chat", json={"message": "/人设"})
    assert "校园损友" in (await response.json())["reply"]
    response = await client.post("/api/chat", json={"message": "/掷骰子"})
    assert "尚未启用" in (await response.json())["reply"]


async def test_reset_only_clears_web_context(client):
    await login(client)
    assistant = client.app[admin.STATE].chat_assistant()
    assistant.memory.save(admin.WEB_CONTEXT, "web", "web")
    assistant.memory.save("different-qq-context", "qq", "qq")
    await client.post("/api/chat/reset", json={})
    assert assistant.memory.get(admin.WEB_CONTEXT) == []
    assert assistant.memory.get("different-qq-context")


async def test_web_chat_concurrency_has_one_active_request(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Assistant:
        llm_enabled = True

        async def generate(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return "mock"

    PasswordStore(tmp_path / "data/admin/password.json").set(PASSWORD)
    app = admin.create_admin_app(
        tmp_path, environ={}, assistant_factory=lambda settings, session: Assistant()
    )
    async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        await login(client)
        first = asyncio.create_task(client.post("/api/chat", json={"message": "hello"}))
        await entered.wait()
        try:
            assert (await client.post("/api/chat", json={"message": "again"})).status == 429
            assert (await client.post("/api/chat/reset", json={})).status == 429
        finally:
            release.set()
        assert (await first).status == 200


async def test_admin_electricity_cache_does_not_send_extra_request_and_different_room_is_limited(
    settings, tmp_path, monkeypatch
):
    calls = []

    async def fake_query(self, dormitory, roomverify):
        calls.append(roomverify)
        return {"ok": True, "remaining_kwh": "12.23", "dormitory": dormitory, "cached": False}

    monkeypatch.setattr(ElectricityClient, "_query", fake_query)
    async with aiohttp.ClientSession() as session:
        client = admin.AdminElectricity(
            replace(settings, electricity_enabled=True), session, tmp_path / "gate.sqlite3"
        )
        await client.query("33#4032")
        assert (await client.query("33#4032"))["cached"]
        with pytest.raises(ElectricityError) as caught:
            await client.query("33#2035")
        assert caught.value.code == "cooldown"
    assert len(calls) == 1


async def test_admin_electricity_429_extends_cooldown(settings, tmp_path, monkeypatch):
    async def throttled(self, dormitory, roomverify):
        raise ElectricityError("mock", "rate_limited", retry_after_seconds=1800)

    monkeypatch.setattr(ElectricityClient, "_query", throttled)
    path = tmp_path / "gate.sqlite3"
    async with aiohttp.ClientSession() as session:
        client = admin.AdminElectricity(replace(settings, electricity_enabled=True), session, path)
        with pytest.raises(ElectricityError):
            await client.query("33#4032")
    gate = RequestGate(path)
    try:
        until = gate.db.execute("SELECT next_allowed FROM cooldown").fetchone()[0]
        assert until >= time.time() + 1795
    finally:
        gate.close()
