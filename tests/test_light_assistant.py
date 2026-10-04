import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from conftest import event_payload

from qqbot.light_assistant import LightAssistant
from qqbot.light_commands import LightCommandRouter
from qqbot.llm import LLMError
from qqbot.messages import Message


def tool_response(arguments, *, name="query_electricity"):
    return {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}


@pytest.fixture
def assistant(settings):
    bot = LightAssistant(replace(settings, llm_enabled=True, electricity_enabled=True), None)
    bot.model.complete = AsyncMock(return_value=tool_response({"dormitory": "33#4032"}))
    bot.electricity.query = AsyncMock(
        return_value={
            "ok": True,
            "dormitory": "33#4032",
            "remaining_kwh": "47.93",
            "queried_at": "2026-10-04 17:00:00",
        }
    )
    return bot


@pytest.mark.parametrize(
    "text",
    [
        "看看33楼4032宿舍电费",
        "帮我看下33幢4032的电还剩多少",
        "我的房间是4032，在33号楼，麻烦瞅一眼余额",
        "三十三号楼4032室的电量怎么样了",
        "33#4032",
        "请告诉我33栋4032室还有多少电",
    ],
)
async def test_natural_language_reaches_model_and_executes_tool(assistant, text):
    reply = await assistant.generate("chat", text)
    messages = assistant.model.complete.call_args.args[0]
    assert messages[-1] == {"role": "user", "content": text}
    assistant.electricity.query.assert_awaited_once_with("33#4032", area="")
    assert "47.93 度" in reply


@pytest.mark.parametrize(
    "text,answer",
    [
        ("你好", "你好，告诉我宿舍楼号和房间号，我可以帮你查剩余电量。"),
        ("帮我写代码", "我只能帮你查宿舍当前电量，不能帮你写代码。"),
        ("最近三天用了多少电", "我只能查当前剩余电量，暂时不能计算历史用电。"),
        ("4032宿舍电费", "4032是哪个楼的房间？请提供完整楼号和房号。"),
    ],
)
async def test_model_decides_clarification_or_refusal(assistant, text, answer):
    assistant.model.complete.return_value = {"content": answer}
    assert await assistant.generate("chat", text) == answer
    assistant.model.complete.assert_awaited_once()
    assistant.electricity.query.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [
        {"dormitory": "4032"},
        {"dormitory": 4032},
        {"dormitory": "33#4032", "endpoint": "https://example.com"},
        {"dormitory": "３３#４０３２"},
        {"dormitory": "33#4032", "area": 2},
    ],
)
async def test_invalid_tool_arguments_do_not_query(assistant, arguments):
    assistant.model.complete.return_value = tool_response(arguments)
    assert "楼号和房间号" in await assistant.generate("chat", "看看宿舍电费")
    assistant.electricity.query.assert_not_awaited()


async def test_unknown_tool_is_not_executed(assistant):
    assistant.model.complete.return_value = tool_response({}, name="run_python")
    await assistant.generate("chat", "看看电费")
    assistant.electricity.query.assert_not_awaited()


async def test_empty_model_response_reports_failure(assistant):
    assistant.model.complete.return_value = {"content": None}
    assert "没有完成查询" in await assistant.generate("chat", "看看33楼4032宿舍电费")
    assistant.electricity.query.assert_not_awaited()


async def test_model_error_does_not_query(assistant):
    assistant.model.complete.side_effect = LLMError("模型服务连接失败或超时")
    assert "暂时不可用" in await assistant.generate("chat", "看看33楼4032宿舍电费")
    assistant.electricity.query.assert_not_awaited()


async def test_directory_still_rejects_nonexistent_room(settings):
    bot = LightAssistant(replace(settings, llm_enabled=True, electricity_enabled=True), None)
    bot.model.complete = AsyncMock(return_value=tool_response({"dormitory": "33#999999"}))
    bot.electricity._query = AsyncMock()
    assert "没有匹配到这个宿舍" in await bot.generate("chat", "看看33楼999999的电费")
    bot.electricity._query.assert_not_awaited()


@pytest.mark.parametrize("text", ["看看33楼4032宿舍电费", "/电费 33#4032", "/随便什么话"])
async def test_router_passes_original_message_to_model(assistant, text):
    message = Message.from_payload(event_payload(group=True, content=text))
    task = LightCommandRouter().plan(message, llm_enabled=True)
    await assistant.generate(task.kind, task.content)
    assert assistant.model.complete.call_args.args[0][-1]["content"] == text
