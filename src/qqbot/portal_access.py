"""用户网页工具：绑定服务端身份，链接和发布结果由代码生成。"""

import re
from datetime import datetime
from decimal import Decimal

from qqbot.commands import SHANGHAI
from qqbot.dorms import DormDirectory, DormError
from qqbot.electricity_history import meter_key
from qqbot.portal_store import PortalError
from qqbot.tools import FunctionTool
from qqbot.user_store import UserDataError


def webpage_request(text):
    return bool(
        re.search(
            r"(?:帮我|给我|请|想要|我要|能不能|可以|做|制作|生成|创建|设计|写|搭建).{0,25}(?:做|制作|生成|创建|设计|写|搭建).{0,30}(?:网页|页面|网站|HTML)|^(?:做|制作|生成|创建|设计|写|搭建).{0,30}(?:网页|页面|网站|HTML)",
            text,
            re.I,
        )
    )


def curve_request(text):
    return any(word in text for word in ("用电曲线", "电量曲线", "电费曲线"))


class PortalAccess:
    def __init__(self, settings, store, users, context, history, *, request_id=""):
        self.settings, self.store, self.users, self.context, self.history = (
            settings,
            store,
            users,
            context,
            history,
        )
        self.owner = users.identity(context) or ""
        self.request_id = request_id
        self.cached = {}

    def available(self):
        if (
            not self.settings.portal_enabled
            or self.settings.dry_run
            or not self.settings.public_base_url
        ):
            raise PortalError("用户网页尚未启用，请联系管理员配置公开域名并开启功能。")
        self.store.owner(self.owner)

    async def curve(self, days=30, dormitory="", area="", minutes=0):
        self.available()
        if minutes not in {0, 30, 60}:
            raise PortalError("链接有效期支持30或60分钟。")
        minutes = minutes or self.settings.curve_link_minutes
        key = ("curve", days, dormitory, area, minutes)
        if key in self.cached:
            return self.cached[key]
        rows = self.history._rows(days, dormitory, area)
        if rows:
            identifier = rows[0]["meter_key"]
        else:
            target = dormitory or self.history.profile.get("dormitory", "")
            selected = (
                area
                or self.history.profile.get("area", "")
                or self.settings.electricity_default_area
            )
            if not target:
                raise PortalError("请先绑定宿舍或查询一次电量，再生成用电曲线。")
            try:
                room = DormDirectory.load(self.settings.electricity_map_path).resolve(
                    target, selected
                )
            except DormError as exc:
                raise PortalError(str(exc)) from None
            identifier = meter_key(
                self.settings.electricity_school_code,
                self.settings.electricity_pay_project,
                room.area,
                room.roomverify,
            )
        link, token = self.store.link(
            self.owner, "curve", identifier, {"days": days}, ttl=minutes * 60
        )
        result = {
            "ok": True,
            "kind": "curve",
            "url": f"{self.settings.public_base_url}/u/{link}#{token}",
            "minutes": minutes,
            "days": days,
        }
        self.cached[key] = result
        return result

    async def webpage(self, title, html):
        self.available()
        if "page" in self.cached:
            return self.cached["page"]
        identifier = self.store.draft(self.owner, title, html, self.request_id)
        self.store.publish(self.owner, identifier, self.settings.page_visibility)
        if self.settings.page_visibility == "public":
            url = f"{self.settings.public_base_url}/p/{identifier}"
        else:
            link, token = self.store.link(self.owner, "page", identifier, ttl=3600)
            url = f"{self.settings.public_base_url}/v/{link}#{token}"
        result = {
            "ok": True,
            "kind": "page",
            "id": identifier,
            "title": title,
            "url": url,
            "visibility": self.settings.page_visibility,
        }
        self.cached["page"] = result
        return result

    def action(self, action, identifier=""):
        self.store.owner(self.owner)
        if action == "list":
            pages = self.store.list_pages(self.owner)
            if not pages:
                return "你在当前聊天范围还没有创建网页。"
            return (
                "你的网页：\n"
                + "\n".join(
                    f"{page['id']} · {page['title']} · {page['visibility']}" for page in pages
                )
                + "\n使用 /撤回网页 ID 或 /删除网页 ID 管理自己的页面。"
            )
        if action == "revoke_curve":
            self.store.revoke(self.owner, kind="curve")
            return "已撤销你在当前聊天范围生成的全部曲线访问链接。"
        if action in {"retract", "delete"}:
            self.store.retract(self.owner, identifier, delete=action == "delete")
            return (
                "已删除这个网页和它的访问链接。"
                if action == "delete"
                else "已撤回网页，公开路径与旧访问链接已失效。"
            )
        if action == "preview":
            self.available()
            self.store.page(identifier, owner=self.owner)
            link, token = self.store.link(self.owner, "page", identifier, ttl=3600)
            return (
                "网页预览链接（有效期一小时，持有链接可访问）：\n"
                f"{self.settings.public_base_url}/v/{link}#{token}"
            )
        raise PortalError("网页管理指令不受支持。")

    def tools(self, *, allow_create=False):
        tools = [
            FunctionTool(
                "electricity_chart",
                "读取当前用户自己的已存电量台账，生成免登录的用电曲线链接；不请求校园接口。链接持有者可查看，有效期30或60分钟。用户明确要曲线或图表时使用。",
                {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "minimum": 1, "maximum": 365},
                        "dormitory": {"type": "string", "maxLength": 64},
                        "area": {"type": "string", "maxLength": 64},
                        "minutes": {"type": "integer", "enum": [30, 60]},
                    },
                    "additionalProperties": False,
                },
                self.curve,
                full_schema=True,
            )
        ]
        if allow_create:
            tools.append(
                FunctionTool(
                    "create_webpage",
                    "用户已明确要求生成网页。制作并公开一个自包含HTML页面，CSS/JS内联；不要放密钥、外部脚本、网络请求、iframe、表单提交或需要服务器执行的代码。只支持简单网页，最多18000字。",
                    {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 80},
                            "html": {"type": "string", "minLength": 1, "maxLength": 18000},
                        },
                        "required": ["title", "html"],
                        "additionalProperties": False,
                    },
                    self.webpage,
                    full_schema=True,
                    arguments_limit=100000,
                )
            )
        return tools


