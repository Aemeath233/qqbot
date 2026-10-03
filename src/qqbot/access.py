"""按可信聊天身份控制工具调用，策略文件由管理页修改并实时读取。"""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from qqbot.user_store import user_id

TOOL = re.compile(r"(?:[A-Za-z0-9_-]{1,64}|mcp__[a-z0-9-]{1,24}__\*|\*)")
UID = re.compile(r"[a-f0-9]{64}")


class AccessError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    uid: str = ""
    group: str = ""
    host: str = "qq"

    @classmethod
    def from_context(cls, context, namespace="", host="qq"):
        uid = user_id(context, namespace) or ""
        try:
            parts = json.loads(context)
            group = (
                parts[1]
                if isinstance(parts, list)
                and len(parts) in {3, 4}
                and parts[0] == "groups"
                and isinstance(parts[1], str)
                else ""
            )
        except (ValueError, TypeError):
            group = ""
        return cls(uid, group, host)


def validate_rules(rules):
    if not isinstance(rules, dict) or len(rules) > 100 or len(json.dumps(rules)) > 32768:
        raise AccessError("最多配置100项工具规则，总长度不能超过32 KiB。")
    for name, rule in rules.items():
        if (
            not isinstance(name, str)
            or not TOOL.fullmatch(name)
            or not isinstance(rule, dict)
            or set(rule) - {"mode", "users", "groups"}
        ):
            raise AccessError("工具规则名称或字段无效。")
        if rule.get("mode") not in {"all", "admin-only", "allowlist", "disabled"}:
            raise AccessError("规则模式应为all、admin-only、allowlist或disabled。")
        users, groups = rule.get("users", []), rule.get("groups", [])
        if (
            not isinstance(users, list)
            or len(users) > 100
            or any(not isinstance(x, str) or not UID.fullmatch(x) for x in users)
        ):
            raise AccessError("用户白名单需填写 /我的标识 返回的64位用户标识。")
        if (
            not isinstance(groups, list)
            or len(groups) > 100
            or any(
                not isinstance(x, str)
                or not x.strip()
                or len(x) > 512
                or any(ord(c) < 32 for c in x)
                for x in groups
            )
        ):
            raise AccessError("群白名单需填写有效的群OpenID。")
    return rules


class AccessStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self):
        try:
            raw = self.path.read_bytes() if self.path.exists() else b""
            data = json.loads(raw) if raw else {"version": 1, "rules": {}}
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or set(data) != {"version", "rules"}
            ):
                raise ValueError
            return validate_rules(data["rules"]), hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, RecursionError):
            raise AccessError("工具权限配置暂时无法读取，工具调用已停止。") from None

    def save(self, rules, revision):
        rules = validate_rules(rules)
        if revision != self.read()[1]:
            raise AccessError("权限配置已变化，请重新读取后再保存。")
        if self.path.is_symlink():
            raise AccessError("权限配置不能是符号链接。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".access-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "rules": rules}, stream, ensure_ascii=False)
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def allowed(self, name, principal: Principal):
        try:
            rules, _ = self.read()
        except AccessError:
            return False
        wildcard = name.rsplit("__", 1)[0] + "__*" if name.startswith("mcp__") else ""
        rule = rules.get(name, rules.get(wildcard, rules.get("*", {"mode": "all"})))
        mode = rule["mode"]
        if mode == "all":
            return True
        if mode == "disabled":
            return False
        if principal.host in {"admin", "console"}:
            return True
        if mode == "admin-only":
            return False
        return bool(principal.uid and principal.uid in rule.get("users", [])) or bool(
            principal.group and principal.group in rule.get("groups", [])
        )
