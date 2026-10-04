import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from qqbot.api import QQAPIError

spec = importlib.util.spec_from_file_location(
    "panel_cleanup", Path(__file__).resolve().parents[1] / "scripts" / "clear_qq_panels.py"
)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


async def test_lists_all_pages_before_deleting():
    api = AsyncMock()
    api.request.side_effect = [
        {"records": [{"panel_id": "one", "scope": "group"}], "next_cursor": "page two"},
        {"records": [{"panel_id": "two", "scope": "group"}], "is_end": True},
    ]
    records = await cleanup.collect_panels(api, ("group",), AsyncMock())
    assert [item["panel_id"] for item in records] == ["one", "two"]
    assert "cursor=page+two" in api.request.call_args_list[1].args[1]
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


async def test_backup_written_before_first_deletion(tmp_path):
    events = []

    async def request(method, path):
        events.append(method)
        if method == "GET":
            return {"panel_id": path.rsplit("/", 1)[1], "panel": {"items": []}}
        assert len(list(tmp_path.glob("*.json"))) == 1
        return {}

    api = AsyncMock()
    api.request.side_effect = request
    records = [{"panel_id": "one", "scope": "group"}, {"panel_id": "two", "scope": "group"}]
    assert await cleanup.clear_panels(api, records, AsyncMock(), backup_dir=tmp_path) == 2
    assert events == ["GET", "GET", "DELETE", "DELETE"]


async def test_detail_failure_does_not_delete(tmp_path):
    api = AsyncMock()
    api.request.side_effect = [{"panel_id": "one"}, QQAPIError(403, "11253")]
    records = [{"panel_id": "one", "scope": "group"}, {"panel_id": "two", "scope": "group"}]
    with pytest.raises(QQAPIError):
        await cleanup.clear_panels(api, records, AsyncMock(), backup_dir=tmp_path)
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


async def test_delete_failure_stops_and_keeps_backup(tmp_path):
    api = AsyncMock()
    api.request.side_effect = [
        {"panel_id": "one"},
        {"panel_id": "two"},
        QQAPIError(429),
    ]
    records = [{"panel_id": "one", "scope": "group"}, {"panel_id": "two", "scope": "group"}]
    with pytest.raises(cleanup.PanelError, match="已删除 0"):
        await cleanup.clear_panels(api, records, AsyncMock(), backup_dir=tmp_path)
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert len(api.request.call_args_list) == 3


async def test_repeated_cursor_stops_without_deleting():
    api = AsyncMock()
    api.request.return_value = {"records": [], "next_cursor": "same", "is_end": False}
    with pytest.raises(cleanup.PanelError, match="重复分页"):
        await cleanup.collect_panels(api, ("group",), AsyncMock())
    assert api.request.await_count == 2


async def test_empty_success_response_is_supported(settings):
    response = AsyncMock()
    response.status = 204
    assert await cleanup.PanelAPI(settings, None)._decode(response) == {}
    response.json.assert_not_awaited()
