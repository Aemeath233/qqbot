from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qqbot.llm import ChatCompletionsClient, LLMError


async def test_compatible_chat_endpoint_tools_and_reasoning(settings):
    captured = []

    async def handle(request):
        assert request.headers["Authorization"] == "Bearer private-model-key"
        payload = await request.json()
        captured.append(payload)
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning_content": "tool needed",
                            "tool_calls": [
                                {
                                    "id": "call1",
                                    "type": "function",
                                    "function": {
                                        "name": "query_electricity",
                                        "arguments": '{"dormitory":"33#4032"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        settings = replace(
            settings,
            llm_base_url=str(server.make_url("/v1")),
            llm_model="compatible-model",
            llm_api_key="private-model-key",
        )
        client = ChatCompletionsClient(settings, session)
        result = await client.complete(
            [{"role": "user", "content": "查电费"}],
            [{"type": "function", "function": {"name": "query_electricity"}}],
        )
        assert result["reasoning_content"] == "tool needed"
        assert result["tool_calls"][0]["id"] == "call1"
    assert captured[0]["tool_choice"] == "auto"
    assert captured[0]["model"] == "compatible-model"
    assert "private-model-key" not in str(captured)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": "bad"}]},
        {"choices": [{"message": {"tool_calls": [{"type": "function"}]}}]},
        {"choices": [{"message": {"content": 123}}]},
    ],
)
async def test_malformed_provider_response_is_rejected(settings, payload):
    async def handle(request):
        return web.json_response(payload)

    app = web.Application()
    app.router.add_post("/chat/completions", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        settings = replace(settings, llm_base_url=str(server.make_url("")))
        with pytest.raises(LLMError):
            await ChatCompletionsClient(settings, session).complete([], [])


async def test_model_errors_hide_body(settings):
    async def handle(request):
        return web.json_response({"error": "private-api-key"}, status=401)

    app = web.Application()
    app.router.add_post("/chat/completions", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        settings = replace(settings, llm_base_url=str(server.make_url("")))
        with pytest.raises(LLMError) as caught:
            await ChatCompletionsClient(settings, session).complete([], [])
        assert "private-api-key" not in str(caught.value)
