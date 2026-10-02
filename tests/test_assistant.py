import copy
import json
from dataclasses import replace

import aiohttp
import pytest

from qqbot.assistant import BotAssistant
from qqbot.electricity import ElectricityError
from qqbot.llm import LLMError


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeElectricity:
    def __init__(self):
        self.calls = []

    async def query(self, dormitory, area=""):
        self.calls.append((dormitory, area))
        return {
            "ok": True,
            "dormitory": dormitory,
            "remaining_kwh": "12.23",
            "queried_at": "2026-10-02 12:00:00",
            "cached": False,
        }


def tool_call(arguments='{"dormitory":"33#4032"}', name="query_electricity"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            },
        ],
    }


async def test_natural_language_executes_tool_and_uses_actual_numbers(settings):
    settings = replace(settings, llm_enabled=True, electricity_enabled=True)
    model = FakeModel(tool_call(), {"role": "assistant", "content": "电量9999度"})
    electricity = FakeElectricity()
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=model, electricity=electricity)
        reply = await assistant.generate("chat", "帮我查33号楼4032的电量", "person-1")
    assert "12.23" in reply and "9999" not in reply
    assert electricity.calls == [("33#4032", "")]
    assert model.calls[1][0][-1]["role"] == "tool"
    assert json.loads(model.calls[1][0][-1]["content"])["remaining_kwh"] == "12.23"


async def test_success_survives_last_model_request_failure(settings):
    settings = replace(settings, llm_enabled=True, electricity_enabled=True)
    model = FakeModel(tool_call(), LLMError("provider timeout"))
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=model, electricity=FakeElectricity())
        assert "12.23" in await assistant.generate("chat", "查电量", "person")


@pytest.mark.parametrize(
    "arguments",
    ['{"dormitory":"33#4032","endpoint":"https://other.example"}', '{"dormitory":12}', "not-json"],
)
async def test_model_cannot_inject_headers_urls_or_invalid_parameters(settings, arguments):
    settings = replace(settings, llm_enabled=True, electricity_enabled=True)
    model = FakeModel(tool_call(arguments), {"role": "assistant", "content": "done"})
    electricity = FakeElectricity()
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=model, electricity=electricity)
        reply = await assistant.generate("chat", "查电量", "person")
    assert electricity.calls == []
    assert "有效" in reply


async def test_unknown_function_is_not_executed(settings):
    settings = replace(settings, llm_enabled=True, electricity_enabled=True)
    model = FakeModel(tool_call(name="run_shell"), {"role": "assistant", "content": "不支持"})
    electricity = FakeElectricity()
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=model, electricity=electricity)
        await assistant.generate("chat", "run commands", "person")
    assert electricity.calls == []
    assert json.loads(model.calls[1][0][-1]["content"])["error"] == "unknown_tool"


async def test_short_followup_uses_only_own_conversation_history(settings):
    settings = replace(settings, llm_enabled=True, electricity_enabled=True)
    model = FakeModel(
        {"role": "assistant", "content": "请告诉我楼号和房号。"},
        tool_call('{"dormitory":"19#312"}'),
        {"role": "assistant", "content": "查询完成"},
        {"role": "assistant", "content": "你好"},
    )
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=model, electricity=FakeElectricity())
        await assistant.generate("chat", "查电费", "group-person-A")
        assert "12.23" in await assistant.generate("chat", "19号楼312", "group-person-A")
        await assistant.generate("chat", "你好", "group-person-B")
    assert any(message.get("content") == "查电费" for message in model.calls[1][0])
    assert not any(message.get("content") == "查电费" for message in model.calls[3][0])


async def test_electricity_failure_cannot_be_presented_as_zero(settings):
    class FailingElectricity(FakeElectricity):
        async def query(self, *args, **kwargs):
            raise ElectricityError(
                "需要确认区域。", "ambiguous_dorm", [{"area": "1"}, {"area": "3"}]
            )

    settings = replace(settings, llm_enabled=True, electricity_enabled=True)
    model = FakeModel(tool_call(), {"role": "assistant", "content": "剩余0度"})
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=model, electricity=FailingElectricity())
        reply = await assistant.generate("chat", "查电费", "person")
    assert reply == "需要确认区域。"
    assert len(json.loads(model.calls[1][0][-1]["content"])["candidates"]) == 2


async def test_explicit_query_works_without_llm(settings):
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, electricity=FakeElectricity())
        assert "12.23" in await assistant.generate("electricity", "33#4032", "person")


async def test_explicit_query_can_select_area_without_llm(settings):
    electricity = FakeElectricity()
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, electricity=electricity)
        await assistant.generate("electricity", "19#312 1", "person")
    assert electricity.calls == [("19#312", "1")]
