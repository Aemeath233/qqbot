"""先持久化再确认回调；固定 msg_seq 配合平台去重。仅运行一个服务实例。"""

import sqlite3
import time
from pathlib import Path

from qqbot.commands import ReplyTask
from qqbot.messages import Message
from qqbot.presentation import Reply

INTERRUPTED_REPLY = (
    "上次请求处理被中断，执行结果无法确认。为避免重复操作，本次未重新执行。"
    "请先核对实际状态；如需继续，请发送新消息。"
)


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
            "generation_started": "INTEGER NOT NULL DEFAULT 0",
            "sender_id": "TEXT NOT NULL DEFAULT ''",
            "reference": "TEXT NOT NULL DEFAULT 'msg_id'",
            "reply_json": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE replies ADD COLUMN {name} {definition}")
        if "generation_started" not in columns:
            # 升级前的未完成生成没有执行记录，不能假定包内函数尚未产生副作用。
            self.db.execute(
                "UPDATE replies SET generation_started=1 WHERE state='pending' AND prepared=0"
            )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS replies_conversation "
            "ON replies(conversation_key,state,created_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS replies_target ON replies(kind,target_id,state)"
        )
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
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM replies WHERE state='pending' AND conversation_key=?",
                    (message.conversation_key,),
                ).fetchone()[0]
                >= 8
            ):
                raise InboxFull
            if (
                message.kind == "groups"
                and self.db.execute(
                    "SELECT COUNT(*) FROM replies WHERE state='pending' "
                    "AND kind='groups' AND target_id=?",
                    (message.target_id,),
                ).fetchone()[0]
                >= 32
            ):
                raise InboxFull
            self.db.execute(
                """INSERT INTO replies
                   (key, kind, target_id, message_id, content, created_at, expires_at, next_try_at,
                    task_kind, task_payload, conversation_key, prepared, sender_id, reference)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    message.sender_id,
                    message.reference,
                ),
            )
        return True

    def add_task(self, message: Message, task: ReplyTask) -> bool:
        if task.kind == "text":
            return self.add(message, task.content)
        return self.add(message, "", task_kind=task.kind, task_payload=task.content)

    def save_content(self, key: str, content: str | Reply):
        # QQ 发送失败后重试这一份回复，不重新请求 LLM 或重新查询电量。
        with self.db:
            self.db.execute(
                "UPDATE replies SET content = ?, reply_json = ?, prepared = 1 WHERE key = ?",
                (
                    content.text if isinstance(content, Reply) else content,
                    content.dumps() if isinstance(content, Reply) else "",
                    key,
                ),
            )

    def start_generation(self, key: str):
        # 必须在任何模型/函数调用之前提交，停机后不重放已经开始的操作。
        with self.db:
            self.db.execute("UPDATE replies SET generation_started=1 WHERE key=?", (key,))

    def next(self, *, excluded_contexts=(), prepared: bool | None = None):
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE replies SET state = 'expired' WHERE state = 'pending' AND expires_at <= ?",
                (now,),
            )
        conditions = ["r.state='pending'", "r.next_try_at<=?"]
        parameters = [now]
        if excluded_contexts:
            conditions.append(
                "r.conversation_key NOT IN (" + ",".join("?" for _ in excluded_contexts) + ")"
            )
            parameters.extend(excluded_contexts)
        if prepared is not None:
            conditions.append("r.prepared=?")
            parameters.append(int(prepared))
        # 同一会话的后续任务不能越过正在运行或等待发送重试的前一条。
        conditions.append("""NOT EXISTS (
            SELECT 1 FROM replies older WHERE older.state='pending'
            AND older.conversation_key=r.conversation_key
            AND (older.created_at<r.created_at OR
                 (older.created_at=r.created_at AND older.rowid<r.rowid))
        )""")
        return self.db.execute(
            "SELECT r.* FROM replies r WHERE "
            + " AND ".join(conditions)
            + " ORDER BY r.created_at,r.rowid LIMIT 1",
            parameters,
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
