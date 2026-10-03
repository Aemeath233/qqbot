import io
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from qqbot.skills import (
    MAX_FILE,
    MAX_UPLOAD,
    SkillAccess,
    SkillError,
    SkillStore,
    parse_skill,
    upload_files,
)


def manifest(name="meal-choice", extra="", body="Use existing tools only."):
    return f"---\nname: {name}\ndescription: Pick a meal\n{extra}---\n{body}\n".encode()


def bundle(files):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return stream.getvalue()


def test_standard_yaml_and_claude_metadata_fallback():
    raw = (
        b"---\nname: meal-choice\ndescription: >\n  Pick a meal\n"
        b"  when undecided\n---\nInstructions"
    )
    assert parse_skill(raw).description == "Pick a meal when undecided"
    skill = parse_skill("# 帮我选饭\n普通说明".encode(), "选饭")
    assert skill.name == "选饭" and skill.description == "帮我选饭" and skill.warnings
    skill, _ = upload_files("dinner.md", b"# Dinner\nInstructions")
    assert skill.name == "dinner"


@pytest.mark.parametrize("name", ["UPPER", "a_b", "-abc", "abc-", "a--b", "con", "a" * 65])
def test_invalid_names(name):
    with pytest.raises(SkillError):
        parse_skill(manifest(name))


@pytest.mark.parametrize(
    "raw",
    [
        b"---\nname: !!python/object/apply:os.system ['echo forbidden']\n---\nBody",
        b"---\n- list\n---\nBody",
        b"---\nname: a\ndescription: x\nuser-invocable: 'false'\n---\nBody",
        b"---\nname: a\ndescription: x\n1: value\n---\nBody",
        b"---\nname: a\ndescription: x\n---\n",
        b"\xff",
        b"No metadata and no fallback name",
        manifest(body="x" * 12001),
    ],
)
def test_invalid_or_unsafe_yaml_never_runs(raw):
    with pytest.raises(SkillError):
        parse_skill(raw)


def test_case_insensitive_manifest_and_platform_resources_preserved():
    raw = bundle(
        {
            "some-wrapper/Meal/SkiLL.MD": manifest(extra="allowed-tools: Bash\ncontext: fork\n"),
            "some-wrapper/Meal/scripts/query.py": b"raise RuntimeError('do not run')",
            "some-wrapper/Meal/assets/icon.png": b"\x89PNG\xff",
            "some-wrapper/Meal/agents/openai.yaml": b"interface: {}",
        }
    )
    skill, files = upload_files("skill.zip", raw)
    assert skill.name == "meal-choice" and skill.warnings
    assert files["SKILL.md"] == manifest(extra="allowed-tools: Bash\ncontext: fork\n")
    assert len(files) == 4 and files["assets/icon.png"] == b"\x89PNG\xff"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "/absolute",
        "C:/escape",
        "meal/../escape",
        "meal/CON.txt",
        "meal/.env",
        "meal/last.",
        "meal/last ",
        "meal/a:b",
    ],
)
def test_zip_paths_cannot_escape_or_collide_on_windows(path):
    with pytest.raises(SkillError):
        upload_files("skill.zip", bundle({"meal/SKILL.md": manifest(), path: b"bad"}))


def test_original_zip_backslash_path_is_checked_before_windows_normalization():
    raw = bundle({"meal/SKILL.md": manifest(), "meal/bad.txt": b"bad"})
    raw = raw.replace(b"meal/bad.txt", b"meal\\bad.txt")
    with pytest.raises(SkillError):
        upload_files("skill.zip", raw)


def test_storage_and_enable_quotas(tmp_path):
    store = SkillStore(tmp_path)
    for index in range(32):
        store.install("SKILL.md", manifest(f"skill-{index}"))
        if index < 16:
            store.set_enabled(f"skill-{index}", True)
    with pytest.raises(SkillError, match="最多保存"):
        store.install("SKILL.md", manifest("additional"))
    with pytest.raises(SkillError, match="最多启用"):
        store.set_enabled("skill-16", True)
    store.set_enabled("skill-0", True)
    store.set_enabled("skill-0", False)
    store.set_enabled("skill-16", True)


