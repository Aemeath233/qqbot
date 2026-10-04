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
    def __init__(self, db, app_id, *, audit=None):
        self.db = db
        self.app_id = app_id
        self.audit = audit
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
        # 用户默认宿舍不包含群；旧版群独立记录以最近一次保存为准，迁移只执行一次。
        migrations = []
        for row in db.execute(
            "SELECT * FROM dorm_bindings ORDER BY created_at DESC,subject ASC"
        ).fetchall():
            try:
                old = json.loads(row["subject"])
            except (ValueError, TypeError):
                continue
            if (
                isinstance(old, list)
                and len(old) in {3, 4}
                and old[0] == app_id
                and isinstance(old[1], str)
                and old[1] in {"groups", "users"}
                and all(isinstance(value, str) and value for value in old[2:])
                and (len(old) == 3 or old[1] == "groups" or old[2] == old[3])
            ):
                context = UserContext(old[1], old[2] if len(old) == 4 else "", old[-1])
                subject = self.subject(context)
                added = db.execute(
                    "INSERT OR IGNORE INTO dorm_bindings VALUES (?,?,?,?)",
                    (subject, row["dormitory"], row["area"], row["created_at"]),
                )
                if added.rowcount and audit:
                    migrations.append(
                        audit.record(
                            "binding_migrated",
                            context,
                            commit=False,
                            dormitory=row["dormitory"],
                            policy="latest_saved_user_binding",
                        )
                    )
        db.commit()
        for entry in migrations:
            audit.publish(entry)

    def subject(self, context):
        if not context.sender_id:
            return ""
        # 精确相同的 OpenID 归同一用户；不同 OpenID 不靠昵称或其他信息猜测合并。
        return json.dumps([self.app_id, context.sender_id], separators=(",", ":"))

    def binding(self, context):
        return self.db.execute(
            "SELECT dormitory,area FROM dorm_bindings WHERE subject=?", (self.subject(context),)
        ).fetchone()

    def binding_signature(self, context):
        current = self.binding(context)
        return json.dumps([current["dormitory"], current["area"]] if current else None)

    def latest_request(self, context):
        return self.db.execute(
            "SELECT * FROM dorm_binding_requests WHERE subject=? AND kind=? AND target_id=? "
            "AND state='pending' "
            "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
            (self.subject(context), context.kind, context.target_id, time.time()),
        ).fetchone()

    def request_binding(self, context, dormitory, area):
        subject = self.subject(context)
        if not subject:
            return Reply("没有取得你的用户标识，暂时不能绑定宿舍。请提供完整宿舍信息查询。")
        current = self.binding(context)
        if current and (current["dormitory"], current["area"]) == (dormitory, area):
            if self.audit:
                self.audit.record("binding_unchanged", context, dormitory=dormitory)
            return Reply(f"你的默认宿舍已经是 {dormitory}，无需更换。直接说“查一下电费”即可。")
        if current:
            text = f"你当前的默认宿舍：{current['dormitory']}。是否更换为 {dormitory}？"
        else:
            text = f"是否将 {dormitory} 设为你的默认宿舍？"
        text += "\n确认后，在其他群也可以直接查询你的默认宿舍。"
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
            entry = (
                self.audit.record(
                    "binding_requested",
                    context,
                    commit=False,
                    request=identity_tag(request_id),
                    previous=current["dormitory"] if current else "",
                    dormitory=dormitory,
                )
                if self.audit
                else None
            )
        if entry:
            self.audit.publish(entry)
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
            self.db.execute("BEGIN IMMEDIATE")
            request = self.db.execute(
                "SELECT * FROM dorm_binding_requests WHERE request_id=? AND subject=? "
                "AND kind=? AND target_id=? AND state='pending' AND expires_at>?",
                (request_id, self.subject(context), context.kind, context.target_id, time.time()),
            ).fetchone()
            if request is None:
                stage = "binding_rejected"
                details = {"reason": "expired_processed_or_foreign_request"}
                reply = Reply("这次确认已处理、取消或失效。若仍需更换，请重新发起绑定。")
            elif not confirm:
                self.db.execute(
                    "UPDATE dorm_binding_requests SET state='cancelled' WHERE request_id=?",
                    (request_id,),
                )
                stage = "binding_cancelled"
                details = {"dormitory": request["dormitory"]}
                reply = Reply("已取消，本次没有修改默认宿舍。")
            elif self.binding_signature(context) != request["expected_binding"]:
                self.db.execute(
                    "UPDATE dorm_binding_requests SET state='stale' WHERE request_id=?",
                    (request_id,),
                )
                stage = "binding_rejected"
                details = {"reason": "binding_changed_since_prompt"}
                reply = Reply("默认宿舍已发生变化，本次没有覆盖。请重新发起更换并确认。")
            else:
                previous = self.binding(context)
                self.db.execute(
                    "INSERT INTO dorm_bindings VALUES (?,?,?,?) ON CONFLICT(subject) DO UPDATE SET "
                    "dormitory=excluded.dormitory,area=excluded.area,created_at=excluded.created_at",
                    (self.subject(context), request["dormitory"], request["area"], time.time()),
                )
                self.db.execute(
                    "UPDATE dorm_binding_requests SET state='confirmed' WHERE request_id=?",
                    (request_id,),
                )
                stage = "binding_saved"
                details = {
                    "previous": previous["dormitory"] if previous else "",
                    "dormitory": request["dormitory"],
                }
                text = (
                    f"你的默认宿舍已设为 {request['dormitory']}。以后直接说“查一下电费”即可。"
                    "\n在其他群也可直接查询此宿舍；普通查询其他宿舍不会修改绑定。"
                    "\n需要更换时，可以说“更换绑定宿舍为33楼2004室”，"
                    "或查询其他宿舍后点击绑定。"
                )
                reply = Reply(text, "# 默认宿舍\n\n" + text)
            entry = (
                self.audit.record(
                    stage,
                    context,
                    commit=False,
                    request=identity_tag(request_id),
                    **details,
                )
                if self.audit
                else None
            )
        if entry:
            self.audit.publish(entry)
        return reply

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
            entry = (
                self.audit.record(
                    "buttons_created",
                    context,
                    commit=False,
                    dormitory=dormitory,
                    request=identity_tag(request_id) if request_id else "",
                    buttons=[
                        {
                            "action": button["id"].split(":", 1)[0],
                            "button": identity_tag(button["id"]),
                            "data": identity_tag(button["action"]["data"]),
                        }
                        for button in buttons
                    ],
                )
                if self.audit
                else None
            )
        if entry:
            self.audit.publish(entry)
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

    def button_evidence(self, data):
        """仅供审计，不授权执行；过期记录仍可用于对照 QQ 上报字段。"""
        parts = data.split(":")
        if len(parts) != 4 or parts[:2] != ["electricity", "v2"]:
            return {}
        record = self.db.execute(
            "SELECT * FROM electricity_buttons WHERE token=?", (parts[3],)
        ).fetchone()
        if record is None:
            return {}
        return {
            "stored_action": record["action"],
            "stored_button": identity_tag(record["button_id"]),
            "source_group": identity_tag(record["target_id"]),
            "source_user": identity_tag(json.loads(record["subject"])[-1]),
            "dormitory": record["dormitory"],
        }
