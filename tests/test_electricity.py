import json
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qqbot.electricity import ElectricityClient, ElectricityError


async def test_valid_nested_json_auth_is_server_side_and_cache(settings):
    requests = []
    settings = replace(
        settings,
        electricity_enabled=True,
        electricity_token="private-session",
        electricity_cookie="private-cookie",
    )

    async def handle(request):
        payload = await request.json()
        inner = json.loads(payload["bizcontent"])
        serial = inner.pop("idserial")
        assert len(serial) == 12 and serial.isascii() and serial.isdigit()
        assert inner == {
            "payproid": 953,
            "schoolcode": "1402",
            "roomverify": "2-11--4-4032",
            "businesstype": 2,
        }
        assert payload["method"] == "samllProgramGetRoomState"
        assert "private-session" in request.headers["Referer"]
        assert request.headers["Cookie"] == "private-cookie"
        requests.append(payload)
        return web.json_response(
            {"returncode": "SUCCESS", "businessData": {"quantity": "12.23", "quantityunit": "度"}}
        )

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = ElectricityClient(settings, session, endpoint=str(server.make_url("/trade")))
        result = await client.query("33#4032")
        assert result["remaining_kwh"] == "12.23"
        assert not result["cached"]
        assert (await client.query("33#4032"))["cached"]
        assert "private" not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-1", True, None, "wrong"])
async def test_invalid_quantity_is_an_error_not_zero(settings, quantity):
    settings = replace(settings, electricity_enabled=True)

    async def handle(request):
        return web.json_response(
            {"returncode": "SUCCESS", "businessData": {"quantity": quantity, "quantityunit": "度"}}
        )

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = ElectricityClient(settings, session, endpoint=str(server.make_url("/trade")))
        with pytest.raises(ElectricityError) as caught:
            await client.query("33#4032")
        assert caught.value.code == "invalid_quantity"


@pytest.mark.parametrize(
    "status, payload, code",
    [
        (401, {}, "auth_failed"),
        (403, {}, "auth_failed"),
        (503, {}, "upstream_error"),
        (200, {"returncode": "FAIL", "returnmsg": "private-token"}, "business_error"),
        (
            200,
            {"returncode": "SUCCESS", "businessData": {"quantity": "1", "quantityunit": "元"}},
            "invalid_quantity",
        ),
    ],
)
async def test_errors_do_not_disclose_upstream_secrets(settings, status, payload, code):
    settings = replace(settings, electricity_enabled=True)

    async def handle(request):
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = ElectricityClient(settings, session, endpoint=str(server.make_url("/trade")))
        with pytest.raises(ElectricityError) as caught:
            await client.query("33#4032")
        assert caught.value.code == code
        assert "private-token" not in str(caught.value)


async def test_directory_ambiguity_prevents_network_calls(settings, tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "rooms": [
                    {"area": "1", "building": "19", "room": "312", "roomverify": "1-1--3-12"},
                    {"area": "3", "building": "19", "room": "312", "roomverify": "3-1--3-12"},
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = replace(settings, electricity_enabled=True, electricity_map_path=path)
    async with aiohttp.ClientSession() as session:
        client = ElectricityClient(settings, session, endpoint="http://127.0.0.1:1/unreachable")
        with pytest.raises(ElectricityError) as caught:
            await client.query("19#312")
        assert caught.value.code == "ambiguous_dorm"
        assert len(caught.value.candidates) == 2


async def test_429_reports_rate_limit_and_preserves_retry_after(settings):
    requests = []

    async def handle(request):
        requests.append(request)
        return web.Response(
            status=429, text="private-server-message", headers={"Retry-After": "1800"}
        )

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = ElectricityClient(
            replace(settings, electricity_enabled=True),
            session,
            endpoint=str(server.make_url("/trade")),
        )
        with pytest.raises(ElectricityError) as caught:
            await client.query("33#4032")
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after_seconds == 1800
    assert "private" not in str(caught.value) and len(requests) == 1


async def test_disabled_tool_never_contacts_network(settings):
    async with aiohttp.ClientSession() as session:
        client = ElectricityClient(settings, session, endpoint="http://127.0.0.1:1/unreachable")
        with pytest.raises(ElectricityError) as caught:
            await client.query("33#4032")
        assert caught.value.code == "disabled"
