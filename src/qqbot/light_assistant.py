"""自然语言入口只允许调用一个工具：查宿舍当前剩余电量。"""

import json
import logging
import re

from qqbot.config import Settings
from qqbot.electricity import ElectricityClient, ElectricityError, format_electricity
from qqbot.llm import ChatCompletionsClient, LLMError

logger = logging.getLogger(__name__)
SYSTEM = (
    "你是电费查询小助手，只帮助用户查询宿舍当前剩余电量，用简洁中文回答。"
    "请根据用户的自然语言决定是否调用唯一工具 query_electricity，不要求固定命令或关键词。"
    "‘看看电费’、‘帮我看下宿舍电费’、‘还有多少电’通常都表示查询当前剩余电量。"
    "例如‘看看33楼4032宿舍电费’应调用 query_electricity，dormitory 为33#4032。"
    "‘/电费 33#4032’和‘/electricity 33#4032’也表示同一查询。"
    "用户要查当前电量，且楼号与房号都明确、只查询一间宿舍时，应调用工具。"
    "缺少楼号或房号时，请用户提供完整信息，不得猜测，也不得调用工具。"
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
                "还有多少电时使用。仅当楼号、房号明确时调用；缺少信息先询问。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dormitory": {"type": "string", "description": "楼号#房号，例如33#2035"},
                    "area": {"type": "string", "description": "可选的、用户明确确认的区域"},
                },
                "required": ["dormitory"],
                "additionalProperties": False,
            },
        },
    }
]


def reject_json_constant(_value: str):
    raise ValueError("不接受非标准 JSON 数字")


class LightAssistant:
    def __init__(self, settings: Settings, session):
        self.settings = settings
        self.model = ChatCompletionsClient(settings, session)
        self.electricity = ElectricityClient(settings, session)

    async def generate(self, task_kind: str, payload: str) -> str:
        if task_kind != "chat":
            return self._refusal()
        if not self.settings.llm_enabled:
            return "自然语言查询尚未配置模型服务，请在 .env 中填写 LLM_API_KEY 和 LLM_MODEL。"
        try:
            response = await self.model.complete(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}],
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
        if len(calls) != 1 or calls[0]["function"]["name"] != "query_electricity":
            return "每条消息只支持查询一个宿舍。"
        try:
            arguments = json.loads(
                calls[0]["function"]["arguments"], parse_constant=reject_json_constant
            )
            if (
                not isinstance(arguments, dict)
                or set(arguments) - {"dormitory", "area"}
                or not isinstance(arguments.get("dormitory"), str)
                or not 0 < len(arguments["dormitory"]) <= 80
                or re.fullmatch(r"[0-9]{1,3}#[0-9]{1,6}", arguments["dormitory"]) is None
                or not isinstance(arguments.get("area", ""), str)
                or len(arguments.get("area", "")) > 80
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return "请告诉我明确的楼号和房间号，例如33号楼2035室。"
        area = arguments.get("area", "").strip()

        try:
            result = await self.electricity.query(arguments["dormitory"], area=area)
        except ElectricityError as exc:
            return str(exc)
        return format_electricity(result)

    @staticmethod
    def _refusal() -> str:
        return "我是小电，电费查询小助手，只能查询宿舍当前剩余电量，其他问题暂时帮不了你。"
