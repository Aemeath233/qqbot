import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from qqbot import admin
from qqbot.admin_auth import PasswordStore
from qqbot.skills import MAX_UPLOAD

PASSWORD = "skill-admin-test-password"
MANIFEST = b"---\nname: dinner\ndescription: Dinner choice\n---\n# Instructions\n"


@pytest.fixture
async def client(tmp_path):
    PasswordStore(tmp_path / "data/admin/password.json").set(PASSWORD)
    app = admin.create_admin_app(tmp_path, environ={})
    async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        yield client


async def login(client):
    client.session.headers["Origin"] = str(client.make_url("/")).rstrip("/")
    response = await client.post("/api/login", json={"password": PASSWORD})
    client.session.headers["X-CSRF-Token"] = (await response.json())["csrf"]


def form(raw=MANIFEST, filename="SKILL.md", extra=False):
    data = aiohttp.FormData()
    data.add_field("file", raw, filename=filename, content_type="application/octet-stream")
    if extra:
        data.add_field("other", b"extra", filename="extra.md")
    return data


async def test_skills_endpoints_require_login_csrf_and_origin(client):
    assert (await client.get("/api/skills")).status == 401
    await login(client)
    client.session.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/skills/upload", data=form())).status == 403
    assert (await client.post("/api/skills/dinner", json={"enabled": True})).status == 403
    await login(client)
    client.session.headers["Origin"] = "https://other.example"
    assert (await client.post("/api/skills/upload", data=form())).status == 403
    assert (await (await client.get("/api/skills")).json())["skills"] == []


async def test_upload_view_toggle_and_next_chat_use_same_store(client):
    await login(client)
    response = await client.post("/api/skills/upload", data=form())
    assert response.status == 200
    assert (await response.json())["skill"]["enabled"] is False
    response = await client.get("/api/skills/dinner")
    assert (await response.json())["content"] == MANIFEST.decode()
    response = await client.post("/api/skills/dinner", json={"enabled": True})
    assert response.status == 200
    assert (await (await client.get("/api/skills")).json())["skills"][0]["enabled"] is True
    response = await client.post("/api/chat", json={"message": "/技能列表"})
    assert "dinner" in (await response.json())["reply"]
    assert (await client.post("/api/skills/upload", data=form())).status == 400
    assert (await client.post("/api/skills/dinner", json={"enabled": False})).status == 200
    response = await client.post("/api/chat", json={"message": "/技能列表"})
    assert "尚未启用" in (await response.json())["reply"]


@pytest.mark.parametrize(
    "raw,filename,extra,status",
    [
        (b"bad", "bad.zip", False, 400),
        (MANIFEST, "skill.py", False, 400),
        (b"x" * (MAX_UPLOAD + 1), "large.md", False, 413),
        (MANIFEST, "SKILL.md", True, 400),
    ],
    ids=["invalid-zip", "executable", "oversized", "multiple"],
)
async def test_upload_rejects_invalid_multiple_and_large_files(
    client, raw, filename, extra, status
):
    await login(client)
    assert (
        await client.post("/api/skills/upload", data=form(raw, filename, extra))
    ).status == status
    assert (await (await client.get("/api/skills")).json())["skills"] == []


async def test_toggle_invalid_input_and_missing_file(client):
    await login(client)
    await client.post("/api/skills/upload", data=form())
    for data in ({"enabled": "true"}, {"enabled": True, "path": "outside"}, []):
        assert (await client.post("/api/skills/dinner", json=data)).status == 400
    assert (await client.get("/api/skills/missing")).status == 404
    assert (await client.post("/api/skills/upload", json={})).status == 400
