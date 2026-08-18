import json
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .forecasts import ForecastConflictError
from .operations import OperationBusy


def resolve_static_root(module_file, bundle_root=None):
    """Resolve local web assets in source and PyInstaller-frozen layouts."""
    if bundle_root is not None:
        return Path(bundle_root) / "yuanjian_app" / "static"
    return Path(module_file).with_name("static")


STATIC_ROOT = resolve_static_root(Path(__file__), getattr(sys, "_MEIPASS", None))


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


def create_server(host, port, token, services):
    """Create a loopback-only server with token-protected application APIs."""
    if host != "127.0.0.1":
        raise ValueError("只允许本机访问")

    class Handler(BaseHTTPRequestHandler):
        server_version = "YuanJian/0.9"

        def log_message(self, format, *args):
            return None

        def _json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, code, message):
            self._json({"error": {"code": code, "message": message}}, status)

        def _authorized(self):
            return self.headers.get("X-YuanJian-Token") == token

        def _discard_small_request_body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return
            if 0 < length <= 65536:
                self.rfile.read(length)

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
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'")
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

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                self._serve_static(self.path, path)
                return
            if not self._require_api_access():
                return
            if path == "/api/forecasts":
                try:
                    _query, limit, offset = self._pagination(parsed, 20)
                    forecasts, total = services.forecasts.list_forecasts(
                        limit=limit, offset=offset
                    )
                except ValueError as error:
                    self._error(400, "invalid_request", str(error))
                    return
                self._json({"forecasts": forecasts, "total": total})
            elif path == "/api/forecasts/progress":
                if services.forecasts is None:
                    self._error(503, "unavailable", "预测能力未装配")
                    return
                self._json(services.forecasts.progress_summary())
            elif path == "/api/interests":
                self._json(
                    {
                        "objects": services.interests.list_objects(),
                        "links": services.interests.list_links(),
                    }
                )
            elif path == "/api/interests/objects":
                self._json({"objects": services.interests.list_objects()})
            elif path == "/api/signals":
                self._json({"signals": services.signals.list_signals()})
            elif path == "/api/knowledge/vaults":
                self._json({"vaults": services.knowledge.discover_vaults()})
            elif path == "/api/knowledge/documents":
                query = parse_qs(parsed.query).get("q", [""])[0]
                self._json({"documents": services.knowledge.list_documents(query)})
            elif path == "/api/external/radar":
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
            elif path == "/api/external/sources":
                self._json({"sources": services.external.list_sources()})
            elif path == "/api/external/rules":
                self._json({"rules": services.external.list_rules()})
            elif path == "/api/cognition/status":
                status = services.cognition_controller.status()
                if services.cognition_operation is not None:
                    status["running"] = services.cognition_operation.running
                    if services.cognition_operation.started_at_monotonic is not None:
                        status["started_at_monotonic"] = (
                            services.cognition_operation.started_at_monotonic
                        )
                self._json(status)
            elif path == "/api/cognition/candidates":
                self._json({
                    "candidates": services.impacts.pending_candidates()
                    if services.impacts is not None else []
                })
            elif path == "/api/risk-dashboard":
                source_states = (
                    services.external.list_sources()
                    if services.external is not None
                    else []
                )
                self._json(
                    services.cognition_controller.risk_dashboard(
                        source_states, limit=5
                    )
                )
            elif path == "/api/cognition/clusters":
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
            elif path.startswith("/api/cognition/clusters/"):
                cluster_id = unquote(path.removeprefix("/api/cognition/clusters/"))
                try:
                    self._json(services.cognition_controller.cluster_detail(cluster_id))
                except KeyError:
                    self._error(404, "cluster_not_found", "事件不存在")
            elif path == "/api/cognition/trends":
                self._json(
                    {
                        "trends": services.trends.summary(
                            services.cognition_controller.now()
                        )
                    }
                )
            elif path == "/api/cognition/jobs":
                self._json({"jobs": services.cognition_controller.list_jobs()})
            elif path == "/api/notifications":
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
            elif path == "/api/settings/startup":
                self._json(
                    services.startup.status()
                    if services.startup is not None
                    else {"installed": False, "available": False}
                )
            elif path == "/api/settings/ai":
                self._json(services.ai_settings.get())
            elif path == "/api/calibration":
                payload = services.forecasts.calibration_summary()
                payload["candidates"] = (
                    services.impacts.pending_candidates()
                    if services.impacts is not None
                    else []
                )
                self._json(payload)
            elif path == "/api/diagnostics":
                if services.diagnostics is None:
                    self._error(503, "unavailable", "诊断能力未装配")
                else:
                    self._json(services.diagnostics.snapshot())
            elif path == "/api/settings/backup":
                if services.backup_service is None:
                    self._error(503, "unavailable", "备份能力未装配")
                else:
                    self._json(services.backup_service.get_setting())
            elif path == "/api/settings/retention":
                if services.retention_service is None:
                    self._error(503, "unavailable", "数据保留能力未装配")
                else:
                    self._json(services.retention_service.get_setting())
            elif path == "/api/settings/learning":
                if services.system_settings is None:
                    self._error(503, "unavailable", "反馈学习未装配")
                else:
                    self._json(services.system_settings.get_learning())
            elif path == "/api/dashboard":
                forecasts, _total = services.forecasts.list_forecasts()
                self._json(
                    {
                        "open": [item for item in forecasts if item["status"] == "open"],
                        "high_alerts": [
                            item
                            for item in forecasts
                            if item["status"] == "open" and item["alert_level"] in {"L3", "L4"}
                        ],
                        "high_signals": services.signals.high_alerts(),
                    }
                )
            elif path == "/api/score":
                self._json(services.forecasts.score_summary())
            elif path.startswith("/api/forecasts/"):
                forecast_id = unquote(path.removeprefix("/api/forecasts/"))
                try:
                    self._json(services.forecasts.get_forecast(forecast_id))
                except KeyError:
                    self._error(404, "forecast_not_found", "预测不存在")
            else:
                self._error(404, "not_found", "接口不存在")

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._require_api_access():
                return
            try:
                payload = self._read_json()
                if path == "/api/events":
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
                    return
                if path == "/api/shutdown":
                    self._json({"status": "shutting_down"})
                    target = (
                        services.desktop.request_exit
                        if services.desktop is not None
                        and hasattr(services.desktop, "request_exit")
                        else self.server.shutdown
                    )
                    threading.Thread(target=target, daemon=True).start()
                    return
                if path == "/api/window/show":
                    if services.desktop is None:
                        raise ValueError("桌面窗口尚未就绪")
                    services.desktop.show_window()
                    self._json({"status": "shown"})
                    return
                if path == "/api/monitoring/toggle":
                    if services.desktop is None:
                        raise ValueError("桌面窗口尚未就绪")
                    self._json({"monitoring": services.desktop.toggle_monitoring()})
                    return
                if path == "/api/knowledge/index":
                    self._json(services.knowledge.index_vault(payload.get("path", "")), 201)
                    return
                if path == "/api/external/sources":
                    source_id = services.external.add_source(payload)
                    self._json({"source_id": source_id}, 201)
                    return
                if path == "/api/external/sources/import-opml":
                    self._json(services.external.import_opml(payload), 201)
                    return
                if path == "/api/external/sources/bulk-enabled":
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
                    return
                if path.startswith("/api/external/sources/") and path.endswith("/enabled"):
                    source_id = unquote(
                        path.removeprefix("/api/external/sources/").removesuffix("/enabled")
                    ).rstrip("/")
                    enabled = payload.get("enabled")
                    if not source_id or not isinstance(enabled, bool):
                        raise ValueError("数据源状态无效")
                    self._json(services.external.set_source_enabled(source_id, enabled))
                    return
                if path == "/api/external/rules":
                    rule_id = services.external.add_watch_rule(payload)
                    self._json({"rule_id": rule_id}, 201)
                    return
                if path.startswith("/api/external/rules/") and path.endswith("/enabled"):
                    rule_id = unquote(
                        path.removeprefix("/api/external/rules/").removesuffix("/enabled")
                    ).rstrip("/")
                    enabled = payload.get("enabled")
                    if not rule_id or not isinstance(enabled, bool):
                        raise ValueError("关注词状态无效")
                    try:
                        self._json(services.external.set_rule_enabled(rule_id, enabled))
                    except KeyError:
                        self._error(404, "rule_not_found", "关注词不存在")
                    return
                if path == "/api/interests/objects":
                    self._json(services.interests.create_object(payload), 201)
                    return
                if path == "/api/interests/links":
                    self._json(services.interests.create_link(payload), 201)
                    return
                if path == "/api/external/refresh":
                    source_id = str(payload.get("source_id", "")).strip()
                    if not source_id:
                        raise ValueError("缺少数据源编号")
                    self._json(services.external.refresh_source(source_id))
                    return
                if path == "/api/cognition/run":
                    if services.cognition_operation is not None:
                        self._json(services.cognition_operation.run("manual"))
                    else:
                        self._json(services.cognition_controller.run_once())
                    return
                if path.startswith("/api/cognition/candidates/") and path.endswith("/confirm"):
                    impact_id = unquote(
                        path.removeprefix("/api/cognition/candidates/").removesuffix("/confirm")
                    ).rstrip("/")
                    try:
                        self._json(
                            services.impacts.confirm_candidate(
                                impact_id, payload.get("probability")
                            ),
                            201,
                        )
                    except KeyError:
                        self._error(404, "candidate_not_found", "候选预测不存在")
                    return
                if path.startswith("/api/cognition/clusters/") and path.endswith("/feedback"):
                    cluster_id = unquote(
                        path.removeprefix("/api/cognition/clusters/").removesuffix("/feedback")
                    ).rstrip("/")
                    try:
                        self._json(
                            services.cognition_controller.feedback(
                                cluster_id, str(payload.get("action", "")), payload
                            )
                        )
                    except KeyError:
                        self._error(404, "cluster_not_found", "事件不存在")
                    return
                if path == "/api/notifications/read-all":
                    self._json(services.notifications.mark_all_read())
                    return
                if path.startswith("/api/notifications/") and path.endswith("/read"):
                    notification_id = unquote(
                        path.removeprefix("/api/notifications/").removesuffix("/read")
                    ).rstrip("/")
                    try:
                        self._json(services.notifications.mark_read(notification_id))
                    except KeyError:
                        self._error(404, "notification_not_found", "提醒不存在")
                    return
                if path == "/api/settings/startup":
                    if services.startup is None:
                        raise ValueError("当前运行方式不支持登录启动设置")
                    enabled = payload.get("enabled")
                    if not isinstance(enabled, bool):
                        raise ValueError("登录启动状态无效")
                    self._json(
                        services.startup.install() if enabled else services.startup.remove()
                    )
                    return
                if path == "/api/settings/ai":
                    self._json(services.ai_settings.save(payload))
                    return
                if path == "/api/export/mobile-summary":
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
                    return
                if path == "/api/forecasts":
                    self._json(services.forecasts.create_forecast(payload), 201)
                    return
                if path.startswith("/api/forecasts/") and path.endswith("/versions"):
                    forecast_id = unquote(
                        path.removeprefix("/api/forecasts/").removesuffix("/versions")
                    ).rstrip("/")
                    self._json(services.forecasts.add_version(forecast_id, payload), 201)
                    return
                if path.startswith("/api/forecasts/") and path.endswith("/resolve"):
                    forecast_id = unquote(
                        path.removeprefix("/api/forecasts/").removesuffix("/resolve")
                    ).rstrip("/")
                    result = services.forecasts.resolve(
                        forecast_id,
                        payload.get("outcome", ""),
                        payload.get("resolved_at", ""),
                        payload.get("note", ""),
                    )
                    self._json(result, 201)
                    return
                self._error(404, "not_found", "接口不存在")
            except ForecastConflictError as error:
                self._error(409, "forecast_conflict", str(error))
            except OperationBusy:
                self._error(409, "operation_busy", "认知任务正在运行，请稍候")
            except (ValueError, json.JSONDecodeError) as error:
                self._error(400, "invalid_request", str(error))
            except KeyError:
                self._error(404, "forecast_not_found", "预测不存在")

        def do_PUT(self):
            path = urlparse(self.path).path
            if not self._require_api_access():
                return
            try:
                payload = self._read_json()
                if path == "/api/settings/backup":
                    if services.backup_service is None:
                        raise ValueError("备份能力未装配")
                    self._json(services.backup_service.put_setting(payload))
                    return
                if path == "/api/settings/retention":
                    if services.retention_service is None:
                        raise ValueError("数据保留能力未装配")
                    self._json(services.retention_service.put_setting(payload))
                    return
                if path == "/api/settings/learning":
                    if services.system_settings is None:
                        raise ValueError("反馈学习未装配")
                    self._json(services.system_settings.put_learning(payload))
                    return
                if path.startswith("/api/external/sources/"):
                    source_id = unquote(path.removeprefix("/api/external/sources/")).rstrip("/")
                    if not source_id or "/" in source_id:
                        raise ValueError("数据源编号无效")
                    self._json(services.external.update_source(source_id, payload))
                    return
                self._error(404, "not_found", "接口不存在")
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
            if path.startswith("/api/external/sources/"):
                source_id = unquote(path.removeprefix("/api/external/sources/")).rstrip("/")
                if not source_id or "/" in source_id:
                    self._error(400, "invalid_request", "数据源编号无效")
                    return
                try:
                    self._json(services.external.delete_source(source_id))
                except ValueError as error:
                    self._error(400, "invalid_request", str(error))
                except KeyError:
                    self._error(404, "not_found", "数据源不存在")
                return
            elif path.startswith("/api/external/rules/"):
                rule_id = unquote(path.removeprefix("/api/external/rules/")).rstrip("/")
                if not rule_id or "/" in rule_id:
                    self._error(400, "invalid_request", "关注词编号无效")
                    return
                try:
                    self._json(services.external.delete_watch_rule(rule_id))
                except ValueError as error:
                    self._error(400, "invalid_request", str(error))
                except KeyError:
                    self._error(404, "rule_not_found", "关注词不存在")
                return
            self._error(404, "not_found", "接口不存在")

    return ThreadingHTTPServer((host, port), Handler)