def portal_reply(result):
    if not result.get("ok"):
        return result.get("message", "网页生成失败，请稍后重试。")
    if result["kind"] == "curve":
        return (
            f"最近{result['days']}天的电量曲线：\n{result['url']}\n"
            f"链接{result['minutes']}分钟后失效，持有链接即可查看；"
            "打开网页不查询校园接口。\n用 /撤销曲线链接 可提前撤销。"
        )
    visibility = "已公开" if result["visibility"] == "public" else "已生成一小时访问链接"
    return (
        f"网页“{result['title']}”{visibility}：\n{result['url']}\n"
        f"网页ID：{result['id']}\n用 /撤回网页 {result['id']} 可撤回。"
    )


def curve_payload(rows, days):
    points = sorted({(row["observed_at"], row["quantity"]) for row in rows})
    if len({stamp for stamp, _ in points}) != len(points):
        raise UserDataError("同一时间有不同电量读数，暂时不能可靠绘制曲线。")
    rises = sum(Decimal(b[1]) > Decimal(a[1]) for a, b in zip(points, points[1:], strict=False))
    change = (
        format(Decimal(points[0][1]) - Decimal(points[-1][1]), "f") if len(points) >= 2 else None
    )
    return {
        "days": days,
        "dormitory": rows[0]["dormitory"] if rows else "",
        "area_name": rows[0]["area_name"] if rows else "",
        "query_count": len(rows),
        "sample_count": len(points),
        "points": [
            {"at": datetime.fromtimestamp(stamp, SHANGHAI).isoformat(), "kwh": amount}
            for stamp, amount in points
        ],
        "net_decrease_kwh": change,
        "has_increase": bool(rises),
        "note": "只显示实际查询读数。余额净变化不是完整耗电量；"
        "充值或余额修正、采样缺口会影响估算。",
    }
