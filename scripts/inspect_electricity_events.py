"""只读查看本地 QQ 消息/按钮任务，不输出消息正文、OpenID 或回调令牌。"""

import argparse
import json
import sqlite3
import time
from datetime import datetime
from hashlib import sha256

from qqbot.commands import SHANGHAI
from qqbot.config import ConfigurationError, Settings


def tag(value):
    return sha256(str(value).encode()).hexdigest()[:10] if value else "未记录"


def read_events(path, *, hours=3, limit=100):
    # mode=ro：路径写错也不会创建空数据库。
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("PRAGMA table_info(replies)")}
        if not {"created_at", "kind", "target_id", "task_kind", "state"} <= columns:
            raise ValueError("数据库缺少任务记录字段，无法读取。")
        rows = db.execute(
            "SELECT * FROM replies WHERE created_at>=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (time.time() - hours * 3600, limit),
        ).fetchall()
        events = []
        for row in reversed(rows):
            dormitory = ""
            if "task_payload" in columns and row["task_kind"] != "chat":
                try:
                    payload = json.loads(row["task_payload"])
                    if isinstance(payload, dict) and isinstance(payload.get("dormitory"), str):
                        dormitory = payload["dormitory"]
                except (ValueError, TypeError):
                    pass
            events.append(
                {
                    "时间": datetime.fromtimestamp(row["created_at"], SHANGHAI).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "场景": row["kind"],
                    "群或私聊指纹": tag(row["target_id"]),
                    "用户指纹": tag(row["sender_id"] if "sender_id" in columns else ""),
                    "任务": row["task_kind"],
                    "宿舍": dormitory,
                    "任务状态": row["state"],
                }
            )
        return events


def main():
    parser = argparse.ArgumentParser(description="只读检查最近查询、点击和绑定确认任务")
    parser.add_argument("--hours", type=int, choices=range(1, 25), default=3, metavar="1-24")
    parser.add_argument("--limit", type=int, choices=range(1, 501), default=100, metavar="1-500")
    args = parser.parse_args()
    try:
        settings = Settings.load(require_qq=False, require_llm=False)
        events = read_events(settings.db_path, hours=args.hours, limit=args.limit)
        print(
            f"只读查看：最近 {args.hours} 小时，最多 {args.limit} 条；实际找到 {len(events)} 条。"
        )
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        print(
            "chat=普通消息；button_query=再次查询；button_bind=点击绑定；"
            "button_confirm_bind=点击是；button_cancel_bind=点击否。"
        )
        print(
            "指纹相同可用于对照同一群/用户，但不能反推出 QQ 号或昵称。"
            "任务记录表示程序收到的事件，不能证明手机实际按了哪个位置。"
            "旧版被拒绝的点击和超一天已清理的任务可能没有保留。"
        )
    except (ConfigurationError, OSError, sqlite3.Error, ValueError) as exc:
        detail = (
            str(exc) if isinstance(exc, (ConfigurationError, ValueError)) else type(exc).__name__
        )
        parser.exit(1, f"查看失败：{detail}\n")


if __name__ == "__main__":
    main()
