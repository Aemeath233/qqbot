"""QQ 按钮点击的受限解析；频道及非消息按钮事件不处理。"""

import time
from dataclasses import dataclass
from datetime import datetime

from qqbot.messages import Message


@dataclass(frozen=True)
class ButtonClick:
    ack_id: str
    message: Message
    data: str
    button_id: str = ""
    occurred_at: float | None = None

    @classmethod
    def parse(cls, payload, app_id):
        event = payload.get("d")
        if not isinstance(event, dict):
            raise ValueError("互动事件缺少数据")
        if event.get("application_id", app_id) != app_id:
            raise ValueError("互动事件 AppID 不匹配")
        data = event.get("data")
        if not isinstance(data, dict) or event.get("type", data.get("type")) != 11:
            return None
        resolved = data.get("resolved")
        if not isinstance(resolved, dict):
            raise ValueError("互动事件缺少按钮数据")
        scene = event.get("scene") or {1: "group", 2: "c2c"}.get(event.get("chat_type"))
        if (
            event.get("chat_type") is not None
            and {"group": 1, "c2c": 2}.get(scene) != event["chat_type"]
        ):
            raise ValueError("互动场景与聊天类型不一致")
        if scene == "group":
            kind, target, sender = (
                "groups",
                event.get("group_openid"),
                event.get("group_member_openid"),
            )
        elif scene == "c2c":
            kind = "users"
            target = sender = event.get("user_openid")
        else:
            return None
        ack_id = event.get("id")
        event_id = payload.get("id") or ack_id
        button_data = resolved.get("button_data")
        button_id = resolved.get("button_id", "")
        if not all(
            isinstance(item, str) and 0 < len(item) <= 512
            for item in (ack_id, event_id, target, sender)
        ):
            raise ValueError("互动事件缺少有效标识")
        if not isinstance(button_data, str) or len(button_data) > 128:
            raise ValueError("按钮数据格式异常")
        if not isinstance(button_id, str) or len(button_id) > 128:
            raise ValueError("按钮标识格式异常")
        expires_at = time.time() + 300
        occurred_at = None
        if isinstance(event.get("timestamp"), str):
            try:
                sent_at = datetime.fromisoformat(event["timestamp"])
                if sent_at.tzinfo is not None:
                    occurred_at = sent_at.timestamp()
                    expires_at = min(expires_at, occurred_at + 300)
            except ValueError:
                pass
        message = Message(
            kind,
            target,
            event_id,
            "",
            expires_at,
            sender_id=sender,
            reference="event_id",
            interaction_id=ack_id,
        )
        return cls(ack_id, message, button_data, button_id, occurred_at)
