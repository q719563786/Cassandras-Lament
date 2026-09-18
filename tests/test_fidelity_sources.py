"""v1.4 · S3（R-10 / R-11 / R-12 / R-14 / R-15 / R-17）：清掉确认过的失真源。

  R-10 · 条目身份只按 `canonical_url` 去重，`content_hash` **算出来只存不用**。
        同一篇通稿被 5 家门户全文转载 = 5 个不同 canonical URL = 5 个独立域名 →
        直接进 E2（若某来源被标为官方，甚至 E3）。README 声称"同域转载不冒充互证"，
        但它只防同域，而同一 URL 本来就被去重了 —— **真正的常见形态（跨域通稿）
        没有防**。这与"一个声音被复制成多个声音就是互证的反面"直接冲突。

  R-11 · 14,315 个趋势快照里 rising 占 48%（只看可判定的占 73%）。一个多数时间在
        报警的探测器等价于没有报警。根因：`SURGE_RATIO` 固定 2.0 倍，而每轮要对
        「约 14 个类别 × 4 个窗口 = 最多 56 次」比较，**无多重比较校正**。

  R-12 · `forecasts` 表一个触发器都没有，父行可删、子行删不掉 → 真库实测留下
        **10,663 条（55%）永久孤儿**；父行被删还会让那条结算记录从
        `resolutions JOIN forecasts` 里整体消失（基准率与 excluded_total 双双静默
        变小）。而且 `forecasts` 没有 `created_at` 列 —— 账本的时间轴只存在于
        渲染出来的文本里。

  R-17 · `_LOCAL_ORG` 以 `省|市|县|区|旗` 为中缀 + 机构后缀，"本地区政府""区域
        管理局"这类不含行政区划的表述仍可能命中。方向保守（宁可判高），
        优先级最低 —— 但要有测试固化，避免以后无意扩大。
"""

import hashlib
import json
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.cognition import CognitionService
from yuanjian_app.database import Database
from yuanjian_app.diagnostics import DiagnosticsService
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.knowledge_base import analyze_power_structure, extract_orgs
from yuanjian_app.system_settings import SystemSettingsService
from yuanjian_app.trends import RISING_BUDGET, TrendService

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "yuanjian_app" / "static"
NODE = "node"


