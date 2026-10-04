"""将群消息和单聊消息统一成一条可回复的消息。"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

EVENTS = {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
LEGACY_MENTION = re.compile(r"^<@!?\d+>\s*")


@dataclass(frozen=True)
class Message:
    kind: Literal["users", "groups"]
    target_id: str
    message_id: str
    content: str
    expires_at: float
    full_group: bool = False
    sender_id: str = ""
    reference: Literal["msg_id", "event_id"] = "msg_id"
    interaction_id: str = ""

    @property
    def conversation_key(self) -> str:
        # 群内按发送者隔离；缺少作者标识时只允许本条消息的上下文。
        if self.kind == "groups" and not self.sender_id:
            return json.dumps(
                [self.kind, self.target_id, "", self.message_id], separators=(",", ":")
            )
        sender = self.sender_id or (self.target_id if self.kind == "users" else self.message_id)
        return json.dumps([self.kind, self.target_id, sender], separators=(",", ":"))

    @property
    def key(self) -> str:
        # 本项目对每条原始消息只回复一次；两个群事件也共享同一个去重键。
        message_id = (
            "interaction:" + (self.interaction_id or self.message_id)
            if self.reference == "event_id"
            else self.message_id
        )
        return json.dumps([self.kind, self.target_id, message_id], separators=(",", ":"))

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, accept_full_group: bool = False):
        event = payload.get("t")
        if event not in EVENTS or (event == "GROUP_MESSAGE_CREATE" and not accept_full_group):
            return None
        data = payload.get("d")
        if not isinstance(data, dict):
            raise ValueError("消息事件 d 必须为对象")
        author = data.get("author")
        if not isinstance(author, dict):
            raise ValueError("消息事件缺少 author")
        if author.get("bot"):
            return None
        message_id = data.get("id")
        kind = "users" if event == "C2C_MESSAGE_CREATE" else "groups"
        target_id = (
            (author.get("user_openid") or author.get("id"))
            if kind == "users"
            else data.get("group_openid")
        )
        sender_id = author.get("user_openid" if kind == "users" else "member_openid") or author.get(
            "id", ""
        )
        if not isinstance(sender_id, str) or len(sender_id) > 512:
            sender_id = ""
        if not all(isinstance(v, str) and 0 < len(v) <= 512 for v in (message_id, target_id)):
            raise ValueError("消息事件缺少有效的消息 ID 或 OpenID")
        content = data.get("content", "")
        if not isinstance(content, str):
            raise ValueError("content 必须为字符串")
        if data.get("message_type", 0) != 0 or not content.strip():
            return None
        content = content.strip()
        if event == "GROUP_AT_MESSAGE_CREATE":
            content = LEGACY_MENTION.sub("", content, count=1).strip()
        window = 3600 if kind == "users" else 300
        now = time.time()
        expires_at = now + window
        if isinstance(data.get("timestamp"), str):
            try:
                sent_at = datetime.fromisoformat(data["timestamp"])
                if sent_at.tzinfo is not None:
                    expires_at = min(expires_at, sent_at.timestamp() + window)
            except ValueError:
                pass
        return cls(
            kind,
            target_id,
            message_id,
            content,
            expires_at,
            event == "GROUP_MESSAGE_CREATE",
            sender_id,
        )
