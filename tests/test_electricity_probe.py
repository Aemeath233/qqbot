import argparse
import json
import socket
import ssl
import time
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qqbot import electricity_probe as diagnostic
from qqbot.electricity_probe import (
    ProbeConfig,
    ProbeError,
    RequestGate,
    make_plan,
    network_failure,
    parse_response,
    probe,
    throttle_delay,
)


def plan(*, dormitory="", port=443, proxy="direct"):
    return make_plan(
        ProbeConfig(token="private-token", cookie="private-cookie", tapp_id="private-tapp"),
        argparse.Namespace(port=port, proxy=proxy, dormitory=dormitory, area=""),
    )


async def test_default_is_one_read_only_area_request_and_report_contains_no_secrets():
    requests = []

    async def handle(request):
        requests.append(await request.json())
        assert request.headers["Cookie"] == "private-cookie"
        return web.json_response(
            {"returncode": "SUCCESS", "businessData": [{"id": "1", "name": "测试区域"}]}
        )

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await probe(replace(plan(), endpoint=str(server.make_url("/trade"))), session)
    assert len(requests) == 1
    assert result["ok"] and result["area_count"] == 1
    inner = json.loads(requests[0]["bizcontent"])
    assert requests[0]["method"] == "samllProgramGetRoom"
    assert inner["optype"] == "1" and inner["buildid"] == "0"
    assert "roomverify" not in inner
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_failure_never_retries_or_follows_redirects(status):
    requests = []

    async def handle(request):
        requests.append(request.path)
        return web.Response(
            status=status,
            text="private-body",
            headers={"Location": "/should-not-be-requested?token=private", "Retry-After": "1800"},
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await probe(replace(plan(), endpoint=str(server.make_url("/trade"))), session)
    assert requests == ["/trade"]
    assert not result["ok"] and result["http_status"] == status
    assert "private" not in json.dumps(result)
    if status == 429:
        assert result["cooldown_seconds"] == 1800


async def test_single_room_uses_confirmed_map_and_strict_quantity_validation():
    requests = []

    async def handle(request):
        requests.append(await request.json())
        return web.json_response(
            {"returncode": "SUCCESS", "businessData": {"quantity": "12.23", "quantityunit": "度"}}
        )

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await probe(
            replace(plan(dormitory="33#4032"), endpoint=str(server.make_url("/trade"))), session
        )
    assert result["remaining_kwh"] == "12.23"
    assert len(requests) == 1
    assert json.loads(requests[0]["bizcontent"])["roomverify"] == "2-11--4-4032"
    assert requests[0]["method"] == "samllProgramGetRoomState"


@pytest.mark.parametrize(
    "payload,category",
    [
        ({"returncode": "FAIL", "businessData": {"quantity": "88"}}, "business_error"),
        (
            {"returncode": "SUCCESS", "businessData": {"balance": "88", "quantityunit": "元"}},
            "invalid_quantity",
        ),
        (
            {"returncode": "SUCCESS", "businessData": {"quantity": "NaN", "quantityunit": "度"}},
            "invalid_quantity",
        ),
    ],
)
def test_failed_business_money_and_nan_are_not_reported_as_kwh(payload, category):
    payload["message"] = "private-token"
    result = parse_response(json.dumps(payload).encode(), plan(dormitory="33#4032"))
    assert not result["ok"] and result["category"] == category
    assert "remaining_kwh" not in result
    assert "private" not in json.dumps(result)


async def test_oversize_body_is_not_saved_or_accepted():
    async def handle(request):
        return web.Response(body=b"private" * 20000)

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await probe(replace(plan(), endpoint=str(server.make_url("/trade"))), session)
    assert result["category"] == "response_too_large"
    assert "private" not in json.dumps(result)


async def test_chunked_small_response_is_fully_read():
    async def handle(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"returncode":"SUCCESS",')
        await response.write(b'"businessData":[]}')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/trade", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await probe(replace(plan(), endpoint=str(server.make_url("/trade"))), session)
    assert result["ok"]


def test_cooldown_is_shared_by_separate_instances_and_persists(tmp_path):
    path = tmp_path / "cooldown.sqlite3"
    first, second = RequestGate(path), RequestGate(path)
    try:
        first.reserve(now=1000)
        with pytest.raises(ProbeError, match="60 秒"):
            second.reserve(now=1000)
        with pytest.raises(ProbeError, match="1 秒"):
            second.reserve(now=1059)
    finally:
        first.close()
        second.close()
    third = RequestGate(path)
    try:
        with pytest.raises(ProbeError):
            third.reserve(now=1059)
        third.reserve(now=1060)
    finally:
        third.close()


@pytest.mark.parametrize(
    "retry_after, expected",
    [
        (None, 600),
        ("invalid-private-token", 600),
        ("10", 600),
        ("900", 900),
        ("Thu, 01 Jan 1970 00:30:00 GMT", 800),
    ],
)
def test_retry_after_respected_with_minimum_ten_minute_cooldown(retry_after, expected):
    assert throttle_delay(retry_after, now=1000) == expected


@pytest.mark.parametrize(
    "error,category",
    [
        (ssl.SSLEOFError(8, "private-cookie"), "tls"),
        (ssl.SSLCertVerificationError(1, "private-cookie"), "tls_certificate"),
        (socket.gaierror(-1, "private-cookie"), "dns"),
        (TimeoutError("private-cookie"), "timeout"),
        (ConnectionRefusedError("private-cookie"), "network"),
    ],
)
def test_network_error_classification_keeps_nested_causes_without_error_text(error, category):
    wrapped = OSError("private-proxy-password")
    wrapped.__cause__ = error
    result = network_failure(wrapped)
    assert result["category"] == category
    assert type(error).__name__ in result["error_types"]
    assert "private" not in json.dumps(result)


def test_dry_run_does_not_call_network_or_touch_cooldown(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(diagnostic, "DATA_DIR", tmp_path / "must-not-exist")
    monkeypatch.setattr(
        diagnostic.ProbeConfig, "load", lambda: ProbeConfig(token="secret-local-token")
    )
    monkeypatch.setattr(diagnostic.sys, "argv", ["test_electricity.py", "--dry-run"])

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not construct a network session or request gate")

    monkeypatch.setattr(diagnostic, "RequestGate", forbidden)
    monkeypatch.setattr(diagnostic, "run_probe", forbidden)
    diagnostic.main()
    assert not diagnostic.DATA_DIR.exists()
    output = capsys.readouterr().out
    assert "0 个请求" in output and "secret-local-token" not in output


def test_cli_persists_429_cooldown_and_second_run_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostic, "DATA_DIR", tmp_path)
    monkeypatch.setattr(diagnostic.ProbeConfig, "load", lambda: ProbeConfig())
    monkeypatch.setattr(diagnostic.sys, "argv", ["test_electricity.py"])
    calls = []

    async def limited(plan):
        calls.append(plan)
        return {
            "ok": False,
            "category": "rate_limited",
            "message": "mock 429",
            "cooldown_seconds": 1800,
        }

    monkeypatch.setattr(diagnostic, "run_probe", limited)
    with pytest.raises(SystemExit) as stopped:
        diagnostic.main()
    assert stopped.value.code == 1
    gate = RequestGate(tmp_path / "cooldown.sqlite3")
    try:
        blocked_until = gate.db.execute("SELECT next_allowed FROM cooldown").fetchone()[0]
        assert blocked_until >= time.time() + 1795
    finally:
        gate.close()
    with pytest.raises(SystemExit) as stopped:
        diagnostic.main()
    assert stopped.value.code == 2 and len(calls) == 1
    assert len(list(tmp_path.glob("report-*.json"))) == 1
