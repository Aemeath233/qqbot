import sqlite3
import time
from dataclasses import replace

from conftest import event_payload

from qqbot.commands import ReplyTask
from qqbot.inbox import Inbox
from qqbot.messages import Message


def test_persistence_and_dedup_across_restarts(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    message = Message.from_payload(event_payload())
    inbox = Inbox(path)
    assert inbox.add(message, "pong")
    assert not inbox.add(message, "pong")
    inbox.close()
    inbox = Inbox(path)
    assert inbox.pending_count() == 1
    assert inbox.next()["content"] == "pong"
    inbox.done(message.key)
    inbox.close()
    inbox = Inbox(path)
    assert not inbox.add(message, "pong")
    assert inbox.pending_count() == 0
    inbox.close()


def test_expired_messages_and_retry(tmp_path):
    inbox = Inbox(tmp_path / "jobs.sqlite3")
    message = Message.from_payload(event_payload())
    assert not inbox.add(replace(message, expires_at=time.time() - 1), "pong")
    assert inbox.add(message, "pong")
    inbox.failed(message.key, 1, retry=True)
    assert inbox.pending_count() == 1
    assert inbox.next() is None
    inbox.failed(message.key, 2, retry=False)
    assert inbox.pending_count() == 0
    inbox.close()


def test_schema_migration_keeps_old_pending_replies(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE replies (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT NOT NULL,
            message_id TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
            expires_at REAL NOT NULL, next_try_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending'
        )""")
        connection.execute(
            "INSERT INTO replies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "users",
                "user",
                "msg",
                "old-reply",
                time.time(),
                time.time() + 100,
                0,
                0,
                "pending",
            ),
        )
    inbox = Inbox(path)
    assert inbox.next()["content"] == "old-reply"
    assert inbox.next()["prepared"] == 1
    inbox.close()


def test_generated_reply_is_persisted_for_send_retry(tmp_path):
    inbox = Inbox(tmp_path / "jobs.sqlite3")
    message = Message.from_payload(event_payload(content="查电费"))
    assert inbox.add_task(message, ReplyTask("chat", message.content))
    assert inbox.next()["prepared"] == 0
    inbox.save_content(message.key, "real-result")
    assert inbox.next()["prepared"] == 1
    assert inbox.next()["content"] == "real-result"
    inbox.close()
