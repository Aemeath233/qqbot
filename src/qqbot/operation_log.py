"""本地操作审计：关联事件、执行和发送；终端只输出标识指纹。"""

import json
import logging
import secrets
import time
import traceback
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path

from qqbot.dorm_state import identity_tag

logger = logging.getLogger(__name__)
operation_trace = ContextVar("operation_trace", default="")
RETENTION_SECONDS = 7 * 86400
MAX_RECORDS = 50000


def error_location(exc):
    # 不记录异常文本、源码行或局部变量，仍能定位发生异常的代码。
    return [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)[-6:]
    ]


class OperationLog:
    def __init__(self, db):
        self.db = db
        self.instance = secrets.token_hex(6)
        db.execute("""CREATE TABLE IF NOT EXISTS bot_operations (
            id INTEGER PRIMARY KEY, created_at REAL NOT NULL, trace TEXT NOT NULL,
            stage TEXT NOT NULL, kind TEXT NOT NULL, target_id TEXT NOT NULL,
            sender_id TEXT NOT NULL, details TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS operations_time ON bot_operations(created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS operations_trace ON bot_operations(trace,id)")
        db.execute("""CREATE TABLE IF NOT EXISTS interaction_receipts (
            callback TEXT PRIMARY KEY, signature TEXT NOT NULL, created_at REAL NOT NULL
        )""")
        db.commit()
        self.prune()
        self.next_prune = time.monotonic() + 60

    def prune(self):
        with self.db:
            self.db.execute(
                "DELETE FROM bot_operations WHERE created_at<?",
                (time.time() - RETENTION_SECONDS,),
            )
            self.db.execute(
                "DELETE FROM interaction_receipts WHERE created_at<?",
                (time.time() - RETENTION_SECONDS,),
            )
            self.db.execute(
                "DELETE FROM bot_operations WHERE id <= COALESCE("
                "(SELECT id FROM bot_operations ORDER BY id DESC LIMIT 1 OFFSET ?),0)",
                (MAX_RECORDS,),
            )
            self.db.execute(
                "DELETE FROM interaction_receipts WHERE rowid IN "
                "(SELECT rowid FROM interaction_receipts ORDER BY created_at DESC,rowid DESC "
                "LIMIT -1 OFFSET ?)",
                (MAX_RECORDS,),
            )

    def remember_callback(self, click):
        callback = sha256(click.ack_id.encode()).hexdigest()
        signature = sha256(
            json.dumps(
                [
                    click.message.kind,
                    click.message.target_id,
                    click.message.sender_id,
                    click.button_id,
                    click.data,
                ]
            ).encode()
        ).hexdigest()
        with self.db:
            old = self.db.execute(
                "SELECT signature FROM interaction_receipts WHERE callback=?", (callback,)
            ).fetchone()
            if old:
                return "duplicate" if old[0] == signature else "conflict"
            self.db.execute(
                "INSERT INTO interaction_receipts VALUES (?,?,?)",
                (callback, signature, time.time()),
            )
        return "first"

    def record(self, stage, context=None, *, trace=None, commit=True, **details):
        # details 仅由调用方显式构造，不传入消息正文、模型响应或整个 QQ 原始事件。
        details = {"instance": self.instance, **details}
        entry = {
            "trace": operation_trace.get() if trace is None else trace,
            "stage": stage,
            "kind": context.kind if context else "",
            "target_id": context.target_id if context else "",
            "sender_id": context.sender_id if context else "",
            "details": details,
        }
        values = (
            time.time(),
            entry["trace"],
            stage,
            entry["kind"],
            entry["target_id"],
            entry["sender_id"],
            json.dumps(details, ensure_ascii=False, separators=(",", ":")),
        )
        sql = (
            "INSERT INTO bot_operations (created_at,trace,stage,kind,target_id,sender_id,details) "
            "VALUES (?,?,?,?,?,?,?)"
        )
        if commit:
            with self.db:
                self.db.execute(sql, values)
            self.publish(entry)
            if time.monotonic() >= self.next_prune:
                self.prune()
                self.next_prune = time.monotonic() + 60
        else:
            # 与绑定写入在同一个事务内，写审计失败则绑定也回滚。
            if not self.db.in_transaction:
                raise RuntimeError("原子审计必须在已有写事务内")
            self.db.execute(sql, values)
        return entry

    @staticmethod
    def publish(entry):
        safe = {k: v for k, v in entry.items() if k not in {"target_id", "sender_id"}}
        safe["group"] = identity_tag(entry["target_id"]) if entry["target_id"] else ""
        safe["user"] = identity_tag(entry["sender_id"]) if entry["sender_id"] else ""
        logger.info("操作记录 %s", json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
