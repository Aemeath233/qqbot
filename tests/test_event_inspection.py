import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

from qqbot.dorm_state import UserContext
from qqbot.operation_log import OperationLog

spec = importlib.util.spec_from_file_location(
    "event_inspection",
    Path(__file__).resolve().parents[1] / "scripts/inspect_electricity_events.py",
)
inspection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspection)


def test_event_report_is_read_only_and_does_not_expose_ids_or_payload(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE replies (created_at REAL,kind TEXT,target_id TEXT,"
            "sender_id TEXT,task_kind TEXT,task_payload TEXT,state TEXT)"
        )
        db.execute(
            "INSERT INTO replies VALUES (?,?,?,?,?,?,?)",
            (
                time.time(),
                "groups",
                "private-group-id",
                "private-user-id",
                "button_bind",
                '{"dormitory":"21#2011","token":"private-callback-token"}',
                "done",
            ),
        )
    before = path.read_bytes()
    report = inspection.read_events(path)
    assert report[0]["宿舍"] == "21#2011"
    assert report[0]["任务"] == "button_bind"
    text = str(report)
    assert "private-group-id" not in text and "private-user-id" not in text
    assert "private-callback-token" not in text
    assert before == path.read_bytes()


def test_wrong_database_path_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        inspection.read_events(path)
    assert not path.exists()


def test_detailed_report_filters_trace_user_group_and_only_reveals_ids_on_request(tmp_path):
    path = tmp_path / "audit.sqlite3"
    with sqlite3.connect(path) as db:
        audit = OperationLog(db)
        audit.record(
            "button_received",
            UserContext("groups", "private-group", "private-user"),
            trace="0123456789",
            data_action="bind",
        )
        audit.record(
            "query_started",
            UserContext("groups", "other-group", "private-user"),
            trace="1123456789",
            dormitory="33#4032",
        )
    before = path.read_bytes()
    events = inspection.read_operations(path, user=inspection.tag("private-user"))
    assert len(events) == 2
    assert "private-user" not in str(events) and "private-group" not in str(events)
    filtered = inspection.read_operations(
        path,
        trace="0123456789",
        group=inspection.tag("private-group"),
        show_ids=True,
    )
    assert len(filtered) == 1
    assert filtered[0]["用户OpenID"] == "private-user"
    assert filtered[0]["群或私聊OpenID"] == "private-group"
    assert filtered[0]["详情"]["data_action"] == "bind"
    assert path.read_bytes() == before


def test_old_database_without_audit_has_empty_report_and_no_mutation(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE old_table (id INTEGER)")
    before = path.read_bytes()
    assert inspection.read_operations(path) == []
    assert path.read_bytes() == before


def test_operation_retention_prunes_old_and_excess_records(tmp_path, monkeypatch):
    monkeypatch.setattr("qqbot.operation_log.MAX_RECORDS", 2)
    with sqlite3.connect(tmp_path / "audit.sqlite3") as db:
        audit = OperationLog(db)
        audit.record("old")
        db.execute("UPDATE bot_operations SET created_at=0")
        db.execute("INSERT INTO interaction_receipts VALUES ('old','signature',0)")
        db.commit()
        for stage in ("first", "second", "third"):
            audit.record(stage)
        audit.prune()
        assert [r[0] for r in db.execute("SELECT stage FROM bot_operations ORDER BY id")] == [
            "second",
            "third",
        ]
        assert db.execute("SELECT COUNT(*) FROM interaction_receipts").fetchone()[0] == 0
