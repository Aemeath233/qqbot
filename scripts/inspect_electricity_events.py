"""只读查看本地 QQ 消息/按钮任务，不输出消息正文、OpenID 或回调令牌。"""

import argparse
import json
import re
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


def read_operations(path, *, hours=3, limit=200, trace="", user="", group="", show_ids=False):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_operations'"
        ).fetchone():
            return []
        db.create_function("identity_tag", 1, tag)
        clauses, params = ["created_at>=?"], [time.time() - hours * 3600]
        for value, expression in (
            (trace, "trace=?"),
            (user, "identity_tag(sender_id)=?"),
            (group, "identity_tag(target_id)=?"),
        ):
            if value:
                clauses.append(expression)
                params.append(value)
        rows = db.execute(
            "SELECT * FROM bot_operations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        events = []
        for row in reversed(rows):
            event = {
                "时间": datetime.fromtimestamp(row["created_at"], SHANGHAI).isoformat(
                    timespec="milliseconds"
                ),
                "追踪号": row["trace"],
                "阶段": row["stage"],
                "场景": row["kind"],
                "群或私聊指纹": tag(row["target_id"]),
                "用户指纹": tag(row["sender_id"]),
                "详情": json.loads(row["details"]),
            }
            if show_ids:
                event["群或私聊OpenID"] = row["target_id"]
                event["用户OpenID"] = row["sender_id"]
            events.append(event)
        return events


def fingerprint(value):
    if re.fullmatch(r"[a-f0-9]{10}", value) is None:
        raise argparse.ArgumentTypeError("请输入日志里的 10 位小写十六进制指纹/追踪号")
    return value


def main():
    parser = argparse.ArgumentParser(description="只读检查最近查询、点击和绑定确认任务")
    parser.add_argument("--hours", type=int, choices=range(1, 169), default=3, metavar="1-168")
    parser.add_argument("--limit", type=int, choices=range(1, 501), default=100, metavar="1-500")
    parser.add_argument(
        "--operations", action="store_true", help="查看详细操作审计（升级后开始记录）"
    )
    parser.add_argument("--trace", type=fingerprint, default="", help="按追踪号筛选详细操作")
    parser.add_argument("--user", type=fingerprint, default="", help="按用户指纹筛选详细操作")
    parser.add_argument("--group", type=fingerprint, default="", help="按群/私聊指纹筛选详细操作")
    parser.add_argument(
        "--show-ids", action="store_true", help="详细操作中显示本地保存的平台 OpenID"
    )
    args = parser.parse_args()
    if not args.operations and (args.trace or args.user or args.group or args.show_ids):
        parser.error("筛选操作和 --show-ids 需要同时指定 --operations")
    try:
        settings = Settings.load(require_qq=False, require_llm=False)
        events = (
            read_operations(
                settings.db_path,
                hours=args.hours,
                limit=args.limit,
                trace=args.trace,
                user=args.user,
                group=args.group,
                show_ids=args.show_ids,
            )
            if args.operations
            else read_events(settings.db_path, hours=args.hours, limit=args.limit)
        )
        print(
            f"只读查看：最近 {args.hours} 小时，最多 {args.limit} 条；实际找到 {len(events)} 条。"
        )
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        if args.operations:
            print(
                "详细审计在升级后开始记录；相同追踪号串联回调、工具和回复。"
                "binding_requested=提出确认，binding_saved=实际保存。"
                "button_received 记录 QQ 上报字段摘要，不能证明屏幕上的实际点击。"
            )
            print(
                "保留最近7天、最多50000条；默认脱敏，--show-ids 显示的是平台 OpenID，并非 QQ 号。"
            )
            return
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
