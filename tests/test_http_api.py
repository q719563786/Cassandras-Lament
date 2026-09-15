import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from yuanjian_app.database import Database
from yuanjian_app.external_radar import ExternalRadarService
from yuanjian_app.external_sources import ExternalItem
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.cognition import CognitionController, CognitionService
from yuanjian_app import http_api
from yuanjian_app.http_api import (
    ROUTES,
    SECURITY_HEADERS,
    Route,
    Services,
    create_server,
    match_route,
    resolve_static_root,
    routes_for,
)
from yuanjian_app.impacts import ImpactService
from yuanjian_app.interests import InterestService
from yuanjian_app.knowledge import KnowledgeService
from yuanjian_app.signals import SignalService
from yuanjian_app.judgments import LocalHeuristicProvider, build_public_bundle
from yuanjian_app.notifications import NotificationService
from yuanjian_app.operations import CognitionOperation, OperationBusy
from yuanjian_app.remote_ai import AiSettingsService, JudgmentQueue
from yuanjian_app.secret_store import DpapiSecretStore
from yuanjian_app.trends import TrendService


# ---------------------------------------------------------------------------
# 客户端传输层的**瞬时异常有界重试**（team-lead 裁定方案 2）
# ---------------------------------------------------------------------------
# 背景：全量跑里 `test_paged_radar_cluster_and_notification_apis_form_action_center_contract`
# 偶发红过一次（14 轮里 1 次），形态是 **ERROR（异常抛出）而不是断言失败**，
# 单跑 30/30 绿。回环 TCP 在负载下会瞬时重置，这不是产品契约的一部分。
#
# 关键区分（这条很重要，别被后来的维护者误解成"把测试调软了"）：
#   - **放松语义断言**（`total==1` 改成"≥0"、先轮询到非空再断言）= 调软 → 不做；
#   - **在传输层重试瞬时异常** = 去掉环境噪声 → 只做这一件，且只对下面三类。
#
# `HTTPError` 单独排除：它是"服务端给了真·HTTP 响应"，是产品行为，必须原样暴露。
TRANSIENT_EXCEPTIONS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    TimeoutError,
)

TRANSIENT_RETRY_LIMIT = 2
TRANSIENT_RETRY_BACKOFF_SECONDS = 0.05

# 可见性：重试次数必须能被看见。频繁非零本身就是产品症状（服务端连接管理有问题），
# 不该被重试悄悄盖掉。
TRANSIENT_RETRIES = {"count": 0, "details": []}
TRANSIENT_RETRY_REPORT = Path(__file__).resolve().parents[1] / "build-artifacts" / "http-api-transient-retries.txt"


def is_transient(error):
    """只认那三类瞬时异常；其余一律不重试。

    `urllib` 会把连接阶段的 `OSError` 包成 `URLError`，所以要把 `reason` 拆开看，
    否则一条"不是瞬时"的 `URLError`（比如域名解析失败）会被误当成可重试。
    """
    if isinstance(error, urllib.error.HTTPError):
        return False
    if isinstance(error, TRANSIENT_EXCEPTIONS):
        return True
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, TRANSIENT_EXCEPTIONS)
    return False


def transient_retry(fetch, backoff=TRANSIENT_RETRY_BACKOFF_SECONDS):
    """跑一次 `fetch()`；只对瞬时异常最多重试 `TRANSIENT_RETRY_LIMIT` 次。

    不吞任何异常：重试额度用完（或异常不是瞬时的）就**原样抛出**，
    调用方与断言看到的行为和没有这层包装时完全一致。
    """
    attempts = 0
    while True:
        try:
            return fetch()
        except Exception as error:  # noqa: BLE001 —— 先分类，不是瞬时就原样抛
            if attempts >= TRANSIENT_RETRY_LIMIT or not is_transient(error):
                raise
            attempts += 1
            TRANSIENT_RETRIES["count"] += 1
            TRANSIENT_RETRIES["details"].append(
                "第 %d 次重试 %s: %s" % (attempts, type(error).__name__, error)
            )
            if backoff:
                time.sleep(backoff * attempts)


def tearDownModule():
    """收尾把重试计数落盘 —— 只在计数 > 0 时落，避免正常运行的噪声。"""
    count = TRANSIENT_RETRIES["count"]
    if count:
        TRANSIENT_RETRY_REPORT.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with TRANSIENT_RETRY_REPORT.open("a", encoding="utf-8") as handle:
            handle.write("=== %s 传输层瞬时异常重试 %d 次 ===\n" % (stamp, count))
            for line in TRANSIENT_RETRIES["details"]:
                handle.write("  %s\n" % line)
    print("[test_http_api] 传输层瞬时异常重试次数=%d" % count)


class RecordingDesktop:
    def __init__(self):
        self.shown = 0
        self.monitoring = True

    def show_window(self):
        self.shown += 1

    def toggle_monitoring(self):
        self.monitoring = not self.monitoring
        return self.monitoring