@pytest.mark.parametrize(
    "files",
    [
        {"meal/SKILL.md": manifest(), "outside.txt": b"bad"},
        {"meal/SKILL.md": manifest(), "second/SKILL.md": manifest("other")},
        {"meal/SKILL.md": manifest(), "meal/skill.md": manifest()},
        {"meal/no-manifest.txt": b"bad"},
        {"meal/SKILL.md": manifest(), "meal/large.txt": b"x" * (MAX_FILE + 1)},
        {"meal/SKILL.md": manifest(), **{f"meal/{i}": b"x" for i in range(80)}},
        {"meal/SKILL.md": manifest(), **{f"meal/{i}": b"x" * MAX_FILE for i in range(9)}},
    ],
)
def test_bad_or_excessive_archives(files):
    with pytest.raises(SkillError):
        upload_files("skill.zip", bundle(files))


def test_symlink_archive_is_rejected():
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("meal/SKILL.md", manifest())
        link = ZipInfo("meal/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "outside.txt")
    with pytest.raises(SkillError):
        upload_files("skill.zip", stream.getvalue())


@pytest.mark.parametrize(
    "filename,raw",
    [
        ("skill.zip", b"invalid"),
        ("skill.py", b"print('no')"),
        ("SKILL.md", b""),
        ("large.md", b"x" * (MAX_UPLOAD + 1)),
    ],
    ids=["invalid-zip", "executable", "empty", "oversized"],
)
def test_upload_limits_and_types(filename, raw):
    with pytest.raises(SkillError):
        upload_files(filename, raw)


async def test_store_access_disabled_manual_only_and_resource_chunks(tmp_path):
    store = SkillStore(tmp_path / "skills")
    raw = bundle(
        {
            "meal/SKILL.md": manifest(),
            "meal/references/notes.md": b"x" * 4001,
            "meal/assets/binary": b"\xff",
            "meal/scripts/run.py": b"raise Exception()",
        }
    )
    assert store.install("skill.zip", raw)["enabled"] is False
    access = SkillAccess(store)
    with pytest.raises(SkillError, match="未启用"):
        await access.load("meal-choice")
    store.set_enabled("meal-choice", True)
    with pytest.raises(SkillError, match="load_skill"):
        await access.read_reference("meal-choice", "references/notes.md")
    loaded = await access.load("meal-choice")
    assert loaded["mode"] == "documentation-only" and "scripts/run.py" in loaded["files"]
    first = await access.read_reference("meal-choice", "references/notes.md")
    assert len(first["content"]) == 4000 and first["next_offset"] == 4000
    assert (await access.read_reference("meal-choice", "references/notes.md", 4000))[
        "content"
    ] == "x"
    for path in ("../outside", "assets/binary"):
        with pytest.raises(SkillError):
            await access.read_reference("meal-choice", path)
    with pytest.raises(SkillError):
        await access.read_reference("meal-choice", "SKILL.md", True)
    store.set_enabled("meal-choice", False)
    with pytest.raises(SkillError):
        await access.read_reference("meal-choice", "SKILL.md")


async def test_invocation_flags(tmp_path):
    store = SkillStore(tmp_path)
    store.install("SKILL.md", manifest(extra="disable-model-invocation: true\n"))
    store.set_enabled("meal-choice", True)
    with pytest.raises(SkillError, match="手动调用"):
        await SkillAccess(store).load("meal-choice")
    assert (await SkillAccess(store, manual_name="meal-choice").load("meal-choice"))["ok"]
    store.install("SKILL.md", manifest("automatic", extra="user-invocable: false\n"))
    store.set_enabled("automatic", True)
    with pytest.raises(SkillError, match="不允许手动"):
        await SkillAccess(store, manual_name="automatic").load("automatic")
    assert (await SkillAccess(store).load("automatic"))["ok"]


def test_duplicate_install_and_registry_failure_do_not_overwrite(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.install("SKILL.md", manifest())
    with pytest.raises(SkillError, match="同名"):
        store.install("SKILL.md", manifest(body="changed"))
    assert "changed" not in store.read("meal-choice")

    def fail(data):
        raise OSError("mock disk failure")

    monkeypatch.setattr(store, "_write_registry", fail)
    with pytest.raises(SkillError, match="无法保存"):
        store.install("SKILL.md", manifest("another"))
    assert not (tmp_path / "another").exists()
    assert not list(tmp_path.glob(".upload-*"))


def test_registry_corruption_is_a_safe_error(tmp_path):
    store = SkillStore(tmp_path)
    (tmp_path / ".registry.json").write_text('{"bad": 1}')
    with pytest.raises(SkillError, match="清单"):
        store.list()
