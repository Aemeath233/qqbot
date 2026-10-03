"""通用 Agent Skills 文档加载器：保留资源，不导入或运行包内程序。"""

import io
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

import yaml

MAX_UPLOAD = 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024
MAX_FILE = 256 * 1024
NAME = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)
DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class SkillError(ValueError):
    pass


def valid_name(name) -> bool:
    return (
        isinstance(name, str)
        and 1 <= len(name) <= 64
        and bool(NAME.fullmatch(name))
        and name == name.lower()
        and name.upper() not in DEVICES
    )


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    warnings: tuple[str, ...] = ()
    auto_invocation: bool = True
    user_invocable: bool = True


def parse_skill(raw: bytes, fallback_name: str = "") -> Skill:
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        if text.startswith("---\n"):
            parts = text.split("\n---\n", 1)
            if len(parts) != 2 or len(parts[0]) > 4096:
                raise ValueError
            metadata, body = yaml.safe_load(parts[0][4:]) or {}, parts[1].strip()
        else:
            metadata, body = {}, text.strip()
        if not isinstance(metadata, dict):
            raise ValueError
        if any(not isinstance(key, str) for key in metadata):
            raise ValueError
        for key in ("disable-model-invocation", "user-invocable"):
            if key in metadata and not isinstance(metadata[key], bool):
                raise ValueError
        name = metadata.get("name") or fallback_name
        description = (
            metadata.get("description")
            or next((line.strip("# ") for line in body.splitlines() if line.strip()), "")[:1024]
        )
        if (
            not valid_name(name)
            or not isinstance(description, str)
            or not 1 <= len(description.strip()) <= 1024
            or not body
            or len(body) > 12000
            or len(raw) > 65536
            or "\0" in text
        ):
            raise ValueError
        warnings = []
        if not metadata.get("name") or not metadata.get("description"):
            warnings.append("已从目录名称或正文生成缺少的元数据；推荐填写name和description。")
        extensions = set(metadata) - {
            "name",
            "description",
            "license",
            "compatibility",
            "metadata",
            "disable-model-invocation",
            "user-invocable",
        }
        if extensions:
            warnings.append("平台扩展已保留；allowed-tools、context、model等不会增加本机权限。")
        if metadata.get("compatibility"):
            warnings.append("运行依赖声明已保留；本机器人仅加载文档并使用已注册的工具。")
        return Skill(
            name,
            description.strip(),
            body,
            tuple(warnings),
            not bool(metadata.get("disable-model-invocation", False)),
            metadata.get("user-invocable", True) is not False,
        )
    except (ValueError, TypeError, KeyError, UnicodeError, yaml.YAMLError, RecursionError):
        raise SkillError(
            "技能需要有效的元数据和Markdown正文；推荐填写小写name和description。"
        ) from None


def safe_path(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path
        or any(
            re.search(r'[<>:"|?*\x00-\x1f]', p)
            or p.startswith(".")
            or p.endswith((".", " "))
            or p.split(".")[0].upper() in DEVICES
            for p in relative.parts
        )
    ):
        raise SkillError("技能文件路径不允许访问。")
    return relative


