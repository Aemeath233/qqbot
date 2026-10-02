"""宿舍目录：以区域、楼号和真实房间记录匹配，别名只能来自明确配置。"""

import json
import re
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path

DORM_PATTERN = re.compile(r"([0-9]{1,3})#([0-9]{1,6})")
AREA_NAMES = {"1": "11—29号楼", "2": "7—10、30—33号楼", "3": "商丘学院"}


class DormError(ValueError):
    def __init__(self, message: str, code: str, candidates: list[dict] | None = None):
        self.code = code
        self.candidates = candidates or []
        super().__init__(message)


@dataclass(frozen=True)
class DormRoom:
    area: str
    area_name: str
    building: str
    room: str
    roomverify: str
    aliases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.building}#{self.room}"


class DormDirectory:
    def __init__(self, rooms: list[DormRoom]):
        self.rooms = rooms
        self.index: dict[tuple[str, str], list[DormRoom]] = {}
        seen = {}
        for record in rooms:
            identity = (record.area, record.building, record.room)
            if identity in seen:
                if seen[identity] != record:
                    raise DormError("宿舍目录存在冲突，请联系管理员。", "conflicting_map")
                continue
            seen[identity] = record
            for room in set((record.room, *record.aliases)):
                self.index.setdefault((record.building, room), []).append(record)
        if not self.index:
            raise DormError("宿舍目录为空，请联系管理员。", "invalid_map")

    @classmethod
    def load(cls, path: Path | None = None):
        try:
            content = (
                path.read_text(encoding="utf-8-sig")
                if path
                else files("qqbot").joinpath("resources/dorm_map.txt").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError):
            raise DormError("宿舍目录无法读取，请联系管理员。", "map_unavailable") from None
        rooms = []
        if path is not None and path.suffix.lower() == ".json":
            try:
                payload = json.loads(content)
                records = payload["rooms"] if isinstance(payload, dict) else payload
                if not isinstance(records, list):
                    raise ValueError
                for item in records:
                    if not isinstance(item, dict):
                        raise ValueError
                    area = item.get("area", "")
                    building, room, roomverify = item["building"], item["room"], item["roomverify"]
                    aliases = item.get("aliases", [])
                    area_name = item.get("area_name", AREA_NAMES.get(area, area))
                    if (
                        not all(
                            isinstance(v, str)
                            for v in (area, building, room, roomverify, area_name)
                        )
                        or len(area) > 64
                        or len(area_name) > 64
                        or not DORM_PATTERN.fullmatch(f"{building}#{room}")
                        or not re.fullmatch(r"[0-9-]{1,80}", roomverify)
                        or not isinstance(aliases, list)
                        or len(aliases) > 20
                        or any(
                            not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,6}", v)
                            for v in aliases
                        )
                    ):
                        raise ValueError
                    rooms.append(
                        DormRoom(area, area_name, building, room, roomverify, tuple(aliases))
                    )
            except (ValueError, KeyError, TypeError):
                raise DormError("宿舍 JSON 目录格式错误，请联系管理员。", "invalid_map") from None
        else:
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.count("=") != 1:
                    raise DormError("宿舍映射文件格式错误，请联系管理员。", "invalid_map")
                label, roomverify = (part.strip() for part in line.split("=", 1))
                match = DORM_PATTERN.fullmatch(label)
                if match is None or not re.fullmatch(r"[0-9-]{1,80}", roomverify):
                    raise DormError("宿舍映射文件格式错误，请联系管理员。", "invalid_map")
                area = roomverify.split("-", 1)[0]
                rooms.append(
                    DormRoom(area, AREA_NAMES.get(area, area), *match.groups(), roomverify)
                )
        return cls(rooms)

    def resolve(self, dormitory: str, area: str = "") -> DormRoom:
        if not isinstance(dormitory, str) or not isinstance(area, str):
            raise DormError("请提供楼号#房号，例如33#2035。", "invalid_dorm")
        match = DORM_PATTERN.fullmatch(dormitory.strip())
        if match is None:
            raise DormError(
                "请提供楼号和房号，例如19#312；只有312时需要先确认楼号。", "invalid_dorm"
            )
        candidates = list(self.index.get(match.groups(), []))
        if area.strip():
            normalized = re.sub(r"[\s—–－\-、，,]", "", area).casefold()
            candidates = [
                room
                for room in candidates
                if any(
                    re.sub(r"[\s—–－\-、，,]", "", value).casefold() == normalized
                    for value in (room.area, room.area_name)
                )
            ]
        if not candidates:
            raise DormError(
                "没有匹配到这个宿舍。请核对区域、楼号和房号；旧编号需要管理员配置对应别名。",
                "dorm_not_found",
            )
        if len(candidates) > 1:
            choices = [
                {"area": room.area, "area_name": room.area_name, "dormitory": room.label}
                for room in candidates
            ]
            names = "、".join(dict.fromkeys(room.area_name or "未标明区域" for room in candidates))
            raise DormError(
                f"这个房号匹配到多个宿舍，请确认区域/主菜单（{names}），我再查询。",
                "ambiguous_dorm",
                choices,
            )
        return candidates[0]

    def export(self, path: Path):
        if path.suffix.lower() != ".json":
            raise ValueError("宿舍目录输出路径必须以 .json 结尾")
        if path.exists():
            raise ValueError("输出文件已存在，请选择新的文件名")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rooms": [asdict(room) for room in self.rooms]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
