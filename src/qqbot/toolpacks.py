"""管理员安装的可执行工具包；安装和查看阶段不导入Python模块。"""

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from qqbot.skills import MAX_FILE, SkillError, read_archive, safe_path, valid_name

PACK_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[a-zA-Z0-9_,.-]+\])?(?:[<>=!~][A-Za-z0-9.*!+<>=~,.-]+)?"
)


class ToolPackError(ValueError):
    pass


@dataclass(frozen=True)
class ToolPack:
    name: str
    description: str
    entrypoint: str
    mode: str
    exports: tuple[str, ...]
    requirements: tuple[str, ...]
    env_keys: tuple[str, ...]


def parse_manifest(raw: bytes) -> ToolPack:
    try:
        if len(raw) > 16384:
            raise ValueError
        data = json.loads(raw.decode("utf-8-sig"))
        allowed = {
            "version",
            "name",
            "description",
            "entrypoint",
            "mode",
            "exports",
            "requirements",
            "env_keys",
        }
        if (
            not isinstance(data, dict)
            or set(data) - allowed
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise ValueError
        name, description = data["name"], data["description"]
        if not valid_name(name) or len(name) > 24 or not PACK_NAME.fullmatch(name):
            raise ValueError
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 512:
            raise ValueError
        entrypoint = safe_path(data.get("entrypoint", "tools.py")).as_posix()
        if not entrypoint.endswith(".py"):
            raise ValueError
        mode = data.get("mode", "functions")
        if mode not in {"functions", "mcp-stdio"}:
            raise ValueError
        exports = data.get("exports", [])
        if (
            not isinstance(exports, list)
            or len(exports) > 8
            or any(not isinstance(x, str) or not TOOL_NAME.fullmatch(x) for x in exports)
            or len(exports) != len(set(exports))
        ):
            raise ValueError
        if mode == "functions" and not exports:
            raise ValueError
        requirements = data.get("requirements", [])
        if (
            not isinstance(requirements, list)
            or len(requirements) > 12
            or any(
                not isinstance(x, str)
                or len(x) > 128
                or not REQUIREMENT.fullmatch(x)
                or re.split(r"[\[<>=!~]", x)[0].casefold().replace("_", "-") in {"mcp", "qqbot"}
                for x in requirements
            )
        ):
            raise ValueError
        env_keys = data.get("env_keys", [])
        if (
            not isinstance(env_keys, list)
            or len(env_keys) > 12
            or len(env_keys) != len(set(env_keys))
            or any(
                not isinstance(x, str)
                or not ENV_NAME.fullmatch(x)
                or x.startswith(("QQ_", "LLM_", "ADMIN_", "PYTHON", "UV_", "LD_", "DYLD_"))
                or x
                in {
                    "PATH",
                    "HOME",
                    "USERPROFILE",
                    "SYSTEMROOT",
                    "COMSPEC",
                    "TEMP",
                    "TMP",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "PATHEXT",
                }
                for x in env_keys
            )
        ):
            raise ValueError
        return ToolPack(
            name,
            description.strip(),
            entrypoint,
            mode,
            tuple(exports),
            tuple(requirements),
            tuple(env_keys),
        )
    except (ValueError, TypeError, KeyError, UnicodeError, SkillError):
        raise ToolPackError(
            "toolpack.json格式无效；需version=1、小写name、description和Python入口，函数模式需exports。"
        ) from None


class ToolPackStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def registry(self):
        try:
            path = self.root / ".registry.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict) or any(
                not valid_name(k) or not isinstance(v, dict) for k, v in data.items()
            ):
                raise ValueError
            return data
        except (ValueError, OSError):
            raise ToolPackError("工具包清单暂时无法读取。") from None

    def write_registry(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".registry-", dir=self.root)
        temporary = Path(path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
            temporary.chmod(0o600)
            os.replace(temporary, self.root / ".registry.json")
        finally:
            temporary.unlink(missing_ok=True)

    def file(self, name, path="toolpack.json"):
        try:
            relative = safe_path(path)
        except (SkillError, TypeError):
            raise ToolPackError("工具文件路径无效。") from None
        if not valid_name(name):
            raise ToolPackError("工具包名称无效。")
        folder = self.root / name
        target = folder.joinpath(*relative.parts)
        if (
            folder.is_symlink()
            or target.is_symlink()
            or not target.resolve().is_relative_to(folder.resolve())
            or not target.is_file()
            or target.stat().st_size > MAX_FILE
        ):
            raise ToolPackError("只能读取工具包目录内的现有文件。")
        return target

    def manifest(self, name):
        pack = parse_manifest(self.file(name).read_bytes())
        if pack.name != name:
            raise ToolPackError("工具包名称与目录不一致。")
        self.file(name, pack.entrypoint)
        return pack

    def revision(self, name, state):
        digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode())
        pack = self.manifest(name)
        digest.update(self.file(name).read_bytes())
        digest.update(self.file(name, pack.entrypoint).read_bytes())
        return digest.hexdigest()

    def list(self):
        result = []
        for name, state in sorted(self.registry().items()):
            pack = self.manifest(name)
            result.append(
                {
                    "name": name,
                    "description": pack.description,
                    "mode": pack.mode,
                    "entrypoint": pack.entrypoint,
                    "exports": list(pack.exports),
                    "requirements": list(pack.requirements),
                    "env_keys": list(pack.env_keys),
                    "env_configured": {
                        key: bool(state.get("env", {}).get(key)) for key in pack.env_keys
                    },
                    "enabled": state.get("enabled") is True and state.get("trusted") is True,
                }
            )
        return result

    def install(self, filename, raw):
        if not filename.lower().endswith(".zip"):
            raise ToolPackError("可执行工具仅支持单工具包ZIP。")
        try:
            files = read_archive(raw)
        except SkillError as exc:
            raise ToolPackError(str(exc)) from None
        manifests = [p for p in files if p.name == "toolpack.json"]
        if len(manifests) != 1:
            raise ToolPackError("每包必须有一个toolpack.json。")
        prefix = manifests[0].parent
        if any(not p.is_relative_to(prefix) for p in files):
            raise ToolPackError("ZIP不能包含工具目录外的文件。")
        normalized = {p.relative_to(prefix).as_posix(): raw for p, raw in files.items()}
        pack = parse_manifest(normalized["toolpack.json"])
        if pack.entrypoint not in normalized:
            raise ToolPackError("工具入口文件不存在。")
        try:
            for path, content in normalized.items():
                if path.endswith(".py"):
                    ast.parse(content.decode("utf-8-sig"), filename=path)
        except (SyntaxError, UnicodeError, ValueError):
            raise ToolPackError("Python源文件不是有效的UTF-8代码。") from None
        registry = self.registry()
        destination = self.root / pack.name
        if pack.name in registry or destination.exists() or destination.is_symlink():
            raise ToolPackError("同名工具包已存在，不覆盖。")
        if len(registry) >= 16:
            raise ToolPackError("最多保存16个工具包。")
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.root))
        installed = False
        try:
            for path, content in normalized.items():
                target = staging / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o600)
            staging.rename(destination)
            installed = True
            registry[pack.name] = {"enabled": False, "trusted": False, "env": {}}
            self.write_registry(registry)
        except OSError:
            if installed and destination.resolve().is_relative_to(self.root):
                shutil.rmtree(destination)
            raise ToolPackError("工具包保存失败，请检查磁盘和权限。") from None
        finally:
            if staging.exists() and staging.resolve().is_relative_to(self.root):
                shutil.rmtree(staging)
        return next(item for item in self.list() if item["name"] == pack.name)

    def enable(self, name, enabled, trusted=False):
        registry = self.registry()
        if name not in registry or not isinstance(enabled, bool) or not isinstance(trusted, bool):
            raise ToolPackError("工具包不存在或启停参数无效。")
        self.manifest(name)
        if enabled and not trusted:
            raise ToolPackError("启用会执行代码，请先明确确认信任该工具包。")
        if (
            enabled
            and not registry[name].get("enabled")
            and sum(x.get("enabled") is True for x in registry.values()) >= 4
        ):
            raise ToolPackError("同时最多启用4个工具包。")
        registry[name].update(enabled=enabled, trusted=trusted if enabled else False)
        self.write_registry(registry)

    def set_env(self, name, values, clear):
        pack = self.manifest(name)
        registry = self.registry()
        if (
            name not in registry
            or not isinstance(values, dict)
            or set(values) - set(pack.env_keys)
            or not isinstance(clear, list)
            or any(key not in pack.env_keys for key in clear)
        ):
            raise ToolPackError("只能配置清单声明的环境字段。")
        existing = registry[name].get("env", {}).copy()
        for key, value in values.items():
            if not isinstance(value, str) or len(value) > 2000 or "\0" in value:
                raise ToolPackError("工具环境字段应为2000字以内的文本。")
            if value:
                existing[key] = value
        for key in clear:
            if values.get(key):
                raise ToolPackError("不能同时填写和清除一个字段。")
            existing.pop(key, None)
        registry[name]["env"] = existing
        self.write_registry(registry)

    def request_restart(self, name):
        registry = self.registry()
        self.manifest(name)
        if (
            name not in registry
            or registry[name].get("enabled") is not True
            or registry[name].get("trusted") is not True
        ):
            raise ToolPackError("请先信任并启用该工具包。")
        registry[name]["restart"] = int(registry[name].get("restart", 0)) + 1
        self.write_registry(registry)
