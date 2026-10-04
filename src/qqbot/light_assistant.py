"""自然语言入口只允许调用一个工具：查宿舍当前剩余电量。"""

import json
import logging
import re

from qqbot.config import Settings
from qqbot.electricity import ElectricityClient, ElectricityError, format_electricity
from qqbot.llm import ChatCompletionsClient, LLMError

logger = logging.getLogger(__name__)
SYSTEM = (
    "你叫小电，是友好、简洁的宿舍电费查询小助手，只查询当前剩余电量。"
    "遇到其他问题要礼貌拒绝，不闲聊，不扮演其他角色。唯一可用的工具是 query_electricity。"
    "用户询问当前电量时，楼号与房间号都明确且房间唯一时才调用工具；"
    "用户没说房间时，先用中文询问楼号和房间号，不得猜测或调用工具。"
    "用户说‘19号楼312’时传19#312；不能把19312擅自拆成楼号和房号。"
    "有多个区域候选时先询问区域，不能猜。工具返回以度为单位的真实剩余电量，不是金额。"
    "本机器人不回答电费之外的问题，也不提供历史分析、耗电量估算、预测或图表。"
    "不得编造、换算或改写工具返回的电量；工具失败时如实说明。"
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_electricity",
            "description": "查询一个明确宿舍当前的剩余电量，单位为度。",
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
ELECTRICITY_INTENT = re.compile(r"电费|电量|用电|电表|宿舍|房间|房号|余额|剩余电")
ROOM_REFERENCE = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:号楼|楼|栋|#)\s*(\d{1,5})(?!\d)"
)
UNSUPPORTED_REQUEST = re.compile(
    r"最近.{0,8}(?:天|周|月)|过去.{0,8}(?:天|周|月)|历史|统计|曲线|分析|"
    r"用了多少|耗电|预测|昨天|前天|上周|上个月|以前|消耗|使用了|少了|"
    r"多少钱|价格|费用|缴费|充值|天气|新闻|翻译|写作|代码|作业|位置|地址|路线|谁|姓名|"
    r"什么时候|怎么.{0,4}(?:交|缴|充值)"
)
CURRENT_QUERY = re.compile(
    r"(?:查|查询).{0,20}(?:电费|电量|余额|剩余|用电)|还有多少|还有没有电|还剩|剩余电|"
    r"余额|几度|多少.{0,3}电|电.{0,4}(?:多少|几度|够)|当前电量|现在电量|有电吗"
)


def reject_json_constant(_value: str):
    raise ValueError("不接受非标准 JSON 数字")


class LightAssistant:
    def __init__(self, settings: Settings, session):
        self.settings = settings
        self.model = ChatCompletionsClient(settings, session)
        self.electricity = ElectricityClient(settings, session)

    async def generate(
        self, task_kind: str, payload: str, _context: str = "", *, request_id: str = ""
    ) -> str:
        if task_kind != "chat":
            return self._refusal()
        if not self.settings.llm_enabled:
            return "自然语言查询尚未配置模型服务，请联系管理员。"
        room_mentioned = ROOM_REFERENCE.search(payload) is not None
        if (
            not CURRENT_QUERY.search(payload)
            or not (ELECTRICITY_INTENT.search(payload) or room_mentioned)
        ):
            return self._refusal()
        if UNSUPPORTED_REQUEST.search(payload):
            return self._refusal()

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
            if not CURRENT_QUERY.search(payload):
                return self._refusal()
            rooms = list(ROOM_REFERENCE.finditer(payload))
            if not rooms:
                return "请明确告诉我楼号和房间号，例如“查一下33号楼2035室还有多少电”。"
            if len(rooms) > 1:
                return "一次只能查一间宿舍，请只提供一个楼号和房间号。"
            return "请补充明确的区域，或把查询写成“查一下33号楼2035室还有多少电”。"
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
                or re.fullmatch(r"\d{1,4}#\d{1,5}", arguments["dormitory"]) is None
                or not isinstance(arguments.get("area", ""), str)
                or len(arguments.get("area", "")) > 80
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return "请告诉我明确的楼号和房间号，例如33号楼2035室。"
        rooms = list(ROOM_REFERENCE.finditer(payload))
        room = rooms[0] if len(rooms) == 1 else None
        building, room_number = arguments["dormitory"].split("#", maxsplit=1)
        if room is None or room.groups() != (building, room_number):
            return "为了避免查错，请在消息里明确写出楼号和房间号，例如33号楼2035室。"
        area = arguments.get("area", "").strip()
        if area:
            if area.isdigit():
                mentioned = re.search(rf"(?<!\d){re.escape(area)}(?!\d)", payload) is not None
            else:
                mentioned = area.casefold() in payload.casefold()
            if not mentioned:
                return "我还不确定查询区域，请明确告诉我你选择的区域名称或编号。"

        try:
            result = await self.electricity.query(
                arguments["dormitory"], area=area
            )
        except ElectricityError as exc:
            return str(exc)
        return format_electricity(result)

    @staticmethod
    def _refusal() -> str:
        return "我是小电，电费查询小助手，只能查询宿舍当前剩余电量，其他问题暂时帮不了你。"
