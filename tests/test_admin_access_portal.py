import json

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from qqbot import admin
from qqbot.admin_auth import PasswordStore
from qqbot.portal_store import PortalError

PASSWORD = "access-portal-test-password"


@pytest.fixture
async def client(tmp_path):
    PasswordStore(tmp_path / "data/admin/password.json").set(PASSWORD)
    async with TestClient(
        TestServer(admin.create_admin_app(tmp_path, environ={})),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    ) as client:
        yield client


async def login(client):
    client.session.headers["Origin"] = str(client.make_url("/")).rstrip("/")
    response = await client.post("/api/login", json={"password": PASSWORD})
    client.session.headers["X-CSRF-Token"] = (await response.json())["csrf"]


async def test_policy_and_page_mutations_require_authenticated_csrf(client):
    assert (await client.get("/api/access")).status == 401
    assert (await client.get("/api/pages")).status == 401
    await login(client)
    data = await (await client.get("/api/access")).json()
    client.session.headers.pop("X-CSRF-Token")
    assert (
        await client.post("/api/access", json={"rules": {}, "revision": data["revision"]})
    ).status == 403
    assert (await client.post("/api/pages/abcdefghijklmnop/retract", json={})).status == 403


async def test_saved_policy_controls_web_chat_immediately_and_cannot_be_bypassed(client):
    await login(client)
    data = await (await client.get("/api/access")).json()
    rules = {"roll_dice": {"mode": "disabled"}}
    assert (
        await client.post("/api/access", json={"rules": rules, "revision": data["revision"]})
    ).status == 200
    response = await client.post("/api/chat", json={"message": "/掷骰子 1d6"})
    assert "未获准" in (await response.json())["reply"]
    assert (
        await client.post("/api/access", json={"rules": {}, "revision": data["revision"]})
    ).status == 400
    data = await (await client.get("/api/access")).json()
    assert (
        await client.post(
            "/api/access",
            json={"rules": {"roll_dice": {"mode": "admin-only"}}, "revision": data["revision"]},
        )
    ).status == 200
    response = await client.post("/api/chat", json={"message": "/掷骰子 1d6"})
    assert "合计" in (await response.json())["reply"]


async def test_admin_can_retract_page_without_listing_owner_identifiers(client):
    await login(client)
    store = client.app[admin.STATE].portal_store
    identifier = store.draft("a" * 64, "demo", "<h1>Demo</h1>", "event")
    store.publish("a" * 64, identifier, "public")
    data = await (await client.get("/api/pages")).json()
    assert data["pages"][0]["id"] == identifier and "a" * 64 not in json.dumps(data)
    assert (await client.post(f"/api/pages/{identifier}/retract", json={})).status == 200
    with pytest.raises(PortalError):
        store.page(identifier, public=True)
