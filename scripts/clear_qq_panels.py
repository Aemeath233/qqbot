"""一次性列出或清除 QQ 保存的指令面板，不修改机器人消息功能。"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp

from qqbot.api import QQAPI, QQAPIError
from qqbot.config import ConfigurationError, Settings

SCOPES = ("c2c", "group", "channel", "dm")


class PanelAPI(QQAPI):
    async def _decode(self, response):
        # 兼容旧版本 QQAPI：DELETE 也可能以 204 无响应体表示成功。
        if response.status == 204:
            return {}
        return await super()._decode(response)


class PanelError(Exception):
    pass


class RequestPacer:
    def __init__(self):
        self.last = {}

    async def wait(self, kind):
        interval = 2.1 if kind == "read" else 6.1
        remaining = self.last.get(kind, -interval) + interval - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        self.last[kind] = time.monotonic()


async def collect_panels(api, scopes, pacer):
    records = {}
    for scope in scopes:
        cursor = ""
        seen = set()
        for _ in range(25):
            params = {"scope": scope, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            await pacer.wait("read")
            page = await api.request("GET", "/v2/panels?" + urlencode(params))
            items = page.get("records")
            if not isinstance(items, list):
                raise PanelError("指令面板列表响应格式异常；未执行删除。")
            for item in items:
                if not isinstance(item, dict):
                    raise PanelError("面板记录格式异常；未执行删除。")
                panel_id = item.get("panel_id")
                if not isinstance(panel_id, str) or not 0 < len(panel_id) <= 256:
                    raise PanelError("面板 ID 格式异常；未执行删除。")
                if item.get("scope") != scope:
                    raise PanelError("面板场景与请求不一致；未执行删除。")
                records[panel_id] = item
            cursor = page.get("next_cursor", "")
            if not isinstance(cursor, str):
                raise PanelError("分页游标格式异常；未执行删除。")
            if page.get("is_end") is True or not cursor:
                break
            if cursor in seen:
                raise PanelError("接口返回重复分页游标；未执行删除。")
            seen.add(cursor)
        else:
            raise PanelError("面板列表超过预期分页数量；未执行删除。")
    return list(records.values())


async def clear_panels(api, records, pacer, *, backup_dir):
    # 所有面板详情（包括关联对象）均取得并写入备份后，才发送第一条删除请求。
    details = []
    for record in records:
        await pacer.wait("read")
        detail = await api.request("GET", "/v2/panels/" + quote(record["panel_id"], safe=""))
        if detail.get("panel_id") != record["panel_id"]:
            raise PanelError("面板详情 ID 不一致；未执行删除。")
        details.append(detail)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ("panels-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"panels": details}, output, ensure_ascii=False, indent=2)
    print(f"配置备份：{backup.resolve()}", flush=True)
    removed = 0
    for record in records:
        await pacer.wait("delete")
        try:
            await api.request("DELETE", "/v2/panels/" + quote(record["panel_id"], safe=""))
        except QQAPIError as exc:
            if exc.code != "40030006":
                # 一旦失败即停止，不对其他面板继续盲目删除，也不自动重试。
                raise PanelError(f"已删除 {removed} 个面板，随后遇到错误，已停止：{exc}") from None
            print("一个面板已不存在，无需重复删除。", flush=True)
            continue
        removed += 1
        print(f"已删除第 {removed} 个面板（场景：{record['scope']}）。", flush=True)
    return removed


async def run(args):
    settings = Settings.load(require_llm=False)
    scopes = SCOPES if args.scope == "all" else (args.scope,)
    pacer = RequestPacer()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        api = PanelAPI(settings, session)
        records = await collect_panels(api, scopes, pacer)
        print(f"找到 {len(records)} 个指令面板。", flush=True)
        if not records:
            print("未发送删除请求。若 QQ 仍显示旧指令，请检查旧版平台指令配置或客户端缓存。")
            return
        for record in records:
            panel = record.get("panel")
            items = panel.get("items", []) if isinstance(panel, dict) else []
            names = [str(item.get("name", "")) for item in items if isinstance(item, dict)]
            print(json.dumps({"scope": record["scope"], "commands": names}, ensure_ascii=False))
        if not args.apply:
            print("仅查看，未修改配置。添加 --apply 才会备份并删除这些面板。")
            return
        removed = await clear_panels(api, records, pacer, backup_dir=Path("data/qq-panels-backup"))
        print(f"清理完成：删除 {removed} 个面板。请重新进入 QQ 会话检查指令列表。")


def main():
    parser = argparse.ArgumentParser(description="清理当前 QQ AppID 在平台保存的旧指令面板")
    parser.add_argument("--scope", choices=("all", *SCOPES), default="group", help="默认只处理群聊")
    parser.add_argument("--apply", action="store_true", help="先备份配置，再删除所选场景的全部面板")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (
        ConfigurationError,
        PanelError,
        QQAPIError,
        aiohttp.ClientError,
        TimeoutError,
        OSError,
    ) as exc:
        detail = (
            str(exc)
            if isinstance(exc, (ConfigurationError, PanelError, QQAPIError))
            else type(exc).__name__
        )
        print(f"清理未完成：{detail}。请查看上方输出确认是否已删除部分面板。", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("已中断；部分面板可能已被删除，备份保存在 data/qq-panels-backup。", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