class SwitchableCognitionOperation:
    def __init__(self, operation):
        self.operation = operation
        self.busy = False

    def run(self, source):
        if self.busy:
            raise OperationBusy("busy")
        return self.operation.run(source)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        database.initialize()
        self._database = database
        interests = InterestService(database)
        interests.ensure_defaults()
        signals = SignalService(database, interests)
        vault = Path(self.temp_dir.name) / "Obsidian" / "Vault"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / "note.md").write_text("# 测试知识\n只读内容", encoding="utf-8")
        knowledge = KnowledgeService(database, home=Path(self.temp_dir.name))
        cognition = CognitionService(database)
        external = ExternalRadarService(
            database,
            fetcher=lambda source: [
                ExternalItem(
                    source["source_id"],
                    source["name"],
                    "https://example.com/policy/1",
                    "医保政策调整",
                    "公开征求意见",
                )
            ],
            on_item_stored=cognition.process_item,
        )
        forecasts = ForecastService(database)
        trends = TrendService(database)
        secret_store = DpapiSecretStore(
            Path(self.temp_dir.name) / "secrets" / "ai-token.dpapi",
            protect=lambda value: bytes(byte ^ 0xA5 for byte in value),
            unprotect=lambda value: bytes(byte ^ 0xA5 for byte in value),
        )
        ai_settings = AiSettingsService(database, secret_store)
        local = LocalHeuristicProvider()
        queue = JudgmentQueue(
            database,
            providers={"local": local},
            bundle_loader=lambda cluster_id: build_public_bundle(
                cognition.get_cluster(cluster_id), cognition.get_cluster(cluster_id)["items"]
            ),
            local_provider=local,
        )
        impacts = ImpactService(database, interests, forecasts)
        notifications = NotificationService(database, notifier=lambda title, body: None)
        controller = CognitionController(
            database,
            cognition,
            trends,
            queue,
            impacts,
            notifications,
            ai_settings,
            now=lambda: datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
        )
        self.desktop = RecordingDesktop()
        self.cognition_operation = SwitchableCognitionOperation(
            CognitionOperation(controller)
        )
        from yuanjian_app.backup import BackupService
        from yuanjian_app.diagnostics import DiagnosticsService
        from yuanjian_app.mobile_export import MobileExportService
        from yuanjian_app.retention import RetentionService
        from yuanjian_app.system_settings import SystemSettingsService

        self.backup_service = BackupService(
            database, Path(self.temp_dir.name) / "backups"
        )
        self.mobile_export = MobileExportService(
            Path(self.temp_dir.name) / "mobile"
        )
        self.services = Services(
            forecasts,
            interests,
            signals,
            knowledge,
            external,
            cognition,
            trends,
            controller,
            notifications,
            impacts,
            None,
            ai_settings,
            cognition_operation=self.cognition_operation,
            desktop=self.desktop,
            system_settings=SystemSettingsService(database),
            diagnostics=DiagnosticsService(
                database,
                external=external,
                ai_settings=ai_settings,
                judgment_queue=queue,
                backup_service=self.backup_service,
            ),
            backup_service=self.backup_service,
            retention_service=RetentionService(database),
            mobile_export=self.mobile_export,
        )
        self.server = create_server("127.0.0.1", 0, "test-token", self.services)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def test_server_rejects_non_loopback_binding(self):
        with self.assertRaisesRegex(ValueError, "只允许本机访问"):
            create_server("0.0.0.0", 0, "token", self.services)

    def test_post_without_session_token_is_forbidden(self):
        request = urllib.request.Request(
            self.base_url + "/api/events",
            data=json.dumps({"text": "新事件"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)

        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_window_show_requires_token_and_wakes_the_existing_window(self):
        request = urllib.request.Request(
            self.base_url + "/api/window/show",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "wrong-token",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

        status, payload = self.post_json("/api/window/show", {})

        self.assertEqual((status, payload["status"]), (200, "shown"))
        self.assertEqual(self.desktop.shown, 1)

    def test_monitoring_toggle_returns_the_new_running_state(self):
        _, paused = self.post_json("/api/monitoring/toggle", {})
        _, resumed = self.post_json("/api/monitoring/toggle", {})

        self.assertFalse(paused["monitoring"])
        self.assertTrue(resumed["monitoring"])

    def test_overlapping_cognition_run_returns_conflict_without_leaking_details(self):
        self.cognition_operation.busy = True
        request = urllib.request.Request(
            self.base_url + "/api/cognition/run",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "test-token",
            },
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)

        self.assertEqual(raised.exception.code, 409)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        raised.exception.close()
        self.assertEqual(payload["error"]["code"], "operation_busy")
        self.assertEqual(payload["error"]["message"], "认知任务正在运行，请稍候")

    def test_home_page_is_local_chinese_ui(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode("utf-8")

        self.assertIn('<html lang="zh-CN">', body)
        self.assertIn("个人利益预知", body)
        self.assertNotIn('src="https://', body)
        self.assertNotIn('href="https://', body)

    def test_home_page_exposes_formal_forecast_and_safe_exit_controls(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode("utf-8")

        self.assertIn("告诉远见", body)
        self.assertIn("安全退出", body)
        self.assertIn('id="shutdown"', body)

    def test_home_page_is_a_six_entry_terminal_shell(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(body.count('class="nav-item"'), 6)
        for view in ("today", "calib", "sources", "diag", "settings", "tell"):
            self.assertIn(f'data-view="{view}"', body)
        self.assertIn("今日远见", body)
        self.assertIn("校准面板", body)
        self.assertIn("源管理", body)
        self.assertIn("诊断中心", body)
        self.assertIn("设置", body)

    def test_home_page_makes_today_the_default_view(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode("utf-8")

        self.assertIn("后台监控", body)
        self.assertIn('id="view-root"', body)
        self.assertIn('id="toast"', body)
        self.assertIn('aria-live="polite"', body)
        with urllib.request.urlopen(self.base_url + "/js/app.js", timeout=2) as response:
            script = response.read().decode("utf-8")
        # v1.0 行动雷达：app.js 改用 renderView() 重拉当前视图，
        # 不再直接调用 /api/cognition/run。/api/shutdown 与 hash 路由仍保留。
        self.assertIn("/api/shutdown", script)
        self.assertIn("#/", script)
        with urllib.request.urlopen(
            self.base_url + "/js/views/today.js", timeout=2
        ) as response:
            today = response.read().decode("utf-8")
        # v1.0 行动雷达首页：并行拉取风险面板 + 预测进度，按 L4/L3 渲染行动卡。
        self.assertIn("/api/risk-dashboard", today)
        self.assertIn("/api/forecasts/progress", today)
        self.assertIn("alert_level", today)

    def test_personal_behavior_input_is_one_step_from_the_main_navigation(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            body = response.read().decode("utf-8")
        with urllib.request.urlopen(
            self.base_url + "/js/views/tell.js", timeout=2
        ) as response:
            script = response.read().decode("utf-8")

        self.assertIn('data-view="tell"', body)
        self.assertIn("/api/events", script)

    def test_static_assets_are_served_from_the_whitelist_only(self):
        for asset, content_type in (
            ("/js/ui_core.js", "text/javascript"),
            ("/css/tokens.css", "text/css"),
            ("/fonts/JetBrainsMono-Regular.woff2", "font/woff2"),
        ):
            with self.subTest(asset=asset):
                with urllib.request.urlopen(
                    self.base_url + asset, timeout=2
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers.get_content_type(), content_type
                    )
        with urllib.request.urlopen(self.base_url + "/js/ui_core.js", timeout=2) as response:
            helper = response.read().decode("utf-8")

        self.assertIn("export function evidenceLabel", helper)
        self.assertNotIn("https://", helper)
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(self.base_url + "/secret.js", timeout=2)
        self.assertEqual(missing.exception.code, 404)

    def test_home_page_loads_module_views_for_today_and_diagnosis(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            home = response.read().decode("utf-8")

        self.assertIn('<script type="module" src="/js/app.js"></script>', home)
        for asset, marker in (
            # v1.0 行动雷达：today.js 改用风险面板接口；
            # diag/calib/settings 仍按原契约断言。
            ("/js/views/today.js", "/api/risk-dashboard"),
            ("/js/views/diag.js", "/api/diagnostics"),
            ("/js/views/calib.js", "/api/calibration"),
            ("/js/views/settings.js", "/api/settings/backup"),
        ):
            with self.subTest(asset=asset):
                with urllib.request.urlopen(
                    self.base_url + asset, timeout=2
                ) as response:
                    script = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn(marker, script)
                # 只禁止真实外链引用；输入框 placeholder 提示文本不算。
                self.assertNotIn('src="https://', script)
                self.assertNotIn("fetch('https://", script)
                self.assertNotIn('fetch("https://', script)

    def test_forecast_api_returns_json(self):
        request = urllib.request.Request(
            self.base_url + "/api/forecasts",
            headers={"X-YuanJian-Token": "test-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, {"forecasts": [], "total": 0})

    def test_external_source_rule_refresh_and_radar_apis_form_a_complete_flow(self):
        status, source = self.post_json(
            "/api/external/sources",
            {
                "name": "官方政策源",
                "kind": "rss",
                "endpoint": "https://example.com/feed.xml",
            },
        )
        _, rule = self.post_json(
            "/api/external/rules", {"query": "医保", "importance": 5}
        )
        _, refresh = self.post_json(
            "/api/external/refresh", {"source_id": source["source_id"]}
        )

        sources = self.get_json("/api/external/sources")
        rules = self.get_json("/api/external/rules")
        radar = self.get_json("/api/external/radar")

        self.assertEqual(status, 201)
        self.assertEqual(rule["rule_id"], rules["rules"][0]["rule_id"])
        self.assertEqual(refresh["new_count"], 1)
        self.assertEqual(sources["sources"][0]["last_status"], "ok")
        self.assertEqual(radar["items"][0]["title"], "医保政策调整")

        _, paused = self.post_json(
            f"/api/external/sources/{source['source_id']}/enabled", {"enabled": False}
        )
        self.assertFalse(paused["enabled"])

    def test_event_api_persists_signal_and_interest_api_lists_filters(self):
        status, created = self.post_json(
            "/api/events", {"text": "明天预计支付12000元医疗费用", "occurred_at": "2026-08-06"}
        )

        self.assertEqual(status, 201)
        self.assertEqual(created["signal"]["alert_level"], "L4")
        signal_request = urllib.request.Request(
            self.base_url + "/api/signals",
            headers={"X-YuanJian-Token": "test-token"},
        )
        interest_request = urllib.request.Request(
            self.base_url + "/api/interests",
            headers={"X-YuanJian-Token": "test-token"},
        )
        with urllib.request.urlopen(signal_request, timeout=2) as response:
            signals = json.loads(response.read().decode("utf-8"))["signals"]
        with urllib.request.urlopen(interest_request, timeout=2) as response:
            interests = json.loads(response.read().decode("utf-8"))["objects"]

        self.assertEqual(len(signals), 1)
        self.assertEqual(len(interests), 7)

    def test_knowledge_api_discovers_indexes_and_lists_documents(self):
        request = urllib.request.Request(
            self.base_url + "/api/knowledge/vaults",
            headers={"X-YuanJian-Token": "test-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            vaults = json.loads(response.read().decode("utf-8"))["vaults"]

        status, result = self.post_json("/api/knowledge/index", {"path": vaults[0]["path"]})
        documents_request = urllib.request.Request(
            self.base_url + "/api/knowledge/documents?q=" + urllib.parse.quote("测试"),
            headers={"X-YuanJian-Token": "test-token"},
        )
        with urllib.request.urlopen(documents_request, timeout=2) as response:
            documents = json.loads(response.read().decode("utf-8"))["documents"]

        self.assertEqual(status, 201)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(documents[0]["title"], "测试知识")

    def valid_forecast(self, **changes):
        data = {
            "forecast_id": "F-HTTP-1",
            "title": "本地接口预测",
            "resolution_criteria": "到期可以核验",
            "window_start": "2026-08-06",
            "window_end": "2026-08-31",
            "probability": 0.65,
            "confidence": "medium",
            "alert_level": "L2",
            "privacy_level": "P2",
        }
        data.update(changes)
        return data

    def post_json(self, path, payload, token="test-token"):
        def fetch():
            request = urllib.request.Request(
                self.base_url + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-YuanJian-Token": token,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        return transient_retry(fetch)

    def get_json(self, path, token="test-token"):
        def fetch():
            request = urllib.request.Request(
                self.base_url + path, headers={"X-YuanJian-Token": token}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        return transient_retry(fetch)

    def request_json(self, path, payload, method, token="test-token"):
        def fetch():
            request = urllib.request.Request(
                self.base_url + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-YuanJian-Token": token,
                },
                method=method,
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        return transient_retry(fetch)

    def test_settings_get_put_and_calibration_diagnostics_contract(self):
        # GET 默认值
        self.assertEqual(self.get_json("/api/settings/backup")["hour"], 3)
        self.assertTrue(self.get_json("/api/settings/learning")["enabled"])
        # PUT 持久化
        status, saved = self.request_json(
            "/api/settings/backup", {"enabled": True, "hour": 5}, "PUT"
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved, {"enabled": True, "hour": 5, "keep": 7})
        self.assertEqual(self.get_json("/api/settings/backup")["hour"], 5)
        status, saved = self.request_json(
            "/api/settings/learning", {"enabled": False}, "PUT"
        )
        self.assertEqual(saved, {"enabled": False})
        # PUT 非法值 → 400
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request_json(
                "/api/settings/retention", {"enabled": True, "days": 2}, "PUT"
            )
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        # 校准端点：扁平字段 + candidates 数组
        calibration = self.get_json("/api/calibration")
        for key in ("hit_rate", "false_positive_rate", "brier", "resolved_total"):
            self.assertIn(key, calibration)
        self.assertEqual(calibration["candidates"], [])
        # 诊断端点：六瓦片扁平字段
        diagnostics = self.get_json("/api/diagnostics")
        for key in ("sources_enabled", "sources_total", "db_bytes", "runtime"):
            self.assertIn(key, diagnostics)

    def test_mobile_summary_export_writes_local_html(self):
        status, payload = self.post_json("/api/export/mobile-summary", {})

        self.assertEqual(status, 201)
        self.assertTrue(str(payload["path"]).endswith(".html"))
        page = Path(payload["path"]).read_text(encoding="utf-8")
        self.assertIn("远见 · 今日摘要", page)
        self.assertNotIn("https://", page)

    def test_source_put_delete_and_opml_roundtrip(self):
        _, created = self.post_json(
            "/api/external/sources",
            {
                "name": "临时源",
                "kind": "rss",
                "url": "https://example.org/feed.xml",
                "region": "heyuan",
                "category": "news",
            },
        )
        source_id = created["source_id"]
        # PUT 更新
        status, updated = self.request_json(
            f"/api/external/sources/{source_id}",
            {"name": "改名源", "url": "https://example.org/other.xml"},
            "PUT",
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated, {"source_id": source_id, "updated": ["endpoint", "name"]})
        sources = {
            item["source_id"]: item
            for item in self.get_json("/api/external/sources")["sources"]
        }
        self.assertEqual(sources[source_id]["url"], "https://example.org/other.xml")
        self.assertTrue(sources[source_id]["user_managed"])
        # DELETE 删除
        request = urllib.request.Request(
            self.base_url + f"/api/external/sources/{source_id}",
            headers={"X-YuanJian-Token": "test-token"},
            method="DELETE",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertNotIn(
            source_id,
            [item["source_id"] for item in self.get_json("/api/external/sources")["sources"]],
        )
        # OPML 导入（xml 键契约）
        opml = (
            '<opml version="2.0"><body><outline text="甲" xmlUrl="https://a.example/rss"/>'
            '<outline text="乙" xmlUrl="https://b.example/rss"/>'
            '<outline text="私网" xmlUrl="http://192.168.1.1/rss"/></body></opml>'
        )
        status, imported = self.post_json(
            "/api/external/sources/import-opml", {"xml": opml}
        )
        self.assertEqual(status, 201)
        self.assertEqual(imported["imported"], 2)
        self.assertEqual(imported["failed"], 1)

    def test_feedback_api_feeds_the_learning_loop(self):
        # 建一条最小事件链路，POST 反馈，校验流水与学习消费。
        from yuanjian_app.cognition import CognitionController  # noqa: F401

        with self.database().connect() as connection:
            connection.execute(
                "INSERT INTO event_clusters(cluster_id, title, first_seen_at, last_seen_at, evidence_hash, created_at, updated_at)"
                " VALUES ('C-FB', '反馈事件', '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z', 'h', '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO personal_impacts(impact_id, cluster_id, judgment_id, interest_id, impact_score, alert_level, components_json, reason, created_at, updated_at)"
                " VALUES ('P-FB', 'C-FB', 'J-FB', 'I-1', 0.5, 'L2', '{}', '测试', '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')"
            )
        status, payload = self.post_json(
            "/api/cognition/clusters/C-FB/feedback", {"action": "false_positive"}
        )
        self.assertEqual(status, 200)
        with self.database().connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM feedback_events"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def database(self):
        return self._database

    def test_create_forecast_api_records_formal_prediction(self):
        status, created = self.post_json("/api/forecasts", self.valid_forecast())

        self.assertEqual(status, 201)
        self.assertEqual(created["version"], 1)
        request = urllib.request.Request(
            self.base_url + "/api/forecasts",
            headers={"X-YuanJian-Token": "test-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["forecasts"][0]["forecast_id"], "F-HTTP-1")

    def test_create_forecast_api_reports_duplicate_identity_as_conflict(self):
        self.post_json("/api/forecasts", self.valid_forecast())

        request = urllib.request.Request(
            self.base_url + "/api/forecasts",
            data=json.dumps(self.valid_forecast()).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "test-token",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()

    def test_add_forecast_version_api_deduplicates_identical_revision(self):
        self.post_json("/api/forecasts", self.valid_forecast())

        _, changed = self.post_json(
            "/api/forecasts/F-HTTP-1/versions",
            self.valid_forecast(probability=0.80),
        )
        _, duplicate = self.post_json(
            "/api/forecasts/F-HTTP-1/versions",
            self.valid_forecast(probability=0.80),
        )

        self.assertEqual(changed, {"forecast_id": "F-HTTP-1", "version": 2, "duplicate": False})
        self.assertEqual(duplicate, {"forecast_id": "F-HTTP-1", "version": 2, "duplicate": True})

    def test_shutdown_api_requires_token_and_stops_server(self):
        forbidden = urllib.request.Request(
            self.base_url + "/api/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(forbidden, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

        status, payload = self.post_json("/api/shutdown", {})
        self.thread.join(timeout=2)

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "shutting_down"})
        self.assertFalse(self.thread.is_alive())

    def test_cognition_run_returns_cluster_judgment_impacts_and_candidate(self):
        self.services.external.fetcher = lambda source: [
            ExternalItem(
                source["source_id"],
                source["name"],
                f"https://{source['source_id'].lower()}.example/policy",
                "广东医保报销比例升至70%",
                "政策本月实施",
                "2026-08-11T00:00:00Z",
            )
        ]
        for index in range(1, 4):
            _, source = self.post_json(
                "/api/external/sources",
                {
                    "source_id": f"S-C{index}",
                    "name": f"来源{index}",
                    "kind": "rss",
                    "endpoint": f"https://feed{index}.example/rss",
                },
            )
            self.post_json("/api/external/refresh", {"source_id": source["source_id"]})

        _, run = self.post_json("/api/cognition/run", {})
        clusters = self.get_json("/api/cognition/clusters")["clusters"]
        detail = self.get_json(
            f"/api/cognition/clusters/{clusters[0]['cluster_id']}"
        )

        self.assertGreaterEqual(run["judgments"]["succeeded"], 1)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(detail["items"]), 3)
        self.assertIn("evidence_level", detail)
        self.assertTrue(detail["judgment"]["causal_chain"])
        self.assertIn("uncertainties", detail["judgment"])
        self.assertTrue(detail["judgment"]["horizons"])
        self.assertTrue(detail["impacts"])
        self.assertTrue(detail["impacts"][0]["candidate"])

    def test_post_without_body_is_accepted_for_endpoints_that_need_no_payload(self):
        """Regression: a fetch() call with {method:'POST'} and no body
        omits Content-Length entirely, which used to fail with '请求内容
        为空或过大' on every endpoint — including /api/cognition/run
        that the Action Home '立即更新判断' button triggers. Endpoints
        that do not need payload must accept an empty request.
        """
        # Simulate browser fetch() with method: 'POST' and no body/data
        # argument: urllib with data=None does NOT set Content-Length.
        request = urllib.request.Request(
            self.base_url + "/api/cognition/run",
            data=None,
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "test-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        # cognition/run returns a dict with judgments/candidates count —
        # the exact shape is not what we're testing; we only need to prove
        # the empty-body request was accepted rather than rejected as
        # "请求内容为空或过大".
        self.assertIn("judgments", payload)

    def test_post_with_oversized_body_returns_bad_request(self):
        """The size guard must still fire — only empty bodies should be
        accepted, not unbounded ones."""
        oversized = "x" * 70000  # > 65536 byte limit
        request = urllib.request.Request(
            self.base_url + "/api/events",
            data=json.dumps({"text": oversized}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "test-token",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)
            body = json.loads(error.read().decode("utf-8"))
            self.assertIn("请求内容为空或过大", body["error"]["message"])
        else:
            self.fail("oversized body should have been rejected")

    def test_ai_settings_never_return_secret_and_posts_require_token(self):
        status, saved = self.post_json(
            "/api/settings/ai",
            {
                "enabled": True,
                "endpoint": "https://api.openai.com/v1/responses",
                "model": "explicit-model",
                "token": "private-api-token",
            },
        )
        visible = self.get_json("/api/settings/ai")

        self.assertEqual(status, 200)
        self.assertTrue(saved["configured"])
        self.assertNotIn("token", visible)
        self.assertNotIn("private-api-token", json.dumps(visible))
        request = urllib.request.Request(
            self.base_url + "/api/settings/ai",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_feedback_stays_in_local_personal_impacts(self):
        cluster_id, judgment_id = "C-feedback", "J-feedback"
        now = "2026-08-11T08:00:00Z"
        result = LocalHeuristicProvider().analyze(
            build_public_bundle(
                {"cluster_id": cluster_id, "title": "政策", "summary": "", "evidence_level": "E1", "categories": ["policy"]},
                [{"source_id": "S-1", "title": "通知", "summary": "公开", "canonical_url": "https://news.example/1", "published_at": now}],
            )
        )
        with self.services.cognition.database.connect() as connection:
            connection.execute("INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,last_seen_at,evidence_hash,categories_json,latest_judgment_id,created_at,updated_at) VALUES (?,?,'',?,?,?,?,?,?,?)", (cluster_id,"政策",now,now,"hash",'["policy"]',judgment_id,now,now))
            connection.execute("INSERT INTO judgments VALUES (?,?,?,?,?,?)", (judgment_id,cluster_id,"local","hash",json.dumps(result.to_dict(),ensure_ascii=False),now))
        self.services.impacts.map_judgment(cluster_id, judgment_id)

        self.post_json(f"/api/cognition/clusters/{cluster_id}/feedback", {"action": "false_positive"})

        with self.services.cognition.database.connect() as connection:
            labels = [row[0] for row in connection.execute("SELECT user_label FROM personal_impacts WHERE cluster_id=?", (cluster_id,))]
        self.assertTrue(labels)
        self.assertEqual(set(labels), {"false_positive"})

    def test_paged_radar_cluster_and_notification_apis_form_action_center_contract(self):
        """雷达 / 待研判 / 提醒 三个分页接口拼成"行动中心"的契约。

        关于这条用例的偶发抖动（2026-09-15 全量 14 轮里红过 1 次，形态是 ERROR
        而不是断言失败；单跑 30/30 绿）—— 先排除一个**看起来像但不成立**的假设：
        `http_api.py` 的 `_post_external_refresh` 是**同步**调用
        `services.external.refresh_source(source_id)` 之后才返回的，所以**不存在**
        「刷新还没写完就去 GET」的竞态。这条用例的失败形态是传输层异常抛出，
        与 `total==1` 这类语义断言无关。

        因此本文件只在客户端传输层对
        `RemoteDisconnected` / `ConnectionResetError` / `TimeoutError` 三类瞬时异常
        做**有界重试**（最多 2 次，计数落 `build-artifacts/http-api-transient-retries.txt`），
        下面的语义断言一个都没有改动。
        """
        self.post_json(
            "/api/external/sources",
            {
                "source_id": "S-PAGE",
                "name": "分页来源",
                "kind": "rss",
                "endpoint": "https://page.example/rss",
            },
        )
        self.post_json(
            "/api/external/rules",
            {"rule_id": "W-PAGE", "query": "医保", "importance": 5},
        )
        self.post_json("/api/external/refresh", {"source_id": "S-PAGE"})

        radar = self.get_json("/api/external/radar?limit=1&offset=0&q=%E5%8C%BB%E4%BF%9D")
        clusters = self.get_json("/api/cognition/clusters?limit=1&offset=0&needs_judgment=true")

        self.assertEqual((radar["total"], radar["limit"], radar["offset"]), (1, 1, 0))
        self.assertEqual((clusters["total"], clusters["limit"], clusters["offset"]), (1, 1, 0))
        self.assertEqual(clusters["clusters"], clusters["items"])

        notification = self.services.notifications.consider(
            {
                "impact_id": "P-PAGE",
                "cluster_id": clusters["items"][0]["cluster_id"],
                "alert_level": "L3",
                "evidence_hash": "page-hash",
                "action_window_hours": 24,
            },
            "需要处理",
        )
        unread = self.get_json("/api/notifications?limit=20&offset=0&status=unread")
        self.assertEqual(unread["total"], 1)
        self.assertEqual(unread["notifications"][0]["notification_id"], notification["notification_id"])

        _, marked = self.post_json("/api/notifications/read-all", {})
        self.assertEqual(marked["updated"], 1)
        self.assertEqual(self.get_json("/api/notifications?status=unread")["total"], 0)

    def test_risk_dashboard_returns_decisions_without_raw_news(self):
        self.post_json(
            "/api/external/sources",
            {
                "source_id": "S-RISK",
                "name": "公开测试源",
                "kind": "rss",
                "endpoint": "https://example.com/feed",
            },
        )

        dashboard = self.get_json("/api/risk-dashboard")

        self.assertEqual(
            set(dashboard),
            {"state", "summary", "counts", "items", "coverage", "generated_at"},
        )
        self.assertEqual(dashboard["coverage"], {"enabled": 1, "healthy": 0})
        serialized = json.dumps(dashboard, ensure_ascii=False)
        self.assertNotIn("canonical_url", serialized)
        self.assertNotIn("last_error", serialized)
        self.assertNotIn("watch_rules", serialized)

    def test_invalid_pagination_returns_bad_request(self):
        request = urllib.request.Request(
            self.base_url + "/api/cognition/clusters?limit=0",
            headers={"X-YuanJian-Token": "test-token"},
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)

        self.assertEqual(raised.exception.code, 400)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        raised.exception.close()

    def test_mark_all_notifications_requires_session_token(self):
        request = urllib.request.Request(
            self.base_url + "/api/notifications/read-all",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)

        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_missing_cluster_and_notification_return_specific_readable_errors(self):
        cluster_request = urllib.request.Request(
            self.base_url + "/api/cognition/clusters/C-missing",
            headers={"X-YuanJian-Token": "test-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cluster_error:
            urllib.request.urlopen(cluster_request, timeout=2)
        self.assertEqual(cluster_error.exception.code, 404)
        cluster_payload = json.loads(cluster_error.exception.read().decode("utf-8"))
        cluster_error.exception.close()
        self.assertEqual(cluster_payload["error"], {"code": "cluster_not_found", "message": "事件不存在"})

        notification_request = urllib.request.Request(
            self.base_url + "/api/notifications/D-missing/read",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "test-token",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as notification_error:
            urllib.request.urlopen(notification_request, timeout=2)
        self.assertEqual(notification_error.exception.code, 404)
        notification_payload = json.loads(notification_error.exception.read().decode("utf-8"))
        notification_error.exception.close()
        self.assertEqual(notification_payload["error"], {"code": "notification_not_found", "message": "提醒不存在"})

    def test_security_headers_cover_pages_and_api_responses(self):
        """静态页面与 JSON 接口都必须带全安全头。

        此前只有静态资源设了 CSP，且 CSP 只写了 default-src 系列；
        Referrer-Policy 尤其关键 —— 会话令牌通过 URL query 交付，
        referrer 一旦外泄就等于泄漏令牌。
        """
        from yuanjian_app.http_api import SECURITY_HEADERS

        expected = {
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                "object-src 'none'; base-uri 'self'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
        }
        self.assertEqual(dict(SECURITY_HEADERS), expected)

        page = urllib.request.urlopen(self.base_url + "/", timeout=2)
        try:
            for name, value in expected.items():
                self.assertEqual(page.headers.get(name), value, f"页面缺少 {name}")
        finally:
            page.close()

        api_request = urllib.request.Request(
            self.base_url + "/api/forecasts",
            headers={"X-YuanJian-Token": "test-token"},
        )
        api = urllib.request.urlopen(api_request, timeout=2)
        try:
            for name, value in expected.items():
                self.assertEqual(api.headers.get(name), value, f"接口缺少 {name}")
            self.assertEqual(api.headers.get("Cache-Control"), "no-store")
        finally:
            api.close()

    def test_unauthorized_response_also_carries_security_headers(self):
        """错误路径同样不能漏头 —— 403 也是浏览器会渲染的响应。"""
        request = urllib.request.Request(self.base_url + "/api/forecasts")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        error = raised.exception
        self.assertEqual(error.code, 403)
        self.assertEqual(error.headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(error.headers.get("X-Content-Type-Options"), "nosniff")
        error.close()

    def test_token_comparison_uses_constant_time_helper(self):
        """定时安全比较：源码里必须是 hmac.compare_digest，而不是裸 ==。"""
        source = (Path(__file__).resolve().parents[1] / "src" / "yuanjian_app" / "http_api.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("hmac.compare_digest(provided, token)", source)
        self.assertNotIn('self.headers.get("X-YuanJian-Token") == token', source)

    def test_app_version_endpoint_reports_the_package_version(self):
        from yuanjian_app import __version__

        unauthorized = urllib.request.Request(self.base_url + "/api/app/version")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(unauthorized, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

        request = urllib.request.Request(
            self.base_url + "/api/app/version",
            headers={"X-YuanJian-Token": "test-token"},
        )
        response = urllib.request.urlopen(request, timeout=2)
        try:
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            response.close()

        self.assertEqual(payload["version"], __version__)
        self.assertIn("q719563786/Foresight", payload["releases_url"])

    def test_update_check_is_unavailable_until_wired(self):
        """未装配更新检查时必须明确 503，而不是静默返回假结果。"""
        request = urllib.request.Request(
            self.base_url + "/api/update-check",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-YuanJian-Token": "test-token",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()

    def test_update_check_requires_token(self):
        request = urllib.request.Request(
            self.base_url + "/api/update-check",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_removed_dashboard_endpoint_is_not_served(self):
        """`/api/dashboard` 已删除，不该再被响应。

        它界面从未调用、无测试覆盖，却会把全部预测正文读出来再在 Python 里筛，
        实测在真实数据上返回 17.5 MB。功能与界面在用的 /api/risk-dashboard 重叠，
        因此整体移除。这条测试防止它被无意间恢复。
        """
        request = urllib.request.Request(
            self.base_url + "/api/dashboard",
            headers={"X-YuanJian-Token": "test-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 404)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        raised.exception.close()
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_risk_dashboard_endpoint_still_serves_the_action_board(self):
        """界面真正依赖的接口必须还在。"""
        request = urllib.request.Request(
            self.base_url + "/api/risk-dashboard",
            headers={"X-YuanJian-Token": "test-token"},
        )
        response = urllib.request.urlopen(request, timeout=10)
        try:
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            response.close()
        self.assertIn("state", payload)


class TransientRetryPolicyTests(unittest.TestCase):
    """重试策略本身要能测、要能红 —— 不是"加了重试就完事"。

    这四条钉住的是**边界**：
    - 三类瞬时异常确实会被重试，且**恰好** 2 次；
    - 额度用完必须把原异常抛出（不许吞掉、不许无限重试）；
    - 非瞬时的错（尤其 `HTTPError` 和裸 `ValueError`）**一次都不许重试**；
    - `URLError` 要拆 `reason` 看，不能整类放行。

    没有这组断言的话，"重试"很容易被后来的人改成 `except Exception: retry`
    —— 那才是真正的"把测试调软"，而且会把真实故障掩盖掉。
    """

    def setUp(self):
        self.before_count = TRANSIENT_RETRIES["count"]
        self.before_details = len(TRANSIENT_RETRIES["details"])
        self.addCleanup(self.restore_counter)

    def restore_counter(self):
        """别让本类的自造重试污染真·重试计数（收尾要落盘的是真实发生的那次）。"""
        TRANSIENT_RETRIES["count"] = self.before_count
        del TRANSIENT_RETRIES["details"][self.before_details :]

    def retries(self):
        return TRANSIENT_RETRIES["count"] - self.before_count

    def test_transient_errors_are_retried_exactly_twice_then_raised(self):
        for error in (
            http.client.RemoteDisconnected("connection closed"),
            ConnectionResetError(10054, "连接被重置"),
            TimeoutError("timed out"),
            urllib.error.URLError(ConnectionResetError(10054, "连接被重置")),
        ):
            with self.subTest(error=type(error).__name__):
                calls = []

                def fetch(error=error, calls=calls):
                    calls.append(1)
                    raise error

                with self.assertRaises(type(error)):
                    transient_retry(fetch, backoff=0)

                self.assertEqual(
                    len(calls), 3, "瞬时异常应重试 2 次（1 次原始 + 2 次重试）后抛出"
                )
        self.assertEqual(self.retries(), 8, "四种瞬时异常各该重试 2 次")

    def test_a_transient_error_that_clears_up_is_not_raised(self):
        """重试的意义：第二/第三次成功就必须返回结果，而不是把异常漏给调用方。"""
        attempts = []

        def fetch():
            attempts.append(1)
            if len(attempts) < 2:
                raise ConnectionResetError(10054, "连接被重置")
            return "ok"

        self.assertEqual(transient_retry(fetch, backoff=0), "ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.retries(), 1)

    def test_http_errors_are_never_retried(self):
        """`HTTPError` 是服务端给了真·HTTP 响应，属于产品行为，必须原样暴露。

        它继承自 `URLError`，所以只按 `URLError` 判会把它一起放行 —— 这条就是防那个。
        """
        calls = []

        def fetch():
            calls.append(1)
            raise urllib.error.HTTPError(
                "http://127.0.0.1/x", 500, "boom", {}, None
            )

        with self.assertRaises(urllib.error.HTTPError):
            transient_retry(fetch, backoff=0)

        self.assertEqual(len(calls), 1, "HTTPError 被重试了 —— 真故障会被掩盖")
        self.assertEqual(self.retries(), 0)

    def test_non_transient_errors_are_never_retried(self):
        """裸异常（含 `URLError` 但 reason 不是那三类）一律一次都不重试。"""
        for error in (
            ValueError("语义错误"),
            urllib.error.URLError("域名解析失败"),
            OSError(2, "No such file or directory"),
        ):
            with self.subTest(error=repr(error)):
                calls = []

                def fetch(error=error, calls=calls):
                    calls.append(1)
                    raise error

                with self.assertRaises(BaseException):
                    transient_retry(fetch, backoff=0)

                self.assertEqual(len(calls), 1, "非瞬时异常被重试了")

    def test_the_retry_limit_is_small_and_finite(self):
        """把它写死在断言里：有人把上限改成 50 或无限，这条要红。"""
        self.assertEqual(TRANSIENT_RETRY_LIMIT, 2)


TOKEN = "routing-token"
SECURITY_HEADER_NAMES = tuple(name for name, _value in SECURITY_HEADERS)


class StubService:
    """按方法名预置返回值的假服务，并记录每次调用。

    与 `HttpApiTests` 里那套"真数据库 + 真服务"不同：这里只关心
    **HTTP 边界与路由分发契约**，所以把服务替换成可记录的桩，
    既让每条路由都被走到，又能断言"到底调了哪个方法、传了什么参数"。

    预置值可以是一个**可调用对象**，此时会用调用参数回调它 —— 这让桩能
    "校验自己的入参"（比如 `get_forecast` 只认 "F-1"）。这一点很关键：
    没有它，路由被写错、把 `/api/forecasts/progress` 当成预测 id 分发下去时，
    桩会照样回 200，端到端用例就成了睁眼瞎。
    """

    def __init__(self, **results):
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "_results", results)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self._results.get(name, {})
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return value(*args, **kwargs)
            return value

        return call

    def called(self, name):
        return [entry for entry in self.calls if entry[0] == name]


def accepts_only(expected, payload):
    """构造一个"只认某个入参"的桩返回值，用于暴露错误分发。"""

    def check(value):
        if value != expected:
            raise KeyError(value)

        return payload

    return check


class StubOperation:
    """`cognition_operation` 的桩：只是两个被读的属性，不是方法。"""

    def __init__(self, running=False, started_at_monotonic=None):
        self.running = running
        self.started_at_monotonic = started_at_monotonic

    def run(self, source):
        return {"source": source}


def build_stub_services(**overrides):
    """一套能让全部 29 条 GET 路由都返回 200 的桩服务。"""
    stubs = {
        "forecasts": StubService(
            list_forecasts=([], 0),
            progress_summary={"total": 0},
            list_overdue=[],
            calibration_summary={},
            score_summary={"score": 0},
            get_forecast=accepts_only("F-1", {"forecast_id": "F-1"}),
            add_version={"version": 2},
            resolve={"outcome": "hit"},
            create_forecast={"forecast_id": "F-new"},
        ),
        "interests": StubService(list_objects=[], list_links=[]),
        "signals": StubService(list_signals=[], ingest={"candidate": {}, "signal": {}}),
        "knowledge": StubService(
            discover_vaults=[], list_documents=[], index_vault={"indexed": 0}
        ),
        "external": StubService(
            radar_page={"items": []},
            list_sources=[],
            list_rules=[],
            add_source="S-new",
            add_watch_rule="R-new",
            import_opml={},
            bulk_set_enabled={"updated": 0},
            set_source_enabled={},
            set_rule_enabled={},
            refresh_source={},
            update_source={},
            delete_source={},
            delete_watch_rule={},
        ),
        "cognition": StubService(
            list_clusters_page={"items": []},
            get_cluster={"cluster_id": "C-1"},
        ),
        "trends": StubService(summary={}),
        "cognition_controller": StubService(
            status={},
            risk_dashboard={"state": "ok"},
            now=lambda: datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
            cluster_detail=accepts_only("C-1", {"cluster_id": "C-1"}),
            list_jobs=[],
            run_once={"clusters": 0},
            feedback={"ok": True},
        ),
        "notifications": StubService(
            list_page={"items": []}, mark_all_read={}, mark_read={}
        ),
        "impacts": StubService(
            pending_candidates=[], confirm_candidate={"impact_id": "P-1"}
        ),
        "startup": StubService(
            status={"installed": False, "available": True},
            install={"installed": True},
            remove={"installed": False},
        ),
        "ai_settings": StubService(get={}, save={}),
        "cognition_operation": StubOperation(running=True, started_at_monotonic=12.5),
        "desktop": RecordingDesktop(),
        "system_settings": StubService(get_learning={}, put_learning={}),
        "diagnostics": StubService(snapshot={"state": "ok"}),
        "backup_service": StubService(get_setting={}, put_setting={}),
        "retention_service": StubService(get_setting={}, put_setting={}),
        "mobile_export": StubService(export={"generated": True}),
        "update_check": StubService(check={"latest": "1.0.0", "update": False}),
    }
    stubs.update(overrides)
    services = Services(
        stubs["forecasts"],
        stubs["interests"],
        stubs["signals"],
        stubs["knowledge"],
        stubs["external"],
        stubs["cognition"],
        stubs["trends"],
        stubs["cognition_controller"],
        stubs["notifications"],
        stubs["impacts"],
        stubs["startup"],
        stubs["ai_settings"],
        cognition_operation=stubs["cognition_operation"],
        desktop=stubs["desktop"],
        system_settings=stubs["system_settings"],
        diagnostics=stubs["diagnostics"],
        backup_service=stubs["backup_service"],
        retention_service=stubs["retention_service"],
        mobile_export=stubs["mobile_export"],
        update_check=stubs["update_check"],
    )
    return services, stubs


def bare_services():
    """所有可选能力都未装配的服务，用来逼出 503 / 400 的 unavailable 分支。"""
    return Services(
        None, None, None, None, None, None, None, None, None, None, None, None
    )


class RoutingTestCase(unittest.TestCase):
    """只做协议层验证：自带服务器 + 显式禁用代理的 opener。"""

    token = TOKEN

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.services, self.stubs = build_stub_services()
        self.start_server(self.services)

    def tearDown(self):
        self.stop_server()
        self.temp_dir.cleanup()

    def start_server(self, services, token=None):
        self.server = create_server(
            "127.0.0.1", 0, self.token if token is None else token, services
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        # 环境里可能设了 http_proxy，会把 127.0.0.1 的请求劫持成 502。
        # 显式装一个空 ProxyHandler，让这些用例不受外部环境影响。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, payload=None, token=None, raw_body=None):
        data = None
        if raw_body is not None:
            data = raw_body
        elif payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token is not False:
            request.add_header("X-YuanJian-Token", self.token if token is None else token)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            headers = error.headers
            error.close()
            return error.code, headers, body

    def body(self, method, path, **kwargs):
        status, _headers, raw = self.call(method, path, **kwargs)
        return status, json.loads(raw.decode("utf-8"))

    def raw_request(self, request_line, headers):
        """直接走 socket 发送原始请求。

        用来构造 `http.client` 不肯发的畸形头部（例如非整数的
        `Content-Length`）—— 这类输入正是服务端需要防御的。
        """
        with socket.create_connection(
            ("127.0.0.1", self.server.server_address[1]), timeout=10
        ) as connection:
            payload = (
                request_line + "\r\n" + "\r\n".join(headers) + "\r\n\r\n"
            ).encode("ascii")
            connection.sendall(payload)
            chunks = []
            while True:
                data = connection.recv(4096)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks)

    def assert_security_headers(self, headers, context=""):
        missing = [
            name for name in SECURITY_HEADER_NAMES if headers.get(name) is None
        ]
        self.assertEqual(missing, [], "缺少安全头 %r（%s）" % (missing, context))
        for name, value in SECURITY_HEADERS:
            self.assertEqual(
                headers.get(name), value, "%s 的值与契约不符（%s）" % (name, context)
            )


class RouteTableTests(RoutingTestCase):
    """路由表本身：匹配优先级、参数提取、构造期校验。"""

    def endpoint_of(self, method, path):
        route, params = match_route(method, path)
        return (route.endpoint if route else None), params

    def test_exact_route_wins_over_the_prefix_route_that_would_swallow_it(self):
        """`/api/forecasts/progress` 必须命中精确路由，而不是 `/api/forecasts/<id>`。

        这是路由表"声明顺序即优先级"的关键不变量：写反了会把 progress 当成
        一个预测 id 去查库，返回 404 forecast_not_found。
        """
        self.assertEqual(
            self.endpoint_of("GET", "/api/forecasts/progress"),
            ("_get_forecast_progress", {}),
        )
        self.assertEqual(
            self.endpoint_of("GET", "/api/forecasts/overdue"),
            ("_get_forecast_overdue", {}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/external/sources/import-opml"),
            ("_post_external_sources_import_opml", {}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/external/sources/bulk-enabled"),
            ("_post_external_sources_bulk_enabled", {}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/notifications/read-all"),
            ("_post_notifications_read_all", {}),
        )
        self.assertEqual(
            self.endpoint_of("GET", "/api/interests/objects"),
            ("_get_interest_objects", {}),
        )

    def test_prefix_route_extracts_the_whole_remainder(self):
        self.assertEqual(
            self.endpoint_of("GET", "/api/forecasts/F-1"),
            ("_get_forecast", {"forecast_id": "F-1"}),
        )
        self.assertEqual(
            self.endpoint_of("GET", "/api/cognition/clusters/C-9"),
            ("_get_cluster_detail", {"cluster_id": "C-9"}),
        )
        self.assertEqual(
            self.endpoint_of("DELETE", "/api/external/sources/S-1"),
            ("_delete_external_source", {"source_id": "S-1"}),
        )
        self.assertEqual(
            self.endpoint_of("PUT", "/api/external/sources/S-1"),
            ("_put_external_source", {"source_id": "S-1"}),
        )

    def test_prefix_suffix_routes_strip_only_the_suffix(self):
        self.assertEqual(
            self.endpoint_of("POST", "/api/external/sources/S-1/enabled"),
            ("_post_source_enabled", {"source_id": "S-1"}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/external/rules/R-1/enabled"),
            ("_post_rule_enabled", {"rule_id": "R-1"}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/forecasts/F-1/versions"),
            ("_post_forecast_versions", {"forecast_id": "F-1"}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/forecasts/F-1/resolve"),
            ("_post_forecast_resolve", {"forecast_id": "F-1"}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/notifications/N-1/read"),
            ("_post_notification_read", {"notification_id": "N-1"}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/cognition/candidates/P-1/confirm"),
            ("_post_candidate_confirm", {"impact_id": "P-1"}),
        )
        self.assertEqual(
            self.endpoint_of("POST", "/api/cognition/clusters/C-1/feedback"),
            ("_post_cluster_feedback", {"cluster_id": "C-1"}),
        )

    def test_prefix_suffix_entry_and_extraction_are_faithfully_replicated(self):
        """进入分支用 `startswith(P) and endswith(S)`，参数取"剩余段去掉末尾 S"。

        这两者并不完全等价：`_post_source_enabled` 里 `path.removeprefix(P)`
        对 `/api/external/sources/a/enabled/b/enabled` 会得到 `a/enabled/b`。
        路由表的文档明确说"如实复刻旧实现、不做顺手修正"，
        这里把该行为钉死，防止有人以"看起来是 bug"为由悄悄改掉语义。
        """
        route, params = match_route(
            "POST", "/api/external/sources/a/enabled/b/enabled"
        )
        self.assertEqual(route.endpoint, "_post_source_enabled")
        self.assertEqual(params, {"source_id": "a/enabled/b"})
        # 处理器随后会 rstrip("/")，但不会把中间的 "/" 当分隔符拆开
        self.assertEqual(params["source_id"].rstrip("/"), "a/enabled/b")

    def test_unknown_path_matches_nothing(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(
                    match_route(method, "/api/definitely-not-a-route"), (None, None)
                )
        self.assertEqual(match_route("PATCH", "/api/forecasts"), (None, None))

    def test_route_rejects_unknown_kind_and_empty_suffix(self):
        with self.assertRaises(ValueError):
            Route("GET", "bogus-kind", "/api/x", "_get_score")
        with self.assertRaises(ValueError):
            Route("POST", "prefix_suffix", "/api/x/", "_post_events", suffix="")

    def test_every_route_points_at_a_handler_that_exists(self):
        """路由表写错一个方法名就会变成运行期 500，必须在建表时就炸出来。"""
        self.assertTrue(ROUTES)
        for route in ROUTES:
            with self.subTest(route=route.path):
                self.assertIn(route.kind, ("exact", "prefix", "prefix_suffix"))
                self.assertTrue(route.endpoint.startswith("_"))
        self.assertEqual(
            set(routes_for("GET")) | set(routes_for("POST")) | set(routes_for("PUT"))
            | set(routes_for("DELETE")),
            set(ROUTES),
        )

    def test_routes_for_returns_only_that_method(self):
        self.assertTrue(all(r.method == "PUT" for r in routes_for("PUT")))
        self.assertEqual(routes_for("put"), routes_for("PUT"))
        self.assertEqual(routes_for("PATCH"), ())

    def test_static_root_resolution_supports_the_frozen_layout(self):
        """PyInstaller 冻结后静态资源在 `_MEIPASS/yuanjian_app/static`。"""
        bundled = resolve_static_root(__file__, bundle_root="C:/bundle")
        self.assertEqual(
            bundled.name, "static"
        )
        self.assertEqual(bundled.parent.name, "yuanjian_app")
        source = resolve_static_root(__file__)
        self.assertEqual(source.name, "static")


class GetRouteContractTests(RoutingTestCase):
    """每一条 GET 路由都必须 200，且带齐安全头。"""

    PREFIX_SAMPLES = {
        "/api/forecasts/": "/api/forecasts/F-1",
        "/api/cognition/clusters/": "/api/cognition/clusters/C-1",
    }

    def sample_path(self, route, query=""):
        if route.kind == "exact":
            return route.path + query
        return self.PREFIX_SAMPLES[route.path] + query

    def test_every_get_route_returns_200_with_security_headers(self):
        for route in routes_for("GET"):
            with self.subTest(path=route.path):
                status, headers, raw = self.call("GET", self.sample_path(route))
                self.assertEqual(status, 200, raw[:200])
                self.assert_security_headers(headers, route.path)
                self.assertIn("application/json", headers.get("Content-Type"))

    def test_get_routes_dispatch_to_the_expected_service_methods(self):
        expected = {
            "/api/forecasts/progress": ("forecasts", "progress_summary"),
            "/api/forecasts/overdue": ("forecasts", "list_overdue"),
            "/api/interests/objects": ("interests", "list_objects"),
            "/api/cognition/status": ("cognition_controller", "status"),
            "/api/cognition/candidates": ("impacts", "pending_candidates"),
            "/api/cognition/trends": ("trends", "summary"),
            "/api/cognition/jobs": ("cognition_controller", "list_jobs"),
            "/api/settings/startup": ("startup", "status"),
            "/api/settings/ai": ("ai_settings", "get"),
            "/api/settings/backup": ("backup_service", "get_setting"),
            "/api/settings/retention": ("retention_service", "get_setting"),
            "/api/settings/learning": ("system_settings", "get_learning"),
            "/api/diagnostics": ("diagnostics", "snapshot"),
            "/api/score": ("forecasts", "score_summary"),
            "/api/external/sources": ("external", "list_sources"),
            "/api/external/rules": ("external", "list_rules"),
            "/api/signals": ("signals", "list_signals"),
            "/api/knowledge/vaults": ("knowledge", "discover_vaults"),
            "/api/risk-dashboard": ("cognition_controller", "risk_dashboard"),
        }
        for path, (stub_name, method) in expected.items():
            with self.subTest(path=path):
                status, _headers, raw = self.call("GET", path)
                self.assertEqual(status, 200, raw[:200])
                self.assertTrue(
                    self.stubs[stub_name].called(method),
                    "%s 没有调用 %s.%s" % (path, stub_name, method),
                )

    def test_cognition_status_merges_the_running_operation(self):
        status, payload = self.body("GET", "/api/cognition/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["started_at_monotonic"], 12.5)

    def test_calibration_attaches_pending_candidates(self):
        status, payload = self.body("GET", "/api/calibration")
        self.assertEqual(status, 200)
        self.assertIn("candidates", payload)
        self.assertTrue(self.stubs["forecasts"].called("calibration_summary"))

    def test_paged_get_routes_forward_the_query_parameters(self):
        self.call("GET", "/api/forecasts?limit=5&offset=3")
        call = self.stubs["forecasts"].called("list_forecasts")[0]
        self.assertEqual(call[1], ())
        self.assertEqual(call[2], {"limit": 5, "offset": 3})

        self.call("GET", "/api/external/radar?limit=4&offset=1&q=" + urllib.parse.quote("医保"))
        call = self.stubs["external"].called("radar_page")[0]
        self.assertEqual(call[2], {"limit": 4, "offset": 1, "query": "医保"})

        self.call("GET", "/api/cognition/clusters?needs_judgment=true")
        call = self.stubs["cognition"].called("list_clusters_page")[0]
        self.assertIs(call[2]["needs_judgment"], True)

        self.call("GET", "/api/cognition/clusters?needs_judgment=false")
        call = self.stubs["cognition"].called("list_clusters_page")[-1]
        self.assertIs(call[2]["needs_judgment"], False)

        self.call("GET", "/api/notifications?status=sent")
        call = self.stubs["notifications"].called("list_page")[0]
        self.assertEqual(call[2]["status"], "sent")

    def test_knowledge_documents_forwards_the_search_term(self):
        self.call("GET", "/api/knowledge/documents?q=" + urllib.parse.quote("预算"))
        call = self.stubs["knowledge"].called("list_documents")[0]
        self.assertEqual(call[1], ("预算",))

    def test_invalid_needs_judgment_filter_returns_400(self):
        status, payload = self.body("GET", "/api/cognition/clusters?needs_judgment=maybe")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "待研判筛选无效")

    def test_non_numeric_pagination_returns_400(self):
        for query in ("?limit=abc", "?offset=abc"):
            with self.subTest(query=query):
                status, payload = self.body("GET", "/api/forecasts" + query)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["message"], "分页参数无效")

    def test_prefix_routes_decoded_url_encoded_identifiers(self):
        self.call("GET", "/api/cognition/clusters/%E4%B8%AD%E6%96%87%E7%B0%87")
        call = self.stubs["cognition_controller"].called("cluster_detail")[0]
        self.assertEqual(call[1], ("中文簇",))

        self.call("GET", "/api/forecasts/F%2F1")
        call = self.stubs["forecasts"].called("get_forecast")[0]
        self.assertEqual(call[1], ("F/1",))

    def test_prefix_route_404_codes_are_specific(self):
        self.stubs["cognition_controller"]._results["cluster_detail"] = KeyError("gone")
        status, payload = self.body("GET", "/api/cognition/clusters/C-missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "cluster_not_found")

        self.stubs["forecasts"]._results["get_forecast"] = KeyError("gone")
        status, payload = self.body("GET", "/api/forecasts/F-missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "forecast_not_found")

    def test_unknown_api_path_returns_404_with_security_headers(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                payload = {} if method in ("POST", "PUT") else None
                status, headers, raw = self.call(method, "/api/nope", payload=payload)
                self.assertEqual(status, 404)
                self.assert_security_headers(headers, method)
                self.assertEqual(json.loads(raw.decode("utf-8"))["error"]["code"], "not_found")

    def test_external_radar_invalid_pagination_returns_400(self):
        status, payload = self.body("GET", "/api/external/radar?limit=abc")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "分页参数无效")

    def test_notifications_invalid_pagination_returns_400(self):
        status, payload = self.body("GET", "/api/notifications?offset=-1")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "分页参数无效")

    def test_uncaught_key_error_from_a_handler_maps_to_404(self):
        """处理器里冒出来的 KeyError 必须被兜成 404，不能变成 500。

        这是旧实现的全局兜底语义，重构路由表时不能丢。
        """
        self.stubs["ai_settings"]._results["save"] = KeyError("gone")
        status, payload = self.body("POST", "/api/settings/ai", payload={})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "对象不存在")

        self.stubs["system_settings"]._results["put_learning"] = KeyError("gone")
        status, payload = self.body("PUT", "/api/settings/learning", payload={})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "对象不存在")

    def test_optional_dependencies_default_to_empty_collections(self):
        """impacts / external 未装配时，这几条路由要退化成空集合而不是报错。"""
        self.stop_server()
        services, stubs = build_stub_services(impacts=None, external=None)
        self.start_server(services)
        self.stubs = stubs

        status, payload = self.body("GET", "/api/cognition/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"candidates": []})

        status, payload = self.body("GET", "/api/calibration")
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"], [])

        status, payload = self.body("GET", "/api/risk-dashboard")
        self.assertEqual(status, 200)
        call = stubs["cognition_controller"].called("risk_dashboard")[0]
        self.assertEqual(call[1], ([],), "未装配外部源时应传空列表")
        self.assertEqual(call[2], {"limit": 50})

        status, payload = self.body("POST", "/api/export/mobile-summary", payload={})
        self.assertEqual(status, 201)
        call = stubs["mobile_export"].called("export")[0]
        self.assertEqual(call[1][0]["state"], "ok")

    def test_create_server_rejects_a_route_pointing_at_a_missing_handler(self):
        """路由表写错方法名必须在**建服务时**炸掉，而不是等用户点到那条路由。"""
        broken = Route("GET", "exact", "/api/broken", "_no_such_handler")
        with mock.patch.object(http_api, "ROUTES", ROUTES + (broken,)):
            with self.assertRaises(RuntimeError) as raised:
                create_server("127.0.0.1", 0, TOKEN, self.services)

        message = str(raised.exception)
        self.assertIn("/api/broken", message)
        self.assertIn("_no_such_handler", message)

    def test_malformed_content_length_is_ignored_instead_of_crashing(self):
        """`Content-Length` 不是整数时必须安全忽略，不能抛出去变成 500。

        `http.client` 只会发合法的整数长度，所以这里用原始 socket 构造。
        未授权路径会先丢弃请求体，正是这段防御逻辑的入口。
        """
        response = self.raw_request(
            "DELETE /api/external/sources/S-1 HTTP/1.1",
            [
                "Host: 127.0.0.1",
                "Content-Length: not-a-number",
                "Connection: close",
            ],
        )

        self.assertTrue(response, "服务端没有返回任何内容")
        status_line = response.split(b"\r\n", 1)[0]
        self.assertIn(b"403", status_line)


class UnavailableServiceTests(RoutingTestCase):
    """未装配可选能力时，GET 走 503、PUT/POST 走 400（按现实现如实断言）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.start_server(bare_services())
        self.stubs = {}

    def test_get_routes_report_503_when_capability_is_missing(self):
        cases = {
            "/api/forecasts/progress": "预测能力未装配",
            "/api/forecasts/overdue": "预测能力未装配",
            "/api/diagnostics": "诊断能力未装配",
            "/api/settings/backup": "备份能力未装配",
            "/api/settings/retention": "数据保留能力未装配",
            "/api/settings/learning": "反馈学习未装配",
        }
        for path, message in cases.items():
            with self.subTest(path=path):
                status, headers, raw = self.call("GET", path)
                self.assertEqual(status, 503)
                self.assert_security_headers(headers, path)
                payload = json.loads(raw.decode("utf-8"))
                self.assertEqual(payload["error"]["code"], "unavailable")
                self.assertEqual(payload["error"]["message"], message)

    def test_update_check_reports_503_when_not_wired(self):
        status, payload = self.body("POST", "/api/update-check", payload={})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "unavailable")
        self.assertEqual(payload["error"]["message"], "更新检查未装配")

    def test_put_settings_report_400_when_capability_is_missing(self):
        """PUT 处理器抛的是 ValueError（不是 503），do_PUT 统一映射成 400。

        与 GET 的 503 不一致，但这是**现实现的行为**，如实钉死；
        要改成 503 属于 src 变更，不是测试放宽。
        """
        for path, message in (
            ("/api/settings/backup", "备份能力未装配"),
            ("/api/settings/retention", "数据保留能力未装配"),
            ("/api/settings/learning", "反馈学习未装配"),
        ):
            with self.subTest(path=path):
                status, payload = self.body("PUT", path, payload={})
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
                self.assertEqual(payload["error"]["message"], message)

    def test_settings_startup_get_falls_back_to_a_safe_default(self):
        """startup 未装配时 GET 不报错，返回保守默认值。"""
        status, payload = self.body("GET", "/api/settings/startup")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"installed": False, "available": False})

    def test_mobile_export_reports_400_when_not_wired(self):
        status, payload = self.body("POST", "/api/export/mobile-summary", payload={})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "移动摘要导出未装配")

    def test_settings_startup_post_reports_400_when_not_supported(self):
        status, payload = self.body(
            "POST", "/api/settings/startup", payload={"enabled": True}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "当前运行方式不支持登录启动设置")


class WriteRouteContractTests(RoutingTestCase):
    """POST / PUT / DELETE 的全部路由。"""

    def test_post_routes_reach_their_handlers(self):
        cases = (
            ("/api/external/sources", {}, 201, "external", "add_source"),
            ("/api/external/rules", {}, 201, "external", "add_watch_rule"),
            ("/api/interests/objects", {}, 201, "interests", "create_object"),
            ("/api/interests/links", {}, 201, "interests", "create_link"),
            ("/api/external/refresh", {"source_id": "S-1"}, 200, "external", "refresh_source"),
            ("/api/knowledge/index", {"path": "Vault"}, 201, "knowledge", "index_vault"),
            ("/api/settings/ai", {}, 200, "ai_settings", "save"),
            ("/api/forecasts", {}, 201, "forecasts", "create_forecast"),
        )
        for path, payload, status, stub_name, method in cases:
            with self.subTest(path=path):
                got_status, headers, raw = self.call("POST", path, payload=payload)
                self.assertEqual(got_status, status, raw[:200])
                self.assert_security_headers(headers, path)
                self.assertTrue(
                    self.stubs[stub_name].called(method),
                    "%s 没有调用 %s.%s" % (path, stub_name, method),
                )

    def test_update_check_returns_the_missing_capability_free_result(self):
        status, payload = self.body("POST", "/api/update-check", payload={})
        self.assertEqual(status, 200)
        self.assertFalse(payload["update"])
        self.assertTrue(self.stubs["update_check"].called("check"))

    def test_external_source_enabled_toggle_validates_the_flag(self):
        status, payload = self.body(
            "POST", "/api/external/sources/S-1/enabled", payload={"enabled": True}
        )
        self.assertEqual(status, 200)
        call = self.stubs["external"].called("set_source_enabled")[0]
        self.assertEqual(call[1], ("S-1", True))

        status, payload = self.body(
            "POST", "/api/external/sources/S-1/enabled", payload={"enabled": "yes"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "数据源状态无效")

    def test_external_rules_enabled_toggle_and_missing_rule(self):
        status, _payload = self.body(
            "POST", "/api/external/rules/R-1/enabled", payload={"enabled": False}
        )
        self.assertEqual(status, 200)
        call = self.stubs["external"].called("set_rule_enabled")[0]
        self.assertEqual(call[1], ("R-1", False))

        self.stubs["external"]._results["set_rule_enabled"] = KeyError("gone")
        status, payload = self.body(
            "POST", "/api/external/rules/R-missing/enabled", payload={"enabled": True}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "rule_not_found")

        status, payload = self.body(
            "POST", "/api/external/rules/R-1/enabled", payload={"enabled": 1}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "关注词状态无效")

    def test_bulk_enabled_requires_a_boolean_and_forwards_filters(self):
        status, _payload = self.body(
            "POST",
            "/api/external/sources/bulk-enabled",
            payload={"enabled": True, "region": "cn", "category": "policy"},
        )
        self.assertEqual(status, 200)
        call = self.stubs["external"].called("bulk_set_enabled")[0]
        self.assertEqual(call[1], (True,))
        self.assertEqual(call[2], {"region": "cn", "category": "policy"})

        status, _payload = self.body(
            "POST",
            "/api/external/sources/bulk-enabled",
            payload={"enabled": True, "region": "", "category": ""},
        )
        self.assertEqual(status, 200)
        call = self.stubs["external"].called("bulk_set_enabled")[-1]
        self.assertEqual(call[2], {"region": None, "category": None})

        status, payload = self.body(
            "POST", "/api/external/sources/bulk-enabled", payload={"enabled": "yes"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "数据源状态无效")

    def test_external_refresh_requires_a_source_id(self):
        status, payload = self.body("POST", "/api/external/refresh", payload={})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "缺少数据源编号")

    def test_events_rejects_an_empty_body_text(self):
        status, payload = self.body("POST", "/api/events", payload={"text": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "事件内容不能为空")

    def test_notification_read_routes(self):
        status, _payload = self.body("POST", "/api/notifications/read-all", payload={})
        self.assertEqual(status, 200)
        self.assertTrue(self.stubs["notifications"].called("mark_all_read"))

        status, _payload = self.body(
            "POST", "/api/notifications/N-1/read", payload={}
        )
        self.assertEqual(status, 200)
        call = self.stubs["notifications"].called("mark_read")[0]
        self.assertEqual(call[1], ("N-1",))

        self.stubs["notifications"]._results["mark_read"] = KeyError("gone")
        status, payload = self.body("POST", "/api/notifications/N-x/read", payload={})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "notification_not_found")

    def test_candidate_confirm_and_cluster_feedback(self):
        status, _payload = self.body(
            "POST", "/api/cognition/candidates/P-1/confirm", payload={"probability": 0.6}
        )
        self.assertEqual(status, 201)
        call = self.stubs["impacts"].called("confirm_candidate")[0]
        self.assertEqual(call[1], ("P-1", 0.6))

        self.stubs["impacts"]._results["confirm_candidate"] = KeyError("gone")
        status, payload = self.body(
            "POST", "/api/cognition/candidates/P-x/confirm", payload={}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "candidate_not_found")

        status, _payload = self.body(
            "POST", "/api/cognition/clusters/C-1/feedback", payload={"action": "mute"}
        )
        self.assertEqual(status, 200)
        call = self.stubs["cognition_controller"].called("feedback")[0]
        self.assertEqual(call[1], ("C-1", "mute", {"action": "mute"}))

        self.stubs["cognition_controller"]._results["feedback"] = KeyError("gone")
        status, payload = self.body(
            "POST", "/api/cognition/clusters/C-x/feedback", payload={}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "cluster_not_found")

    def test_settings_startup_install_and_remove(self):
        status, payload = self.body(
            "POST", "/api/settings/startup", payload={"enabled": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["installed"])

        status, payload = self.body(
            "POST", "/api/settings/startup", payload={"enabled": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["installed"])

        status, payload = self.body(
            "POST", "/api/settings/startup", payload={"enabled": "yes"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "登录启动状态无效")

    def test_forecast_version_and_resolve_routes(self):
        status, payload = self.body(
            "POST", "/api/forecasts/F-1/versions", payload={"probability": 0.7}
        )
        self.assertEqual(status, 201)
        call = self.stubs["forecasts"].called("add_version")[0]
        self.assertEqual(call[1], ("F-1", {"probability": 0.7}))

        status, payload = self.body(
            "POST",
            "/api/forecasts/F-1/resolve",
            payload={"outcome": "hit", "resolved_at": "2026-09-01", "note": "备注"},
        )
        self.assertEqual(status, 201)
        call = self.stubs["forecasts"].called("resolve")[0]
        self.assertEqual(call[1], ("F-1", "hit", "2026-09-01", "备注"))

    def test_cognition_run_uses_the_controller_when_no_operation_is_wired(self):
        self.stop_server()
        services, stubs = build_stub_services(cognition_operation=None)
        self.start_server(services)
        self.stubs = stubs

        status, _payload = self.body("POST", "/api/cognition/run", payload={})
        self.assertEqual(status, 200)
        self.assertTrue(stubs["cognition_controller"].called("run_once"))

    def test_cognition_run_conflict_is_reported_as_409(self):
        self.stubs["cognition_operation"] = None
        self.stop_server()
        services, stubs = build_stub_services(
            cognition_operation=StubOperation()
        )
        stubs["cognition_operation"].run = lambda source: (_ for _ in ()).throw(
            OperationBusy("busy")
        )
        self.start_server(services)
        self.stubs = stubs

        status, payload = self.body("POST", "/api/cognition/run", payload={})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "operation_busy")

    def test_window_show_and_monitoring_toggle_report_a_missing_desktop(self):
        self.stop_server()
        services, stubs = build_stub_services(desktop=None)
        self.start_server(services)
        self.stubs = stubs

        for path in ("/api/window/show", "/api/monitoring/toggle"):
            with self.subTest(path=path):
                status, payload = self.body("POST", path, payload={})
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["message"], "桌面窗口尚未就绪")

    def test_monitoring_toggle_returns_the_new_state(self):
        status, payload = self.body("POST", "/api/monitoring/toggle", payload={})
        self.assertEqual(status, 200)
        self.assertEqual(payload["monitoring"], False)

    def test_mobile_export_returns_created(self):
        status, _headers, raw = self.call(
            "POST", "/api/export/mobile-summary", payload={}
        )
        self.assertEqual(status, 201, raw[:200])
        self.assertTrue(self.stubs["mobile_export"].called("export"))

    def test_shutdown_prefers_the_desktop_exit_hook(self):
        events = []

        class ExitingDesktop(RecordingDesktop):
            def request_exit(self):
                events.append("exit")

        self.stop_server()
        services, stubs = build_stub_services(desktop=ExitingDesktop())
        self.start_server(services)
        self.stubs = stubs

        status, payload = self.body("POST", "/api/shutdown", payload={})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "shutting_down")
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        self.assertEqual(events, ["exit"], "没有走 desktop.request_exit 钩子")

    def test_put_routes_reach_their_handlers(self):
        cases = (
            ("/api/settings/backup", "backup_service", "put_setting"),
            ("/api/settings/retention", "retention_service", "put_setting"),
            ("/api/settings/learning", "system_settings", "put_learning"),
        )
        for path, stub_name, method in cases:
            with self.subTest(path=path):
                status, _headers, raw = self.call("PUT", path, payload={})
                self.assertEqual(status, 200, raw[:200])
                self.assertTrue(self.stubs[stub_name].called(method))

    def test_put_external_source_updates_and_validates_the_identifier(self):
        status, _payload = self.body(
            "PUT", "/api/external/sources/S-1", payload={"name": "新名字"}
        )
        self.assertEqual(status, 200)
        call = self.stubs["external"].called("update_source")[0]
        self.assertEqual(call[1], ("S-1", {"name": "新名字"}))

        status, payload = self.body(
            "PUT", "/api/external/sources/a%2Fb", payload={"name": "x"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "数据源编号无效")

    def test_delete_external_source_paths(self):
        status, _payload = self.body("DELETE", "/api/external/sources/S-1")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.stubs["external"].called("delete_source")[0][1], ("S-1",)
        )

        status, payload = self.body("DELETE", "/api/external/sources/")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "数据源编号无效")

        self.stubs["external"]._results["delete_source"] = KeyError("gone")
        status, payload = self.body("DELETE", "/api/external/sources/S-x")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

        self.stubs["external"]._results["delete_source"] = ValueError("在用")
        status, payload = self.body("DELETE", "/api/external/sources/S-inuse")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "在用")

    def test_delete_watch_rule_paths(self):
        status, _payload = self.body("DELETE", "/api/external/rules/R-1")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.stubs["external"].called("delete_watch_rule")[0][1], ("R-1",)
        )

        status, payload = self.body("DELETE", "/api/external/rules/")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "关注词编号无效")

        self.stubs["external"]._results["delete_watch_rule"] = KeyError("gone")
        status, payload = self.body("DELETE", "/api/external/rules/R-x")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "rule_not_found")

        self.stubs["external"]._results["delete_watch_rule"] = ValueError("非法")
        status, payload = self.body("DELETE", "/api/external/rules/R-y")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["message"], "非法")

    def test_unexpected_service_errors_become_a_clean_500(self):
        """处理器抛未预期异常时，必须返回干净 500，不能泄漏堆栈。"""
        self.stubs["forecasts"]._results["score_summary"] = RuntimeError(
            "内部细节不应外泄"
        )

        status, headers, raw = self.call("GET", "/api/score")

        self.assertEqual(status, 500)
        self.assert_security_headers(headers, "/api/score")
        text = raw.decode("utf-8")
        self.assertNotIn("内部细节不应外泄", text)
        self.assertNotIn("Traceback", text)
        self.assertEqual(json.loads(text)["error"]["code"], "internal_error")


class RequestBodyBoundaryTests(RoutingTestCase):
    """超限 / 谎报 Content-Length 的请求体：有界排空之后再如实回 400。

    这是 Windows 回环 RST 修复的验收面：服务端在"还没读完请求体就要拒绝"时，
    若直接关连接，内核会发 RST，客户端拿到的是 ``RemoteDisconnected``，而不是
    我们写好的那句可读的 400。所以这些用例全部走原始 socket —— ``urllib`` 与
    ``http.client`` 不肯构造这些畸形请求。
    """

    def raw_post(self, declared_length, body, shutdown_write=False, timeout=15):
        """发一个 Content-Length 与实际 body 不一致的 POST，回读整个响应。

        返回 ``(响应字节, 耗时秒)``。耗时用来验证排空确实"有界"。
        """
        request_lines = [
            "POST /api/events HTTP/1.1",
            "Host: 127.0.0.1",
            "Content-Type: application/json",
            "X-YuanJian-Token: %s" % self.token,
            "Content-Length: %d" % declared_length,
            "Connection: close",
        ]
        head = ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii")
        started = time.monotonic()
        with socket.create_connection(
            ("127.0.0.1", self.server.server_address[1]), timeout=timeout
        ) as connection:
            connection.sendall(head + body)
            if shutdown_write:
                connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                data = connection.recv(4096)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks), time.monotonic() - started

    def assert_bad_request(self, response):
        """断言拿到的是完整的 400 响应，而不是被 RST 掐断的空连接。"""
        self.assertTrue(response, "服务端没有返回任何内容（连接被 RST 了？）")
        head, _, body = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        self.assertIn(b"400", status_line)
        # 头部是 ASCII，正文是 UTF-8，分开解码，避免用 latin-1 把中文读成乱码。
        header_text = head.decode("latin-1")
        # 安全头由 _json 统一发送，排空后的 4xx 也不能漏。
        for name, _value in SECURITY_HEADERS:
            self.assertIn(name + ": ", header_text, "400 响应缺少安全头 %s" % name)
        self.assertIn("请求内容为空或过大", body.decode("utf-8"))

    def test_oversized_body_from_a_client_that_half_closes_is_still_400(self):
        """对端报 70000+ 字节却只发一部分就半关闭：必须是可读的 400。

        真实场景是粘贴超长文本时连接被提前掐断 —— 此时既不能把工作线程挂在
        读上，也不能因为"读不满"就丢掉响应。

        诚实声明（用 ``build-artifacts/t8_drain_mutation_control.py`` 实测过）：
        这条用例**不能**判别"排空"与"不排空"。本机 Windows 上把
        ``_drain_request_body`` 换成 no-op 之后，100B / 64KiB / 70KB / 200KB /
        900KB 五种体积的客户端**全都**仍能读到 400 —— 头部是走
        ``BufferedReader`` 读的，缓冲区吞下的字节不留在内核，既然没有内核残留
        就不会有 RST，那个"用户看到网络错误"的症状在这台机器上复现不出来。
        所以它钉的是"部分请求体也必须给出完整 400（含安全头）"这条响应契约，
        真正能判别修复的是下面那条谎报长度的用例。
        """
        response, _elapsed = self.raw_post(
            70000, b"x" * 20000, shutdown_write=True
        )

        self.assert_bad_request(response)

    def test_lying_content_length_cannot_pin_the_worker_forever(self):
        """一个字节都不发、只报个天文数字：排空必须超时收手，工作线程要能回来。

        这是"有界"二字的真正含义：读多久由服务端的 5 秒读超时决定，绝不由对端
        给的 Content-Length 决定 —— 否则一个恶意（或只是实现糟糕的）客户端就能
        用一个不存在的 4GB 请求体永久占住一个工作线程。
        """
        response, elapsed = self.raw_post(500000, b"", shutdown_write=False)

        self.assert_bad_request(response)
        self.assertGreaterEqual(
            elapsed, 4.0, "没有等到读超时就回了 400，说明排空根本没在等真实数据"
        )
        self.assertLess(elapsed, 12.0, "排空耗时失控，5 秒读超时没有生效")


class StaticAssetBoundaryTests(RoutingTestCase):
    """静态资源白名单与路径穿越防护。"""

    def test_root_and_every_whitelisted_asset_are_served(self):
        for path, (relative, content_type) in http_api.STATIC_FILES.items():
            with self.subTest(path=path):
                status, headers, raw = self.call("GET", path)
                self.assertEqual(status, 200, "%s -> %s" % (path, raw[:120]))
                self.assertEqual(headers.get("Content-Type"), content_type)
                self.assert_security_headers(headers, path)
                self.assertTrue(raw, "静态资源内容为空")
                self.assertEqual(headers.get("Cache-Control"), "no-cache")
                self.assertEqual(int(headers.get("Content-Length")), len(raw))

    def test_unknown_static_path_returns_404(self):
        status, headers, raw = self.call("GET", "/js/not-registered.js")
        self.assertEqual(status, 404)
        self.assert_security_headers(headers)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "页面不存在")

    def test_traversal_attempts_are_rejected_with_403(self):
        for path in (
            "/../secrets.txt",
            "/%2e%2e/secrets.txt",
            "/js/%2Fetc%2Fpasswd",
            "/js/%5Cwindows",
        ):
            with self.subTest(path=path):
                status, headers, raw = self.call("GET", path)
                self.assertEqual(status, 403, "%s -> %s" % (path, raw[:120]))
                self.assert_security_headers(headers)
                self.assertEqual(
                    json.loads(raw.decode("utf-8"))["error"]["code"], "forbidden"
                )

    def test_registered_asset_missing_on_disk_returns_404(self):
        with mock.patch.dict(
            http_api.STATIC_FILES, {"/ghost.js": ("js/ghost.js", "text/javascript")}
        ):
            status, _headers, raw = self.call("GET", "/ghost.js")

        self.assertEqual(status, 404)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "页面资源不存在")

    def test_registered_asset_escaping_the_static_root_returns_403(self):
        with mock.patch.dict(
            http_api.STATIC_FILES, {"/escape.js": ("../escape.js", "text/javascript")}
        ):
            status, _headers, raw = self.call("GET", "/escape.js")

        self.assertEqual(status, 403)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "forbidden")
        self.assertEqual(payload["error"]["message"], "非法的资源路径")


class SecurityAndTokenTests(RoutingTestCase):
    """令牌校验与安全头覆盖。"""

    def test_every_response_carries_all_security_headers(self):
        probes = (
            ("GET", "/", None),
            ("GET", "/js/app.js", None),
            ("GET", "/api/app/version", None),
            ("GET", "/api/nope", None),
            ("GET", "/api/nope.js", None),
            ("POST", "/api/unknown", {}),
            ("PUT", "/api/unknown", {}),
            ("DELETE", "/api/unknown", None),
            ("GET", "/api/app/version", None),
        )
        for method, path, payload in probes:
            with self.subTest(method=method, path=path):
                status, headers, _raw = self.call(method, path, payload=payload)
                self.assert_security_headers(headers, "%s %s %s" % (method, path, status))

    def test_security_headers_also_cover_unauthenticated_responses(self):
        for method, path in (
            ("GET", "/api/app/version"),
            ("POST", "/api/update-check"),
            ("PUT", "/api/settings/backup"),
            ("DELETE", "/api/external/sources/S-1"),
        ):
            with self.subTest(method=method):
                status, headers, _raw = self.call(method, path, token="wrong")
                self.assertEqual(status, 403)
                self.assert_security_headers(headers, method)

    def test_missing_or_wrong_token_is_rejected_on_every_method(self):
        for method, path in (
            ("GET", "/api/app/version"),
            ("POST", "/api/update-check"),
            ("PUT", "/api/settings/backup"),
            ("DELETE", "/api/external/sources/S-1"),
        ):
            for token in (False, "", "wrong-token", TOKEN + "x", TOKEN[:-1]):
                with self.subTest(method=method, token=token):
                    status, _headers, raw = self.call(
                        method, path, token=token, payload={} if method != "GET" else None
                    )
                    self.assertEqual(status, 403, raw[:120])
                    self.assertEqual(
                        json.loads(raw.decode("utf-8"))["error"]["code"], "forbidden"
                    )

    def test_static_assets_are_not_behind_the_token(self):
        """页面本身必须能在带 token 的 URL 下加载，静态资源不能要令牌。"""
        status, _headers, raw = self.call("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(raw)

    def test_correct_token_is_accepted(self):
        status, payload = self.body("GET", "/api/app/version", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("version", payload)

    def test_health_style_endpoints_require_the_token_too(self):
        status, _headers, _raw = self.call("GET", "/api/score", token=False)
        self.assertEqual(status, 403)


class AppVersionAndUpdateCheckTests(RoutingTestCase):
    """两条较新的路由：/api/app/version 与 /api/update-check。"""

    def test_app_version_reports_package_version_and_releases_url(self):
        from yuanjian_app import __version__
        from yuanjian_app.update_check import RELEASES_PAGE

        status, payload = self.body("GET", "/api/app/version")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"version": __version__, "releases_url": RELEASES_PAGE})

    def test_update_check_returns_the_service_result(self):
        status, payload = self.body("POST", "/api/update-check", payload={})

        self.assertEqual(status, 200)
        self.assertEqual(payload["latest"], "1.0.0")
        call = self.stubs["update_check"].called("check")[0]
        from yuanjian_app import __version__

        self.assertEqual(call[1], (__version__,))

    def test_update_check_requires_the_token(self):
        status, _headers, _raw = self.call("POST", "/api/update-check", token=False)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
