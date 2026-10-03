from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def split_bill(total: str, people: int) -> dict:
    """按用户给出的总金额和人数计算AA均分，只做计算，不付款。total以元计，最多两位小数。"""
    try:
        amount = Decimal(total)
        if not amount.is_finite() or not 0 <= amount <= 100000 or not 1 <= people <= 1000:
            raise ValueError
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("总金额需为0～100000元且最多两位小数，人数需为1～1000。") from None
    per_person = (amount / people).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "total_yuan": str(amount),
        "people": people,
        "per_person_yuan": str(per_person),
        "note": "每人金额四舍五入到分；最后一人可能需要调整尾差。",
    }


def format_duration(minutes: int) -> dict:
    """将用户给出的分钟数换算为小时和分钟，方便查看学习或活动时长。"""
    if not 0 <= minutes <= 1000000:
        raise ValueError("分钟数需为0～1000000。")
    hours, remainder = divmod(minutes, 60)
    return {"minutes": minutes, "formatted": f"{hours}小时{remainder}分钟"}
