import json
from dataclasses import replace
from urllib.parse import urlsplit

import aiohttp
import pytest

from qqbot.assistant import BotAssistant
from qqbot.electricity_history import HistoryAccess, meter_key
from qqbot.portal_access import PortalAccess, curve_payload, webpage_request
from qqbot.portal_store import PortalError, PortalStore
from qqbot.user_store import UserDataError, UserStore

A = json.dumps(["users", "alice", "alice"])
B = json.dumps(["users", "bob", "bob"])


def test_capabilities_expire_revoke_and_only_store_hashes(tmp_path):
    now = [1000.0]
    store = PortalStore(tmp_path / "portal.sqlite3", clock=lambda: now[0])
    owner = "a" * 64
    identifier, token = store.link(owner, "curve", "b" * 64, {"days": 7}, ttl=1800)
    assert token.encode() not in store.path.read_bytes()
    assert store.authorize(identifier, token, "curve")["owner"] == owner
    for bad in ("x" * 43, "short"):
        with pytest.raises(PortalError):
            store.authorize(identifier, bad, "curve")
    now[0] += 1800
    with pytest.raises(PortalError):
        store.authorize(identifier, token, "curve")
    identifier, token = store.link(owner, "curve", "b" * 64)
    store.revoke(owner, kind="curve")
    with pytest.raises(PortalError):
        store.authorize(identifier, token, "curve")


def test_pages_are_owner_scoped_publication_is_reversible_and_creation_idempotent(tmp_path):
    store = PortalStore(tmp_path / "portal.sqlite3")
    alice, bob = "a" * 64, "b" * 64
    identifier = store.draft(alice, "Title", "<h1>Hello</h1>", "request-1")
    assert store.draft(alice, "Different", "changed", "request-1") == identifier
    assert len(store.list_pages(alice)) == 1 and store.list_pages(bob) == []
    with pytest.raises(PortalError):
        store.page(identifier, public=True)
    for operation in (
        lambda: store.publish(bob, identifier, "public"),
        lambda: store.retract(bob, identifier),
    ):
        with pytest.raises(PortalError):
            operation()
    store.publish(alice, identifier, "public")
    assert store.page(identifier, public=True)["content"] == "<h1>Hello</h1>"
    link, token = store.link(alice, "page", identifier)
    store.retract(alice, identifier)
    with pytest.raises(PortalError):
        store.page(identifier, public=True)
    with pytest.raises(PortalError):
        store.authorize(link, token, "page")
    store.forget(alice)
    assert store.list_pages() == []


@pytest.mark.parametrize(
    "title,content",
    [("", "html"), ("a\nb", "html"), ("x" * 81, "html"), ("valid", "x" * 18001), ("valid", "\0")],
)
def test_page_validation(tmp_path, title, content):
    store = PortalStore(tmp_path / "portal.sqlite3")
    with pytest.raises(PortalError):
        store.draft("a" * 64, title, content, "request")


def test_curve_statistics_deduplicate_samples_and_detect_increases():
    rows = [
        {"observed_at": stamp, "quantity": quantity, "dormitory": "33#4032", "area_name": "区域2"}
        for stamp, quantity in [(1000, "50.0"), (2000, "40.0"), (2000, "40.0"), (3000, "45.0")]
    ]
    result = curve_payload(rows, 3)
    assert result["sample_count"] == 3 and result["query_count"] == 4
    assert result["net_decrease_kwh"] == "5.0" and result["has_increase"]
    assert "owner" not in result and "meter_key" not in result
    with pytest.raises(UserDataError):
        curve_payload([rows[0], {**rows[0], "quantity": "51.0"}], 3)


async def test_curve_creation_uses_own_history_and_half_hour_link(settings, tmp_path):
    settings = replace(settings, portal_enabled=True, public_base_url="https://bot.example.com")
    users = UserStore(tmp_path / "users.sqlite3")
    users.save_profile(A, dormitory="33#4032", area="2")
    access = PortalAccess(
        settings,
        PortalStore(settings.portal_db_path),
        users,
        A,
        HistoryAccess(settings, users, A, users.profile(A)),
    )
    result = await access.curve(days=7, minutes=30)
    parts = urlsplit(result["url"])
    capability = access.store.authorize(parts.path.split("/")[-1], parts.fragment, "curve")
    assert capability["owner"] == users.identity(A) and result["minutes"] == 30
    assert capability["target"] == meter_key("1402", 953, "2", "2-11--4-4032")
    assert await access.curve(days=7, minutes=30) == result
    access.action("revoke_curve")
    with pytest.raises(PortalError):
        access.store.authorize(parts.path.split("/")[-1], parts.fragment, "curve")


async def test_requested_webpage_is_published_and_other_user_cannot_retract(settings):
    settings = replace(
        settings, portal_enabled=True, public_base_url="https://bot.example.com", llm_enabled=True
    )
    calls = []

    class Model:
        async def complete(self, messages, tools):
            calls.append((messages.copy(), tools))
            if len(calls) == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "page-1",
                            "type": "function",
                            "function": {
                                "name": "create_webpage",
                                "arguments": json.dumps(
                                    {"title": "计数器", "html": "<button>Count</button>"}
                                ),
                            },
                        }
                    ],
                }
            assert "url" not in json.loads(messages[-1]["content"])
            return {"role": "assistant", "content": "https://invented.example/page"}

    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=Model())
        reply = await assistant.generate("webpage", "一个计数器", A, request_id="page-event")
        assert "bot.example.com/p/" in reply and "invented.example" not in reply
        page = assistant.portal_store.list_pages(assistant.users.identity(A))[0]
        assert page["visibility"] == "public"
        other = await assistant.generate(
            "portal_action", json.dumps({"action": "retract", "identifier": page["id"]}), B
        )
        assert "失败" in other
        assert assistant.portal_store.page(page["id"], public=True)
        await assistant.generate("profile", '{"action":"forget"}', A)
        assert assistant.portal_store.list_pages() == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我做一个网页", True),
        ("生成一个简单的计数器网页", True),
        ("你好", False),
        ("我想看用电曲线", False),
    ],
)
def test_page_creation_only_available_for_explicit_requests(text, expected):
    assert webpage_request(text) is expected


async def test_model_cannot_claim_published_url_without_creating_page(settings):
    settings = replace(
        settings, portal_enabled=True, public_base_url="https://bot.example.com", llm_enabled=True
    )

    class Model:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": "已生成，https://invented.example/page"}

    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=Model())
        reply = await assistant.generate("webpage", "一个网页", A)
    assert "尚未生成" in reply and "invented.example" not in reply


async def test_request_for_real_electricity_curve_never_exposes_generic_html_generator(settings):
    settings = replace(
        settings, portal_enabled=True, public_base_url="https://bot.example.com", llm_enabled=True
    )
    seen = []

    class Model:
        async def complete(self, messages, tools):
            seen.extend(tool["function"]["name"] for tool in tools)
            return {"role": "assistant", "content": "需要真实数据。"}

    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, model=Model())
        await assistant.generate("webpage", "最近3天电量曲线", A)
    assert "electricity_chart" in seen and "create_webpage" not in seen
