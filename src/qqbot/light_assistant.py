"""模型选择查询电量或提出绑定确认请求；保存绑定必须由用户确认。"""

import json
import logging
import re

from qqbot.config import Settings
from qqbot.electricity import ElectricityClient, ElectricityError
from qqbot.llm import ChatCompletionsClient, LLMError
from qqbot.presentation import electricity_reply

logger = logging.getLogger(__name__)
SYSTEM = (
    "你是电费查询小助手，只帮助用户查询宿舍当前剩余电量，用简洁中文回答。"
    "请根据用户的自然语言选择 query_electricity 或 request_dorm_binding，不要求固定命令或关键词。"
    "‘看看电费’、‘帮我看下宿舍电费’、‘还有多少电’通常都表示查询当前剩余电量。"
    "例如‘看看33楼4032宿舍电费’应调用 query_electricity，dormitory 为33#4032。"
    "‘/电费 33#4032’和‘/electricity 33#4032’也表示同一查询。"
    "用户要查当前电量，且楼号与房号都明确、只查询一间宿舍时，应调用工具。"
    "未绑定默认宿舍且缺少楼号或房号时，请用户提供完整信息，不得猜测，也不得调用工具。"
    "已绑定默认宿舍时，用户说‘查一下电费’‘还有多少电’，应调用 query_electricity，"
    "可以省略 dormitory 和 area，让程序使用绑定。用户明确指定其他宿舍时查询指定宿舍。"
    "普通查询和再次查询只能调用 query_electricity，绝不能自动绑定。"
    "仅当用户明确说要绑定或更换默认宿舍时，调用 request_dorm_binding 并提供目标宿舍。"
    "例如‘更换绑定宿舍为33楼2004室’提出绑定确认；该工具只显示是/否按钮，不直接保存。"
    "缺少新宿舍时先询问，不能擅自沿用原宿舍进行更换。"
    "用户点击‘是’或回复明确的确认指令后，才会由程序保存，不能声称提出请求就已绑定。"
    "不同群和不同用户的绑定独立，使用的默认宿舍仅属于当前用户在当前会话中的绑定。"
    "如果用户要求同时查询多间宿舍，请用户选择一间。"
    "用户说‘19号楼312’时传19#312；不能把19312擅自拆成楼号和房号。"
    "area 仅在用户明确提供区域名称或编号时传入，否则省略。"
    "只负责当前剩余电量，不提供历史分析、耗电量估算、预测、图表、金额换算或缴费充值。"
    "用户问其他问题时，简短礼貌地说明只能帮助查询宿舍电量，不回答其他问题或扮演其他角色。"
    "用户问候或询问用法时，可以简短引导其提供宿舍信息。"
    "没有工具结果时，不得声称已经查询成功，不得编造电量。"
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_electricity",
            "description": (
                "查询一个宿舍当前剩余电量，单位为度。用户说查电费、看看宿舍电费、"
                "还有多少电时使用。楼号、房号明确时可查询指定宿舍。"
                "存在已绑定默认宿舍时可省略 dormitory；未绑定且缺少信息则先询问。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dormitory": {
                        "type": "string",
                        "description": "楼号#房号，例如33#2035；查询已绑定宿舍时可省略或传空字符串",
                    },
                    "area": {"type": "string", "description": "可选的、用户明确确认的区域"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }
]
TOOLS.append(
    {
        "type": "function",
        "function": {
            "name": "request_dorm_binding",
            "description": (
                "用户明确要求绑定或更换默认宿舍时使用；仅提出确认请求，不保存绑定。"
                "普通查电费禁止调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dormitory": {
                        "type": "string",
                        "description": "用户明确提供的新宿舍，楼号#房号，例如33#2004",
                    },
                    "area": {"type": "string", "description": "可选的明确区域名称或编号"},
                },
                "required": ["dormitory"],
                "additionalProperties": False,
            },
        },
    }
)


