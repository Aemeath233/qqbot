"""先持久化再确认回调；固定 msg_seq 配合平台去重。仅运行一个服务实例。"""

import sqlite3
import time
from pathlib import Path

from qqbot.commands import ReplyTask
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
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(replies)")}
        # 已部署的旧数据库保留任务，新列默认把旧任务视为已生成的文本回复。
        for name, definition in {
            "task_kind": "TEXT NOT NULL DEFAULT 'text'",
            "task_payload": "TEXT NOT NULL DEFAULT ''",
            "conversation_key": "TEXT NOT NULL DEFAULT ''",
            "prepared": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE replies ADD COLUMN {name} {definition}")
        self.db.commit()

    def add(
        self, message: Message, content: str, *, task_kind: str = "text", task_payload: str = ""
    ) -> bool:
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
                   (key, kind, target_id, message_id, content, created_at, expires_at, next_try_at,
                    task_kind, task_payload, conversation_key, prepared)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.key,
                    message.kind,
                    message.target_id,
                    message.message_id,
                    content,
                    now,
                    message.expires_at,
                    now,
                    task_kind,
                    task_payload,
                    message.conversation_key,
                    int(task_kind == "text"),
                ),
            )
        return True

    def add_task(self, message: Message, task: ReplyTask) -> bool:
        if task.kind == "text":
            return self.add(message, task.content)
        return self.add(message, "", task_kind=task.kind, task_payload=task.content)

    def save_content(self, key: str, content: str):
        # QQ 发送失败后重试这一份回复，不重新请求 LLM 或重新查询电量。
        with self.db:
            self.db.execute(
                "UPDATE replies SET content = ?, prepared = 1 WHERE key = ?", (content, key)
            )

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