# ---------------------------------------------------------------------- #
# R-10 · 跨域同文检测
# ---------------------------------------------------------------------- #
class SyndicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = CognitionService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def add_source(self, source_id, domain, primary=False):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO external_sources(source_id, name, kind, endpoint,"
                " config_json) VALUES (?, ?, 'rss', ?, ?)",
                (
                    source_id, source_id, f"https://{domain}/feed",
                    json.dumps({"primary_source": primary}),
                ),
            )

    def add_item(self, item_id, source_id, domain, title, summary, hour=0):
        timestamp = f"2026-08-11T{hour:02d}:00:00Z"
        url = f"https://{domain}/{item_id.lower()}"
        content_hash = hashlib.sha256(f"{title}\n{summary}".encode()).hexdigest()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO external_items(item_id, canonical_url, title, summary,"
                " published_at, fetched_at, source_id, source_name, language,"
                " content_hash, first_seen_at, last_seen_at, source_count, raw_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Chinese', ?, ?, ?, 1, '{}')",
                (
                    item_id, url, title, summary, timestamp, timestamp,
                    source_id, source_id, content_hash, timestamp, timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO external_item_sources VALUES (?, ?, ?, ?)",
                (item_id, source_id, url, timestamp),
            )

    def cluster_of(self, item_ids):
        for item_id in item_ids:
            self.service.process_item(item_id)
        clusters = self.service.list_clusters()
        self.assertEqual(len(clusters), 1, "夹具应聚成一个事件簇")
        return clusters[0]["cluster_id"]

    def test_same_text_across_domains_counts_as_one_independent_voice(self):
        """3 条不同 URL、**相同** content_hash 的条目 → 独立域名计数为 1。

        这就是"一个声音被复制成多个声音"：三家门户转载同一篇通稿，
        在 E 级眼里必须仍然是**一个**独立来源。
        """
        for source_id, domain in (("S-A", "a.example"), ("S-B", "b.example"),
                                  ("S-C", "c.example")):
            self.add_source(source_id, domain)
        shared = ("广东医保报销比例升至70%", "政策本月实施")
        self.add_item("E-1", "S-A", "a.example", *shared, 0)
        self.add_item("E-2", "S-B", "b.example", *shared, 1)
        self.add_item("E-3", "S-C", "c.example", *shared, 2)

        cluster_id = self.cluster_of(("E-1", "E-2", "E-3"))
        detail = self.service.get_cluster(cluster_id)

        self.assertEqual(detail["source_domains"], 3)
        self.assertEqual(detail["independent_domains"], 1)
        self.assertEqual(detail["syndicated_domains"], 2)
        # 独立声音只有 1 个 → 进不了 E2（`_recalculate` 用的是去重后的计数）。
        self.assertEqual(detail["evidence_level"], "E1")

    def test_distinct_texts_across_domains_count_separately(self):
        """不同 content_hash 的 3 条 → 计数为 3（对照：上一条不是"把什么都算成 1"）。

        标题相同、**摘要各自不同**：内容哈希因此不同（哈希算的是 title+summary），
        而标题相同保证它们仍聚成一个事件簇。
        """
        for source_id, domain in (("S-A", "a.example"), ("S-B", "b.example"),
                                  ("S-C", "c.example")):
            self.add_source(source_id, domain)
        title = "广东医保报销比例升至70%"
        self.add_item("E-1", "S-A", "a.example", title, "政策本月实施", 0)
        self.add_item("E-2", "S-B", "b.example", title, "省医保局确认调整方案", 1)
        self.add_item("E-3", "S-C", "c.example", title, "本月起执行新标准", 2)

        cluster_id = self.cluster_of(("E-1", "E-2", "E-3"))
        detail = self.service.get_cluster(cluster_id)

        self.assertEqual(detail["independent_domains"], 3)
        self.assertEqual(detail["syndicated_domains"], 0)
        self.assertEqual(detail["evidence_level"], "E2")

    def test_same_text_from_one_domain_does_not_multiply_voices_either(self):
        """同一域名内的多条（不同 URL）本来就不算互证。"""
        self.add_source("S-A", "a.example")
        shared = ("广东医保报销比例升至70%", "政策本月实施")
        self.add_item("E-1", "S-A", "a.example", *shared, 0)
        self.add_item("E-2", "S-A", "a.example", *shared, 1)

        cluster_id = self.cluster_of(("E-1", "E-2"))
        detail = self.service.get_cluster(cluster_id)

        self.assertEqual(detail["independent_domains"], 1)
        self.assertEqual(detail["evidence_level"], "E1")

    def test_primary_source_needs_two_real_voices_for_e3(self):
        """官方来源 + 转载不算互证 → 停在 E1；换成真正不同的两条 → E2/E3。"""
        self.add_source("S-OFFICIAL", "a.example", primary=True)
        self.add_source("S-B", "b.example")
        shared = ("广东医保报销比例升至70%", "政策本月实施")
        self.add_item("E-1", "S-OFFICIAL", "a.example", *shared, 0)
        self.add_item("E-2", "S-B", "b.example", *shared, 1)

        cluster_id = self.cluster_of(("E-1", "E-2"))
        detail = self.service.get_cluster(cluster_id)

        self.assertEqual(detail["independent_domains"], 1)
        self.assertEqual(detail["primary_source_count"], 1)
        self.assertNotIn(detail["evidence_level"], {"E3", "E4"})


# ---------------------------------------------------------------------- #
# R-11 · 趋势阈值分位化 + 健康指标
# ---------------------------------------------------------------------- #
class TrendHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = TrendService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def add_events(self, category, per_day, days, first_day_offset=0):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=first_day_offset)
        with self.database.connect() as connection:
            for day in range(days):
                for index in range(per_day):
                    moment = start + timedelta(days=day, hours=index * 3)
                    cluster_id = f"C-{category}-{day}-{index}"
                    connection.execute(
                        "INSERT INTO event_clusters(cluster_id,title,summary,"
                        "first_seen_at,last_seen_at,evidence_level,evidence_hash,"
                        "categories_json,created_at,updated_at)"
                        " VALUES (?,?,'',?,?,'E1',?,'[\"" + category + "\"]',?,?)",
                        (
                            cluster_id, f"事件 {cluster_id}", moment.isoformat().replace("+00:00", "Z"),
                            moment.isoformat().replace("+00:00", "Z"),
                            f"h-{cluster_id}",
                            moment.isoformat().replace("+00:00", "Z"),
                            moment.isoformat().replace("+00:00", "Z"),
                        ),
                    )

    def test_threshold_gets_stricter_as_comparisons_multiply(self):
        """多重比较校正的直接证据：比较次数越多，阈值越高。

        旧口径每一轮要对最多 56 次比较各判一次"是否翻倍"，却按单次比较的尺度取固定
        倍数 —— 比较次数越多，"至少有一次误报"的概率越高，于是阈值整体失效。
        """
        expected = 6.0
        single = TrendService._rising_threshold(expected, 1)
        many = TrendService._rising_threshold(expected, 56)

        self.assertGreater(many, single, "比较次数必须进入分母")
        # 旧固定倍数 2.0 倍在 expected=6 时是 12：单次比较下它够严，56 次比较下不够。
        self.assertGreater(many, expected * 2.0)
        self.assertLess(single, expected * 2.0)

    def test_flat_event_stream_keeps_rising_under_budget(self):
        """喂入"平稳"的事件序列：rising 占比必须低于 20% 预算。"""
        # 30 天 × 每天 6 条：够 24h / 168h 两个窗口判定（各 >=5 条），
        # 且速率恒定 —— 任何"上升"都是误报。
        self.add_events("finance", per_day=6, days=30)

        result = self.service.capture(datetime(2026, 7, 1, tzinfo=timezone.utc))
        health = result["health"]

        self.assertGreater(health["judgeable"], 0, "夹具必须产出可判定的窗口")
        self.assertLess(health["rising_share"], RISING_BUDGET)
        self.assertFalse(health["threshold_failed"])
        self.assertEqual(health["rising"], 0)

    def test_stored_health_reads_the_latest_snapshot_without_writing(self):
        """诊断面板用的健康度是**只读**的：从最近一次采样算，绝不触发新采集。"""
        captured_at = "2026-07-01T00:00:00Z"
        rows = (
            ("T-1", "rising"), ("T-2", "rising"), ("T-3", "normal"),
            ("T-4", "normal"), ("T-5", "accumulating"), ("T-6", "low_sample"),
        )
        with self.database.connect() as connection:
            for snapshot_id, status in rows:
                connection.execute(
                    "INSERT INTO trend_snapshots(snapshot_id,captured_at,category,"
                    "window_hours,event_count,status) VALUES (?,?,?,?,?,?)",
                    (snapshot_id, captured_at, snapshot_id, 24, 3, status),
                )
        before = self._count()

        health = self.service.stored_health()

        # 只算可判定的（rising + normal）：2 / 4 = 50% > 20% → 阈值失效。
        self.assertEqual(health["judgeable"], 4)
        self.assertEqual(health["rising"], 2)
        self.assertEqual(health["rising_share"], 0.5)
        self.assertTrue(health["threshold_failed"])
        self.assertEqual(health["captured_at"], captured_at)
        self.assertEqual(self._count(), before, "读路径不得写库")

    def test_stored_health_without_any_sample_reports_none(self):
        health = self.service.stored_health()
        self.assertIsNone(health["rising_share"])
        self.assertFalse(health["threshold_failed"])

    def _count(self):
        with self.database.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM trend_snapshots").fetchone()[0]


