import asyncio
import io
import json
from zipfile import ZipFile

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from qqbot import admin
from qqbot.admin_auth import PasswordStore

PASSWORD = "toolpack-admin-test-password"


def package():
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "toolpack.json",
            json.dumps(
                {
                    "version": 1,
                    "name": "demo",
                    "description": "Test tool",
                    "exports": ["add"],
                    "env_keys": ["DEMO_TOKEN"],
                }
            ),
        )
        archive.writestr(
            "tools.py",
            'def add(a: int, b: int) -> int:\n    """Add two integers."""\n    return a + b\n',
        )
    return stream.getvalue()


def form(raw=None):
    data = aiohttp.FormData()
    data.add_field(
        "file",
        package() if raw is None else raw,
        filename="demo.zip",
        content_type="application/zip",
    )
    return data


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


async def test_upload_auth_origin_csrf_and_no_import_until_trusted(client):
    assert (await client.get("/api/toolpacks")).status == 401
    await login(client)
    client.session.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/toolpacks/upload", data=form())).status == 403
    await login(client)
    client.session.headers["Origin"] = "https://other.example"
    assert (await client.post("/api/toolpacks/upload", data=form())).status == 403
    await login(client)
    assert (await client.post("/api/toolpacks/upload", data=form())).status == 200
    response = await client.get("/api/toolpacks")
    assert (await response.json())["packages"][0]["status"] == "disabled"
    assert (await client.post("/api/toolpacks/demo", json={"enabled": True})).status == 400
    assert not client.app[admin.STATE].tool_manager.workers
    response = await client.get("/api/toolpacks/demo")
    assert '"exports"' in (await response.json())["manifest"]
    assert (await client.post("/api/toolpacks/upload", data=form())).status == 400


async def test_enable_env_restart_stop_and_cleanup_of_real_process(client):
    await login(client)
    await client.post("/api/toolpacks/upload", data=form())
    response = await client.post(
        "/api/toolpacks/demo/env", json={"values": {"DEMO_TOKEN": "mock-private-token"}}
    )
    assert response.status == 200 and "mock-private-token" not in await response.text()
    response = await client.post("/api/toolpacks/demo", json={"enabled": True, "trusted": True})
    assert response.status == 200
    manager = client.app[admin.STATE].tool_manager
    worker = manager.workers["demo"]
    await asyncio.wait_for(worker.ready.wait(), 15)
    assert worker.status == "running", worker.message
    response = await client.get("/api/toolpacks")
    text = await response.text()
    assert "mock-private-token" not in text and "mcp__demo__add" in text
    response = await client.post("/api/toolpacks/demo/restart", json={})
    assert response.status == 200 and worker.task.done()
    next_worker = manager.workers["demo"]
    await asyncio.wait_for(next_worker.ready.wait(), 15)
    assert next_worker.status == "running"
    response = await client.post("/api/toolpacks/demo", json={"enabled": False})
    assert response.status == 200 and next_worker.task.done()
    assert manager.workers == {}


async def test_invalid_parameters_and_oversized_upload_fail_without_execution(client):
    await login(client)
    assert (await client.post("/api/toolpacks/upload", data=form(b"invalid"))).status == 400
    assert (
        await client.post("/api/toolpacks/upload", data=form(b"x" * (1024 * 1024 + 1)))
    ).status == 413
    await client.post("/api/toolpacks/upload", data=form())
    for payload in (
        {"enabled": "true", "trusted": True},
        {"enabled": True, "trusted": "yes"},
        {"enabled": True, "trusted": True, "command": "python"},
    ):
        assert (await client.post("/api/toolpacks/demo", json=payload)).status == 400
    assert (
        await client.post("/api/toolpacks/demo/env", json={"values": {"PYTHONPATH": "bad"}})
    ).status == 400
    assert (await client.post("/api/toolpacks/demo/restart", json={})).status == 400
    assert client.app[admin.STATE].tool_manager.workers == {}
