import asyncio
import json
import os

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values

from qqbot import admin
from qqbot.admin_auth import LoginLimit, PasswordStore, Sessions
from qqbot.admin_config import ConfigConflict, ConfigStore
from qqbot.config import ConfigurationError, Settings
from qqbot.electricity_probe import ProbeError, RequestGate

PASSWORD = "unit-test-admin-password"


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".env").write_text(
        "# keep this comment\nQQ_APP_ID=test-app\nQQ_APP_SECRET=secret-qq-value\n"
        "LLM_API_KEY=secret-model-value\nLLM_MODEL=test-model\n"
        "UNMANAGED_FIELD=preserve-me\nELECTRICITY_COOKIE=secret-cookie-value\n",
        encoding="utf-8",
    )
    PasswordStore(tmp_path / "data/admin/password.json").set(PASSWORD)
    return tmp_path


@pytest.fixture
async def client(root):
    app = admin.create_admin_app(root, environ={})
    async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        yield client


async def login(client):
    origin = str(client.make_url("/")).rstrip("/")
    client.session.headers["Origin"] = origin
    response = await client.post("/api/login", json={"password": PASSWORD})
    assert response.status == 200
    client.session.headers["X-CSRF-Token"] = (await response.json())["csrf"]
    return response


async def test_login_cookie_logout_and_unauthenticated_settings(client):
    assert (await client.get("/api/settings")).status == 401
    response = await login(client)
    cookie = response.cookies[admin.COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert (await client.get("/api/settings")).status == 200
    assert (await client.post("/api/logout", json={})).status == 200
    assert (await client.get("/api/settings")).status == 401


async def test_keys_never_appear_in_settings_or_save_response(client, root):
    await login(client)
    response = await client.get("/api/settings")
    body = await response.json()
    assert body["secrets"]["QQ_APP_SECRET"]
    for secret in ("secret-qq-value", "secret-model-value", "secret-cookie-value"):
        assert secret not in json.dumps(body)
    response = await client.post(
        "/api/settings",
        json={
            "revision": body["revision"],
            "values": {"LLM_API_KEY": "new-model-secret"},
        },
    )
    assert response.status == 200
    assert "new-model-secret" not in await response.text()
    assert dotenv_values(root / ".env")["LLM_API_KEY"] == "new-model-secret"


async def test_csrf_and_origin_are_required_for_mutations(client, root):
    await login(client)
    config = await (await client.get("/api/settings")).json()
    original = (root / ".env").read_bytes()
    payload = {"revision": config["revision"], "values": {"QQ_APP_ID": "changed"}}
    client.session.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/settings", json=payload)).status == 403
    client.session.headers["X-CSRF-Token"] = config["csrf"]
    client.session.headers["Origin"] = "https://untrusted.example"
    assert (await client.post("/api/settings", json=payload)).status == 403
    assert (root / ".env").read_bytes() == original


async def test_login_rejects_cross_origin_and_dns_rebinding(client):
    assert (
        await client.post(
            "/api/login",
            json={"password": PASSWORD},
            headers={"Origin": "https://untrusted.example"},
        )
    ).status == 403
    response = await client.get("/", headers={"Host": "untrusted.example"})
    assert response.status == 403


async def test_password_reset_invalidates_old_sessions(client, root):
    await login(client)
    PasswordStore(root / "data/admin/password.json").set("different-admin-password")
    assert (await client.get("/api/settings")).status == 401


async def test_wrong_password_attempts_are_limited(client):
    origin = str(client.make_url("/")).rstrip("/")
    for _ in range(5):
        response = await client.post(
            "/api/login", json={"password": "wrong-password"}, headers={"Origin": origin}
        )
        assert response.status == 401
    response = await client.post(
        "/api/login", json={"password": PASSWORD}, headers={"Origin": origin}
    )
    assert response.status == 429
    assert PASSWORD not in await response.text()


async def test_unknown_and_malformed_updates_do_not_change_file(client, root):
    await login(client)
    public = await (await client.get("/api/settings")).json()
    original = (root / ".env").read_bytes()
    for payload in [
        {"values": {"QQ_HOST": "0.0.0.0"}},
        {"values": {"QQ_APP_SECRET": "secret\nINJECTED_KEY=true"}},
        {"values": {"LLM_ENABLED": True, "LLM_API_KEY": "", "LLM_MODEL": ""}},
        {"values": {}, "clear_secrets": [{}]},
    ]:
        response = await client.post(
            "/api/settings", json={"revision": public["revision"], **payload}
        )
        assert response.status == 400
        assert (root / ".env").read_bytes() == original


async def test_stale_revision_is_rejected(client, root):
    await login(client)
    public = await (await client.get("/api/settings")).json()
    with (root / ".env").open("a", encoding="utf-8") as stream:
        stream.write("EXTERNAL_EDIT=keep\n")
    response = await client.post(
        "/api/settings",
        json={
            "revision": public["revision"],
            "values": {"QQ_APP_ID": "changed"},
        },
    )
    assert response.status == 409
    assert dotenv_values(root / ".env")["EXTERNAL_EDIT"] == "keep"


async def test_tests_run_only_after_authenticated_manual_post(root):
    calls = []

    async def manual(state):
        calls.append("qq")
        return {"ok": True, "message": "mock-only"}

    app = admin.create_admin_app(root, environ={}, checks={"qq": manual})
    async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        await client.get("/")
        assert (await client.post("/api/test/qq", json={})).status == 403
        await login(client)
        await client.get("/api/settings")
        assert calls == []
        assert (await client.post("/api/test/qq", json={})).status == 200
        assert calls == ["qq"]


async def test_concurrent_test_click_is_rejected(root):
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(state):
        entered.set()
        await release.wait()
        return {"ok": True, "message": "mock-only"}

    app = admin.create_admin_app(root, environ={}, checks={"qq": waiting})
    async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        await login(client)
        first = asyncio.create_task(client.post("/api/test/qq", json={}))
        await entered.wait()
        try:
            assert (await client.post("/api/test/qq", json={})).status == 429
        finally:
            release.set()
        assert (await first).status == 200


async def test_electricity_button_shares_cooldown_with_diagnostic_script(client, monkeypatch, root):
    requests = []

    async def mocked_probe(plan, session):
        requests.append(plan)
        assert json.loads(plan.payload["bizcontent"])["optype"] == "1"
        return {"ok": True, "message": "mock-area-list", "category": "directory"}

    monkeypatch.setattr(admin, "probe", mocked_probe)
    await login(client)
    assert (await client.post("/api/test/electricity", json={})).status == 200
    response = await client.post("/api/test/electricity", json={})
    assert response.status == 400 and "冷却" in (await response.json())["message"]
    assert len(requests) == 1
    gate = RequestGate(root / "data/electricity-test/cooldown.sqlite3")
    try:
        with pytest.raises(ProbeError):
            gate.reserve()
    finally:
        gate.close()


async def test_security_headers_and_static_assets(client):
    for path in ("/", "/admin.js", "/admin.css"):
        response = await client.get(path)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert (await client.get("/.env")).status == 404
    assert (await client.get("/data/admin/password.json")).status == 404


async def test_qq_and_model_tests_use_saved_credentials_without_echoing_them(client, monkeypatch):
    requests = []

    class FakeQQ:
        def __init__(self, settings, session):
            assert settings.app_secret == "secret-qq-value"

        async def me(self):
            requests.append("qq")
            return {"username": "mock"}

    class FakeLLM:
        def __init__(self, settings, session):
            assert settings.llm_api_key == "secret-model-value"
            assert settings.llm_enabled

        async def complete(self, messages, tools):
            requests.append("llm")
            assert not tools
            return {"role": "assistant", "content": "secret-model-value"}

    monkeypatch.setattr(admin, "QQAPI", FakeQQ)
    monkeypatch.setattr(admin, "ChatCompletionsClient", FakeLLM)
    await login(client)
    for service in ("qq", "llm"):
        response = await client.post(f"/api/test/{service}", json={})
        assert response.status == 200 and (await response.json())["ok"]
        assert "secret-" not in await response.text()
    assert requests == ["qq", "llm"]


async def test_electricity_429_preserves_longer_retry_after(client, monkeypatch, root):
    async def throttled(plan, session):
        return {
            "ok": False,
            "category": "rate_limited",
            "message": "mock 429",
            "cooldown_seconds": 1800,
        }

    monkeypatch.setattr(admin, "probe", throttled)
    await login(client)
    response = await client.post("/api/test/electricity", json={})
    assert (await response.json())["category"] == "rate_limited"
    gate = RequestGate(root / "data/electricity-test/cooldown.sqlite3")
    try:
        with pytest.raises(ProbeError, match="1[78][0-9][0-9] 秒"):
            gate.reserve()
    finally:
        gate.close()


async def test_public_domain_login_requires_https_and_sets_secure_cookie(root):
    app = admin.create_admin_app(root, environ={"ADMIN_ALLOWED_HOSTS": "admin.example.test"})
    async with TestClient(TestServer(app)) as client:
        headers = {"Host": "admin.example.test", "Origin": "http://admin.example.test"}
        assert (
            await client.post("/api/login", json={"password": PASSWORD}, headers=headers)
        ).status == 403
        headers["Origin"] = "https://admin.example.test"
        response = await client.post("/api/login", json={"password": PASSWORD}, headers=headers)
        assert response.status == 200 and response.cookies[admin.COOKIE]["secure"]


def test_save_preserves_blank_secrets_comments_and_unmanaged_settings(root):
    store = ConfigStore(root / ".env", environ={})
    public = store.save(
        {
            "revision": store.revision(),
            "values": {"LLM_MODEL": "new-model", "LLM_API_KEY": ""},
        }
    )
    saved = dotenv_values(root / ".env")
    assert saved["LLM_API_KEY"] == "secret-model-value"
    assert saved["UNMANAGED_FIELD"] == "preserve-me"
    assert "# keep this comment" in (root / ".env").read_text(encoding="utf-8")
    assert "secret-model-value" not in json.dumps(public)
    if os.name != "nt":
        assert (root / ".env").stat().st_mode & 0o777 == 0o600


def test_clear_secret_is_explicit_and_saved_quotes_are_literal(root):
    store = ConfigStore(root / ".env", environ={})
    literal = "key-with-'quotes'-${DO_NOT_EXPAND}"
    store.save({"revision": store.revision(), "values": {"LLM_API_KEY": literal}})
    assert store.values()["LLM_API_KEY"] == literal
    store.save({"revision": store.revision(), "clear_secrets": ["LLM_API_KEY"]})
    assert not store.public()["secrets"]["LLM_API_KEY"]


def test_environment_credentials_are_not_overwritten(root):
    store = ConfigStore(root / ".env", environ={"LLM_API_KEY": "external-secret-key"})
    public = store.public()
    assert "LLM_API_KEY" in public["locked_fields"]
    assert "external-secret-key" not in json.dumps(public)
    for payload in [{"values": {"LLM_API_KEY": "new"}}, {"clear_secrets": ["LLM_API_KEY"]}]:
        with pytest.raises(ConfigurationError):
            store.save({"revision": store.revision(), **payload})


def test_mid_save_external_change_is_not_overwritten(root, monkeypatch):
    from qqbot import admin_config

    original = admin_config.set_key

    def edit_concurrently(*args, **kwargs):
        with (root / ".env").open("a", encoding="utf-8") as stream:
            stream.write("EXTERNAL_EDIT=keep\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(admin_config, "set_key", edit_concurrently)
    store = ConfigStore(root / ".env", environ={})
    with pytest.raises(ConfigConflict):
        store.save({"revision": store.revision(), "values": {"QQ_APP_ID": "changed"}})
    assert dotenv_values(root / ".env")["QQ_APP_ID"] == "test-app"
    assert dotenv_values(root / ".env")["EXTERNAL_EDIT"] == "keep"
    assert not list(root.glob(".env.admin-*"))


def test_password_is_hashed_and_sessions_expire(root):
    passwords = PasswordStore(root / "data/admin/password.json")
    assert PASSWORD not in passwords.path.read_text(encoding="utf-8")
    assert passwords.verify(PASSWORD) and not passwords.verify("wrong-password")
    now = [0]
    sessions = Sessions(clock=lambda: now[0])
    token, csrf = sessions.issue(passwords.fingerprint())
    assert sessions.get(token, passwords.fingerprint()) == csrf
    now[0] = 8 * 3600
    assert sessions.get(token, passwords.fingerprint()) is None


def test_login_limit_expires():
    now = [0]
    limit = LoginLimit(clock=lambda: now[0])
    assert all(limit.reserve() for _ in range(5))
    assert not limit.reserve()
    now[0] = 300
    assert limit.reserve()


def test_mapping_validation_does_not_change_process_environment(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "external-key")
    Settings.from_values({"LLM_ENABLED": "false", "LLM_API_KEY": "candidate"}, require_qq=False)
    assert os.environ["LLM_API_KEY"] == "external-key"


def test_first_start_requires_a_personal_admin_password(tmp_path):
    with pytest.raises(ConfigurationError, match="set-password"):
        admin.create_admin_app(tmp_path, environ={})