def upload_files(filename: str, raw: bytes) -> tuple[Skill, dict[str, bytes]]:
    if not raw or len(raw) > MAX_UPLOAD:
        raise SkillError("上传文件应为1 MiB以内的 Markdown 或单技能 ZIP。")
    if filename.lower().endswith(".md"):
        fallback = Path(filename).stem.lower() if Path(filename).stem.lower() != "skill" else ""
        return parse_skill(raw, fallback), {"SKILL.md": raw}
    if not filename.lower().endswith(".zip"):
        raise SkillError("只支持 Markdown 文件或 ZIP 技能包。")
    files, seen = {}, set()
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 80 or sum(info.file_size for info in infos) > MAX_EXPANDED:
                raise SkillError("技能包最多80个条目，解压内容不能超过2 MiB。")
            for info in infos:
                # ZipInfo在Windows上会规范化反斜杠；检查规范化前的原始名称。
                path = safe_path(info.orig_filename)
                if (
                    stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 1
                    or info.file_size > MAX_FILE
                    or path.as_posix().casefold() in seen
                ):
                    raise SkillError("技能包包含链接、重复文件、加密文件或超大文件。")
                seen.add(path.as_posix().casefold())
                if not info.is_dir():
                    content = archive.read(info)
                    if len(content) > MAX_FILE:
                        raise SkillError("技能资源不能超过256 KiB。")
                    files[path] = content
    except (BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise SkillError("ZIP 无法读取。") from None
    manifests = [path for path in files if path.name.casefold() == "skill.md"]
    if len(manifests) != 1:
        raise SkillError("每次上传一个技能，包内必须只有一个 SKILL.md。")
    prefix = manifests[0].parent
    if any(not path.is_relative_to(prefix) for path in files):
        raise SkillError("请将单个技能目录单独压缩，不能包含目录外的文件。")
    normalized = {path.relative_to(prefix).as_posix(): value for path, value in files.items()}
    manifest = manifests[0].relative_to(prefix).as_posix()
    normalized["SKILL.md"] = normalized.pop(manifest)
    return parse_skill(normalized["SKILL.md"], prefix.name.lower()), normalized


class SkillStore:
    def __init__(self, root: Path):
        self.root = root

    def _registry(self) -> dict:
        path = self.root / ".registry.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError
            if any(
                not valid_name(key) or not isinstance(value, dict) for key, value in data.items()
            ):
                raise ValueError
            return data
        except (OSError, ValueError):
            raise SkillError("技能清单暂时无法读取。") from None

    def _write_registry(self, data: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".registry-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
            os.replace(temporary, self.root / ".registry.json")
        finally:
            temporary.unlink(missing_ok=True)

    def _file(self, name: str, path: str) -> Path:
        if not valid_name(name):
            raise SkillError("技能名称无效。")
        relative = safe_path(path)
        folder = self.root / name
        target = folder.joinpath(*relative.parts)
        if (
            folder.is_symlink()
            or target.is_symlink()
            or not target.resolve().is_relative_to(folder.resolve())
        ):
            raise SkillError("只能读取当前技能目录内的文件。")
        if not target.is_file() or target.stat().st_size > MAX_FILE:
            raise SkillError("技能文件不存在或超过读取限制。")
        return target

    def list(self, *, enabled_only=False) -> list[dict]:
        result = []
        for name, state in sorted(self._registry().items()):
            if not isinstance(state, dict):
                continue
            enabled = state.get("enabled") is True
            if enabled_only and not enabled:
                continue
            try:
                skill = parse_skill(self._file(name, "SKILL.md").read_bytes(), name)
                result.append(
                    {
                        "name": name,
                        "description": skill.description,
                        "enabled": enabled,
                        "warnings": list(skill.warnings),
                        "auto_invocation": skill.auto_invocation,
                        "user_invocable": skill.user_invocable,
                    }
                )
            except (SkillError, OSError):
                continue
        return result

    def install(self, filename: str, raw: bytes) -> dict:
        skill, files = upload_files(filename, raw)
        registry = self._registry()
        destination = self.root / skill.name
        if skill.name in registry or destination.exists() or destination.is_symlink():
            raise SkillError("同名技能已存在，请换一个名称，避免覆盖已有内容。")
        if len(registry) >= 32:
            raise SkillError("最多保存32个技能。")
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.root))
        try:
            for path, content in files.items():
                target = staging / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o600)
            staging.rename(destination)
            registry[skill.name] = {"enabled": False}
            try:
                self._write_registry(registry)
            except OSError:
                if destination.resolve().is_relative_to(self.root.resolve()):
                    shutil.rmtree(destination)
                raise SkillError("技能无法保存，请检查目录权限和可用空间。") from None
        finally:
            if staging.exists() and staging.resolve().is_relative_to(self.root.resolve()):
                shutil.rmtree(staging)
        return {
            "name": skill.name,
            "description": skill.description,
            "enabled": False,
            "warnings": list(skill.warnings),
        }

    def set_enabled(self, name: str, enabled: bool):
        registry = self._registry()
        if not isinstance(enabled, bool) or name not in registry:
            raise SkillError("技能不存在或启停参数无效。")
        parse_skill(self._file(name, "SKILL.md").read_bytes(), name)
        if (
            enabled
            and sum(item.get("enabled") is True for item in registry.values()) >= 16
            and not registry[name].get("enabled")
        ):
            raise SkillError("同时最多启用16个技能。")
        registry[name]["enabled"] = enabled
        self._write_registry(registry)

    def read(self, name: str, path: str = "SKILL.md") -> str:
        try:
            return self._file(name, path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise SkillError(
                "该资源无法作为UTF-8文本读取；二进制文件只保留，不自动解析。"
            ) from None


class SkillAccess:
    def __init__(self, store: SkillStore, *, manual_name=""):
        self.store, self.manual_name = store, manual_name
        self.loaded: set[str] = set()

    def _enabled(self, name: str):
        candidates = {item["name"]: item for item in self.store.list(enabled_only=True)}
        if name not in candidates:
            raise SkillError("该技能不存在或未启用。")
        if name == self.manual_name:
            if not candidates[name]["user_invocable"]:
                raise SkillError("此技能不允许手动调用。")
        elif not candidates[name]["auto_invocation"]:
            raise SkillError("此技能仅允许通过 /技能 名称 任务 手动调用。")

    async def load(self, name: str) -> dict:
        self._enabled(name)
        skill = parse_skill(self.store.read(name).encode(), name)
        self.loaded.add(name)
        folder = self.store.root / name
        paths = [
            p.relative_to(folder).as_posix()
            for p in folder.rglob("*")
            if p.is_file() and not p.is_symlink()
        ][:80]
        return {
            "ok": True,
            "name": name,
            "instructions": skill.body,
            "files": paths,
            "mode": "documentation-only",
        }

    async def read_reference(self, name: str, path: str, offset: int = 0) -> dict:
        self._enabled(name)
        if name not in self.loaded:
            raise SkillError("请先调用 load_skill。")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_FILE:
            raise SkillError("读取位置无效。")
        text = self.store.read(name, path)
        return {
            "ok": True,
            "name": name,
            "path": path,
            "content": text[offset : offset + 4000],
            "next_offset": offset + 4000 if offset + 4000 < len(text) else None,
        }
