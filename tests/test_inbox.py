import time
from dataclasses import replace

from conftest import event_payload

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
