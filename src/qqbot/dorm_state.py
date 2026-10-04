"""默认宿舍和短期按钮操作，均按平台用户标识隔离。"""

import json
import secrets
import time
from dataclasses import dataclass

from qqbot.presentation import Reply, electricity_reply


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
        db.commit()

    def subject(self, context):
        if not context.sender_id:
            return ""
        # 群成员和单聊用户的 OpenID 不保证相同，不能猜测跨场景的账号对应关系。
        return json.dumps([self.app_id, context.kind, context.sender_id], separators=(",", ":"))

    def binding(self, context):
        return self.db.execute(
            "SELECT dormitory,area FROM dorm_bindings WHERE subject=?", (self.subject(context),)
        ).fetchone()

    def bind_once(self, context, dormitory, area):
        subject = self.subject(context)
        if not subject:
            return Reply("没有取得你的用户标识，暂时不能绑定宿舍。请提供完整宿舍信息查询。")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO dorm_bindings VALUES (?,?,?,?)",
                (subject, dormitory, area, time.time()),
            )
        binding = self.binding(context)
        current = binding["dormitory"]
        if current != dormitory or binding["area"] != area:
            text = (
                f"你已绑定默认宿舍 {current}。每个账号只能绑定一个默认宿舍，绑定后不能更换。"
                "仍可在查询时明确指定其他宿舍。"
            )
        else:
            text = (
                f"默认宿舍已绑定为 {current}。以后直接说“查一下电费”或“还有多少电”即可。"
                "每个账号只能绑定一个默认宿舍，绑定后不能更换。"
            )
        return Reply(text, "# 默认宿舍\n\n" + text)

    def card(self, result, context):
        reply = electricity_reply(result)
        if not result.get("ok") or not self.subject(context):
            return reply
        labels = {"query": "再次查询", "bind": "绑定此宿舍", "help": "使用帮助"}
        buttons = []
        with self.db:
            self.db.execute("DELETE FROM electricity_buttons WHERE expires_at<=?", (time.time(),))
            for action, label in labels.items():
                token = secrets.token_urlsafe(18)
                self.db.execute(
                    "INSERT INTO electricity_buttons VALUES (?,?,?,?,?,?,?,?)",
                    (
                        token,
                        self.subject(context),
                        context.kind,
                        context.target_id,
                        action,
                        result["dormitory"],
                        result.get("area", ""),
                        time.time() + 86400,
                    ),
                )
                click = {
                    "type": 1,
                    "data": "electricity:" + token,
                    "permission": {"type": 0, "specify_user_ids": [context.sender_id]},
                }
                if action == "bind":
                    click["modal"] = {
                        "content": "每个账号仅能绑定一个默认宿舍，绑定后不可更换。是否确认？",
                        "confirm_text": "绑定",
                        "cancel_text": "取消",
                    }
                buttons.append(
                    {
                        "id": action,
                        "render_data": {"label": label, "style": 1},
                        "action": click,
                    }
                )
        keyboard = {"content": {"rows": [{"buttons": buttons}]}}
        return Reply(reply.text, reply.markdown, keyboard)

    def action(self, data, context):
        if not isinstance(data, str) or not data.startswith("electricity:") or len(data) > 128:
            return None
        record = self.db.execute(
            "SELECT * FROM electricity_buttons WHERE token=? AND expires_at>?",
            (data.split(":", 1)[1], time.time()),
        ).fetchone()
        if record is None:
            return None
        if (
            record["subject"] != self.subject(context)
            or record["kind"] != context.kind
            or record["target_id"] != context.target_id
        ):
            raise PermissionError("按钮仅限原查询用户在原会话中使用")
        return record
