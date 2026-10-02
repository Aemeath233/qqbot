import asyncio
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import event_payload, signed_payload

from qqbot.api import QQAPIError
from qqbot.inbox import InboxFull
from qqbot.server import RUNTIME, create_app


class RecordingAPI:
    def __init__(self):
        self.calls = []
        self.called = asyncio.Event()
        self.allow_reply = asyncio.Event()
        self.allow_reply.set()

    async def send_text(self, *args):
        self.calls.append(args)
        self.called.set()
        await self.allow_reply.wait()
        return {"id": "reply"}


@pytest.mark.parametrize("group", [False, True])
async def test_signed_message_acks_before_remote_reply_and_deduplicates(settings, group):
    api = RecordingAPI()
    api.allow_reply.clear()
    app = create_app(settings, api=api)
    async with TestClient(TestServer(app)) as client:
        payload = event_payload(group=group)
        body, headers = signed_payload(settings, payload)
        response = await asyncio.wait_for(client.post("/qqbot", data=body, headers=headers), 1)
        assert response.status == 200
        assert await response.json() == {"op": 12, "d": 0}
        await asyncio.wait_for(api.called.wait(), 1)
        # 同一消息的另一次投递具有不同的事件 ID，仍然只回复一次。
        payload["id"] = "delivery-2"
        body, headers = signed_payload(settings, payload)
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        assert len(api.calls) == 1
        assert api.calls[0][0:3] == (
            "groups" if group else "users",
            "group-1" if group else "sender-1",
            "msg-1",
        )
        api.allow_reply.set()
    # 重启之后平台重新投递已经处理过的消息，仍然不新增任务。
    app2 = create_app(settings, api=RecordingAPI())
    async with TestClient(TestServer(app2)) as client:
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        assert app2[RUNTIME].inbox.pending_count() == 0


async def test_unsigned_challenge_is_supported_but_unsigned_events_are_rejected(settings):
    api = RecordingAPI()
    async with TestClient(TestServer(create_app(settings, api=api))) as client:
        headers = {"X-Bot-Appid": settings.app_id}
        challenge = {"op": 13, "d": {"plain_token": "hello", "event_ts": "1725442341"}}
        response = await client.post("/qqbot", json=challenge, headers=headers)
        assert response.status == 200
        assert len((await response.json())["signature"]) == 128
        assert (await client.post("/qqbot", json=event_payload(), headers=headers)).status == 401
        assert api.calls == []


async def test_invalid_signature_and_wrong_appid(settings):
    async with TestClient(TestServer(create_app(settings, api=RecordingAPI()))) as client:
        body, headers = signed_payload(settings, event_payload())
        assert (await client.post("/qqbot", data=body + b" ", headers=headers)).status == 401
        headers["X-Bot-Appid"] = "other-app"
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 401
        # 错误的签名头不能通过切换为 challenge 绕过验签。
        challenge = {"op": 13, "d": {"plain_token": "hello", "event_ts": "1725442341"}}
        body, headers = signed_payload(settings, challenge)
        headers["X-Signature-Ed25519"] = "0" * 128
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 401


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}},
        {"op": 13, "d": {"plain_token": "a", "event_ts": 123}},
        {"op": 13, "d": {"plain_token": '{"op":0}', "event_ts": "1725442341"}},
        {"op": 0, "t": []},
        {"op": True},
        {"op": 7},
    ],
)
async def test_malformed_requests(settings, payload):
    async with TestClient(TestServer(create_app(settings, api=RecordingAPI()))) as client:
        body, headers = signed_payload(settings, payload)
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 400


async def test_non_message_event_is_acked_and_health(settings):
    async with TestClient(TestServer(create_app(settings, api=RecordingAPI()))) as client:
        body, headers = signed_payload(settings, {"op": 0, "t": "FRIEND_ADD", "d": {}})
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        response = await client.get("/healthz")
        assert await response.json() == {"status": "ok", "mode": "live"}


async def test_group_full_mode_is_opt_in_and_avoids_chatter(settings):
    settings = replace(settings, accept_group_messages=True)
    api = RecordingAPI()
    app = create_app(settings, api=api)
    async with TestClient(TestServer(app)) as client:
        body, headers = signed_payload(
            settings, event_payload(group=True, full=True, content="闲聊")
        )
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        assert app[RUNTIME].inbox.pending_count() == 0
        body, headers = signed_payload(settings, event_payload(group=True, full=True))
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        await asyncio.wait_for(api.called.wait(), 1)
        assert len(api.calls) == 1


async def test_reply_failure_keeps_retry_in_persistent_inbox(settings):
    class FailingAPI(RecordingAPI):
        async def send_text(self, *args):
            self.called.set()
            raise QQAPIError(503, "temporary")

    api = FailingAPI()
    app = create_app(settings, api=api)
    async with TestClient(TestServer(app)) as client:
        body, headers = signed_payload(settings, event_payload())
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        await asyncio.wait_for(api.called.wait(), 1)
        row = app[RUNTIME].inbox.db.execute("SELECT * FROM replies").fetchone()
        assert row["state"] == "pending"
        assert row["attempts"] == 1
    recovered = RecordingAPI()
    restarted = create_app(settings, api=recovered)
    async with TestClient(TestServer(restarted)):
        # 加速模拟到达重试时间，确认重启服务能恢复任务。
        with restarted[RUNTIME].inbox.db:
            restarted[RUNTIME].inbox.db.execute("UPDATE replies SET next_try_at = 0")
        restarted[RUNTIME].wakeup.set()
        await asyncio.wait_for(recovered.called.wait(), 1)
        assert len(recovered.calls) == 1


async def test_failed_business_request_is_not_retried(settings):
    class RejectedAPI(RecordingAPI):
        async def send_text(self, *args):
            self.called.set()
            raise QQAPIError(200, 40034005)

    api = RejectedAPI()
    app = create_app(settings, api=api)
    async with TestClient(TestServer(app)) as client:
        body, headers = signed_payload(settings, event_payload())
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        await asyncio.wait_for(api.called.wait(), 1)
        assert app[RUNTIME].inbox.pending_count() == 0
        assert app[RUNTIME].inbox.db.execute("SELECT state FROM replies").fetchone()[0] == "failed"


async def test_full_inbox_does_not_acknowledge_unstored_event(settings, monkeypatch):
    app = create_app(settings, api=RecordingAPI())
    async with TestClient(TestServer(app)) as client:

        def full(*args):
            raise InboxFull

        monkeypatch.setattr(app[RUNTIME].inbox, "add", full)
        body, headers = signed_payload(settings, event_payload())
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 503


async def test_pending_reply_survives_worker_cancellation(settings):
    api = RecordingAPI()
    api.allow_reply.clear()
    async with TestClient(TestServer(create_app(settings, api=api))) as client:
        body, headers = signed_payload(settings, event_payload())
        assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
        await asyncio.wait_for(api.called.wait(), 1)
    recovered = RecordingAPI()
    async with TestClient(TestServer(create_app(settings, api=recovered))):
        await asyncio.wait_for(recovered.called.wait(), 1)
        assert recovered.calls[0][2] == "msg-1"
