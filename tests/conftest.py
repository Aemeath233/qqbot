import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from qqbot.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        "test-app",
        "test-secret",
        db_path=tmp_path / "inbox.sqlite3",
        skills_dir=tmp_path / "skills",
    )


def event_payload(*, group=False, full=False, content="/ping", message_id="msg-1"):
    author = {"bot": False}
    author["member_openid" if group else "user_openid"] = "sender-1"
    data = {"id": message_id, "author": author, "content": content}
    if group:
        data["group_openid"] = "group-1"
    event = (
        "GROUP_MESSAGE_CREATE"
        if full
        else ("GROUP_AT_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE")
    )
    return {"op": 0, "t": event, "id": "delivery-1", "d": data}


def signed_payload(settings, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stamp = str(int(time.time()))
    secret = settings.app_secret.encode()
    key = Ed25519PrivateKey.from_private_bytes((secret * 32)[:32])
    headers = {
        "X-Bot-Appid": settings.app_id,
        "X-Signature-Timestamp": stamp,
        "X-Signature-Ed25519": key.sign(stamp.encode() + body).hex(),
        "Content-Type": "application/json",
    }
    return body, headers
