import json
import time
from dataclasses import replace

import aiohttp
import pytest

from qqbot.assistant import BotAssistant
from qqbot.electricity import ElectricityError
from qqbot.electricity_history import HistoryAccess, format_history, meter_key
from qqbot.tools import ToolRegistry, history_tools
from qqbot.user_store import UserDataError, UserStore

A = json.dumps(["users", "alice", "alice"])
B = json.dumps(["users", "bob", "bob"])
NOW = 1800000000.0
METER = meter_key("1402", 953, "2", "2-11--4-4032")


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.sqlite3", clock=lambda: NOW)


def record(
    store, context, stamp, quantity, *, event=None, cached=False, identifier=METER, dorm="33#4032"
):
    store.add_reading(
        context,
        {
            "meter_key": identifier,
            "dormitory": dorm,
            "area": "2",
            "area_name": "测试区域",
            "quantity": quantity,
            "observed_at": stamp,
            "cached": cached,
        },
        event or f"{identifier}-{stamp}-{quantity}",
        365,
    )


async def test_three_day_calculation_uses_decimal_and_exact_observed_interval(settings, store):
    record(store, A, NOW - 3 * 86400, "51.20")
    record(store, A, NOW - 86400, "45.15")
    record(store, A, NOW, "40.10")
    result = await HistoryAccess(settings, store, A, {}).usage(3)
    assert result["consumption_estimate_kwh"] == "11.10"
    assert result["covered_hours"] == 72 and result["sample_count"] == 3
    text = format_history(result)
    assert "未充值" in text and "11.10" in text and "72" in text


async def test_short_history_never_claims_full_requested_three_days(settings, store):
    record(store, A, NOW - 3600, "10.50")
    record(store, A, NOW, "9.25")
    result = await HistoryAccess(settings, store, A, {}).usage(3)
    assert result["covered_hours"] == 1
    assert "实际覆盖" in format_history(result)
    assert result["consumption_estimate_kwh"] == "1.25"


async def test_increase_suppresses_consumption_estimate_even_if_net_decrease_positive(
    settings, store
):
    record(store, A, NOW - 2 * 86400, "50.00")
    record(store, A, NOW - 86400, "55.00")
    record(store, A, NOW, "40.00")
    result = await HistoryAccess(settings, store, A, {}).usage(3)
    assert result["possible_recharge"] and result["consumption_estimate_kwh"] is None
    assert result["net_decrease_kwh"] == "10.00"
    assert "不能" in format_history(result) and "充值" in format_history(result)


async def test_other_user_readings_are_never_used_for_current_user_estimate(settings, store):
    record(store, B, NOW - 3 * 86400, "100.00")
    record(store, B, NOW, "90.00")
    record(store, A, NOW, "89.00")
    access = HistoryAccess(settings, store, A, {})
    assert not (await access.usage(3))["ok"]
    history = await access.history(3)
    assert history["query_count"] == 1
    assert "100.00" not in json.dumps(history)
    assert "user_id" not in json.dumps(history) and "meter_key" not in json.dumps(history)


async def test_different_meters_require_selection_and_bound_dorm_selects_own_meter(settings, store):
    second = meter_key("1402", 953, "2", "2-11--2-2035")
    record(store, A, NOW - 3600, "30.00")
    record(store, A, NOW, "29.00")
    record(store, A, NOW, "100.00", identifier=second, dorm="33#2035")
    with pytest.raises(UserDataError, match="多间"):
        await HistoryAccess(settings, store, A, {}).usage(3)
    result = await HistoryAccess(settings, store, A, {"dormitory": "33#4032", "area": "2"}).usage(3)
    assert result["consumption_estimate_kwh"] == "1.00"
    explicit = await HistoryAccess(settings, store, A, {}).usage(3, "33#4032", "2")
    assert explicit["last_kwh"] == "29.00"


async def test_cached_accesses_are_logged_but_not_counted_as_independent_points(settings, store):
    record(store, A, NOW - 30, "42.00", event="first")
    record(store, A, NOW - 30, "42.00", event="second", cached=True)
    record(store, A, NOW - 30, "42.00", event="third", cached=True)
    access = HistoryAccess(settings, store, A, {})
    result = await access.history(3)
    assert result["query_count"] == 3 and "缓存" in format_history(result)
    assert not (await access.usage(3))["ok"]


