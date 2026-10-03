"""LLM 可调用的函数白名单；参数验证后才执行 Python 函数。"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from qqbot.electricity import ElectricityClient, ElectricityError
from qqbot.games import GameError, draw_lots, roll_dice
from qqbot.skills import MAX_FILE, SkillError
from qqbot.user_store import UserDataError


@dataclass(frozen=True)
class FunctionTool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[dict]]
    full_schema: bool = False


def validate_schema(schema):
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or len(json.dumps(schema)) > 16000
    ):
        raise ValueError("工具参数Schema需为16000字以内的object。")
    pending = [(schema, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 16 or count > 1000:
            raise ValueError("工具Schema过于复杂。")
        if isinstance(value, dict):
            if "$id" in value:
                raise ValueError("不支持Schema外部标识。")
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in value and (
                    not isinstance(value[key], str) or not value[key].startswith("#/")
                ):
                    raise ValueError("不支持外部Schema引用。")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    Draft202012Validator.check_schema(schema)


def no_remote_schema(uri):
    raise NoSuchResource(ref=uri)


def invalid_json_constant(value):
    raise ValueError("Non-finite JSON number")


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, FunctionTool] = {}

    def register(self, tool: FunctionTool):
        if tool.name in self._tools:
            raise ValueError("工具名重复")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, arguments: str) -> dict:
        if name not in self._tools:
            return {"ok": False, "error": "unknown_tool", "message": "当前不支持这个功能。"}
        if not isinstance(arguments, str) or len(arguments) > 4000:
            return self._invalid()
        try:
            values = json.loads(arguments, parse_constant=invalid_json_constant)
        except (ValueError, UnicodeError):
            return self._invalid()
        tool = self._tools[name]
        if tool.full_schema:
            try:
                Draft202012Validator(
                    tool.parameters, registry=Registry(retrieve=no_remote_schema)
                ).validate(values)
            except (ValidationError, SchemaError, ValueError, RecursionError, Unresolvable):
                return self._invalid()
            return await tool.handler(**values) if isinstance(values, dict) else self._invalid()
        properties = tool.parameters.get("properties", {})
        if (
            not isinstance(values, dict)
            or set(values) - set(properties)
            or set(tool.parameters.get("required", [])) - set(values)
        ):
            return self._invalid()
        for key, value in values.items():
            spec = properties[key]
            if spec.get("type") == "string":
                if not isinstance(value, str) or len(value) > spec.get("maxLength", 128):
                    return self._invalid()
            elif spec.get("type") == "integer":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not spec.get("minimum", 0) <= value <= spec.get("maximum", 365)
                ):
                    return self._invalid()
            else:
                return self._invalid()
        try:
            return await tool.handler(**values)
        except ElectricityError as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": str(exc),
                "candidates": exc.candidates,
            }
        except (GameError, UserDataError, SkillError) as exc:
            return {"ok": False, "message": str(exc)}

    @staticmethod
    def _invalid() -> dict:
        return {"ok": False, "error": "invalid_arguments", "message": "请提供有效的工具参数。"}


def electricity_tool(client: ElectricityClient, *, profile=None, recorder=None) -> FunctionTool:
    profile = profile or {}

    async def query(dormitory: str = "", area: str = "") -> dict:
        use_profile = not dormitory.strip()
        target = profile.get("dormitory", "") if use_profile else dormitory
        if not target:
            raise ElectricityError("请先提供楼号和房号，或绑定你的宿舍。", "missing_dormitory")
        selected = area or (profile.get("area", "") if use_profile else "")
        result = await client.query(target, area=selected)
        return (
            recorder(result)
            if recorder is not None
            else {k: v for k, v in result.items() if not k.startswith("_")}
        )

    return FunctionTool(
        name="query_electricity",
        description=(
            "查询宿舍的当前剩余电量，单位为度。需要用户明确提供楼号和房号。"
            "将'33号楼2035宿舍'转换为'33#2035'；不要猜测宿舍，不返回余额金额。"
            "用户说19号楼312就传19#312，不擅自改成19312；别名由目录解决。"
            "有多个候选时先问区域，再用用户选定的area重新查询。"
            "已有明确绑定宿舍且用户未指定其他房间时，可省略dormitory使用绑定值。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "dormitory": {"type": "string", "description": "楼号#房号，例如33#2035"},
                "area": {"type": "string", "description": "可选：用户确认的主菜单/区域编号或名称"},
            },
            "required": [] if profile.get("dormitory") else ["dormitory"],
            "additionalProperties": False,
        },
        handler=query,
    )


def game_tools() -> list[FunctionTool]:
    async def dice(expression="1d6"):
        return roll_dice(expression)

    async def draw(options=""):
        return draw_lots(options)

    return [
        FunctionTool(
            "roll_dice",
            "由程序掷骰子；如2d6。结果须按工具返回展示，不自行选择点数。",
            {
                "type": "object",
                "properties": {"expression": {"type": "string", "maxLength": 8}},
                "additionalProperties": False,
            },
            dice,
        ),
        FunctionTool(
            "draw_lots",
            "在用户给定选项中随机选择，用|分隔；空值抽娱乐签。结果由程序生成。",
            {
                "type": "object",
                "properties": {"options": {"type": "string", "maxLength": 1000}},
                "additionalProperties": False,
            },
            draw,
        ),
    ]


def history_tools(access) -> list[FunctionTool]:
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "description": "最近多少天，默认3",
            },
            "dormitory": {
                "type": "string",
                "description": "可选楼号#房号；省略使用绑定宿舍或唯一历史宿舍",
            },
            "area": {"type": "string", "description": "可选，用户确认的区域"},
        },
        "additionalProperties": False,
    }
    return [
        FunctionTool(
            "electricity_history",
            "读取当前用户已记录的电费历史，不请求校园接口。不能查询其他用户，不能补造过去读数。",
            parameters,
            access.history,
        ),
        FunctionTool(
            "electricity_usage",
            "计算当前用户同一电表的余额净变化和条件性耗电估算。必须说明实际覆盖时段；充值、校正或数据不足时不能声称准确耗电。",
            parameters,
            access.usage,
        ),
    ]


def skill_tools(access) -> list[FunctionTool]:
    return [
        FunctionTool(
            "load_skill",
            "按名称读取已启用文档技能的说明与资源目录；不执行任何脚本。",
            {
                "type": "object",
                "properties": {"name": {"type": "string", "maxLength": 64}},
                "required": ["name"],
                "additionalProperties": False,
            },
            access.load,
        ),
        FunctionTool(
            "read_skill_file",
            "先加载技能，再分段读取其目录内的UTF-8参考文件；不能访问其他路径或执行代码。",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 64},
                    "path": {"type": "string", "maxLength": 128},
                    "offset": {"type": "integer", "minimum": 0, "maximum": MAX_FILE},
                },
                "required": ["name", "path"],
                "additionalProperties": False,
            },
            access.read_reference,
        ),
    ]
