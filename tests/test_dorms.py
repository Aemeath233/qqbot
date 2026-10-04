import json

import pytest

from qqbot.dorms import DormDirectory, DormError, DormRoom


def test_builtin_directory_is_deduplicated_and_exact():
    directory = DormDirectory.load()
    assert len(directory.rooms) == 4097
    assert directory.resolve("33#4032").roomverify == "2-11--4-4032"
    with pytest.raises(DormError, match="没有匹配"):
        directory.resolve("19#19312")
    # 不凭数字长度或后缀创建未经确认的别名。
    with pytest.raises(DormError):
        directory.resolve("33#432")


def test_aliases_canonical_labels_and_area_disambiguation(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "rooms": [
                    {
                        "area": "1",
                        "building": "19",
                        "room": "312",
                        "aliases": ["19312"],
                        "roomverify": "1-218--221-102210012",
                    },
                    {
                        "area": "3",
                        "building": "19",
                        "room": "312",
                        "aliases": ["19312"],
                        "roomverify": "3-77---20351",
                    },
                    {
                        "area": "2",
                        "building": "33",
                        "room": "4032",
                        "aliases": ["432"],
                        "roomverify": "2-11--4-4032",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    directory = DormDirectory.load(path)
    assert directory.resolve("19#19312", "1").label == "19#312"
    assert directory.resolve("19#312", "11-29号楼").area == "1"
    assert directory.resolve("19#312", "商丘学院").area == "3"
    assert directory.resolve("33#432").label == "33#4032"
    with pytest.raises(DormError) as error:
        directory.resolve("19#312")
    assert error.value.code == "ambiguous_dorm"
    assert len(error.value.candidates) == 2
    with pytest.raises(DormError, match="楼号"):
        directory.resolve("312")


def test_alias_collision_is_not_silently_resolved():
    directory = DormDirectory(
        [
            DormRoom("2", "area", "33", "4032", "2-11--4-4032", ("432",)),
            DormRoom("2", "area", "33", "432", "2-11--4-432"),
        ]
    )
    with pytest.raises(DormError) as caught:
        directory.resolve("33#432")
    assert caught.value.code == "ambiguous_dorm"


def test_conflicting_and_invalid_directory(tmp_path):
    with pytest.raises(DormError, match="冲突"):
        DormDirectory(
            [
                DormRoom("1", "area", "19", "312", "1-1--1-12"),
                DormRoom("1", "area", "19", "312", "1-1--1-13"),
            ]
        )
    path = tmp_path / "map.txt"
    path.write_text("19#312=1-1--1-12\n19#312=1-1--1-12\n", encoding="utf-8")
    assert DormDirectory.load(path).resolve("19#312").roomverify == "1-1--1-12"
    path.write_text("19#312=bad-url\n", encoding="utf-8")
    with pytest.raises(DormError):
        DormDirectory.load(path)
