import pytest
from conftest import event_payload

from qqbot.commands import CommandRouter
from qqbot.messages import Message


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/ping", "pong"),
        ("PING", "pong"),
        ("/help", "可用命令"),
        ("/帮助", "可用命令"),
        ("/复读 你好 世界", "你好 世界"),
        (" /echo one\ntwo ", "one\ntwo"),
        ("/echo", "用法"),
        ("/time", "北京时间"),
        ("/status", "服务运行正常"),
        ("你好", "暂不支持"),
        ("/复读 " + "字" * 1001, "最多 1000"),
    ],
)
def test_commands(text, expected):
    message = Message.from_payload(event_payload(content=text))
    assert expected in CommandRouter().reply(message)


def test_group_normalization_and_full_mode():
    router = CommandRouter()
    message = Message.from_payload(event_payload(group=True, content="<@!123> /ping"))
    assert router.reply(message).startswith("pong")
    payload = event_payload(group=True, full=True, content="ping")
    assert Message.from_payload(payload) is None
    message = Message.from_payload(payload, accept_full_group=True)
    assert router.reply(message) is None
    payload["d"]["content"] = "/ping"
    assert router.reply(Message.from_payload(payload, accept_full_group=True)).startswith("pong")
    payload["d"]["content"] = "/不存在"
    assert router.reply(Message.from_payload(payload, accept_full_group=True)) is None


def test_ignore_bot_and_nontext_messages():
    payload = event_payload()
    payload["d"]["author"]["bot"] = True
    assert Message.from_payload(payload) is None
    payload["d"]["author"]["bot"] = False
    payload["d"]["message_type"] = 3
    assert Message.from_payload(payload) is None


def test_plan_natural_chat_and_full_group_requires_explicit_chat():
    router = CommandRouter()
    message = Message.from_payload(event_payload(content="帮我查33号楼4032"))
    assert router.plan(message, llm_enabled=True).kind == "chat"
    message = Message.from_payload(event_payload(content="/电费 33#4032"))
    assert router.plan(message).kind == "electricity"
    message = Message.from_payload(
        event_payload(group=True, full=True, content="/random"), accept_full_group=True
    )
    assert router.plan(message, llm_enabled=True) is None
    message = Message.from_payload(
        event_payload(group=True, full=True, content="/聊天 查电量"), accept_full_group=True
    )
    assert router.plan(message, llm_enabled=True).kind == "chat"


def test_group_conversations_are_scoped_to_author():
    payload = event_payload(group=True)
    first = Message.from_payload(payload)
    payload["d"]["author"]["member_openid"] = "other-user"
    second = Message.from_payload(payload)
    assert first.conversation_key != second.conversation_key
