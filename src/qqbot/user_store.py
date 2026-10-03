"""用户主动登记的资料与查询台账，身份由可信回调上下文决定。"""

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path


class UserDataError(Exception):
    pass


def user_id(context: str, namespace: str = "") -> str | None:
    if context == "local-console":
        scope = context
    else:
        try:
            parts = json.loads(context)
        except (ValueError, TypeError):
            return None
        if (
            not isinstance(parts, list)
            or len(parts) != 3
            or not isinstance(parts[0], str)
            or parts[0] not in {"users", "groups"}
            or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in parts[1:])
        ):
            return None
        scope = json.dumps(parts, separators=(",", ":"))
    return hashlib.sha256((namespace + "\0" + scope).encode()).hexdigest()


class UserStore:
    def __init__(self, path: Path, *, namespace: str = "", clock=time.time):
        self.path, self.namespace, self.clock = path, namespace, clock

    def identity(self, context: str) -> str | None:
        return user_id(context, self.namespace)

    @contextmanager
    def connect(self, *, write=False):
        db = None
        try:
            if write:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=2)
                self.path.chmod(0o600)
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS profiles (
                        user_id TEXT PRIMARY KEY, nickname TEXT NOT NULL DEFAULT '',
                        dormitory TEXT NOT NULL DEFAULT '', area TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS electricity_queries (
                        id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, event_key TEXT NOT NULL,
                        meter_key TEXT NOT NULL, dormitory TEXT NOT NULL, area TEXT NOT NULL,
                        area_name TEXT NOT NULL, quantity TEXT NOT NULL, observed_at REAL NOT NULL,
                        requested_at REAL NOT NULL, cached INTEGER NOT NULL,
                        UNIQUE(user_id, event_key)
                    );
                    CREATE INDEX IF NOT EXISTS electricity_by_user_meter
                        ON electricity_queries(user_id, meter_key, observed_at);
                """)
            else:
                db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except (sqlite3.Error, OSError):
            raise UserDataError("用户记忆或查询台账暂时不可用，请稍后重试。") from None
        finally:
            if db is not None:
                db.close()

    def profile(self, context: str) -> dict:
        identity = self.identity(context)
        if identity is None or not self.path.exists():
            return {}
        with self.connect() as db:
            row = db.execute(
                "SELECT nickname, dormitory, area FROM profiles WHERE user_id=?", (identity,)
            ).fetchone()
        return dict(row) if row else {}

    def save_profile(self, context: str, **updates) -> dict:
        identity = self.identity(context)
        if identity is None:
            raise UserDataError("缺少稳定的发送者标识，暂时不能保存记忆。")
        if set(updates) - {"nickname", "dormitory", "area"}:
            raise UserDataError("不支持保存这个资料字段。")
        if any(
            not isinstance(v, str) or len(v) > 64 or any(ord(c) < 32 for c in v)
            for v in updates.values()
        ):
            raise UserDataError("资料不能包含换行或超长内容。")
        with self.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO profiles(user_id, updated_at) VALUES (?, ?)",
                (identity, self.clock()),
            )
            for key, value in updates.items():
                db.execute(
                    f"UPDATE profiles SET {key}=?, updated_at=? WHERE user_id=?",
                    (value, self.clock(), identity),
                )
        return self.profile(context)

    def forget(self, context: str, *, history_only=False):
        identity = self.identity(context)
        if identity is None:
            raise UserDataError("缺少稳定的发送者标识，暂时不能处理记忆。")
        if not self.path.exists():
            return
        with self.connect(write=True) as db:
            db.execute("DELETE FROM electricity_queries WHERE user_id=?", (identity,))
            if not history_only:
                db.execute("DELETE FROM profiles WHERE user_id=?", (identity,))

    def add_reading(self, context: str, reading: dict, event_key: str, retention_days: int):
        identity = self.identity(context)
        if identity is None:
            raise UserDataError("缺少稳定的发送者标识，本次读数未保存到个人台账。")
        now = self.clock()
        stamp = reading["observed_at"]
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or not 0 < stamp <= now + 5
        ):
            raise UserDataError("读数时间异常，本次查询未保存到历史。")
        try:
            raw = reading["quantity"]
            if not isinstance(raw, str) or len(raw) > 32:
                raise ValueError
            amount = Decimal(raw)
            if (
                not amount.is_finite()
                or not 0 <= amount <= 10000000
                or abs(amount.as_tuple().exponent) > 32
            ):
                raise ValueError
        except (ValueError, InvalidOperation):
            raise UserDataError("读数电量异常，本次查询未保存到历史。") from None
        with self.connect(write=True) as db:
            db.execute(
                """INSERT INTO electricity_queries
                   (user_id,event_key,meter_key,dormitory,area,area_name,quantity,observed_at,requested_at,cached)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,event_key) DO UPDATE SET
                   quantity=excluded.quantity, observed_at=excluded.observed_at,
                   requested_at=excluded.requested_at, cached=excluded.cached""",
                (
                    identity,
                    event_key,
                    reading["meter_key"],
                    reading["dormitory"],
                    reading["area"],
                    reading["area_name"],
                    reading["quantity"],
                    stamp,
                    now,
                    int(reading["cached"]),
                ),
            )
            db.execute(
                "DELETE FROM electricity_queries WHERE requested_at < ?",
                (now - retention_days * 86400,),
            )
            db.execute(
                "DELETE FROM electricity_queries WHERE user_id=? AND id NOT IN "
                "(SELECT id FROM electricity_queries WHERE user_id=? ORDER BY id DESC LIMIT 20000)",
                (identity, identity),
            )

    def readings(
        self, context: str, days: int, *, meter_key: str = "", limit: int = 20000
    ) -> list[dict]:
        identity = self.identity(context)
        if identity is None:
            raise UserDataError("缺少稳定的发送者标识，暂时不能读取个人历史。")
        if not self.path.exists():
            return []
        now = self.clock()
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM electricity_queries WHERE user_id=? AND observed_at BETWEEN ? AND ? "
                "AND (?='' OR meter_key=?) ORDER BY observed_at DESC, id DESC LIMIT ?",
                (identity, now - days * 86400, now, meter_key, meter_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]
