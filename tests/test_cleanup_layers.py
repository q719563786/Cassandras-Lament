"""C/D/E 层清理 + 阈值触发 + 6 小时间隔 + 硬不变量的对抗性测试。

规格来源：`build-artifacts/CONTRACT-cdef.md` §4（返回值与约束）、§5（硬不变量）。
本文件**不采信任何自述**，只按契约断言真实行为。

硬不变量（契约 §5）是本文件的核心：清理前后以下数据必须逐字节等价 ——
`judgments` / `forecasts` / `forecast_versions` / `resolutions` / `event_clusters` /
`interest_objects` / `interest_links`，`audit_log` 只增不减，
`trend_snapshots` 中 `window_hours=720` 一行都不能少。
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from yuanjian_app.database import Database
from yuanjian_app.retention import (
    CLUSTER_DETAIL_TABLES,
    DEFAULT_CLUSTER_DAYS,
    DEFAULT_DAYS,
    DEFAULT_JOB_DAYS,
    DEFAULT_MAX_JUDGMENTS_PER_CLUSTER,
    DEFAULT_MIN_INTERVAL_HOURS,
    DEFAULT_RUN_DAYS,
    DEFAULT_TABLE_THRESHOLD_MB,
    DEFAULT_THRESHOLD_MB,
    JOB_ABSOLUTE_MAX_DAYS,
    PROTECTED_SNAPSHOT_WINDOWS,
    SNAPSHOT_KEEP_DAYS,
    RetentionService,
    read_retention_setting,
    write_retention_setting,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

# 契约 §5 逐条列出的硬不变量表
IMMUTABLE_TABLES = (
    "judgments",
    "forecasts",
    "forecast_versions",
    "resolutions",
    "event_clusters",
    "interest_objects",
    "interest_links",
)


def _stamp(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def table_fingerprint(connection, table, where="1=1", params=()):
    """返回（行数, 内容 sha256）。用于证明"逐字节等价"。"""
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(%s)" % table)]
    rows = connection.execute(
        "SELECT * FROM %s WHERE %s" % (table, where), params
    ).fetchall()
    payload = sorted(
        json.dumps([row[column] for column in columns], ensure_ascii=False, default=str)
        for row in rows
    )
    digest = hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()
    return len(rows), digest


class CleanupBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = Clock(NOW)
        self.service = RetentionService(self.database, now=self.clock)

    def tearDown(self):
        self.temporary.cleanup()

    # -- 造数据 -----------------------------------------------------------

    def add_cluster(self, connection, cluster_id, last_seen_at, needs_judgment=0):
        stamp = _stamp(last_seen_at)
        connection.execute(
            "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,last_seen_at,"
            "evidence_level,evidence_hash,categories_json,status,needs_judgment,"
            "independent_domains,primary_source_count,created_at,updated_at)"
            " VALUES (?,?,'',?,?,'E2','hash','[\"policy\"]','active',?,1,1,?,?)",
            (cluster_id, "标题" + cluster_id, stamp, stamp, needs_judgment, stamp, stamp),
        )

    def add_cluster_detail(self, connection, cluster_id, at):
        """在每张可清理明细表里给该簇各挂一行。"""
        stamp = _stamp(at)
        connection.execute(
            "INSERT INTO personal_impacts(impact_id,cluster_id,judgment_id,interest_id,"
            "impact_score,alert_level,components_json,reason,candidate_json,created_at,updated_at)"
            " VALUES (?,?,'J-x','I-1',0.9,'L3','{}','原因','{}',?,?)",
            ("P-" + cluster_id, cluster_id, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO notification_log(notification_id,cluster_id,impact_id,created_at,"
            "alert_level,reason,evidence_hash,status,delivery)"
            " VALUES (?,?,?,?,'L3','原因','hash','sent','windows')",
            ("N-" + cluster_id, cluster_id, "P-" + cluster_id, stamp),
        )
        connection.execute(
            "INSERT INTO event_entities(entity_id,cluster_id,name,normalized_name,category,"
            "confidence) VALUES (?,?,?,'甲','actor',0.9)",
            ("E-" + cluster_id, cluster_id, "甲" + cluster_id),
        )
        connection.execute(
            "INSERT INTO event_cluster_items(cluster_id,item_id,similarity,merge_reason,"
            "source_domain,is_primary,added_at) VALUES (?,?,1.0,'new_cluster','example.com',1,?)",
            (cluster_id, "ITEM-" + cluster_id, stamp),
        )

    def add_job(self, connection, job_id, cluster_id, created_at, status="succeeded"):
        stamp = _stamp(created_at)
        connection.execute(
            "INSERT INTO judgment_jobs(job_id,cluster_id,evidence_hash,provider,model,status,"
            "attempts,request_chars,created_at,next_attempt_at)"
            " VALUES (?,?,?,'local','model',?,1,10,?,?)",
            (job_id, cluster_id, "hash-" + job_id, status, stamp, stamp),
        )

    def add_judgment(self, connection, judgment_id, cluster_id, created_at, provider="local"):
        connection.execute(
            "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,"
            "content_json,created_at) VALUES (?,?,?,?,'{}',?)",
            (judgment_id, cluster_id, provider, "hash-" + judgment_id, _stamp(created_at)),
        )

    def add_run(self, connection, run_id, started_at, source_id="src-1"):
        stamp = _stamp(started_at)
        connection.execute(
            "INSERT INTO external_runs(run_id,source_id,started_at,finished_at,status,"
            "fetched_count,new_count,error_type,error_message)"
            " VALUES (?,?,?,?,'ok',1,1,'','')",
            (run_id, source_id, stamp, stamp),
        )

    def add_snapshot(self, connection, snapshot_id, window_hours, captured_at, category="policy"):
        connection.execute(
            "INSERT INTO trend_snapshots(snapshot_id,captured_at,category,window_hours,"
            "event_count,baseline_count,surge_ratio,status)"
            " VALUES (?,?,?,?,10,5,2.0,'rising')",
            (snapshot_id, _stamp(captured_at), category, window_hours),
        )

    def add_item(self, connection, item_id, published_at):
        stamp = _stamp(published_at)
        connection.execute(
            "INSERT INTO external_items(item_id,canonical_url,title,summary,published_at,"
            "fetched_at,source_id,source_name,content_hash,first_seen_at,last_seen_at)"
            " VALUES (?,?,?,'','',?,?,'s','h',?,?)",
            (item_id, "https://example.com/" + item_id, item_id, stamp, "src-1", stamp, stamp),
        )

    def seed_immutable(self, connection):
        """填满契约 §5 的所有硬不变量表。"""
        connection.execute(
            "INSERT INTO interest_objects(object_id,name,category,importance,privacy_level,status)"
            " VALUES ('I-1','家庭','family',5,'private','active')"
        )
        connection.execute(
            "INSERT INTO interest_links(link_id,source_id,target_id,relationship,"
            "impact_direction,strength) VALUES ('L-1','I-1','I-1','self','positive',3)"
        )
        connection.execute(
            "INSERT INTO forecasts(forecast_id,status,window_end,category)"
            " VALUES ('F-1','open','2026-12-31','policy')"
        )
        connection.execute(
            "INSERT INTO forecast_versions(forecast_id,version,probability,content_sha256,content)"
            " VALUES ('F-1',1,0.7,'sha','内容')"
        )
        connection.execute(
            "INSERT INTO resolutions(forecast_id,outcome,resolved_at,probability,brier_score)"
            " VALUES ('F-1','hit','2026-09-01T00:00:00Z',0.7,0.09)"
        )

    def seed_every_layer(self):
        """同时铺出 A/B/C/D/E 五层可删数据，用于幂等与回滚用例。

        每层刻意放**两条**过期数据：只放一条的话，"每次只删一条"这类部分清理
        缺陷在第二次跑时刚好没得删，幂等测试会假绿。两条才能让它现形。

        注意：B 层会先删掉过期簇的 `judgment_jobs`，所以 C 层的可删作业必须挂在
        **未过期**的簇上，否则测不到 C 层。
        """
        with self.database.connect() as connection:
            self.seed_immutable(connection)
            # A 层：过期原始条目 ×2 + 一条应保留
            self.add_item(connection, "ITEM-old", NOW - timedelta(days=DEFAULT_DAYS + 30))
            self.add_item(connection, "ITEM-old2", NOW - timedelta(days=DEFAULT_DAYS + 10))
            self.add_item(connection, "ITEM-new", NOW - timedelta(days=1))
            # B 层：过期簇 + 全套明细；结论仍保留
            self.add_cluster(connection, "C-expired", NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20))
            self.add_cluster_detail(connection, "C-expired", NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20))
            self.add_judgment(connection, "J-old", "C-expired", NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20))
            # C 层：未过期簇上的陈旧成功作业 ×2（已有研判 → 冗余）+ 一条应保留
            self.add_cluster(connection, "C-live", NOW - timedelta(days=1))
            self.add_judgment(connection, "J-live", "C-live", NOW - timedelta(days=1))
            self.add_job(connection, "JOB-old", "C-live", NOW - timedelta(days=DEFAULT_JOB_DAYS + 3))
            self.add_job(connection, "JOB-old2", "C-live", NOW - timedelta(days=DEFAULT_JOB_DAYS + 1))
            self.add_job(connection, "JOB-new", "C-live", NOW - timedelta(days=1))
            # D 层：过期采集运行 ×2 + 一条应保留
            self.add_run(connection, "RUN-old", NOW - timedelta(days=DEFAULT_RUN_DAYS + 10))
            self.add_run(connection, "RUN-old2", NOW - timedelta(days=DEFAULT_RUN_DAYS + 3))
            self.add_run(connection, "RUN-new", NOW - timedelta(days=1))
            # E 层：各窗口过期 ×2 / 未过期，以及受保护的 720h
            for window in SNAPSHOT_KEEP_DAYS:
                self.add_snapshot(
                    connection,
                    "S-%d-old" % window,
                    window,
                    NOW - timedelta(days=SNAPSHOT_KEEP_DAYS[window] + 30),
                )
                self.add_snapshot(
                    connection,
                    "S-%d-old2" % window,
                    window,
                    NOW - timedelta(days=SNAPSHOT_KEEP_DAYS[window] + 1),
                )
            self.add_snapshot(connection, "S-6-new", 6, NOW - timedelta(days=1))
            self.add_snapshot(connection, "S-720-old", 720, NOW - timedelta(days=900))

    # -- 只读工具 ---------------------------------------------------------

    def counts(self, table, where="1=1", params=()):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (table, where), params
            ).fetchone()[0]

    def immutable_fingerprints(self):
        with self.database.connect() as connection:
            return {table: table_fingerprint(connection, table) for table in IMMUTABLE_TABLES}

    def audit_state(self, limit=None):
        """返回 (冻结上限, (行数, 内容哈希), 当前最大 audit_id)。

        冻结上限很关键：清理会**追加**审计行，若拿"当前最大 id"去算内容哈希，
        新行会被算进去，得出"审计被改动"的假阳性。所以先冻结上限再比内容。
        """
        with self.database.connect() as connection:
            highest = connection.execute(
                "SELECT COALESCE(MAX(audit_id),0) FROM audit_log"
            ).fetchone()[0]
            limit = highest if limit is None else limit
            frozen = table_fingerprint(connection, "audit_log", "audit_id <= ?", (limit,))
        return limit, frozen, highest

    def protected_snapshot_ids(self):
        with self.database.connect() as connection:
            return sorted(
                row[0]
                for row in connection.execute(
                    "SELECT snapshot_id FROM trend_snapshots WHERE window_hours IN (%s)"
                    % ",".join("?" * len(PROTECTED_SNAPSHOT_WINDOWS)),
                    PROTECTED_SNAPSHOT_WINDOWS,
                )
            )

    def freelist_count(self):
        """独立连接读 ``PRAGMA freelist_count``——对账用，不经过被测代码。"""
        with self.database.connect() as connection:
            return connection.execute("PRAGMA freelist_count").fetchone()[0]


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class HardInvariantTests(CleanupBase):
    """契约 §5：清理前后的硬不变量。任何一条被违反 = 交付不合格。"""

    def test_immutable_tables_are_byte_identical_after_cleanup(self):
        self.seed_every_layer()
        before = self.immutable_fingerprints()
        audit_limit, audit_frozen, _ = self.audit_state()

        result = self.service.run()

        self.assertEqual(result["status"], "ok")
        # 先确认这次清理**真的删了东西**，否则等价性是空断言
        removed = (
            result["deleted_items"]
            + sum(result["deleted_detail"].values())
            + result["deleted_jobs"]
            + result["deleted_runs"]
            + sum(result["downsampled_snapshots"].values())
        )
        self.assertGreater(removed, 0, "本次清理没有任何删除，等价性断言不成立")

        self.assertEqual(self.immutable_fingerprints(), before)
        # 清理前的审计内容必须一字未动（新追加的审计行不算）
        self.assertEqual(self.audit_state(limit=audit_limit)[1], audit_frozen)

    def test_audit_log_is_append_only(self):
        """审计只增不减：清理**前**就存在的审计行，内容必须一字未动。

        这里刻意先塞一条与清理无关的审计行。否则清理前的审计是空的，
        "旧行没被改"就成了空断言 —— 这条前置数据是让断言有牙齿的关键。
        """
        self.seed_every_layer()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(occurred_at,action,object_type,object_id,details_json)"
                " VALUES (?,'pre_existing','note','X','{}')",
                (_stamp(NOW - timedelta(days=30)),),
            )
        limit, frozen_before, highest_before = self.audit_state()
        self.assertEqual(frozen_before[0], 1, "前置审计行没造出来，断言会变成空的")

        self.service.run()

        _, frozen_after, highest_after = self.audit_state(limit=limit)
        self.assertGreater(highest_after, highest_before, "有效清理必须留下审计")
        self.assertEqual(frozen_after, frozen_before, "旧审计行的内容被改动或删除了")
        self.assertEqual(self.counts("audit_log", "action='pre_existing'"), 1)

    def test_720h_snapshots_are_never_touched(self):
        self.seed_every_layer()
        protected_before = self.protected_snapshot_ids()
        self.assertTrue(protected_before, "用例自身没造出受保护的 720h 快照")

        self.service.run()

        self.assertEqual(self.protected_snapshot_ids(), protected_before)
        self.assertEqual(self.counts("trend_snapshots", "window_hours=720"), 1)

    def test_conclusions_survive_even_when_cluster_is_expired(self):
        self.seed_every_layer()

        self.service.run()

        # 簇本身与研判是结论，明细被清、结论必须留
        self.assertEqual(self.counts("event_clusters", "cluster_id='C-expired'"), 1)
        self.assertEqual(self.counts("judgments", "cluster_id='C-expired'"), 1)
        self.assertEqual(self.counts("personal_impacts", "cluster_id='C-expired'"), 0)
        self.assertEqual(self.counts("event_cluster_items", "cluster_id='C-expired'"), 0)

    def test_unknown_snapshot_windows_are_left_alone(self):
        """不在 SNAPSHOT_KEEP_DAYS 也不在保护名单里的窗口一律不动。"""
        with self.database.connect() as connection:
            self.add_snapshot(connection, "S-999", 999, NOW - timedelta(days=2000))

        self.service.run()

        self.assertEqual(self.counts("trend_snapshots", "window_hours=999"), 1)


class IdempotencyTests(CleanupBase):
    """契约 §7.2：连跑两次，第二次各层删除数必须为 0。"""

    def test_second_run_deletes_nothing_in_any_layer(self):
        """契约 §7.2：第二次跑，每一层的**删除计数**都必须归 0。

        `expired_clusters` 单独在 `test_expired_clusters_is_a_scan_metric`
        里断言 —— 它是"扫到多少过期簇"，不是"删了多少"，见那里的说明。
        """
        self.seed_every_layer()

        first = self.service.run()
        second = self.service.run()

        for key in ("deleted_items", "deleted_jobs", "deleted_runs"):
            self.assertGreater(first[key], 0, "第一次跑 %s 就该有删除" % key)
        self.assertGreater(sum(first["deleted_detail"].values()), 0)
        self.assertGreater(sum(first["downsampled_snapshots"].values()), 0)

        self.assertEqual(second["deleted_items"], 0)
        self.assertEqual(sum(second["deleted_detail"].values()), 0)
        self.assertEqual(second["deleted_jobs"], 0)
        self.assertEqual(second["deleted_runs"], 0)
        self.assertEqual(sum(second["downsampled_snapshots"].values()), 0)

    def test_expired_clusters_is_a_scan_metric(self):
        """`expired_clusters` 是"有多少簇越过了明细保留窗口"，不是删除数。

        依据：契约 §4 里 B 层有**两个**并列键 —— `expired_clusters` 与
        `deleted_detail`。既然删除数已经由 `deleted_detail` 表达，命名用
        "expired" 而非 "deleted" 只能理解为扫描量。

        这条断言把该语义**显式钉死**，避免它被误当成删除计数：
        第二次跑时 `deleted_detail` 全 0（没东西可删），但 `expired_clusters`
        仍为 1（那个簇依然过期，结论本身永远不删）。
        """
        self.seed_every_layer()

        self.service.run()
        second = self.service.run()

        self.assertEqual(second["expired_clusters"], 1)
        self.assertEqual(sum(second["deleted_detail"].values()), 0)
        # 过期簇的数量不应随清理次数增长
        self.assertEqual(second["expired_clusters"], self.counts(
            "event_clusters", "last_seen_at < ?",
            (_stamp(NOW - timedelta(days=DEFAULT_CLUSTER_DAYS)),),
        ))

    def test_second_run_leaves_audit_log_untouched(self):
        """只有真删了才写审计 —— 空跑不该每天留一条全零记录。"""
        self.seed_every_layer()

        self.service.run()
        after_first = self.audit_state()
        self.service.run()

        self.assertEqual(self.audit_state(), after_first)
        self.assertEqual(self.counts("audit_log", "action='retention_cleanup'"), 1)


class MinIntervalTests(CleanupBase):
    """契约 §4.2：6 小时最小间隔；threshold 受约束，manual 绕过。"""

    def test_threshold_trigger_is_skipped_inside_the_interval(self):
        self.seed_every_layer()

        first = self.service.run(trigger="threshold")
        self.assertEqual(first["status"], "ok")

        second = self.service.run(trigger="threshold")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["trigger"], "threshold")
        self.assertEqual(second["deleted_items"], 0)
        self.assertEqual(second["deleted_detail"], {})
        self.assertEqual(second["downsampled_snapshots"], {})

    def test_manual_trigger_bypasses_the_interval(self):
        self.seed_every_layer()
        self.service.run(trigger="threshold")

        # 造一批新的可删数据，证明 manual 真的执行了
        with self.database.connect() as connection:
            self.add_run(connection, "RUN-late", NOW - timedelta(days=DEFAULT_RUN_DAYS + 5))

        result = self.service.run(trigger="manual")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["trigger"], "manual")
        self.assertEqual(result["deleted_runs"], 1)

    def test_scheduled_trigger_is_not_interval_limited(self):
        self.seed_every_layer()
        self.service.run(trigger="scheduled")

        with self.database.connect() as connection:
            self.add_run(connection, "RUN-late", NOW - timedelta(days=DEFAULT_RUN_DAYS + 5))

        result = self.service.run(trigger="scheduled")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deleted_runs"], 1)

    def test_skipped_run_does_not_reset_the_interval(self):
        """间隔以"上一次真删"为准，空跑不能把 6 小时窗口向后推。"""
        self.seed_every_layer()
        self.service.run(trigger="threshold")

        # 第 5 小时：仍在间隔内 → skipped，且不该写审计
        self.clock.value = NOW + timedelta(hours=5)
        self.assertEqual(self.service.run(trigger="threshold")["status"], "skipped")

        # 第 7 小时：距**上一次真删**已 7 小时 > 6 小时 → 必须放行
        self.clock.value = NOW + timedelta(hours=7)
        with self.database.connect() as connection:
            self.add_run(connection, "RUN-late", NOW - timedelta(days=DEFAULT_RUN_DAYS + 5))
        result = self.service.run(trigger="threshold")

        self.assertEqual(
            result["status"], "ok", "空跑把间隔重置了：6 小时窗口被错误地重新计时"
        )
        self.assertEqual(result["deleted_runs"], 1)

    def test_interval_boundary_at_exactly_six_hours(self):
        """恰好 6 小时不满足 `< interval`，必须放行。"""
        self.seed_every_layer()
        self.service.run(trigger="threshold")

        self.clock.value = NOW + timedelta(hours=DEFAULT_MIN_INTERVAL_HOURS)
        with self.database.connect() as connection:
            self.add_run(connection, "RUN-late", NOW - timedelta(days=DEFAULT_RUN_DAYS + 5))

        self.assertEqual(self.service.run(trigger="threshold")["status"], "ok")

    def test_disabled_setting_wins_over_interval(self):
        """enabled=False 优先返回 disabled，不该被跳过逻辑挡成 skipped。"""
        self.seed_every_layer()
        self.service.run(trigger="threshold")
        write_retention_setting(self.database, {"enabled": False})

        self.assertEqual(self.service.run(trigger="threshold")["status"], "disabled")


class RollbackTests(CleanupBase):
    """契约 §4.3：单事务，任一层失败整体回滚，不留半删状态。"""

    def test_failure_in_last_layer_rolls_back_every_earlier_layer(self):
        self.seed_every_layer()
        before = self.immutable_fingerprints()
        audit_before = self.audit_state()
        detail_before = {
            table: self.counts(table) for table in CLUSTER_DETAIL_TABLES
        }

        with mock.patch.object(
            RetentionService,
            "_downsample_snapshots",
            side_effect=RuntimeError("注入的 E 层故障"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.run()

        # A/B/C/D 层都已经执行过 DELETE —— 必须全部被回滚
        self.assertEqual(self.immutable_fingerprints(), before)
        self.assertEqual(
            {table: self.counts(table) for table in CLUSTER_DETAIL_TABLES}, detail_before
        )
        self.assertEqual(self.counts("external_items", "item_id='ITEM-old'"), 1)
        self.assertEqual(self.counts("external_runs", "run_id='RUN-old'"), 1)
        self.assertEqual(self.counts("judgment_jobs", "job_id='JOB-old'"), 1)
        self.assertEqual(self.audit_state(), audit_before)

    def test_failure_does_not_leave_a_resumable_half_state(self):
        """回滚后立刻重跑必须能完整清理 —— 证明没有残留的半删状态。"""
        self.seed_every_layer()

        with mock.patch.object(
            RetentionService, "_downsample_snapshots", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.service.run()

        result = self.service.run()

        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["deleted_jobs"], 0)
        self.assertGreater(result["deleted_runs"], 0)
        self.assertGreater(sum(result["downsampled_snapshots"].values()), 0)


class ThresholdTests(CleanupBase):
    """契约 §4 `should_run_by_threshold`：只读检查，不删除。"""

    def test_small_database_does_not_need_cleanup(self):
        self.seed_every_layer()

        report = self.service.should_run_by_threshold()

        self.assertEqual(
            set(report), {"needed", "reason", "db_bytes", "largest_table", "largest_bytes"}
        )
        self.assertFalse(report["needed"])
        self.assertEqual(report["reason"], "")
        self.assertGreater(report["db_bytes"], 0)
        self.assertIsInstance(report["largest_bytes"], int)

    def test_report_is_read_only(self):
        self.seed_every_layer()
        before = self.counts("external_items")
        audit_before = self.audit_state()

        self.service.should_run_by_threshold()

        self.assertEqual(self.counts("external_items"), before)
        self.assertEqual(self.audit_state(), audit_before)

    def test_database_size_over_threshold_triggers_confirmation(self):
        write_retention_setting(self.database, {"threshold_mb": 1})
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO runtime_state(state_key,value_json,updated_at) VALUES ('bloat',?,?)",
                ("x" * (3 * 1024 * 1024), _stamp(NOW)),
            )

        report = self.service.should_run_by_threshold()

        self.assertTrue(report["needed"], "3MB 的库对上 1MB 阈值必须要求清理")
        self.assertIn("库文件", report["reason"])
        self.assertGreater(report["db_bytes"], 1024 * 1024)

    def test_table_size_rule_is_evaluated(self):
        """覆盖"任一表超过 DEFAULT_TABLE_THRESHOLD_MB"这条分支。

        单元测试造不出 500 MB 的真表，所以把表阈值临时改成 0 —— 任何有内容的
        库都必然超标，从而真的走到该分支，而不是假装测过。
        """
        self.seed_every_layer()

        with mock.patch(
            "yuanjian_app.retention.DEFAULT_TABLE_THRESHOLD_MB", 0
        ):
            report = self.service.should_run_by_threshold()

        self.assertTrue(report["needed"])
        self.assertIn("表", report["reason"])
        self.assertGreater(report["largest_bytes"], 0)
        self.assertTrue(report["largest_table"])

    def test_threshold_trigger_records_threshold_mb_in_audit(self):
        self.seed_every_layer()

        self.service.run(trigger="threshold")

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT details_json FROM audit_log WHERE action='retention_cleanup'"
            ).fetchone()
        details = json.loads(row[0])
        self.assertEqual(details["trigger"], "threshold")
        self.assertEqual(details["threshold_mb"], DEFAULT_THRESHOLD_MB)
        self.assertIn("db_bytes_before", details)
        self.assertIn("db_bytes_after", details)

    def test_scheduled_trigger_omits_threshold_mb(self):
        self.seed_every_layer()

        self.service.run(trigger="scheduled")

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT details_json FROM audit_log WHERE action='retention_cleanup'"
            ).fetchone()
        self.assertNotIn("threshold_mb", json.loads(row[0]))


class JobLayerTests(CleanupBase):
    """C 层：`judgment_jobs` 的两条清理规则。"""

    def seed_live_cluster(self):
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-live", NOW - timedelta(days=1))
            self.add_judgment(connection, "J-live", "C-live", NOW - timedelta(days=1))

    def test_old_succeeded_job_with_judgment_is_removed(self):
        self.seed_live_cluster()
        with self.database.connect() as connection:
            self.add_job(connection, "JOB-old", "C-live", NOW - timedelta(days=DEFAULT_JOB_DAYS + 1))

        result = self.service.run()

        self.assertEqual(result["deleted_jobs"], 1)
        self.assertEqual(self.counts("judgment_jobs", "job_id='JOB-old'"), 0)

    def test_old_succeeded_job_without_judgment_is_kept(self):
        """簇还没有研判时删掉作业流水等于丢工作 —— 必须留。"""
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-bare", NOW - timedelta(days=1))
            self.add_job(connection, "JOB-bare", "C-bare", NOW - timedelta(days=DEFAULT_JOB_DAYS + 1))

        result = self.service.run()

        self.assertEqual(result["deleted_jobs"], 0)
        self.assertEqual(self.counts("judgment_jobs", "job_id='JOB-bare'"), 1)

    def test_recent_succeeded_job_with_judgment_is_kept(self):
        self.seed_live_cluster()
        with self.database.connect() as connection:
            self.add_job(connection, "JOB-fresh", "C-live", NOW - timedelta(days=1))

        result = self.service.run()

        self.assertEqual(result["deleted_jobs"], 0)
        self.assertEqual(self.counts("judgment_jobs", "job_id='JOB-fresh'"), 1)

    def test_absolute_age_rule_cleans_stale_jobs_of_any_status(self):
        """僵死的 pending/failed 作业靠 180 天绝对上限兜底。"""
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-bare", NOW - timedelta(days=1))
            for status in ("pending", "queued", "failed", "retry", "queued_budget"):
                self.add_job(
                    connection,
                    "JOB-" + status,
                    "C-bare",
                    NOW - timedelta(days=JOB_ABSOLUTE_MAX_DAYS + 10),
                    status=status,
                )

        result = self.service.run()

        self.assertEqual(result["deleted_jobs"], 5)
        self.assertEqual(self.counts("judgment_jobs"), 0)

    def test_absolute_rule_boundary_is_strictly_less_than(self):
        """恰好 180 天不满足 `created_at < cutoff`，不该被删。"""
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-bare", NOW - timedelta(days=1))
            self.add_job(
                connection,
                "JOB-exact",
                "C-bare",
                NOW - timedelta(days=JOB_ABSOLUTE_MAX_DAYS),
                status="failed",
            )

        self.assertEqual(self.service.run()["deleted_jobs"], 0)
        self.assertEqual(self.counts("judgment_jobs"), 1)

    def test_job_days_setting_is_honoured(self):
        self.seed_live_cluster()
        with self.database.connect() as connection:
            self.add_job(connection, "JOB-3d", "C-live", NOW - timedelta(days=3))

        write_retention_setting(self.database, {"job_days": 5})
        self.assertEqual(self.service.run()["deleted_jobs"], 0)

        write_retention_setting(self.database, {"job_days": 2})
        self.assertEqual(self.service.run()["deleted_jobs"], 1)


class RunLayerTests(CleanupBase):
    """D 层：`external_runs` 按 run_days 清理。"""

    def test_old_runs_are_deleted_and_recent_kept(self):
        with self.database.connect() as connection:
            self.add_run(connection, "RUN-old", NOW - timedelta(days=DEFAULT_RUN_DAYS + 1))
            self.add_run(connection, "RUN-new", NOW - timedelta(days=DEFAULT_RUN_DAYS - 1))

        result = self.service.run()

        self.assertEqual(result["deleted_runs"], 1)
        self.assertEqual(self.counts("external_runs", "run_id='RUN-old'"), 0)
        self.assertEqual(self.counts("external_runs", "run_id='RUN-new'"), 1)

    def test_run_days_setting_is_honoured(self):
        with self.database.connect() as connection:
            self.add_run(connection, "RUN-30d", NOW - timedelta(days=30))

        write_retention_setting(self.database, {"run_days": 60})
        self.assertEqual(self.service.run()["deleted_runs"], 0)

        write_retention_setting(self.database, {"run_days": 20})
        self.assertEqual(self.service.run()["deleted_runs"], 1)


class SnapshotLayerTests(CleanupBase):
    """E 层：按窗口降采样，720h 永远不动。"""

    def test_each_window_uses_its_own_keep_days(self):
        with self.database.connect() as connection:
            for window, keep_days in SNAPSHOT_KEEP_DAYS.items():
                self.add_snapshot(
                    connection,
                    "S-%d-old" % window,
                    window,
                    NOW - timedelta(days=keep_days + 1),
                )
                self.add_snapshot(
                    connection,
                    "S-%d-new" % window,
                    window,
                    NOW - timedelta(days=keep_days - 1),
                )
            self.add_snapshot(connection, "S-720-old", 720, NOW - timedelta(days=5000))

        result = self.service.run()

        self.assertEqual(
            set(result["downsampled_snapshots"]), set(SNAPSHOT_KEEP_DAYS)
        )
        for window, keep_days in SNAPSHOT_KEEP_DAYS.items():
            with self.subTest(window=window):
                self.assertEqual(result["downsampled_snapshots"][window], 1)
                self.assertIsInstance(window, int)
                self.assertEqual(self.counts("trend_snapshots", "snapshot_id=?", ("S-%d-old" % window,)), 0)
                self.assertEqual(self.counts("trend_snapshots", "snapshot_id=?", ("S-%d-new" % window,)), 1)
        self.assertEqual(self.counts("trend_snapshots", "snapshot_id='S-720-old'"), 1)

    def test_counts_dict_always_lists_every_window(self):
        """即使某个窗口删了 0 行，返回值也要给出该窗口键，便于核对。"""
        with self.database.connect() as connection:
            self.add_snapshot(connection, "S-6-new", 6, NOW - timedelta(days=1))

        result = self.service.run()

        self.assertEqual(sorted(result["downsampled_snapshots"]), sorted(SNAPSHOT_KEEP_DAYS))
        self.assertEqual(set(result["downsampled_snapshots"].values()), {0})

    def test_protected_window_is_not_in_the_downsample_table(self):
        for window in PROTECTED_SNAPSHOT_WINDOWS:
            self.assertNotIn(
                window,
                SNAPSHOT_KEEP_DAYS,
                "720h 同时出现在保留表和保护名单里，语义会冲突",
            )


class DisabledAndSettingTests(CleanupBase):
    """契约 §4.1 与 §3：禁用时的返回结构、设置键的 clamp 与越界。"""

    def test_disabled_returns_zeroed_counts_for_every_new_key(self):
        self.seed_every_layer()
        write_retention_setting(self.database, {"enabled": False})

        result = self.service.run()

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["deleted_items"], 0)
        self.assertEqual(result["expired_clusters"], 0)
        self.assertEqual(result["deleted_detail"], {})
        self.assertEqual(result["deleted_jobs"], 0)
        self.assertEqual(result["deleted_runs"], 0)
        self.assertEqual(result["downsampled_snapshots"], {})
        # 一个字节都不许动
        self.assertEqual(self.counts("personal_impacts"), 1)
        self.assertEqual(self.counts("external_items"), 3)
        self.assertEqual(self.counts("judgment_jobs"), 3)
        self.assertEqual(self.counts("external_runs"), 3)
        self.assertEqual(self.counts("trend_snapshots"), 8)

    def test_result_always_contains_new_contract_keys(self):
        expected = {
            "status",
            "trigger",
            "deleted_items",
            "expired_clusters",
            "deleted_detail",
            "deleted_jobs",
            "deleted_runs",
            "downsampled_snapshots",
            "cutoff",
            "cluster_cutoff",
            "db_bytes_before",
            "db_bytes_after",
            "free_pages_before",
            "free_pages_after",
        }
        self.assertEqual(set(self.service.run()), expected)
        write_retention_setting(self.database, {"enabled": False})
        self.assertEqual(set(self.service.run()), expected)

    def test_free_pages_grow_when_rows_are_actually_deleted(self):
        """清理生效的证据是空闲页增长，不是体积缩小（库是 auto_vacuum=0）。

        这条不去信 docstring 的说法：先用一张临时表「造出又丢掉」两页空闲页，
        让 ``free_pages_before`` 有个非零的真值可对——否则它恒等于 0，
        "before 根本没读库"这种缺陷就测不出来（变异对照实测过）。然后再铺出
        足够多的过期行（整页被删空才回得到空闲列表），最后用**独立连接**读
        ``PRAGMA freelist_count`` 与返回值对账。
        同时钉住"不自动 VACUUM"：物理体积正常相等，谁把它当 bug 改掉都会现形。
        """
        with self.database.connect() as connection:
            self.seed_immutable(connection)
            connection.execute("CREATE TABLE scratch(payload)")
            connection.executemany(
                "INSERT INTO scratch(payload) VALUES (?)",
                [(b"x" * 400,) for _ in range(4000)],
            )
            connection.execute("DROP TABLE scratch")
            for index in range(3000):
                self.add_item(
                    connection,
                    "ITEM-bulk-%04d" % index,
                    NOW - timedelta(days=DEFAULT_DAYS + 30),
                )
        before = self.freelist_count()
        self.assertGreater(before, 0, "没造出空闲页，before 的断言会退化成空断言")

        result = self.service.run()

        self.assertEqual(result["deleted_items"], 3000)
        self.assertEqual(result["free_pages_before"], before)
        self.assertEqual(result["free_pages_after"], self.freelist_count())
        self.assertGreater(
            result["free_pages_after"],
            result["free_pages_before"],
            "删了 3000 行却没有释放任何空闲页，说明删除没有真的落到磁盘页上",
        )
        self.assertEqual(result["db_bytes_before"], result["db_bytes_after"])

    def test_free_pages_are_present_but_zero_on_early_exit(self):
        """disabled / skipped 提前返回时，两个空闲页键也必须在，取 0。

        返回值是"结构恒定"的：调用方不需要先判断 status 才敢取键，也不该因为
        这次没干活就到手一个 KeyError。
        """
        self.seed_every_layer()
        write_retention_setting(self.database, {"enabled": False})
        disabled = self.service.run()
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["free_pages_before"], 0)
        self.assertEqual(disabled["free_pages_after"], 0)

        # 打开开关后第一次必然执行（此前没有留存清理审计 → 不触发间隔），
        # 紧跟的第二次落在 6 小时窗口内 → skipped，两个键仍须存在且为 0。
        write_retention_setting(self.database, {"enabled": True})
        self.assertEqual(self.service.run("threshold")["status"], "ok")
        skipped = self.service.run("threshold")

        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["free_pages_before"], 0)
        self.assertEqual(skipped["free_pages_after"], 0)

    def test_defaults_match_the_contract(self):
        setting = read_retention_setting(self.database)

        self.assertTrue(setting["enabled"])
        self.assertEqual(setting["days"], DEFAULT_DAYS)
        self.assertEqual(setting["cluster_days"], DEFAULT_CLUSTER_DAYS)
        self.assertEqual(setting["job_days"], DEFAULT_JOB_DAYS)
        self.assertEqual(setting["run_days"], DEFAULT_RUN_DAYS)
        self.assertEqual(
            setting["max_judgments_per_cluster"], DEFAULT_MAX_JUDGMENTS_PER_CLUSTER
        )
        self.assertEqual(setting["threshold_mb"], DEFAULT_THRESHOLD_MB)
        self.assertEqual(setting["min_interval_hours"], DEFAULT_MIN_INTERVAL_HOURS)

    def test_clamps_garbage_input_instead_of_raising(self):
        for garbage in (
            {"job_days": 0, "run_days": 1, "max_judgments_per_cluster": 0,
             "threshold_mb": 0, "min_interval_hours": -5},
            {"job_days": 9999, "run_days": 99999, "max_judgments_per_cluster": 9999,
             "threshold_mb": 10 ** 12, "min_interval_hours": 99999},
            {"job_days": "abc", "run_days": None, "max_judgments_per_cluster": [],
             "threshold_mb": {}, "min_interval_hours": "x"},
        ):
            with self.subTest(payload_keys=sorted(garbage)):
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO runtime_state(state_key,value_json,updated_at)"
                        " VALUES ('settings.retention',?,?)"
                        " ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json",
                        (json.dumps(garbage), _stamp(NOW)),
                    )
                setting = read_retention_setting(self.database)

                self.assertGreaterEqual(setting["job_days"], 1)
                self.assertLessEqual(setting["job_days"], 180)
                self.assertGreaterEqual(setting["run_days"], 7)
                self.assertLessEqual(setting["run_days"], 730)
                self.assertGreaterEqual(setting["max_judgments_per_cluster"], 1)
                self.assertLessEqual(setting["max_judgments_per_cluster"], 100)
                self.assertGreaterEqual(setting["threshold_mb"], 1)
                self.assertGreaterEqual(setting["min_interval_hours"], 0)

    def test_corrupt_setting_json_falls_back_to_defaults(self):
        for raw in ("{ not json", "[]", "null", '{"days": "x"}'):
            with self.subTest(raw=raw):
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO runtime_state(state_key,value_json,updated_at)"
                        " VALUES ('settings.retention',?,?)"
                        " ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json",
                        (raw, _stamp(NOW)),
                    )
                setting = read_retention_setting(self.database)

                self.assertEqual(setting["job_days"], DEFAULT_JOB_DAYS)
                self.assertEqual(setting["run_days"], DEFAULT_RUN_DAYS)
                self.assertEqual(setting["min_interval_hours"], DEFAULT_MIN_INTERVAL_HOURS)

    def test_new_keys_reject_out_of_range_on_write(self):
        cases = (
            ("job_days", 0),
            ("job_days", 181),
            ("run_days", 6),
            ("run_days", 731),
            ("max_judgments_per_cluster", 0),
            ("max_judgments_per_cluster", 101),
            ("threshold_mb", 0),
            ("min_interval_hours", -1),
            ("min_interval_hours", 169),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    write_retention_setting(self.database, {key: value})

    def test_new_keys_accept_in_range_on_write(self):
        updated = write_retention_setting(
            self.database,
            {
                "job_days": 30,
                "run_days": 365,
                "max_judgments_per_cluster": 12,
                "threshold_mb": 4096,
                "min_interval_hours": 12,
            },
        )

        self.assertEqual(updated["job_days"], 30)
        self.assertEqual(updated["run_days"], 365)
        self.assertEqual(updated["max_judgments_per_cluster"], 12)
        self.assertEqual(updated["threshold_mb"], 4096)
        self.assertEqual(updated["min_interval_hours"], 12)
        self.assertEqual(read_retention_setting(self.database), updated)

    def test_zero_min_interval_disables_the_interval_gate(self):
        self.seed_every_layer()
        write_retention_setting(self.database, {"min_interval_hours": 0})
        self.service.run(trigger="threshold")

        with self.database.connect() as connection:
            self.add_run(connection, "RUN-late", NOW - timedelta(days=DEFAULT_RUN_DAYS + 5))

        self.assertEqual(self.service.run(trigger="threshold")["status"], "ok")


if __name__ == "__main__":
    unittest.main()
