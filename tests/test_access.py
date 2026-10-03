import json

import aiohttp
import pytest

from qqbot.access import AccessError, AccessStore, Principal, validate_rules
from qqbot.assistant import BotAssistant
from qqbot.tools import FunctionTool, ToolRegistry

A = json.dumps(["groups", "group-a", "alice"])
B = json.dumps(["groups", "group-b", "bob"])


def save(store, rules):
    store.save(rules, store.read()[1])


def test_policy_defaults_precedence_user_group_and_operator(tmp_path):
    store = AccessStore(tmp_path / "access.json")
    alice = Principal.from_context(A, "bot")
    bob = Principal.from_context(B, "bot")
    assert store.allowed("roll_dice", alice)
    save(
        store,
        {
            "*": {"mode": "disabled"},
            "mcp__demo__*": {"mode": "admin-only"},
            "mcp__demo__read": {"mode": "allowlist", "users": [alice.uid], "groups": ["group-b"]},
        },
    )
    assert store.allowed("mcp__demo__read", alice) and store.allowed("mcp__demo__read", bob)
    assert not store.allowed("roll_dice", alice) and not store.allowed("mcp__demo__write", bob)
    assert store.allowed("mcp__demo__write", Principal(host="admin"))
    assert store.allowed("mcp__demo__write", Principal(host="console"))
    assert not store.allowed("roll_dice", Principal(host="admin"))
    assert not store.allowed("mcp__demo__read", Principal())


def test_namespace_and_chat_scopes_have_distinct_user_identifiers():
    assert Principal.from_context(A, "one").uid != Principal.from_context(A, "two").uid
    assert (
        Principal.from_context(A).uid
        != Principal.from_context(json.dumps(["users", "alice", "alice"])).uid
    )
    assert Principal.from_context(json.dumps(["groups", "group-a", "", "message"])).uid == ""


@pytest.mark.parametrize(
    "rules",
    [
        {"roll_dice": {"mode": "bad"}},
        {"roll_dice": {"mode": "allowlist", "users": ["123456"]}},
        {"roll_dice": {"mode": "all", "command": "python"}},
        {"bad/path": {"mode": "all"}},
        {"*": {"mode": "allowlist", "groups": ["a\nb"]}},
    ],
)
def test_invalid_rules_rejected(rules):
    with pytest.raises(AccessError):
        validate_rules(rules)


async def test_stale_registry_cannot_execute_after_policy_changes(tmp_path):
    store = AccessStore(tmp_path / "access.json")
    actor = Principal.from_context(A)
    calls = []

    async def handler():
        calls.append(True)
        return {"ok": True}

    registry = ToolRegistry(allowed=lambda name: store.allowed(name, actor))
    registry.register(
        FunctionTool("sample", "sample", {"type": "object", "properties": {}}, handler)
    )
    assert registry.schemas()
    save(store, {"sample": {"mode": "disabled"}})
    assert registry.schemas() == []
    assert (await registry.execute("sample", "{}"))["error"] == "forbidden"
    assert calls == []


async def test_commands_and_model_tools_use_same_access_rules(settings):
    save(
        AccessStore(settings.access_path),
        {
            "query_electricity": {"mode": "disabled"},
            "roll_dice": {"mode": "allowlist", "groups": ["group-a"]},
            "mcp__demo__*": {"mode": "admin-only"},
        },
    )
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session)
        assert "未获准" in await assistant.generate("electricity", "33#4032", A)
        assert "未获准" in await assistant.generate("game", '{"action":"dice","value":"1d6"}', B)
        assert "合计" in await assistant.generate("game", '{"action":"dice","value":"1d6"}', A)
        assert Principal.from_context(A, settings.app_id).uid in await assistant.generate(
            "identity", "", A
        )


def test_corruption_fails_closed_and_revision_prevents_overwrite(tmp_path):
    store = AccessStore(tmp_path / "access.json")
    old = store.read()[1]
    save(store, {"*": {"mode": "all"}})
    with pytest.raises(AccessError, match="变化"):
        store.save({}, old)
    store.path.write_text("bad-json")
    assert not store.allowed("roll_dice", Principal(host="admin"))