# ---------------------------------------------------------------------- #
# R-12 · forecasts 不可删 + created_at + 基准率不依赖父行
# ---------------------------------------------------------------------- #
class LedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = ForecastService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def _forecast(self, forecast_id, category="finance"):
        return {
            "forecast_id": forecast_id,
            "title": "截至2026-09-10：河源市水务局是否挂出泵类采购公告",
            "resolution_criteria": "以河源市水务局官网公告为判定依据",
            "observable_signals": "河源市水务局挂出泵类采购公告",
            "window_start": "2026-08-01",
            "window_end": "2026-09-10",
            "probability": 0.65,
            "category": category,
            "confirmed_by": "user",
        }

    def test_forecasts_cannot_be_deleted(self):
        self.service.create_forecast(self._forecast("F-1"))

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute("DELETE FROM forecasts WHERE forecast_id='F-1'")

        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], 1
            )

    def test_forecasts_status_can_still_be_updated(self):
        """只加 no_delete、**不加 no_update** —— 否则整条结算流程会被数据库拒绝。"""
        self.service.create_forecast(self._forecast("F-1"))

        self.service.resolve("F-1", "occurred", "2026-09-11", "人工")

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, resolved_by FROM forecasts WHERE forecast_id='F-1'"
            ).fetchone()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["resolved_by"], "user")

    def test_forecasts_created_at_column_is_written_and_history_stays_null(self):
        self.service.create_forecast(self._forecast("F-NEW"))
        with self.database.connect() as connection:
            value = connection.execute(
                "SELECT created_at FROM forecasts WHERE forecast_id='F-NEW'"
            ).fetchone()[0]
        self.assertTrue(value, "新条目必须把创建时间写进列里，而不是只写在渲染出来的文本里")

        # 迁移：老库（没有该列）打开后自动补列，**历史行保持 NULL**（不知道就是不知道）。
        legacy = Path(self.temporary.name) / "legacy.db"
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE forecasts(forecast_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " window_end TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general',"
            " confirmed_by TEXT NOT NULL DEFAULT 'unknown', base_rate REAL,"
            " base_rate_sample INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO forecasts(forecast_id, status, window_end) VALUES ('F-OLD','open','2026-08-16')"
        )
        connection.commit()
        Database._apply_column_migrations(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(forecasts)")}
        row = connection.execute(
            "SELECT created_at, resolved_by FROM forecasts WHERE forecast_id='F-OLD'"
        ).fetchone()
        connection.close()

        self.assertIn("created_at", columns)
        self.assertIn("resolved_by", columns)
        self.assertIsNone(row[0], "历史行不回填 —— 不知道就是不知道")
        self.assertEqual(row[1], "unknown")

    def test_base_rate_survives_parent_row_deletion(self):
        """基准率取样不得依赖父行存活（R-12 第 3 条）。

        删父行现在被触发器拦住了，所以这里先**把触发器拆掉**来模拟 v1.4 之前的
        库（真库里已经有 10,663 条这样的孤儿子行）。断言：即便父行不在，
        `base_rate_for_category` 仍能统计到那条结算。
        """
        for index in range(6):
            self.service.create_forecast(self._forecast(f"F-{index}", category="catOrphan"))
            self.service.resolve(
                f"F-{index}", "occurred" if index < 4 else "not_occurred", "2026-09-11", "x"
            )

        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER forecasts_no_delete")
            connection.execute("DELETE FROM forecasts WHERE forecast_id='F-0'")

        rate, sample = self.service.base_rate_for_category("catOrphan")

        self.assertEqual(sample, 6, "被删父行的结算记录也必须算进样本")
        self.assertAlmostEqual(rate, 4 / 6, places=4)
        # excluded_total 也必须看得到它们，而不是静默缩小。
        summary = self.service.calibration_summary()
        self.assertEqual(summary["resolved_total"], 6)


# ---------------------------------------------------------------------- #
# R-14 / R-15 · 文案口径与证据等级提示
# ---------------------------------------------------------------------- #
class AttentionCopyTests(unittest.TestCase):
    def test_today_view_no_longer_calls_the_tier_risk(self):
        source = (STATIC / "js" / "views" / "today.js").read_text(encoding="utf-8")

        self.assertNotIn("高等级风险", source)
        self.assertNotIn("个人风险雷达", source)
        self.assertIn("重点关注", source)

    def test_today_empty_state_renders_the_attention_wording(self):
        html = _render_today(
            {
                "state": "stable",
                "items": [],
                "summary": "",
            }
        )

        self.assertIn("重点关注", html)
        self.assertNotIn("高等级风险", html)


class EvidenceLevelVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.settings = SystemSettingsService(self.database)
        self.diagnostics = DiagnosticsService(
            self.database,
            trends=TrendService(self.database),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _add_cluster(self, level, count):
        with self.database.connect() as connection:
            for index in range(count):
                connection.execute(
                    "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                    "last_seen_at,evidence_level,evidence_hash,categories_json,"
                    "created_at,updated_at) VALUES (?,?,'','2026-08-01','2026-08-01',?,?,"
                    "'[\"finance\"]','2026-08-01','2026-08-01')",
                    (f"C-{level}-{index}", "t", level, f"h-{level}-{index}"),
                )

    def test_evidence_distribution_and_unreachable_levels_are_visible(self):
        """实测 E3/E4 从未出现，因为 `primary_source` 从未在任何信息源上标记过。
        四级体系实际只跑两级，最窄概率区间（E4 ±0.07）不可达 ——
        这件事必须在界面上说出来，而不是等用户自己发现。
        """
        self._add_cluster("E1", 3)
        self._add_cluster("E2", 2)

        snapshot = self.diagnostics.snapshot()

        self.assertEqual(snapshot["evidence_levels"], {"E1": 3, "E2": 2, "E3": 0, "E4": 0})
        self.assertEqual(snapshot["primary_source_count"], 0)
        self.assertIn("尚未标记任何官方来源", snapshot["evidence_level_note"])
        self.assertIn("E4", snapshot["evidence_level_note"])

    def test_marking_a_primary_source_clears_the_notice(self):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO external_sources(source_id, name, kind, endpoint,"
                " enabled, config_json) VALUES ('S-1','官方','rss','https://x/feed',1,?)",
                (json.dumps({"primary_source": True}),),
            )

        snapshot = self.diagnostics.snapshot()

        self.assertEqual(snapshot["primary_source_count"], 1)
        self.assertEqual(snapshot["evidence_level_note"], "")

    def test_disabled_sources_do_not_count_as_marked_official(self):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO external_sources(source_id, name, kind, endpoint,"
                " enabled, config_json) VALUES ('S-1','官方','rss','https://x/feed',0,?)",
                (json.dumps({"primary_source": True}),),
            )

        self.assertEqual(self.diagnostics.snapshot()["primary_source_count"], 0)

    def test_trend_health_is_exposed_on_the_diagnostics_panel(self):
        snapshot = self.diagnostics.snapshot()

        self.assertIn("trend_health", snapshot)
        self.assertIsInstance(snapshot["trend_health"], dict)
        self.assertIsNone(snapshot["trend_health"]["rising_share"])


# ---------------------------------------------------------------------- #
# R-17 · 机构名匹配的过配行为固化
# ---------------------------------------------------------------------- #
class LocalOrgMatchingTests(unittest.TestCase):
    """`_LOCAL_ORG` 以 `省|自治区|自治州|市|县|区|旗` 为中缀 + 机构后缀。

    已知过配：不含行政区划的"本地区政府""区域管理局"仍可能命中（"地区"/"区域"
    里含"区"），把 `delay_risk` 抬到"高"。**方向是保守的（宁可判高）**，
    所以这条优先级最低 —— 但必须有测试固化当前行为，避免以后无意扩大。
    将来若要收紧，改 `_LOCAL_ORG` 之前先改这里。
    """

    def test_recognizable_local_org_names_are_extracted(self):
        found = extract_orgs("河源市水务局发布采购公告")
        self.assertIn("河源市水务局", found.get("local", []))

    def test_overmatching_is_pinned_not_expanded(self):
        """固化当前（保守的）过配行为，并把它写成"已知"，而不是当成正确。"""
        found = extract_orgs("本地区政府发布通知")
        self.assertTrue(
            found.get("local"),
            "当前行为：'本地区政府' 会命中地方政府文法。若这里变红，说明匹配规则"
            "被改动了 —— 请先确认是有意收紧，再同步更新本断言。",
        )

    def test_ordinary_text_without_org_suffixes_matches_nothing(self):
        found = extract_orgs("天气转凉，注意添衣")
        self.assertEqual(found, {})

    def test_delay_risk_direction_stays_conservative(self):
        """过配的后果是把 `delay_risk` 判高 —— 保守方向。固化它。"""
        analysis = analyze_power_structure([], "本地区政府发布通知，要求限期整改")
        self.assertEqual(analysis["delay_risk"], "高")
        self.assertTrue(analysis["matched_orgs"])


# ---------------------------------------------------------------------- #
# today.js 的真跑 harness（R-14 只改文案，但文案也要真的渲染出来才算数）
# ---------------------------------------------------------------------- #
_TODAY_HARNESS = r"""
const { pathToFileURL } = require('node:url');

const viewUrl = process.argv[1];
const dashboard = process.argv[2];

class FakeElement {
  constructor(tag) {
    this.tag = tag || 'div';
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.style = {};
    this.classList = new Set();
    this.innerHTML = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.disabled = false;
    this.href = '';
  }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() { return null; }
  removeEventListener() { return null; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  getElementsByTagName() { return []; }
  click() { return null; }
  remove() { return null; }
}
class FakeLocation { constructor() { this.href = 'http://localhost/'; this.search = ''; this.hash = '#/today'; } }
class FakeDocument {
  constructor() { this.body = new FakeElement('body'); this._byId = {}; }
  createElement(tag) { return new FakeElement(tag); }
  getElementById(id) { if (!this._byId[id]) this._byId[id] = new FakeElement('div'); return this._byId[id]; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  addEventListener() { return null; }
}
class FakeMutationObserver { constructor() {} observe() {} disconnect() {} }
class FakeResponse { constructor(ok, body) { this.ok = ok; this._body = body; this.status = ok ? 200 : 500; } async text() { return this._body; } }

globalThis.window = { location: new FakeLocation() };
globalThis.location = globalThis.window.location;
globalThis.document = new FakeDocument();
globalThis.MutationObserver = FakeMutationObserver;
globalThis.HTMLElement = FakeElement;
globalThis.Node = class {};
globalThis.localStorage = { getItem: () => null, setItem: () => null, removeItem: () => null };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
globalThis.fetch = async (url) => {
  const target = String(url);
  if (target.includes('/api/risk-dashboard')) return new FakeResponse(true, dashboard);
  // 给一条待确认候选，让 needsOnboarding 为 false —— 这样首页会走到
  // "stable 空态"那句话，而不是新手引导。
  if (target.includes('/api/cognition/candidates')) {
    return new FakeResponse(true, JSON.stringify({ candidates: [{ id: 'P-1', statement: 'x', category: 'work' }] }));
  }
  if (target.includes('/api/forecasts/progress')) {
    return new FakeResponse(true, JSON.stringify({ resolved_total: 0, hit_total: 0, miss_total: 0 }));
  }
  return new FakeResponse(true, 'null');
};

(async () => {
  const mod = await import(viewUrl);
  const root = document.getElementById('view-root');
  await mod.render(root);
  process.stdout.write('HTML_START' + root.innerHTML + 'HTML_END');
})().catch(error => {
  process.stderr.write('RENDER_FAIL ' + (error && error.stack || String(error)));
  process.exit(1);
});
"""


def _render_today(dashboard) -> str:
    result = subprocess.run(
        [
            NODE, "-e", _TODAY_HARNESS,
            (STATIC / "js" / "views" / "today.js").resolve().as_uri(),
            json.dumps(dashboard, ensure_ascii=False),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"today.js 渲染失败：{result.stderr!r}")
    match = re.search(r"HTML_START(.*)HTML_END", result.stdout, re.S)
    if not match:
        raise AssertionError(f"harness 没有吐出 HTML：{result.stdout!r}")
    return match.group(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
