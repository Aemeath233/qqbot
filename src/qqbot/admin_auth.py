"""单管理员密码、短期内存会话和登录尝试限制。"""

import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from qqbot.config import ConfigurationError


class PasswordStore:
    def __init__(self, path: Path):
        self.path = path

    def set(self, password: str):
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            raise ConfigurationError("管理密码长度应为 8～256 个字符。")
        salt = secrets.token_bytes(16)
        digest = self._derive(password, salt)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".password-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"algorithm": "scrypt-v1", "salt": salt.hex(), "hash": digest.hex()})
                )
            temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def verify(self, password: str) -> bool:
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("algorithm") != "scrypt-v1":
            raise ConfigurationError("管理密码文件格式错误，请通过终端重新设置密码。")
        salt, expected = bytes.fromhex(data["salt"]), bytes.fromhex(data["hash"])
        if len(salt) != 16 or len(expected) != 32:
            raise ConfigurationError("管理密码文件格式错误，请通过终端重新设置密码。")
        return secrets.compare_digest(self._derive(password, salt), expected)


class Sessions:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.items: dict[str, tuple[float, str, str]] = {}

    def issue(self, fingerprint: str) -> tuple[str, str]:
        self._purge()
        if len(self.items) >= 32:
            self.items.pop(next(iter(self.items)))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.items[token] = (self.clock() + 8 * 3600, csrf, fingerprint)
        return token, csrf

    def get(self, token: str, fingerprint: str) -> str | None:
        self._purge()
        value = self.items.get(token)
        if value is not None and secrets.compare_digest(value[2], fingerprint):
            return value[1]
        self.items.pop(token, None)
        return None

    def _purge(self):
        now = self.clock()
        self.items = {key: value for key, value in self.items.items() if value[0] > now}


class LoginLimit:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.attempts: list[float] = []

    def reserve(self) -> bool:
        now = self.clock()
        self.attempts = [stamp for stamp in self.attempts if now - stamp < 300]
        if len(self.attempts) >= 5:
            return False
        self.attempts.append(now)
        return True
