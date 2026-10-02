"""先持久化再确认回调；固定 msg_seq 配合平台去重。仅运行一个服务实例。"""

import sqlite3
import time
from pathlib import Path

from qqbot.messages import Message


class InboxFull(Exception):
    pass


class Inbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS replies (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                next_try_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS replies_ready ON replies(state, next_try_at)")
        self.db.commit()

    def add(self, message: Message, content: str) -> bool:
        now = time.time()
        with self.db:
            self.db.execute(
                "DELETE FROM replies WHERE state != 'pending' AND created_at < ?", (now - 86400,)
            )
            self.db.execute(
                "UPDATE replies SET state = 'expired' WHERE state = 'pending' AND expires_at <= ?",
                (now,),
            )
            if (
                message.expires_at <= now
                or self.db.execute("SELECT 1 FROM replies WHERE key = ?", (message.key,)).fetchone()
            ):
                return False
            if self.pending_count() >= 1000:
                raise InboxFull
            self.db.execute(
                """INSERT INTO replies
                   (key, kind, target_id, message_id, content, created_at, expires_at, next_try_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.key,
                    message.kind,
                    message.target_id,
                    message.message_id,
                    content,
                    now,
                    message.expires_at,
                    now,
                ),
            )
        return True

    def next(self):
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE replies SET state = 'expired' WHERE state = 'pending' AND expires_at <= ?",
                (now,),
            )
        return self.db.execute(
            """SELECT * FROM replies WHERE state = 'pending' AND next_try_at <= ?
               ORDER BY created_at LIMIT 1""",
            (now,),
        ).fetchone()

    def done(self, key: str):
        with self.db:
            self.db.execute("UPDATE replies SET state = 'done' WHERE key = ?", (key,))

    def failed(self, key: str, attempts: int, *, retry: bool):
        with self.db:
            self.db.execute(
                "UPDATE replies SET state = ?, attempts = ?, next_try_at = ? WHERE key = ?",
                (
                    "pending" if retry else "failed",
                    attempts,
                    time.time() + min(2**attempts, 30),
                    key,
                ),
            )

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM replies WHERE state = 'pending'").fetchone()[0]

    def close(self):
        self.db.close()
