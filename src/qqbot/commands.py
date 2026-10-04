"""消息任务数据和北京时间常量。"""

from dataclasses import dataclass
from datetime import timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ReplyTask:
    kind: str
    content: str
