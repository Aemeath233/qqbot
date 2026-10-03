"""同一bot域名下的个人曲线和静态网页，访问数据不触发校园请求。"""

import html
import json
import re
from importlib.resources import files
from urllib.parse import urlsplit

from aiohttp import web

from qqbot.portal_access import curve_payload
from qqbot.portal_store import ID, PortalError, PortalStore
from qqbot.user_store import UserDataError, UserStore

STYLE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-src 'none'"
)
FRAME_CSP = "sandbox allow-scripts; " + STYLE_CSP + "; frame-ancestors 'self'"
PORTAL_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
    "frame-src 'self' about:; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def headers(csp=PORTAL_CSP):
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
        "Content-Security-Policy": csp,
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }


def register_portal(app, settings):
    store = PortalStore(settings.portal_db_path)
    user_path = settings.db_path.with_name(
        f"{settings.db_path.stem}.userdata{settings.db_path.suffix}"
    )
    users = UserStore(user_path, namespace=settings.app_id)

    def check(request):
        if not settings.portal_enabled or settings.dry_run:
            raise web.HTTPNotFound()
        configured = urlsplit(settings.public_base_url)
        if request.host.casefold() != configured.netloc.casefold():
            raise web.HTTPNotFound()

    def page_response(title, content, mode="", identifier=""):
        source = files("qqbot").joinpath("resources/portal/index.html").read_text(encoding="utf-8")
        values = {
            "TITLE": html.escape(title),
            "CONTENT": content,
            "MODE": html.escape(mode, quote=True),
            "ID": html.escape(identifier, quote=True),
        }
        source = re.sub(r"\{\{(TITLE|CONTENT|MODE|ID)\}\}", lambda match: values[match[1]], source)
        return web.Response(text=source, content_type="text/html", headers=headers())

    async def home(request):
        check(request)
        return page_response(
            settings.bot_name + " · 用户网页",
            '<section class="card"><h2>从QQ聊天打开你的页面</h2>'
            "<p>发送 /用电曲线 查看自己的已存台账；发送 /做网页 描述需求，生成简单网页。</p>"
            "<p>曲线通过短期访问链接查看，用户生成的页面发布到独立路径。"
            "本站不列出其他用户的记录或网页。</p></section>",
        )

    async def asset(request):
        check(request)
        name = request.match_info["filename"]
        if name not in {"portal.css", "portal.js"}:
            raise web.HTTPNotFound()
        return web.Response(
            text=files("qqbot").joinpath("resources/portal", name).read_text(encoding="utf-8"),
            content_type="text/css" if name.endswith(".css") else "text/javascript",
            headers=headers(),
        )

    async def link_page(request):
        check(request)
        identifier = request.match_info["identifier"]
        if not ID.fullmatch(identifier):
            raise web.HTTPNotFound()
        mode = "curve" if request.path.startswith("/u/") else "preview"
        content = (
            '<section class="card"><p id="portal-state" role="status">正在验证访问链接…</p>'
            '<div id="curve-panel" hidden><div class="metrics" id="metrics"></div>'
            '<label>查看区间 <select id="days"><option value="3">最近3天</option>'
            '<option value="7">最近7天</option><option value="30" selected>最近30天</option>'
            '<option value="365">全部已授权区间</option></select></label>'
            '<div id="chart" role="img" aria-label="剩余电量曲线"></div>'
            '<p id="chart-note" class="muted"></p><details><summary>查看原始读数</summary>'
            '<div id="readings"></div></details></div>'
            '<iframe id="preview-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" '
            'title="用户网页预览" hidden></iframe></section>'
        )
        return page_response(
            "用电曲线" if mode == "curve" else "网页预览", content, mode, identifier
        )

    async def capability(request):
        check(request)
        origin = request.headers.get("Origin", "")
        if origin.rstrip("/").casefold() != settings.public_base_url.casefold():
            raise web.HTTPForbidden()
        raw = await request.read()
        if len(raw) > 2048:
            raise web.HTTPBadRequest()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) - {"token", "days"}:
                raise ValueError
            mode = request.match_info["mode"]
            if mode not in {"curve", "page"}:
                raise ValueError
            link = store.authorize(request.match_info["identifier"], payload.get("token"), mode)
            if mode == "page":
                page = store.page(link["target"], owner=link["owner"])
                body = {
                    "title": page["title"],
                    "html": '<meta http-equiv="Content-Security-Policy" '
                    f'content="{html.escape(STYLE_CSP, quote=True)}">' + page["content"],
                }
            else:
                if not settings.electricity_history_enabled:
                    raise PortalError("电费历史功能已关闭。")
                maximum = link["options"]["days"]
                days = payload.get("days", maximum)
                if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
                    raise ValueError
                days = min(days, maximum)
                rows = users._readings_by_identity(
                    link["owner"], days, meter_key=link["target"], limit=3000
                )
                body = {**curve_payload(rows, days), "maximum_days": maximum}
            return web.json_response({**body, "expires_at": link["expires_at"]}, headers=headers())
        except (PortalError, UserDataError, ValueError, KeyError, TypeError):
            return web.json_response(
                {"message": "访问链接无效、已过期或已撤销，或当前记录无法显示。"},
                status=404,
                headers=headers(),
            )

    async def published(request):
        check(request)
        try:
            page = store.page(request.match_info["identifier"], public=True)
        except PortalError:
            raise web.HTTPNotFound() from None
        identifier = html.escape(page["id"], quote=True)
        return page_response(
            page["title"],
            '<p class="muted">用户生成的公开网页 · 静态内容与浏览器交互</p>'
            '<iframe class="published-frame" sandbox="allow-scripts" '
            f'referrerpolicy="no-referrer" src="/_page-content/{identifier}" '
            f'title="{html.escape(page["title"], quote=True)}"></iframe>',
        )

    async def published_content(request):
        check(request)
        try:
            page = store.page(request.match_info["identifier"], public=True)
        except PortalError:
            raise web.HTTPNotFound() from None
        return web.Response(
            text=page["content"], content_type="text/html", headers=headers(FRAME_CSP)
        )

    app.router.add_get("/", home)
    app.router.add_get("/portal-assets/{filename}", asset)
    app.router.add_get("/u/{identifier}", link_page)
    app.router.add_get("/v/{identifier}", link_page)
    app.router.add_post("/_portal/{mode}/{identifier}", capability)
    app.router.add_get("/p/{identifier}", published)
    app.router.add_get("/_page-content/{identifier}", published_content)
