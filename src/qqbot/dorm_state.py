"""默认宿舍和短期按钮操作，均按平台用户标识隔离。"""

import json
import logging
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256

from qqbot.presentation import Reply, electricity_reply

logger = logging.getLogger(__name__)


def identity_tag(value):
    return sha256(str(value).encode()).hexdigest()[:10]


@dataclass(frozen=True)
class UserContext:
    kind: str
    target_id: str
    sender_id: str


class DormState:
    def __init__(self, db, app_id):
        self.db = db
        self.app_id = app_id
        db.execute("""CREATE TABLE IF NOT EXISTS dorm_bindings (
            subject TEXT PRIMARY KEY, dormitory TEXT NOT NULL, area TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS electricity_buttons (
            token TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
            target_id TEXT NOT NULL, action TEXT NOT NULL, dormitory TEXT NOT NULL,
            area TEXT NOT NULL, expires_at REAL NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS buttons_expiry ON electricity_buttons(expires_at)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(electricity_buttons)")}
        for name in ("button_id", "request_id"):
            if name not in columns:
                db.execute(
                    f"ALTER TABLE electricity_buttons ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        db.execute("""CREATE TABLE IF NOT EXISTS dorm_binding_requests (
            request_id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
            target_id TEXT NOT NULL, dormitory TEXT NOT NULL, area TEXT NOT NULL,
            expected_binding TEXT NOT NULL, expires_at REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL
        )""")
        # 私聊目标就是用户本人，可以安全迁移。旧群绑定缺少来源群，保留但不自动继承。
        for row in db.execute("SELECT * FROM dorm_bindings").fetchall():
            try:
                old = json.loads(row["subject"])
            except (ValueError, TypeError):
                continue
            if (
                isinstance(old, list)
                and len(old) == 3
                and old[:2] == [app_id, "users"]
                and isinstance(old[2], str)
                and old[2]
            ):
                subject = self.subject(UserContext("users", old[2], old[2]))
                db.execute(
                    "INSERT OR IGNORE INTO dorm_bindings VALUES (?,?,?,?)",
                    (subject, row["dormitory"], row["area"], row["created_at"]),
                )
        db.commit()

    def subject(self, context):
        if not context.sender_id:
            return ""
        # 群成员和单聊用户的 OpenID 不保证相同，不能猜测跨场景的账号对应关系。
        return json.dumps(
            [self.app_id, context.kind, context.target_id, context.sender_id], separators=(",", ":")
        )

    def binding(self, context):
        return self.db.execute(
            "SELECT dormitory,area FROM dorm_bindings WHERE subject=?", (self.subject(context),)
        ).fetchone()

    def binding_signature(self, context):
        current = self.binding(context)
        return json.dumps([current["dormitory"], current["area"]] if current else None)

    def latest_request(self, context):
        return self.db.execute(
            "SELECT * FROM dorm_binding_requests WHERE subject=? AND state='pending' "
            "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
            (self.subject(context), time.time()),
        ).fetchone()

    def request_binding(self, context, dormitory, area):
        subject = self.subject(context)
        if not subject:
            return Reply("没有取得你的用户标识，暂时不能绑定宿舍。请提供完整宿舍信息查询。")
        current = self.binding(context)
        where = "本群" if context.kind == "groups" else "当前私聊"
        if current and (current["dormitory"], current["area"]) == (dormitory, area):
            return Reply(f"{where}的默认宿舍已经是 {dormitory}，无需更换。直接说“查一下电费”即可。")
        if current:
            text = f"{where}当前默认宿舍：{current['dormitory']}。是否更换为 {dormitory}？"
        else:
            text = f"是否将 {dormitory} 设为你在{where}的默认宿舍？"
        text += "\n点击“是”确认，或点击“否”取消；也可以回复“确认更换”或“取消更换”。"
        request_id = secrets.token_urlsafe(18)
        with self.db:
            self.db.execute(
                "UPDATE dorm_binding_requests SET state='superseded' "
                "WHERE subject=? AND state='pending'",
                (subject,),
            )
            self.db.execute(
                "INSERT INTO dorm_binding_requests VALUES (?,?,?,?,?,?,?,?,'pending',?)",
                (
                    request_id,
                    subject,
                    context.kind,
                    context.target_id,
                    dormitory,
                    area,
                    self.binding_signature(context),
                    time.time() + 600,
                    time.time(),
                ),
            )
        keyboard = self.keyboard(
            context,
            dormitory,
            area,
            {"confirm_bind": "是", "cancel_bind": "否"},
            request_id=request_id,
        )
        return Reply(text, "# 默认宿舍确认\n\n" + text, keyboard)

    def finish_binding(self, context, request_id, *, confirm):
        if type(confirm) is not bool:
            raise ValueError("确认结果必须为明确的是或否")
        with self.db:
            request = self.db.execute(
                "SELECT * FROM dorm_binding_requests WHERE request_id=? AND subject=? "
                "AND kind=? AND target_id=? AND state='pending' AND expires_at>?",
                (request_id, self.subject(context), context.kind, context.target_id, time.time()),
            ).fetchone()
            if request is None:
                return Reply("这次确认已处理、取消或失效。若仍需更换，请重新发起绑定。")
            if not confirm:
                self.db.execute(
                    "UPDATE dorm_binding_requests SET state='cancelled' WHERE request_id=?",
                    (request_id,),
                )
                return Reply("已取消，本次没有修改默认宿舍。")
            if self.binding_signature(context) != request["expected_binding"]:
                self.db.execute(
                    "UPDATE dorm_binding_requests SET state='stale' WHERE request_id=?",
                    (request_id,),
                )
                return Reply("默认宿舍已发生变化，本次没有覆盖。请重新发起更换并确认。")
            self.db.execute(
                "INSERT INTO dorm_bindings VALUES (?,?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                "dormitory=excluded.dormitory,area=excluded.area,created_at=excluded.created_at",
                (self.subject(context), request["dormitory"], request["area"], time.time()),
            )
            self.db.execute(
                "UPDATE dorm_binding_requests SET state='confirmed' WHERE request_id=?",
                (request_id,),
            )
        where = "本群" if context.kind == "groups" else "当前私聊"
        text = f"{where}的默认宿舍已设为 {request['dormitory']}。以后直接说“查一下电费”即可。"
        text += "\n需要更换时，可以说“更换绑定宿舍为33楼2004室”，或查询其他宿舍后点击绑定。"
        logger.info(
            "用户确认保存默认宿舍：群=%s，用户=%s，请求=%s",
            identity_tag(context.target_id),
            identity_tag(context.sender_id),
            identity_tag(request_id),
        )
        return Reply(text, "# 默认宿舍\n\n" + text)

    def card(self, result, context):
        reply = electricity_reply(result)
        if not result.get("ok") or not self.subject(context):
            return reply
        keyboard = self.keyboard(
            context,
            result["dormitory"],
            result.get("area", ""),
            {"query": "再次查询", "bind": "绑定此宿舍", "help": "使用帮助"},
        )
        return Reply(reply.text, reply.markdown, keyboard)

    def keyboard(self, context, dormitory, area, labels, *, request_id=""):
        buttons = []
        with self.db:
            self.db.execute("DELETE FROM electricity_buttons WHERE expires_at<=?", (time.time(),))
            for action, label in labels.items():
                token = secrets.token_urlsafe(18)
                button_id = f"{action}:{token}"
                self.db.execute(
                    "INSERT INTO electricity_buttons "
                    "(token,subject,kind,target_id,action,dormitory,area,expires_at,"
                    "button_id,request_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        token,
                        self.subject(context),
                        context.kind,
                        context.target_id,
                        action,
                        dormitory,
                        area,
                        time.time() + (600 if request_id else 86400),
                        button_id,
                        request_id,
                    ),
                )
                click = {
                    "type": 1,
                    "data": f"electricity:v2:{action}:{token}",
                    "permission": {"type": 0, "specify_user_ids": [context.sender_id]},
                }
                button = {
                    "id": button_id,
                    "render_data": {"label": label, "style": 1},
                    "action": click,
                }
                if request_id:
                    button["group_id"] = request_id
                buttons.append(button)
        return {"content": {"rows": [{"buttons": buttons}]}}

    def action(self, data, context, *, button_id=""):
        if not isinstance(data, str) or not data.startswith("electricity:v2:") or len(data) > 128:
            return None
        parts = data.split(":")
        if len(parts) != 4:
            return None
        action, token = parts[2:]
        record = self.db.execute(
            "SELECT * FROM electricity_buttons WHERE token=? AND expires_at>?",
            (token, time.time()),
        ).fetchone()
        if record is None:
            return None
        if (
            not record["button_id"]
            or record["action"] != action
            or record["button_id"] != f"{action}:{token}"
            or (button_id and button_id != record["button_id"])
        ):
            raise PermissionError("按钮动作和标识不一致")
        if (
            record["subject"] != self.subject(context)
            or record["kind"] != context.kind
            or record["target_id"] != context.target_id
        ):
            logger.warning(
                "拒绝跨会话或跨用户按钮：来源群=%s，事件群=%s，动作=%s",
                identity_tag(record["target_id"]),
                identity_tag(context.target_id),
                record["action"],
            )
            raise PermissionError("按钮仅限原查询用户在原会话中使用")
        return record
