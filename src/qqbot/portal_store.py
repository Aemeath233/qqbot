"""用户网页草稿、发布状态和可撤销的访问能力；不保存明文访问令牌。"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ID = re.compile(r"[A-Za-z0-9_-]{16,40}")
OWNER = re.compile(r"[a-f0-9]{64}")


class PortalError(ValueError):
    pass


class PortalStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = path, clock

    @contextmanager
    def connect(self, *, write=False):
        db = None
        try:
            if write:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=2)
                self.path.chmod(0o600)
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS pages (
                        id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                        content TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'draft',
                        created_at REAL NOT NULL, published_at REAL,
                        request_key TEXT NOT NULL, UNIQUE(owner, request_key)
                    );
                    CREATE TABLE IF NOT EXISTS links (
                        id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                        target TEXT NOT NULL, options TEXT NOT NULL, token_hash TEXT NOT NULL,
                        expires_at REAL NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS pages_owner ON pages(owner);
                    CREATE INDEX IF NOT EXISTS links_owner ON links(owner);
                """)
            else:
                db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except (sqlite3.Error, OSError):
            raise PortalError("网页数据暂时不可用，请稍后再试。") from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def owner(owner):
        if not isinstance(owner, str) or not OWNER.fullmatch(owner):
            raise PortalError("缺少可靠的用户标识，不能创建个人网页。")

    def draft(self, owner, title, content, request_key):
        self.owner(owner)
        if (
            not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 80
            or any(ord(c) < 32 for c in title)
        ):
            raise PortalError("网页标题应为1～80字。")
        if not isinstance(content, str) or not 1 <= len(content) <= 18000 or "\0" in content:
            raise PortalError("单页HTML最多18000字，请生成自包含的简单网页。")
        request_key = request_key or secrets.token_urlsafe(16)
        with self.connect(write=True) as db:
            existing = db.execute(
                "SELECT id FROM pages WHERE owner=? AND request_key=?", (owner, request_key)
            ).fetchone()
            if existing:
                return existing[0]
            if db.execute("SELECT COUNT(*) FROM pages WHERE owner=?", (owner,)).fetchone()[0] >= 10:
                raise PortalError("每个聊天身份最多保留10个网页，请先删除不需要的页面。")
            if db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] >= 500:
                raise PortalError("本站网页数量已达上限，请联系管理员。")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM pages WHERE owner=? AND created_at>?",
                    (owner, self.clock() - 86400),
                ).fetchone()[0]
                >= 5
            ):
                raise PortalError("每天最多创建5个网页。")
            identifier = secrets.token_urlsafe(12)
            db.execute(
                "INSERT INTO pages(id,owner,title,content,created_at,request_key) "
                "VALUES (?,?,?,?,?,?)",
                (identifier, owner, title.strip(), content, self.clock(), request_key),
            )
        return identifier

    def page(self, identifier, *, owner=None, public=False):
        if (
            not isinstance(identifier, str)
            or not ID.fullmatch(identifier)
            or not self.path.exists()
        ):
            raise PortalError("网页不存在或已撤回。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM pages WHERE id=?", (identifier,)).fetchone()
        if (
            not row
            or (owner is not None and row["owner"] != owner)
            or (public and row["visibility"] != "public")
        ):
            raise PortalError("网页不存在或已撤回。")
        return dict(row)

    def publish(self, owner, identifier, visibility):
        self.page(identifier, owner=owner)
        if visibility not in {"public", "unlisted"}:
            raise PortalError("发布模式无效。")
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE pages SET visibility=?,published_at=? WHERE id=? AND owner=?",
                (visibility, self.clock(), identifier, owner),
            )

    def retract(self, owner, identifier, *, delete=False):
        self.page(identifier, owner=owner)
        with self.connect(write=True) as db:
            db.execute("DELETE FROM links WHERE target=? AND kind='page'", (identifier,))
            if delete:
                db.execute("DELETE FROM pages WHERE id=? AND owner=?", (identifier, owner))
            else:
                db.execute(
                    "UPDATE pages SET visibility='draft',published_at=NULL WHERE id=? AND owner=?",
                    (identifier, owner),
                )

    def list_pages(self, owner=None):
        if not self.path.exists():
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,title,visibility,created_at,published_at FROM pages "
                "WHERE (? IS NULL OR owner=?) ORDER BY created_at DESC LIMIT 500",
                (owner, owner),
            ).fetchall()
        return [dict(row) for row in rows]

    def link(self, owner, kind, target, options=None, *, ttl=3600):
        self.owner(owner)
        if kind not in {"curve", "page"}:
            raise PortalError("访问链接类型无效。")
        if kind == "page":
            self.page(target, owner=owner)
        identifier, token = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
        with self.connect(write=True) as db:
            db.execute("DELETE FROM links WHERE expires_at<=?", (self.clock(),))
            if db.execute("SELECT COUNT(*) FROM links WHERE owner=?", (owner,)).fetchone()[0] >= 30:
                raise PortalError("有效访问链接已达上限，请先撤销旧链接。")
            db.execute(
                "INSERT INTO links VALUES (?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    owner,
                    kind,
                    target,
                    json.dumps(options or {}),
                    hashlib.sha256(token.encode()).hexdigest(),
                    self.clock() + ttl,
                    self.clock(),
                ),
            )
        return identifier, token

    def authorize(self, identifier, token, kind):
        if (
            not isinstance(identifier, str)
            or not ID.fullmatch(identifier)
            or not isinstance(token, str)
            or not 40 <= len(token) <= 64
            or not self.path.exists()
        ):
            raise PortalError("访问链接无效、已过期或已撤销。")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM links WHERE id=? AND kind=?", (identifier, kind)
            ).fetchone()
        if (
            not row
            or row["expires_at"] <= self.clock()
            or not hmac.compare_digest(
                row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
            )
        ):
            raise PortalError("访问链接无效、已过期或已撤销。")
        return {**dict(row), "options": json.loads(row["options"])}

    def revoke(self, owner, *, kind=None):
        self.owner(owner)
        if not self.path.exists():
            return
        with self.connect(write=True) as db:
            db.execute(
                "DELETE FROM links WHERE owner=? AND (? IS NULL OR kind=?)", (owner, kind, kind)
            )

    def forget(self, owner):
        self.owner(owner)
        if not self.path.exists():
            return
        with self.connect(write=True) as db:
            db.execute("DELETE FROM links WHERE owner=?", (owner,))
            db.execute("DELETE FROM pages WHERE owner=?", (owner,))
