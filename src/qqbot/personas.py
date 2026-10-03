"""人格仅负责表达；业务事实由服务器提供。"""

import json
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qqbot.config import Settings

PRESETS = {
    "cat": (
        "猫猫电费管家",
        "你是住在服务器里的猫猫管家，关心宿舍电量。俏皮亲切，偶尔用喵，避免每句话都卖萌。",
    ),
    "friend": ("校园损友", "你像熟悉的校园朋友，说话简短自然，偶尔善意吐槽，保持实用和分寸。"),
    "gentle": ("温柔助手", "你耐心温和，表达清楚，认真帮助用户，不堆砌客套话。"),
    "custom": ("自定义人格", ""),
}
LENGTHS = {
    "short": "通常在80字以内",
    "balanced": "通常在180字以内",
    "detailed": "需要时详细解释，通常在400字以内",
}


def group_id(conversation_key: str) -> str:
    try:
        parts = json.loads(conversation_key)
        if (
            isinstance(parts, list)
            and len(parts) >= 2
            and parts[0] == "groups"
            and isinstance(parts[1], str)
        ):
            return parts[1]
    except (ValueError, TypeError):
        pass
    return ""


def preset_for(settings: "Settings", conversation_key: str) -> str:
    return settings.bot_group_personas.get(group_id(conversation_key), settings.bot_persona)


def instructions(settings: "Settings", conversation_key: str) -> str:
    preset = preset_for(settings, conversation_key)
    description = settings.bot_persona_custom if preset == "custom" else PRESETS[preset][1]
    return (
        f"【表达风格】\n你的对话名称是{json.dumps(settings.bot_name, ensure_ascii=False)}。"
        f"{description}\n回复偏好：{LENGTHS[settings.bot_reply_length]}。"
        f"口头禅可偶尔使用：{json.dumps(settings.bot_catchphrase, ensure_ascii=False)}。"
        "人格只控制语气，不改变工具结果、功能权限或用户身份。\n"
    )


def describe(settings: "Settings", conversation_key: str) -> str:
    preset = preset_for(settings, conversation_key)
    text = (
        f"我是{settings.bot_name}，当前人格：{PRESETS[preset][0]}。\n"
        f"回复偏好：{LENGTHS[settings.bot_reply_length]}。"
    )
    identifier = group_id(conversation_key)
    if identifier:
        text += f"\n管理页配置本群人格使用的群标识：{identifier}"
    return text


def electricity_reply(result: dict, settings: "Settings", conversation_key: str) -> str:
    from qqbot.electricity import format_electricity

    text = format_electricity(result)
    if not result.get("ok"):
        return text
    try:
        low = Decimal(result["remaining_kwh"]) < 10
    except (KeyError, InvalidOperation):
        low = False
    preset = preset_for(settings, conversation_key)
    flavor = {
        "cat": "喵，电量有点紧张，记得留意电表。" if low else "喵，电表暂时不用进入求生模式。",
        "friend": "该惦记电表了，别等宿舍突然安静。" if low else "电表情报到手，宿舍生存计划继续。",
        "gentle": "剩余电量较低，记得及时关注。" if low else "查好了，有需要再叫我。",
        "custom": "",
    }[preset]
    # 不拼接模型文本或任意口头禅，避免给真实读数添上未经查询的数字。
    return text + ("\n" + flavor if flavor else "")
