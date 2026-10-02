import asyncio
import json
from dataclasses import replace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import event_payload, signed_payload

from qqbot.server import create_app


async def test_webhook_to_model_tool_to_electricity_to_qq(settings):
    upstream_calls = []
    delivered = asyncio.Event()
    replies = []

    async def model(request):
        payload = await request.json()
        assert "test-secret" not in json.dumps(payload)
        assert "private-session" not in json.dumps(payload)
        upstream_calls.append("llm")
        if payload["messages"][-1]["role"] == "tool":
            assert json.loads(payload["messages"][-1]["content"])["remaining_kwh"] == "12.23"
            message = {"role": "assistant", "content": "模型误写为9999度"}
        else:
            assert payload["messages"][-1]["content"] == "帮我看看33号楼4032还有多少电"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "query1",
                        "type": "function",
                        "function": {
                            "name": "query_electricity",
                            "arguments": '{"dormitory":"33#4032"}',
                        },
                    },
                ],
            }
        return web.json_response({"choices": [{"message": message}]})

    async def electricity(request):
        payload = await request.json()
        assert json.loads(payload["bizcontent"])["roomverify"] == "2-11--4-4032"
        upstream_calls.append("electricity")
        return web.json_response(
            {
                "returncode": "SUCCESS",
                "businessData": {
                    "quantity": "12.23",
                    "quantityunit": "度",
                },
            }
        )

    class QQRecorder:
        async def send_text(self, kind, target, message_id, content):
            replies.append(content)
            delivered.set()
            return {"id": "qq-reply"}

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", model)
    upstream.router.add_post("/trade", electricity)
    async with TestServer(upstream) as server:
        settings = replace(
            settings,
            llm_enabled=True,
            llm_model="tool-model",
            llm_api_key="private-model-key",
            llm_base_url=str(server.make_url("/v1")),
            electricity_enabled=True,
            electricity_endpoint=str(server.make_url("/trade")),
            electricity_token="private-session",
        )
        async with TestClient(TestServer(create_app(settings, api=QQRecorder()))) as client:
            body, headers = signed_payload(
                settings, event_payload(content="帮我看看33号楼4032还有多少电")
            )
            assert (await client.post("/qqbot", data=body, headers=headers)).status == 200
            await asyncio.wait_for(delivered.wait(), 2)
    assert upstream_calls == ["llm", "electricity", "llm"]
    assert len(replies) == 1
    assert "12.23" in replies[0] and "9999" not in replies[0]
