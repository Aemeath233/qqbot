import json
import time
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from qqbot.portal_store import PortalStore
from qqbot.server import create_app
from qqbot.user_store import UserStore

A = json.dumps(["users", "alice", "alice"])
B = json.dumps(["users", "bob", "bob"])


@pytest.fixture
async def portal(settings):
    settings = replace(settings, portal_enabled=True, public_base_url="https://bot.example.com")
    users = UserStore(
        settings.db_path.with_name("inbox.userdata.sqlite3"), namespace=settings.app_id
    )
    now = time.time() - 10
    for who, quantity, stamp in [(A, "50.0", now - 86400), (A, "40.0", now), (B, "999.0", now)]:
        users.add_reading(
            who,
            {
                "meter_key": "a" * 64,
                "dormitory": "33#4032",
                "area": "2",
                "area_name": "区域2",
                "quantity": quantity,
                "observed_at": stamp,
                "cached": False,
            },
            f"{who}-{stamp}",
            365,
        )
    store = PortalStore(settings.portal_db_path)
    async with TestClient(TestServer(create_app(settings))) as client:
        client.session.headers.update(Host="bot.example.com", Origin="https://bot.example.com")
        yield client, store, users, settings


async def test_curve_only_exposes_capability_owner_and_immutable_meter(portal):
    client, store, users, _ = portal
    identifier, token = store.link(users.identity(A), "curve", "a" * 64, {"days": 7}, ttl=1800)
    response = await client.post(f"/_portal/curve/{identifier}", json={"token": token, "days": 365})
    assert response.status == 200
    data = await response.json()
    assert data["days"] == 7 and data["sample_count"] == 2 and data["net_decrease_kwh"] == "10.0"
    assert "999.0" not in json.dumps(data)
    assert not any(field in data for field in ["owner", "user_id", "meter_key", "token_hash"])
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert (
        await client.post(
            f"/_portal/curve/{identifier}", json={"token": token, "owner": users.identity(B)}
        )
    ).status == 404
    store.revoke(users.identity(A), kind="curve")
    assert (await client.post(f"/_portal/curve/{identifier}", json={"token": token})).status == 404


async def test_invalid_tokens_origins_hosts_and_body_fail_without_data(portal):
    client, store, users, _ = portal
    identifier, token = store.link(users.identity(A), "curve", "a" * 64, {"days": 7})
    for payload in (
        {"token": "x" * 43},
        {"token": token, "days": True},
        {"token": token, "days": 0},
        {"token": token, "days": 366},
    ):
        response = await client.post(f"/_portal/curve/{identifier}", json=payload)
        assert response.status == 404 and "50.0" not in await response.text()
    assert (
        await client.post(
            f"/_portal/curve/{identifier}", json={"token": token}, headers={"Origin": "null"}
        )
    ).status == 403
    assert (
        await client.post(
            f"/_portal/curve/{identifier}", json={"token": token}, headers={"Host": "other.example"}
        )
    ).status == 404
    assert (await client.get("/portal-assets/../../../.env")).status == 404
    response = await client.get(f"/u/{identifier}")
    assert response.status == 200 and token not in await response.text()
    assert "noindex" in response.headers["X-Robots-Tag"]


async def test_public_page_wraps_untrusted_content_and_direct_content_is_sandboxed(portal):
    client, store, users, _ = portal
    source = '<script>window.parent.document.body.textContent="escaped"</script><h1>Example</h1>'
    identifier = store.draft(users.identity(A), '<img src=x onerror="bad()">', source, "event")
    assert (await client.get(f"/p/{identifier}")).status == 404
    store.publish(users.identity(A), identifier, "public")
    response = await client.get(f"/p/{identifier}")
    wrapper = await response.text()
    assert response.status == 200 and source not in wrapper
    assert 'sandbox="allow-scripts"' in wrapper and "allow-same-origin" not in wrapper
    assert "<img src=x" not in wrapper and "&lt;img" in wrapper
    response = await client.get(f"/_page-content/{identifier}")
    assert await response.text() == source
    csp = response.headers["Content-Security-Policy"]
    assert (
        "sandbox allow-scripts" in csp
        and "connect-src 'none'" in csp
        and "allow-same-origin" not in csp
    )
    store.retract(users.identity(A), identifier)
    assert (await client.get(f"/p/{identifier}")).status == 404
    assert (await client.get(f"/_page-content/{identifier}")).status == 404


async def test_unlisted_page_needs_expiring_bearer_and_public_index_lists_no_users(portal):
    client, store, users, _ = portal
    identifier = store.draft(users.identity(A), "Private preview", "<h1>Hidden</h1>", "preview")
    store.publish(users.identity(A), identifier, "unlisted")
    link, token = store.link(users.identity(A), "page", identifier, ttl=1800)
    assert (await client.get(f"/p/{identifier}")).status == 404
    assert (await client.get(f"/_page-content/{identifier}")).status == 404
    response = await client.post(f"/_portal/page/{link}", json={"token": token})
    assert response.status == 200 and "Hidden" in (await response.json())["html"]
    root = await (await client.get("/")).text()
    assert identifier not in root and users.identity(A) not in root


async def test_disabled_portal_serves_no_public_paths(settings):
    async with TestClient(TestServer(create_app(settings))) as client:
        for path in ["/", "/portal-assets/portal.js", "/p/abcdefghijklmnop", "/u/abcdefghijklmnop"]:
            assert (await client.get(path)).status == 404