async def test_conflicting_readings_at_same_timestamp_do_not_produce_estimate(settings, store):
    record(store, A, NOW - 3600, "42.00")
    record(store, A, NOW - 3600, "41.00")
    record(store, A, NOW, "40.00")
    result = await HistoryAccess(settings, store, A, {}).usage(3)
    assert not result["ok"] and "不同电量" in result["message"]


async def test_window_excludes_old_readings_and_future_timestamps_are_rejected(settings, store):
    record(store, A, NOW - 4 * 86400, "100.00")
    record(store, A, NOW, "99.00")
    assert not (await HistoryAccess(settings, store, A, {}).usage(3))["ok"]
    with pytest.raises(UserDataError):
        record(store, A, NOW + 60, "98.00")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "1e999999", "not-number"])
def test_invalid_balance_cannot_enter_history_table(store, amount):
    with pytest.raises(UserDataError):
        record(store, A, NOW, amount)
    assert store.readings(A, 3) == []


def test_replayed_message_updates_one_event_and_query_log_survives_restart(store):
    record(store, A, NOW - 30, "42.00", event="same-user-query")
    record(store, A, NOW, "41.90", event="same-user-query")
    reloaded = UserStore(store.path, clock=lambda: NOW)
    rows = reloaded.readings(A, 3)
    assert len(rows) == 1 and rows[0]["quantity"] == "41.90"


def test_forget_only_deletes_current_users_ledger_and_profile(store):
    record(store, A, NOW, "42.00")
    record(store, B, NOW, "42.00")
    store.save_profile(A, nickname="阿明")
    store.save_profile(B, nickname="小红")
    store.forget(A)
    assert store.profile(A) == {} and store.readings(A, 3) == []
    assert store.profile(B)["nickname"] == "小红" and len(store.readings(B, 3)) == 1


async def test_model_cannot_choose_other_owner_or_invalid_day_count(settings, store):
    registry = ToolRegistry()
    for tool in history_tools(HistoryAccess(settings, store, A, {})):
        registry.register(tool)
    for params in [{"days": 3, "user_id": "bob"}, {"days": 0}, {"days": 366}, {"days": True}]:
        result = await registry.execute("electricity_usage", json.dumps(params))
        assert not result["ok"] and result["error"] == "invalid_arguments"


async def test_successful_queries_log_without_extra_requests_and_retry_deduplicates(settings):
    class Electric:
        def __init__(self):
            self.calls = 0

        async def query(self, dormitory, area=""):
            self.calls += 1
            return {
                "ok": True,
                "dormitory": "33#4032",
                "area": "2",
                "remaining_kwh": "12.23",
                "queried_at": "unused",
                "_observed_at": time.time(),
                "cached": False,
            }

    electric = Electric()
    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, electricity=electric)
        await assistant.generate("electricity", "33#4032", A, request_id="same-message")
        await assistant.generate("electricity", "33#4032", A, request_id="same-message")
        assert len(assistant.users.readings(A, 3)) == 1
        history = await assistant.generate("history", '{"days":3}', A)
        usage = await assistant.generate("usage", '{"days":3}', A)
    assert electric.calls == 2 and "12.23" in history and "不足" in usage


async def test_failed_electricity_queries_are_not_logged(settings):
    class Failing:
        async def query(self, dormitory, area=""):
            raise ElectricityError("模拟失败")

    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(settings, session, electricity=Failing())
        assert await assistant.generate("electricity", "33#4032", A) == "模拟失败"
        assert assistant.users.readings(A, 3) == []


async def test_statistics_override_model_fabricated_numbers(settings, store):
    record(store, A, NOW - 86400, "40.00")
    record(store, A, NOW, "35.00")
    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "stats",
                    "type": "function",
                    "function": {
                        "name": "electricity_usage",
                        "arguments": '{"days":3}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "你准确用了9999度"},
    ]

    class Model:
        async def complete(self, messages, tools):
            return responses.pop(0)

    async with aiohttp.ClientSession() as session:
        assistant = BotAssistant(
            replace(settings, llm_enabled=True), session, model=Model(), user_store=store
        )
        reply = await assistant.generate("chat", "帮我看看用电情况", A)
    assert "5.00" in reply and "9999" not in reply and "未充值" in reply
