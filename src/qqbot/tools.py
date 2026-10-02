"""LLM 可调用的函数白名单；参数验证后才执行 Python 函数。"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from qqbot.electricity import ElectricityClient, ElectricityError


@dataclass(frozen=True)
class FunctionTool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[dict]]


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
            values = json.loads(arguments)
        except (ValueError, UnicodeError):
            return self._invalid()
        tool = self._tools[name]
        properties = tool.parameters.get("properties", {})
        if (
            not isinstance(values, dict)
            or set(values) - set(properties)
            or set(tool.parameters.get("required", [])) - set(values)
        ):
            return self._invalid()
        # 当前工具只接受短字符串，任何 URL、凭证或 shell 参数都没有对应入口。
        if any(not isinstance(value, str) or len(value) > 128 for value in values.values()):
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

    @staticmethod
    def _invalid() -> dict:
        return {"ok": False, "error": "invalid_arguments", "message": "请提供有效的楼号和房号。"}


def electricity_tool(client: ElectricityClient) -> FunctionTool:
    return FunctionTool(
        name="query_electricity",
        description=(
            "查询宿舍的当前剩余电量，单位为度。需要用户明确提供楼号和房号。"
            "将'33号楼2035宿舍'转换为'33#2035'；不要猜测宿舍，不返回余额金额。"
            "用户说19号楼312就传19#312，不擅自改成19312；别名由目录解决。"
            "有多个候选时先问区域，再用用户选定的area重新查询。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "dormitory": {"type": "string", "description": "楼号#房号，例如33#2035"},
                "area": {"type": "string", "description": "可选：用户确认的主菜单/区域编号或名称"},
            },
            "required": ["dormitory"],
            "additionalProperties": False,
        },
        handler=client.query,
    )
