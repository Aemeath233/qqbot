"""管理页的配置读写：字段白名单、密钥不回显、原子保存与版本检查。"""

import hashlib
import os
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key

from qqbot.config import ConfigurationError, Settings

DEFAULTS = {
    "QQ_APP_ID": "",
    "QQ_APP_SECRET": "",
    "LLM_ENABLED": "false",
    "LLM_BASE_URL": "https://api.openai.com/v1",
    "LLM_API_KEY": "",
    "LLM_MODEL": "",
    "LLM_TIMEOUT": "30",
    "ELECTRICITY_ENABLED": "false",
    "ELECTRICITY_SCHOOL_CODE": "1402",
    "ELECTRICITY_PAY_PROJECT": "953",
    "ELECTRICITY_ENDPOINT": Settings.electricity_endpoint,
    "ELECTRICITY_DEFAULT_AREA": "",
    "ELECTRICITY_MAP_PATH": "",
    "ELECTRICITY_TOKEN": "",
    "ELECTRICITY_COOKIE": "",
    "ELECTRICITY_TAPP_ID": "",
}
SECRET_FIELDS = {
    "QQ_APP_SECRET",
    "LLM_API_KEY",
    "ELECTRICITY_TOKEN",
    "ELECTRICITY_COOKIE",
    "ELECTRICITY_TAPP_ID",
}
BOOL_FIELDS = {"LLM_ENABLED", "ELECTRICITY_ENABLED"}


class ConfigConflict(Exception):
    pass


class ConfigStore:
    def __init__(self, path: Path, *, environ=None):
        self.path = path
        self.environ = os.environ if environ is None else environ

    def revision(self) -> str:
        raw = self.path.read_bytes() if self.path.exists() else b""
        return hashlib.sha256(raw).hexdigest()

    def values(self) -> dict[str, str]:
        saved = (
            {
                key: value
                for key, value in dotenv_values(
                    self.path, encoding="utf-8-sig", interpolate=False
                ).items()
                if value is not None
            }
            if self.path.exists()
            else {}
        )
        return {**DEFAULTS, **saved, **self.environ}

    def public(self) -> dict:
        values = self.values()
        return {
            "revision": self.revision(),
            "values": {
                key: values[key].strip().lower() if key in BOOL_FIELDS else values[key]
                for key in DEFAULTS
                if key not in SECRET_FIELDS
            },
            "secrets": {key: bool(values[key]) for key in SECRET_FIELDS},
            "locked_fields": [key for key in DEFAULTS if key in self.environ],
        }

    def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"revision", "values", "clear_secrets"}:
            raise ConfigurationError("配置请求格式错误。")
        if payload.get("revision") != self.revision():
            raise ConfigConflict("配置已被其他操作修改，请刷新页面后重试。")
        updates, clear = payload.get("values", {}), payload.get("clear_secrets", [])
        if (
            not isinstance(updates, dict)
            or set(updates) - DEFAULTS.keys()
            or not isinstance(clear, list)
            or any(not isinstance(key, str) or key not in SECRET_FIELDS for key in clear)
        ):
            raise ConfigurationError("只允许修改页面提供的配置字段。")
        normalized = {}
        for key, value in updates.items():
            if key in self.environ:
                raise ConfigurationError(f"{key} 由进程环境提供，请在部署环境中修改。")
            if key in BOOL_FIELDS and isinstance(value, bool):
                value = str(value).lower()
            if not isinstance(value, str) or len(value) > 16384 or "\n" in value or "\r" in value:
                raise ConfigurationError(f"{key} 不能包含换行或超长内容。")
            value = value.strip()
            if key in SECRET_FIELDS and not value:
                continue  # 密钥输入框留空时保持原值。
            normalized[key] = value
        for key in clear:
            if key in self.environ or normalized.get(key):
                raise ConfigurationError("不能同时清除和填写密钥，或清除进程环境提供的密钥。")
            normalized[key] = ""
        Settings.from_values({**self.values(), **normalized}, require_qq=False)
        if self.path.is_symlink():
            raise ConfigurationError("当前 .env 是符号链接，请直接在服务器维护该文件。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = self.path.read_text(encoding="utf-8-sig") if self.path.exists() else ""
        descriptor, name = tempfile.mkstemp(prefix=".env.admin-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(raw)
            for key, value in normalized.items():
                set_key(temporary, key, value, quote_mode="always", encoding="utf-8")
            if payload["revision"] != self.revision():
                raise ConfigConflict("配置已被其他操作修改，请刷新页面后重试。")
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.public()
