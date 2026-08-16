import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.external_radar import ExternalRadarService
from yuanjian_app.external_sources import ExternalItem
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.cognition import CognitionController, CognitionService
from yuanjian_app.http_api import Services, create_server
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
        self.assertIn("/api/cognition/run", script)
        self.assertIn("/api/shutdown", script)
        self.assertIn("#/", script)
        with urllib.request.urlopen(
            self.base_url + "/js/views/today.js", timeout=2
        ) as response:
            today = response.read().decode("utf-8")
        # Action Home v0.9 consumes /api/cognition/candidates + /api/forecasts/progress
        # instead of /api/risk-dashboard. The deep-dive card surfaces backend
        # GYW analysis from candidate.gyw.
        self.assertIn("/api/cognition/candidates", today)
        self.assertIn("/api/forecasts/progress", today)
        self.assertIn("gyw", today)

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
            ("/js/views/today.js", "/api/cognition/candidates"),
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

    def get_json(self, path, token="test-token"):
        request = urllib.request.Request(
            self.base_url + path, headers={"X-YuanJian-Token": token}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def request_json(self, path, payload, method, token="test-token"):
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


if __name__ == "__main__":
    unittest.main()
