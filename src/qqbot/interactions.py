"""识别明确的资料登记和常见历史请求，不依赖模型授权写入。"""

import re
import unicodedata


def dorm_reference(text: str) -> tuple[str, str]:
    text = unicodedata.normalize("NFKC", text).strip()
    patterns = (
        r"([0-9]{1,3})\s*#\s*([0-9]{1,6})(?:\s+(.+))?",
        r"([0-9]{1,3})\s*(?:号楼|楼|栋|号公寓)\s*([0-9]{1,6})\s*(?:宿舍|房间|号房|室)?(?:\s+(.+))?",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return f"{match[1]}#{match[2]}", (match[3] or "").strip()
    raise ValueError("请提供楼号和房号，例如33#2035或33号楼2035宿舍；需要时在后面加区域。")


def profile_request(text: str) -> dict | None:
    text = text.strip().removesuffix("。").removesuffix("！")
    nickname = re.fullmatch(
        r"(?:以后叫我|叫我|记住我叫|记住(?:我的)?昵称(?:是|为)?)[：:\s]*(.+)", text
    )
    if nickname:
        return {"action": "nickname", "value": nickname[1].strip()}
    dorm = re.fullmatch(r"(?:绑定(?:我的)?宿舍|记住(?:我的)?宿舍(?:是|为)?)[：:\s]*(.+)", text)
    if dorm:
        return {"action": "dorm", "value": dorm[1].strip()}
    dorm = re.fullmatch(r"(?:我的宿舍是|我住)(.+?)[，,\s]*(?:请)?(?:帮我)?记住", text)
    if dorm:
        return {"action": "dorm", "value": dorm[1].strip()}
    if text in {"我的记忆", "你记住了什么", "你记住了我什么"}:
        return {"action": "show"}
    if text in {"忘记我", "清除我的记忆"}:
        return {"action": "forget"}
    return None


def history_request(text: str) -> tuple[str, int] | None:
    text = text.strip().rstrip("？?。！!")
    match = re.fullmatch(
        r"(?:我(?:的)?|帮我(?:看看|查查|查一下)?)?最近([0-9]{1,3}|[一二三四五六七八九十两]{1,3})天"
        r"(?:用了多少电|用电量(?:是多少)?|用电情况|电量变化|电费历史|查询记录)",
        text,
    )
    if not match:
        return None
    value = match[1]
    numbers = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value.isascii() and value.isdigit():
        days = int(value)
    elif value in numbers:
        days = numbers[value]
    elif "十" in value:
        left, right = value.split("十", 1)
        if left not in {*numbers, ""} or right not in {*numbers, ""}:
            return None
        days = numbers.get(left, 1) * 10 + numbers.get(right, 0)
    else:
        return None
    return ("history" if match[0].endswith(("历史", "记录")) else "usage"), days
