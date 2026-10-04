import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

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