def reject_json_constant(_value: str):
    raise ValueError("不接受非标准 JSON 数字")


class LightAssistant:
    def __init__(self, settings: Settings, session):
        self.settings = settings
        self.model = ChatCompletionsClient(settings, session)
        self.electricity = ElectricityClient(settings, session)
        self.state = None

    async def generate(self, task_kind: str, payload: str, *, context=None):
        if task_kind != "chat":
            return self._refusal()
        if not self.settings.llm_enabled:
            return "自然语言查询尚未配置模型服务，请在 .env 中填写 LLM_API_KEY 和 LLM_MODEL。"
        binding = (
            self.state.binding(context) if self.state is not None and context is not None else None
        )
        binding_prompt = (
            f"当前用户在本会话已绑定默认宿舍：{binding['dormitory']}，区域编号：{binding['area']}。"
            if binding is not None
            else "当前用户在本会话尚未绑定默认宿舍，不能使用其他群或其他用户的绑定。"
        )
        try:
            response = await self.model.complete(
                [
                    {"role": "system", "content": SYSTEM + binding_prompt},
                    {"role": "user", "content": payload},
                ],
                TOOLS,
            )
        except LLMError as exc:
            logger.warning("电费问答模型失败：%s", str(exc))
            return "自然语言查询暂时不可用，请稍后重试。"

        calls = response.get("tool_calls") or []
        if not calls:
            content = response.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            logger.warning("电费模型未返回文本或工具调用")
            return "这次没有完成查询，请稍后重试，并在消息里提供楼号和房间号。"
        if len(calls) != 1 or calls[0]["function"]["name"] not in {
            "query_electricity",
            "request_dorm_binding",
        }:
            return "每条消息只支持处理一个电费操作。"
        function_name = calls[0]["function"]["name"]
        try:
            arguments = json.loads(
                calls[0]["function"]["arguments"], parse_constant=reject_json_constant
            )
            if (
                not isinstance(arguments, dict)
                or set(arguments) - {"dormitory", "area"}
                or (
                    "dormitory" in arguments
                    and (
                        not isinstance(arguments["dormitory"], str)
                        or (
                            arguments["dormitory"] != ""
                            and re.fullmatch(r"[0-9]{1,3}#[0-9]{1,6}", arguments["dormitory"])
                            is None
                        )
                    )
                )
                or not isinstance(arguments.get("area", ""), str)
                or len(arguments.get("area", "")) > 80
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return "请告诉我明确的楼号和房间号，例如33号楼2035室。"
        area = arguments.get("area", "").strip()
        dormitory = arguments.get("dormitory")
        if function_name == "request_dorm_binding":
            if not dormitory:
                return "请提供想绑定或更换的新宿舍楼号和房号。"
            return self.request_binding(dormitory, area, context=context)
        if not dormitory:
            if binding is None:
                return "你还没有绑定默认宿舍，请告诉我楼号和房号。查询后可以点击“绑定此宿舍”。"
            dormitory = binding["dormitory"]
            area = area or binding["area"]
        elif binding is not None and dormitory == binding["dormitory"]:
            area = area or binding["area"]
        return await self.query_electricity(dormitory, area, context=context)

    def request_binding(self, dormitory, area="", *, context=None):
        if self.state is None or context is None:
            return "请在 QQ 对话中发起宿舍绑定或更换。"
        try:
            room = self.electricity.resolve_room(dormitory, area)
        except ElectricityError as exc:
            return str(exc)
        return self.state.request_binding(context, room.label, room.area)

    async def query_electricity(self, dormitory, area="", *, context=None):
        try:
            result = await self.electricity.query(dormitory, area=area)
        except ElectricityError as exc:
            return str(exc)
        if self.state is not None and context is not None:
            return self.state.card(result, context)
        return electricity_reply(result).text

    @staticmethod
    def _refusal() -> str:
        return "我是小电，电费查询小助手，只能查询宿舍当前剩余电量，其他问题暂时帮不了你。"
