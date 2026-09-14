import hmac
import json
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .forecasts import ForecastConflictError
from .operations import OperationBusy
from .update_check import RELEASES_PAGE


def resolve_static_root(module_file, bundle_root=None):
    """Resolve local web assets in source and PyInstaller-frozen layouts."""
    if bundle_root is not None:
        return Path(bundle_root) / "yuanjian_app" / "static"
    return Path(module_file).with_name("static")


STATIC_ROOT = resolve_static_root(Path(__file__), getattr(sys, "_MEIPASS", None))


# 所有响应统一附带的安全头。
# Referrer-Policy 尤其重要：会话令牌通过 URL query 交给前端（pywebview 注入
# ?token=），任何形式的 referrer 外泄都等于直接泄漏令牌。
# frame-ancestors / base-uri / object-src / form-action 用来补全 CSP 的默认缺口
# ——只写 default-src 时，这几项并不会被完整覆盖。
SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)


# 静态资源白名单注册表：URL 路径 -> (磁盘相对路径, MIME 类型)。
# 新增前端文件必须逐个登记，未登记路径一律 404；字体文件由后端投放，先预登记。
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/css/tokens.css": ("css/tokens.css", "text/css; charset=utf-8"),
    "/css/base.css": ("css/base.css", "text/css; charset=utf-8"),
    "/css/layout.css": ("css/layout.css", "text/css; charset=utf-8"),
    "/css/components.css": ("css/components.css", "text/css; charset=utf-8"),
    "/css/views.css": ("css/views.css", "text/css; charset=utf-8"),
    "/js/icons.js": ("js/icons.js", "text/javascript; charset=utf-8"),
    "/js/ui_core.js": ("js/ui_core.js", "text/javascript; charset=utf-8"),
    "/js/api.js": ("js/api.js", "text/javascript; charset=utf-8"),
    "/js/router.js": ("js/router.js", "text/javascript; charset=utf-8"),
    "/js/app.js": ("js/app.js", "text/javascript; charset=utf-8"),
    "/js/views/today.js": (
        "js/views/today.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/tell.js": (
        "js/views/tell.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/calib.js": (
        "js/views/calib.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/sources.js": (
        "js/views/sources.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/sources-form.js": (
        "js/views/sources-form.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/diag.js": ("js/views/diag.js", "text/javascript; charset=utf-8"),
    "/js/views/settings.js": (
        "js/views/settings.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/notifications.js": (
        "js/views/notifications.js",
        "text/javascript; charset=utf-8",
    ),
    "/js/views/cluster.js": (
        "js/views/cluster.js",
        "text/javascript; charset=utf-8",
    ),
    "/fonts/JetBrainsMono-Regular.woff2": (
        "fonts/JetBrainsMono-Regular.woff2",
        "font/woff2",
    ),
    "/fonts/JetBrainsMono-Bold.woff2": (
        "fonts/JetBrainsMono-Bold.woff2",
        "font/woff2",
    ),
}


@dataclass(frozen=True)
class Route:
    """一条 API 路由，取代原先 ``do_*`` 里的 if/elif 长链。

    三种匹配形态（由 `kind` 选择），与旧实现一一对应：

    - ``"exact"``：请求路径与 `path` 完全相等。对应 ``path == ...``。
    - ``"prefix"``：请求路径以 `path` 开头，其后**整段剩余**（可为空、可含
      ``/``）作为参数交给处理器。对应 ``path.startswith(...)`` 加
      ``path.removeprefix(...)``。
    - ``"prefix_suffix"``：请求路径**既**以 `path` 开头**又**以 `suffix` 结尾，
      中间那段作为参数。对应旧实现的
      ``path.startswith(P) and path.endswith(S)`` 加上
      ``path.removeprefix(P).removesuffix(S)``——注意提取用的是"剩余段是否以 S
      结尾"，与进入分支的判据并不完全相同，这里如实复刻，不做"顺手修正"。

    匹配到的参数值**保持 URL 编码原样**，由处理器自行 ``unquote``。这样做的
    原因是旧实现各路由对参数的处理并不一致（GET 的 ``/api/forecasts/<id>``
    不 ``rstrip("/")``，POST 的 ``<id>/versions`` 会），统一在这里解码会把差异
    抹平，正是"行为不完全等价"的来源。

    `endpoint` 是 Handler 上的处理器方法名；处理器统一签名
    ``(services, params, parsed, payload)``，GET/DELETE 的 `payload` 为 None。
    """

    method: str
    kind: str
    path: str
    endpoint: str
    param: str = "rest"
    suffix: str = ""

    def __post_init__(self):
        if self.kind not in ("exact", "prefix", "prefix_suffix"):
            raise ValueError(f"未知的路由类型: {self.kind}")
        if self.kind == "prefix_suffix" and not self.suffix:
            # 空后缀会让 `rest[: -len(suffix)]` 变成 `rest[:0]`，静默把参数清空。
            raise ValueError("prefix_suffix 路由必须给出非空 suffix")

    def match(self, request_path):
        """匹配成功返回参数字典（可能为空 dict），失败返回 None。"""
        if self.kind == "exact":
            return {} if request_path == self.path else None
        if self.kind == "prefix":
            if not request_path.startswith(self.path):
                return None
            return {self.param: request_path[len(self.path):]}
        if not (request_path.startswith(self.path) and request_path.endswith(self.suffix)):
            return None
        rest = request_path[len(self.path):]
        if rest.endswith(self.suffix):
            rest = rest[: -len(self.suffix)]
        return {self.param: rest}


# 路由表：**声明顺序即匹配优先级**，与旧 if/elif 链的求值顺序逐条对齐。
# 精确路由必须排在会吞掉它们的 prefix 路由之前（例如 /api/forecasts/progress
# 要排在 /api/forecasts/<id> 之前），这一点由本表的书写顺序保证。
ROUTES = (
    # ---------------- GET ----------------
    Route("GET", "exact", "/api/app/version", "_get_app_version"),
    Route("GET", "exact", "/api/forecasts", "_get_forecasts"),
    Route("GET", "exact", "/api/forecasts/progress", "_get_forecast_progress"),
    Route("GET", "exact", "/api/forecasts/overdue", "_get_forecast_overdue"),
    Route("GET", "exact", "/api/interests", "_get_interests"),
    Route("GET", "exact", "/api/interests/objects", "_get_interest_objects"),
    Route("GET", "exact", "/api/signals", "_get_signals"),
    Route("GET", "exact", "/api/knowledge/vaults", "_get_knowledge_vaults"),
    Route("GET", "exact", "/api/knowledge/documents", "_get_knowledge_documents"),
    Route("GET", "exact", "/api/external/radar", "_get_external_radar"),
    Route("GET", "exact", "/api/external/sources", "_get_external_sources"),
    Route("GET", "exact", "/api/external/rules", "_get_external_rules"),
    Route("GET", "exact", "/api/cognition/status", "_get_cognition_status"),
    Route("GET", "exact", "/api/cognition/candidates", "_get_cognition_candidates"),
    Route("GET", "exact", "/api/risk-dashboard", "_get_risk_dashboard"),
    Route("GET", "exact", "/api/cognition/clusters", "_get_cognition_clusters"),
    Route(
        "GET",
        "exact",
        "/api/cognition/trends",
        "_get_cognition_trends",
    ),
    Route("GET", "exact", "/api/cognition/jobs", "_get_cognition_jobs"),
    Route("GET", "exact", "/api/notifications", "_get_notifications"),
    Route("GET", "exact", "/api/settings/startup", "_get_settings_startup"),
    Route("GET", "exact", "/api/settings/ai", "_get_settings_ai"),
    Route("GET", "exact", "/api/calibration", "_get_calibration"),
    Route("GET", "exact", "/api/diagnostics", "_get_diagnostics"),
    Route("GET", "exact", "/api/settings/backup", "_get_settings_backup"),
    Route("GET", "exact", "/api/settings/retention", "_get_settings_retention"),
    Route("GET", "exact", "/api/settings/learning", "_get_settings_learning"),
    Route("GET", "exact", "/api/score", "_get_score"),
    # 前缀路由放在该方法的最后，等价于旧链里的最后几个 startswith 分支。
    Route(
        "GET",
        "prefix",
        "/api/cognition/clusters/",
        "_get_cluster_detail",
        param="cluster_id",
    ),
    Route(
        "GET",
        "prefix",
        "/api/forecasts/",
        "_get_forecast",
        param="forecast_id",
    ),
    # ---------------- POST ----------------
    Route("POST", "exact", "/api/update-check", "_post_update_check"),
    Route("POST", "exact", "/api/events", "_post_events"),
    Route("POST", "exact", "/api/shutdown", "_post_shutdown"),
    Route("POST", "exact", "/api/window/show", "_post_window_show"),
    Route("POST", "exact", "/api/monitoring/toggle", "_post_monitoring_toggle"),
    Route("POST", "exact", "/api/knowledge/index", "_post_knowledge_index"),
    Route("POST", "exact", "/api/external/sources", "_post_external_sources"),
    Route(
        "POST",
        "exact",
        "/api/external/sources/import-opml",
        "_post_external_sources_import_opml",
    ),
    Route(
        "POST",
        "exact",
        "/api/external/sources/bulk-enabled",
        "_post_external_sources_bulk_enabled",
    ),
    Route(
        "POST",
        "prefix_suffix",
        "/api/external/sources/",
        "_post_source_enabled",
        param="source_id",
        suffix="/enabled",
    ),
    Route("POST", "exact", "/api/external/rules", "_post_external_rules"),
    Route(
        "POST",
        "prefix_suffix",
        "/api/external/rules/",
        "_post_rule_enabled",
        param="rule_id",
        suffix="/enabled",
    ),
    Route("POST", "exact", "/api/interests/objects", "_post_interest_objects"),
    Route("POST", "exact", "/api/interests/links", "_post_interest_links"),
    Route("POST", "exact", "/api/external/refresh", "_post_external_refresh"),
    Route("POST", "exact", "/api/cognition/run", "_post_cognition_run"),
    Route(
        "POST",
        "prefix_suffix",
        "/api/cognition/candidates/",
        "_post_candidate_confirm",
        param="impact_id",
        suffix="/confirm",
    ),
    Route(
        "POST",
        "prefix_suffix",
        "/api/cognition/clusters/",
        "_post_cluster_feedback",
        param="cluster_id",
        suffix="/feedback",
    ),
    Route(
        "POST",
        "exact",
        "/api/notifications/read-all",
        "_post_notifications_read_all",
    ),
    Route(
        "POST",
        "prefix_suffix",
        "/api/notifications/",
        "_post_notification_read",
        param="notification_id",
        suffix="/read",
    ),
    Route("POST", "exact", "/api/settings/startup", "_post_settings_startup"),
    Route("POST", "exact", "/api/settings/ai", "_post_settings_ai"),
    Route(
        "POST",
        "exact",
        "/api/export/mobile-summary",
        "_post_export_mobile_summary",
    ),
    Route("POST", "exact", "/api/forecasts", "_post_forecasts"),
    Route(
        "POST",
        "prefix_suffix",
        "/api/forecasts/",
        "_post_forecast_versions",
        param="forecast_id",
        suffix="/versions",
    ),
    Route(
        "POST",
        "prefix_suffix",
        "/api/forecasts/",
        "_post_forecast_resolve",
        param="forecast_id",
        suffix="/resolve",
    ),
    # ---------------- PUT ----------------
    Route("PUT", "exact", "/api/settings/backup", "_put_settings_backup"),
    Route("PUT", "exact", "/api/settings/retention", "_put_settings_retention"),
    Route("PUT", "exact", "/api/settings/learning", "_put_settings_learning"),
    Route(
        "PUT",
        "prefix",
        "/api/external/sources/",
        "_put_external_source",
        param="source_id",
    ),
    # ---------------- DELETE ----------------
    Route(
        "DELETE",
        "prefix",
        "/api/external/sources/",
        "_delete_external_source",
        param="source_id",
    ),
    Route(
        "DELETE",
        "prefix",
        "/api/external/rules/",
        "_delete_watch_rule",
        param="rule_id",
    ),
)

_ROUTES_BY_METHOD = {}
for _route in ROUTES:
    _ROUTES_BY_METHOD.setdefault(_route.method, []).append(_route)
_ROUTES_BY_METHOD = {
    method: tuple(routes) for method, routes in _ROUTES_BY_METHOD.items()
}


def routes_for(method):
    """返回某个 HTTP 方法的路由元组（已按匹配优先级排序）。可内省。"""
    return _ROUTES_BY_METHOD.get(str(method).upper(), ())


def match_route(method, path):
    """按声明顺序匹配，返回 ``(route, params)``；无匹配时 route 为 None。

    无匹配即旧实现里每条 ``do_*`` 末尾的那句 404 兜底。
    """
    for route in routes_for(method):
        params = route.match(path)
        if params is not None:
            return route, params
    return None, None


@dataclass(frozen=True)
class Services:
    """Services exposed to the local HTTP boundary."""

    forecasts: object
    interests: object
    signals: object
    knowledge: object = None
    external: object = None
    cognition: object = None
    trends: object = None
    cognition_controller: object = None
    notifications: object = None
    impacts: object = None
    startup: object = None
    ai_settings: object = None
    cognition_operation: object = None
    desktop: object = None
    system_settings: object = None
    diagnostics: object = None
    backup_service: object = None
    retention_service: object = None
    mobile_export: object = None
    scheduler: object = None
    update_check: object = None


def create_server(host, port, token, services):
    """Create a loopback-only server with token-protected application APIs."""
    if host != "127.0.0.1":
        raise ValueError("只允许本机访问")

    class Handler(BaseHTTPRequestHandler):
        server_version = "YuanJian/1.0"

        def log_message(self, format, *args):
            return None

        def handle_one_request(self):
            """捕获所有HTTP方法的未预期异常，返回干净的500，避免堆栈跟踪泄漏。"""
            try:
                super().handle_one_request()
            except Exception:
                try:
                    self._error(500, "internal_error", "服务内部错误，请稍后重试")
                except Exception:
                    pass

        def _send_security_headers(self):
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)

        def _json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, code, message):
            self._json({"error": {"code": code, "message": message}}, status)

        def _authorized(self):
            # 定时安全比较：逐字节 == 会在首个不同字节提前返回，理论上可用于
            # 逐位试探令牌。本机场景下几乎不可利用，但改一行没有成本。
            provided = self.headers.get("X-YuanJian-Token") or ""
            return hmac.compare_digest(provided, token)

        #: 单次排空请求体时最多读取的字节数。
        #: 绝不把 Content-Length 当真——对端可以谎报一个天文数字却一个字节
        #: 都不发，如果按它给的长度去读，工作线程就永远回不来。
        DRAIN_LIMIT_BYTES = 1024 * 1024

        #: 排空期间的单次读超时（秒）。谎报 Content-Length 却不发数据、或以
        #: 极低吞吐拖时间的对端，都靠它兜底。
        DRAIN_TIMEOUT_SECONDS = 5.0

        def _drain_request_body(self, length):
            """有界排空请求体，规避 Windows 回环 RST（R4）。

            在 Windows 上，只要服务端在响应前关闭连接而接收缓冲区里仍有未读
            的请求字节，内核就会发 RST，客户端拿到的是 ``RemoteDisconnected``
            而不是我们写好的 4xx。所以凡是"还没读请求体就要拒绝"的分支，都
            必须先把请求体读掉再响应。

            读取量以 ``min(length, DRAIN_LIMIT_BYTES)`` 为界，并在读取期间临
            时收紧 socket 超时：长度由对端提供，既可能是天文数字，也可能根本
            兑不了现，不能让它决定我们要阻塞多久。
            """
            if length <= 0:
                return
            connection = getattr(self, "connection", None)
            original_timeout = None
            timeout_pinned = False
            if connection is not None:
                try:
                    original_timeout = connection.gettimeout()
                    connection.settimeout(self.DRAIN_TIMEOUT_SECONDS)
                    timeout_pinned = True
                except OSError:
                    timeout_pinned = False
            try:
                remaining = min(length, self.DRAIN_LIMIT_BYTES)
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                # 对端提前挂断或读超时：已经无法再排空。丢弃失败只会退回
                # RST，不该把这个 4xx 升级成 500，所以静默收场。
                pass
            finally:
                if timeout_pinned:
                    try:
                        connection.settimeout(original_timeout)
                    except OSError:
                        pass

        def _discard_small_request_body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return
            if 0 < length <= 65536:
                self._drain_request_body(length)

        def _require_api_access(self):
            if not self._authorized():
                # On Windows, closing a response while request bytes remain unread can
                # reset the loopback connection before the client receives the 403.
                self._discard_small_request_body()
                self._error(403, "forbidden", "本次操作没有有效的本机会话令牌")
                return False
            return True

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                # 先把超限的请求体排空，再报这个错。若直接 raise，服务端会在
                # 接收缓冲区仍有未读字节时关闭连接，Windows 回环随即发 RST，
                # 用户粘贴超过 65KB 文本时看到的是一个莫名其妙的网络错误，而
                # 不是下面这句可读的"请求内容为空或过大"。
                self._drain_request_body(length)
                raise ValueError("请求内容为空或过大")
            if length <= 0:
                # An empty POST body is a legal request — endpoints that do
                # not need payload (e.g. /api/cognition/run, notification
                # mark-as-read) would otherwise fail with a misleading
                # "empty body" error when callers (such as fetch with no
                # body argument) omit Content-Length entirely.
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _pagination(self, parsed, default_limit):
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", [default_limit])[0])
                offset = int(query.get("offset", [0])[0])
            except (TypeError, ValueError) as error:
                raise ValueError("分页参数无效") from error
            if not 1 <= limit <= 100 or offset < 0:
                raise ValueError("分页参数无效")
            return query, limit, offset

        def _static(self, name, content_type):
            path = (STATIC_ROOT / name).resolve()
            # 纵深防御：解析后必须仍位于静态根目录内，阻断相对路径逃逸。
            if STATIC_ROOT.resolve() not in path.parents:
                self._error(403, "forbidden", "非法的资源路径")
                return
            if not path.is_file():
                self._error(404, "not_found", "页面资源不存在")
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, raw_path, path):
            """按白名单注册表分发静态资源，含编码路径穿越校验。"""
            lowered = raw_path.lower()
            if ".." in lowered or "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
                self._error(403, "forbidden", "非法的资源路径")
                return
            entry = STATIC_FILES.get(path)
            if entry is None:
                self._error(404, "not_found", "页面不存在")
                return
            self._static(entry[0], entry[1])

        def _dispatch(self, method, path, parsed, payload):
            """查表分发；无匹配即 404 兜底（旧实现每条 do_* 末尾的那句）。"""
            route, params = match_route(method, path)
            if route is None:
                self._error(404, "not_found", "接口不存在")
                return
            getattr(self, route.endpoint)(services, params, parsed, payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                self._serve_static(self.path, path)
                return
            if not self._require_api_access():
                return
            self._dispatch("GET", path, parsed, None)

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._require_api_access():
                return
            try:
                payload = self._read_json()
                self._dispatch("POST", path, None, payload)
            except ForecastConflictError as error:
                self._error(409, "forecast_conflict", str(error))
            except OperationBusy:
                self._error(409, "operation_busy", "认知任务正在运行，请稍候")
            except (ValueError, json.JSONDecodeError) as error:
                self._error(400, "invalid_request", str(error))
            except KeyError:
                self._error(404, "not_found", "对象不存在")

        def do_PUT(self):
            path = urlparse(self.path).path
            if not self._require_api_access():
                return
            try:
                payload = self._read_json()
                self._dispatch("PUT", path, None, payload)
            except (ValueError, json.JSONDecodeError) as error:
                self._error(400, "invalid_request", str(error))
            except KeyError:
                self._error(404, "not_found", "对象不存在")

        def do_DELETE(self):
            path = urlparse(self.path).path
            if not self._require_api_access():
                return
            # 无请求体的删除也走丢弃逻辑，规避 Windows 回环 RST（R4）。
            self._discard_small_request_body()
            self._dispatch("DELETE", path, None, None)

        # ---- GET 处理器 -------------------------------------------------
        # 签名统一为 (services, params, parsed, payload)：GET/DELETE 的 payload
        # 为 None。统一签名让路由表只需记住一个方法名，不必区分调用约定。

        def _get_app_version(self, services, params, parsed, payload):
            self._json({"version": __version__, "releases_url": RELEASES_PAGE})

        def _get_forecasts(self, services, params, parsed, payload):
            try:
                _query, limit, offset = self._pagination(parsed, 20)
                forecasts, total = services.forecasts.list_forecasts(
                    limit=limit, offset=offset
                )
            except ValueError as error:
                self._error(400, "invalid_request", str(error))
                return
            self._json({"forecasts": forecasts, "total": total})

        def _get_forecast_progress(self, services, params, parsed, payload):
            if services.forecasts is None:
                self._error(503, "unavailable", "预测能力未装配")
                return
            self._json(services.forecasts.progress_summary())

        def _get_forecast_overdue(self, services, params, parsed, payload):
            if services.forecasts is None:
                self._error(503, "unavailable", "预测能力未装配")
                return
            self._json({"overdue": services.forecasts.list_overdue()})

        def _get_interests(self, services, params, parsed, payload):
            self._json(
                {
                    "objects": services.interests.list_objects(),
                    "links": services.interests.list_links(),
                }
            )

        def _get_interest_objects(self, services, params, parsed, payload):
            self._json({"objects": services.interests.list_objects()})

        def _get_signals(self, services, params, parsed, payload):
            self._json({"signals": services.signals.list_signals()})

        def _get_knowledge_vaults(self, services, params, parsed, payload):
            self._json({"vaults": services.knowledge.discover_vaults()})

        def _get_knowledge_documents(self, services, params, parsed, payload):
            query = parse_qs(parsed.query).get("q", [""])[0]
            self._json({"documents": services.knowledge.list_documents(query)})

        def _get_external_radar(self, services, params, parsed, payload):
            try:
                query, limit, offset = self._pagination(parsed, 10)
                self._json(
                    services.external.radar_page(
                        limit=limit,
                        offset=offset,
                        query=query.get("q", [""])[0],
                    )
                )
            except ValueError as error:
                self._error(400, "invalid_request", str(error))

        def _get_external_sources(self, services, params, parsed, payload):
            self._json({"sources": services.external.list_sources()})

        def _get_external_rules(self, services, params, parsed, payload):
            self._json({"rules": services.external.list_rules()})

        def _get_cognition_status(self, services, params, parsed, payload):
            status = services.cognition_controller.status()
            if services.cognition_operation is not None:
                status["running"] = services.cognition_operation.running
                if services.cognition_operation.started_at_monotonic is not None:
                    status["started_at_monotonic"] = (
                        services.cognition_operation.started_at_monotonic
                    )
            self._json(status)

        def _get_cognition_candidates(self, services, params, parsed, payload):
            self._json(
                {
                    "candidates": services.impacts.pending_candidates()
                    if services.impacts is not None
                    else []
                }
            )

        def _get_risk_dashboard(self, services, params, parsed, payload):
            source_states = (
                services.external.list_sources()
                if services.external is not None
                else []
            )
            self._json(
                services.cognition_controller.risk_dashboard(source_states, limit=50)
            )

        def _get_cognition_clusters(self, services, params, parsed, payload):
            try:
                query, limit, offset = self._pagination(parsed, 10)
                raw_needs = query.get("needs_judgment", [""])[0].casefold()
                needs_judgment = {"": None, "true": True, "false": False}.get(raw_needs)
                if raw_needs not in {"", "true", "false"}:
                    raise ValueError("待研判筛选无效")
                page = services.cognition.list_clusters_page(
                    limit=limit,
                    offset=offset,
                    query=query.get("q", [""])[0],
                    category=query.get("category", [""])[0],
                    evidence=query.get("evidence", [""])[0],
                    needs_judgment=needs_judgment,
                )
                self._json({**page, "clusters": page["items"]})
            except ValueError as error:
                self._error(400, "invalid_request", str(error))

        def _get_cluster_detail(self, services, params, parsed, payload):
            cluster_id = unquote(params["cluster_id"])
            try:
                self._json(services.cognition_controller.cluster_detail(cluster_id))
            except KeyError:
                self._error(404, "cluster_not_found", "事件不存在")

        def _get_cognition_trends(self, services, params, parsed, payload):
            self._json(
                {
                    "trends": services.trends.summary(
                        services.cognition_controller.now()
                    )
                }
            )

        def _get_cognition_jobs(self, services, params, parsed, payload):
            self._json({"jobs": services.cognition_controller.list_jobs()})

        def _get_notifications(self, services, params, parsed, payload):
            try:
                query, limit, offset = self._pagination(parsed, 20)
                page = services.notifications.list_page(
                    limit=limit,
                    offset=offset,
                    status=query.get("status", [""])[0],
                )
                self._json({**page, "notifications": page["items"]})
            except ValueError as error:
                self._error(400, "invalid_request", str(error))

        def _get_settings_startup(self, services, params, parsed, payload):
            self._json(
                services.startup.status()
                if services.startup is not None
                else {"installed": False, "available": False}
            )

        def _get_settings_ai(self, services, params, parsed, payload):
            self._json(services.ai_settings.get())

        def _get_calibration(self, services, params, parsed, payload):
            summary = services.forecasts.calibration_summary()
            summary["candidates"] = (
                services.impacts.pending_candidates()
                if services.impacts is not None
                else []
            )
            self._json(summary)

        def _get_diagnostics(self, services, params, parsed, payload):
            if services.diagnostics is None:
                self._error(503, "unavailable", "诊断能力未装配")
                return
            self._json(services.diagnostics.snapshot())

        def _get_settings_backup(self, services, params, parsed, payload):
            if services.backup_service is None:
                self._error(503, "unavailable", "备份能力未装配")
                return
            self._json(services.backup_service.get_setting())

        def _get_settings_retention(self, services, params, parsed, payload):
            if services.retention_service is None:
                self._error(503, "unavailable", "数据保留能力未装配")
                return
            self._json(services.retention_service.get_setting())

        def _get_settings_learning(self, services, params, parsed, payload):
            if services.system_settings is None:
                self._error(503, "unavailable", "反馈学习未装配")
                return
            self._json(services.system_settings.get_learning())

        def _get_score(self, services, params, parsed, payload):
            self._json(services.forecasts.score_summary())

        def _get_forecast(self, services, params, parsed, payload):
            forecast_id = unquote(params["forecast_id"])
            try:
                self._json(services.forecasts.get_forecast(forecast_id))
            except KeyError:
                self._error(404, "forecast_not_found", "预测不存在")

        # ---- POST 处理器 ------------------------------------------------

        def _post_update_check(self, services, params, parsed, payload):
            # 只在用户点按「检查更新」时走到这里。整个程序唯一会主动外发的
            # 请求，且只取公开发布信息，不带本机任何数据。
            if services.update_check is None:
                self._error(503, "unavailable", "更新检查未装配")
                return
            self._json(services.update_check.check(__version__))

        def _post_events(self, services, params, parsed, payload):
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("事件内容不能为空")
            signal = services.signals.ingest(
                text,
                payload.get("occurred_at", ""),
                source_type="manual",
                source_ref="user",
            )
            self._json({"candidate": signal["candidate"], "signal": signal}, 201)

        def _post_shutdown(self, services, params, parsed, payload):
            self._json({"status": "shutting_down"})
            target = (
                services.desktop.request_exit
                if services.desktop is not None
                and hasattr(services.desktop, "request_exit")
                else self.server.shutdown
            )
            threading.Thread(target=target, daemon=True).start()

        def _post_window_show(self, services, params, parsed, payload):
            if services.desktop is None:
                raise ValueError("桌面窗口尚未就绪")
            services.desktop.show_window()
            self._json({"status": "shown"})

        def _post_monitoring_toggle(self, services, params, parsed, payload):
            if services.desktop is None:
                raise ValueError("桌面窗口尚未就绪")
            self._json({"monitoring": services.desktop.toggle_monitoring()})

        def _post_knowledge_index(self, services, params, parsed, payload):
            self._json(services.knowledge.index_vault(payload.get("path", "")), 201)

        def _post_external_sources(self, services, params, parsed, payload):
            source_id = services.external.add_source(payload)
            self._json({"source_id": source_id}, 201)

        def _post_external_sources_import_opml(self, services, params, parsed, payload):
            self._json(services.external.import_opml(payload), 201)

        def _post_external_sources_bulk_enabled(
            self, services, params, parsed, payload
        ):
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("数据源状态无效")
            region = str(payload.get("region", "")).strip()
            category = str(payload.get("category", "")).strip()
            self._json(
                services.external.bulk_set_enabled(
                    enabled,
                    region=region or None,
                    category=category or None,
                )
            )

        def _post_source_enabled(self, services, params, parsed, payload):
            source_id = unquote(params["source_id"]).rstrip("/")
            enabled = payload.get("enabled")
            if not source_id or not isinstance(enabled, bool):
                raise ValueError("数据源状态无效")
            self._json(services.external.set_source_enabled(source_id, enabled))

        def _post_external_rules(self, services, params, parsed, payload):
            rule_id = services.external.add_watch_rule(payload)
            self._json({"rule_id": rule_id}, 201)

        def _post_rule_enabled(self, services, params, parsed, payload):
            rule_id = unquote(params["rule_id"]).rstrip("/")
            enabled = payload.get("enabled")
            if not rule_id or not isinstance(enabled, bool):
                raise ValueError("关注词状态无效")
            try:
                self._json(services.external.set_rule_enabled(rule_id, enabled))
            except KeyError:
                self._error(404, "rule_not_found", "关注词不存在")

        def _post_interest_objects(self, services, params, parsed, payload):
            self._json(services.interests.create_object(payload), 201)

        def _post_interest_links(self, services, params, parsed, payload):
            self._json(services.interests.create_link(payload), 201)

        def _post_external_refresh(self, services, params, parsed, payload):
            source_id = str(payload.get("source_id", "")).strip()
            if not source_id:
                raise ValueError("缺少数据源编号")
            self._json(services.external.refresh_source(source_id))

        def _post_cognition_run(self, services, params, parsed, payload):
            if services.cognition_operation is not None:
                self._json(services.cognition_operation.run("manual"))
            else:
                self._json(services.cognition_controller.run_once())

        def _post_candidate_confirm(self, services, params, parsed, payload):
            impact_id = unquote(params["impact_id"]).rstrip("/")
            try:
                self._json(
                    services.impacts.confirm_candidate(
                        impact_id, payload.get("probability")
                    ),
                    201,
                )
            except KeyError:
                self._error(404, "candidate_not_found", "候选预测不存在")

        def _post_cluster_feedback(self, services, params, parsed, payload):
            cluster_id = unquote(params["cluster_id"]).rstrip("/")
            try:
                self._json(
                    services.cognition_controller.feedback(
                        cluster_id, str(payload.get("action", "")), payload
                    )
                )
            except KeyError:
                self._error(404, "cluster_not_found", "事件不存在")

        def _post_notifications_read_all(self, services, params, parsed, payload):
            self._json(services.notifications.mark_all_read())

        def _post_notification_read(self, services, params, parsed, payload):
            notification_id = unquote(params["notification_id"]).rstrip("/")
            try:
                self._json(services.notifications.mark_read(notification_id))
            except KeyError:
                self._error(404, "notification_not_found", "提醒不存在")

        def _post_settings_startup(self, services, params, parsed, payload):
            if services.startup is None:
                raise ValueError("当前运行方式不支持登录启动设置")
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("登录启动状态无效")
            self._json(
                services.startup.install() if enabled else services.startup.remove()
            )

        def _post_settings_ai(self, services, params, parsed, payload):
            self._json(services.ai_settings.save(payload))

        def _post_export_mobile_summary(self, services, params, parsed, payload):
            if services.mobile_export is None:
                raise ValueError("移动摘要导出未装配")
            source_states = (
                services.external.list_sources()
                if services.external is not None
                else []
            )
            dashboard = services.cognition_controller.risk_dashboard(
                source_states, limit=5
            )
            self._json(services.mobile_export.export(dashboard), 201)

        def _post_forecasts(self, services, params, parsed, payload):
            self._json(services.forecasts.create_forecast(payload), 201)

        def _post_forecast_versions(self, services, params, parsed, payload):
            forecast_id = unquote(params["forecast_id"]).rstrip("/")
            self._json(services.forecasts.add_version(forecast_id, payload), 201)

        def _post_forecast_resolve(self, services, params, parsed, payload):
            forecast_id = unquote(params["forecast_id"]).rstrip("/")
            result = services.forecasts.resolve(
                forecast_id,
                payload.get("outcome", ""),
                payload.get("resolved_at", ""),
                payload.get("note", ""),
            )
            self._json(result, 201)

        # ---- PUT 处理器 -------------------------------------------------

        def _put_settings_backup(self, services, params, parsed, payload):
            if services.backup_service is None:
                raise ValueError("备份能力未装配")
            self._json(services.backup_service.put_setting(payload))

        def _put_settings_retention(self, services, params, parsed, payload):
            if services.retention_service is None:
                raise ValueError("数据保留能力未装配")
            self._json(services.retention_service.put_setting(payload))

        def _put_settings_learning(self, services, params, parsed, payload):
            if services.system_settings is None:
                raise ValueError("反馈学习未装配")
            self._json(services.system_settings.put_learning(payload))

        def _put_external_source(self, services, params, parsed, payload):
            source_id = unquote(params["source_id"]).rstrip("/")
            if not source_id or "/" in source_id:
                raise ValueError("数据源编号无效")
            self._json(services.external.update_source(source_id, payload))

        # ---- DELETE 处理器 ----------------------------------------------

        def _delete_external_source(self, services, params, parsed, payload):
            source_id = unquote(params["source_id"]).rstrip("/")
            if not source_id or "/" in source_id:
                self._error(400, "invalid_request", "数据源编号无效")
                return
            try:
                self._json(services.external.delete_source(source_id))
            except ValueError as error:
                self._error(400, "invalid_request", str(error))
            except KeyError:
                self._error(404, "not_found", "数据源不存在")

        def _delete_watch_rule(self, services, params, parsed, payload):
            rule_id = unquote(params["rule_id"]).rstrip("/")
            if not rule_id or "/" in rule_id:
                self._error(400, "invalid_request", "关注词编号无效")
                return
            try:
                self._json(services.external.delete_watch_rule(rule_id))
            except ValueError as error:
                self._error(400, "invalid_request", str(error))
            except KeyError:
                self._error(404, "rule_not_found", "关注词不存在")

    # 路由表指向的处理器必须真实存在：写错一个方法名就变成运行期 500，
    # 在这里一次性炸出来，比等用户点到那条路由再报错便宜得多。
    for route in ROUTES:
        if not hasattr(Handler, route.endpoint):
            raise RuntimeError(
                f"路由 {route.method} {route.path} 指向不存在的处理器 "
                f"{route.endpoint}"
            )

    class _YuanJianServer(ThreadingHTTPServer):
        # 后台调度写库突发时，多个请求线程可能短暂等待SQLite锁；
        # 把默认仅5的连接积压队列提高到128，让用户点击排队等待而非被直接RST
        # （RST在前端表现为 TypeError: Failed to fetch）。
        request_queue_size = 128
        daemon_threads = True       # 请求处理线程不阻塞进程退出
        block_on_close = False
        allow_reuse_address = True

    return _YuanJianServer((host, port), Handler)
