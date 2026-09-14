"""结论明细分级清理的契约测试。

这一组测试钉死四件事：
1. 过期簇的**派生明细**会被清掉（此前永不清理，是库体积膨胀的主因之一）
2. **结论永不删除**：事件簇与研判都保留；`judgments` 还受数据库触发器保护
3. **预测账本与趋势快照永不删除**（校准统计只读它们，不能被清理影响）
4. 清理窗口彼此独立：结论明细用 cluster_days，原始条目用 days
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.retention import (
    CLUSTER_DETAIL_TABLES,
    DEFAULT_CLUSTER_DAYS,
    RetentionService,
    read_retention_setting,
    write_retention_setting,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RetentionCascadeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "yuanjian.db")
        self.database.initialize()
        self.service = RetentionService(self.database, now=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def add_cluster(self, connection, cluster_id, last_seen_at):
        """造一个簇、一条研判，并在每张可清理的明细表里各挂一行。"""
        stamp = _iso(last_seen_at)
        connection.execute(
            """
            INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,last_seen_at,
                evidence_level,evidence_hash,categories_json,status,needs_judgment,
                independent_domains,primary_source_count,created_at,updated_at)
            VALUES (?,?,?,?,?,'E2','hash','["finance"]','active',0,1,1,?,?)
            """,
            (cluster_id, f"标题{cluster_id}", f"摘要{cluster_id}", stamp, stamp, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,content_json,created_at)"
            " VALUES (?,?,'local','hash','{}',?)",
            (f"J-{cluster_id}", cluster_id, stamp),
        )
        connection.execute(
            "INSERT INTO personal_impacts(impact_id,cluster_id,judgment_id,interest_id,"
            "impact_score,alert_level,components_json,reason,candidate_json,created_at,updated_at)"
            " VALUES (?,?,?,'I-1',0.9,'L3','{}','原因','{}',?,?)",
            (f"P-{cluster_id}", cluster_id, f"J-{cluster_id}", stamp, stamp),
        )
        connection.execute(
            "INSERT INTO notification_log(notification_id,cluster_id,impact_id,created_at,"
            "alert_level,reason,evidence_hash,status,delivery)"
            " VALUES (?,?,?,?,'L3','原因','hash','sent','windows')",
            (f"N-{cluster_id}", cluster_id, f"P-{cluster_id}", stamp),
        )
        connection.execute(
            "INSERT INTO judgment_jobs(job_id,cluster_id,evidence_hash,provider,model,status,"
            "attempts,request_chars,created_at,next_attempt_at)"
            " VALUES (?,?,'hash','local','local','done',1,10,?,?)",
            (f"JOB-{cluster_id}", cluster_id, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO event_entities(entity_id,cluster_id,name,normalized_name,category,confidence)"
            " VALUES (?,?,?,'甲','actor',0.9)",
            (f"E-{cluster_id}", cluster_id, f"甲{cluster_id}"),
        )
        connection.execute(
            "INSERT INTO event_cluster_items(cluster_id,item_id,similarity,merge_reason,"
            "source_domain,is_primary,added_at) VALUES (?,?,1.0,'new_cluster','example.com',1,?)",
            (cluster_id, f"ITEM-{cluster_id}", stamp),
        )

    def add_ledger_rows(self, connection):
        """造一条预测账本与一条趋势快照，它们绝不能被清理。"""
        connection.execute(
            "INSERT INTO forecasts(forecast_id,status,window_end,category)"
            " VALUES ('F-1','open','2026-12-31','finance')"
        )
        connection.execute(
            "INSERT INTO forecast_versions(forecast_id,version,probability,content_sha256,content)"
            " VALUES ('F-1',1,0.7,'sha','内容')"
        )
        connection.execute(
            "INSERT INTO resolutions(forecast_id,outcome,resolved_at,probability,brier_score)"
            " VALUES ('F-1','hit','2026-09-01T00:00:00Z',0.7,0.09)"
        )
        connection.execute(
            "INSERT INTO trend_snapshots(snapshot_id,captured_at,category,window_hours,"
            "event_count,baseline_count,surge_ratio,status)"
            " VALUES ('S-1','2026-01-01T00:00:00Z','finance',24,10,5,2.0,'rising')"
        )

    def counts(self, table, where="1=1", params=()):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (table, where), params
            ).fetchone()[0]

    def test_expired_cluster_detail_is_cleaned_but_conclusions_survive(self):
        with self.database.connect() as connection:
            # 200 天前：超出结论明细默认 180 天窗口，明细该清
            self.add_cluster(connection, "C-old", NOW - timedelta(days=200))
            # 100 天前：在 180 天内，该留（虽然已超出原始条目的 60 天）
            self.add_cluster(connection, "C-edge", NOW - timedelta(days=100))
            # 5 天前：最该留
            self.add_cluster(connection, "C-new", NOW - timedelta(days=5))
            self.add_ledger_rows(connection)

        result = self.service.run()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expired_clusters"], 1)
        self.assertEqual(sorted(result["deleted_detail"]), sorted(CLUSTER_DETAIL_TABLES))
        for table in CLUSTER_DETAIL_TABLES:
            self.assertEqual(result["deleted_detail"][table], 1, table)

        # 过期簇的派生明细都没了
        for table in CLUSTER_DETAIL_TABLES:
            self.assertEqual(
                self.counts(table, "cluster_id='C-old'"),
                0,
                "%s 仍留有已过期簇的明细" % table,
            )

        # 结论必须保留：簇本身与它的研判都还在
        self.assertEqual(self.counts("event_clusters", "cluster_id='C-old'"), 1)
        self.assertEqual(self.counts("judgments", "cluster_id='C-old'"), 1)

        # 未过期的两个簇一行都没少
        for cluster_id in ("C-edge", "C-new"):
            for table in CLUSTER_DETAIL_TABLES + ("judgments",):
                self.assertEqual(
                    self.counts(table, "cluster_id=?", (cluster_id,)),
                    1,
                    "%s 误删了 %s 的行" % (table, cluster_id),
                )

        # 预测账本与趋势快照原样保留
        self.assertEqual(self.counts("forecasts"), 1)
        self.assertEqual(self.counts("forecast_versions"), 1)
        self.assertEqual(self.counts("resolutions"), 1)
        self.assertEqual(self.counts("trend_snapshots"), 1)

        # 审计里能查到这次删除的明细
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT details_json FROM audit_log WHERE action='retention_cleanup'"
            ).fetchone()
        details = json.loads(row[0])
        self.assertEqual(details["expired_clusters"], 1)
        self.assertEqual(details["cluster_days"], DEFAULT_CLUSTER_DAYS)
        self.assertEqual(details["deleted_detail"]["personal_impacts"], 1)

    def test_judgments_are_protected_by_a_database_trigger(self):
        """judgments 的不可变性由触发器强制，清理逻辑不可能绕过它。

        这是"判读不可变"这条产品承诺的落地方式，和不可变预测账本同级。
        如果有人日后想删研判，数据库会直接拒绝。
        """
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-1", NOW)

        with self.database.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM judgments WHERE cluster_id='C-1'")

        self.assertEqual(self.counts("judgments"), 1)

    def test_run_is_idempotent(self):
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-old", NOW - timedelta(days=200))

        first = self.service.run()
        second = self.service.run()

        self.assertEqual(first["deleted_detail"]["personal_impacts"], 1)
        # 第二次没有任何明细可删
        self.assertEqual(sum(second["deleted_detail"].values()), 0)
        # 结论仍在
        self.assertEqual(self.counts("event_clusters"), 1)
        self.assertEqual(self.counts("judgments"), 1)
        # 只应留下一条审计记录（第二次没有实际删除，不该再写）
        self.assertEqual(self.counts("audit_log", "action='retention_cleanup'"), 1)

    def test_cluster_window_is_configurable_and_independent(self):
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-100", NOW - timedelta(days=100))

        write_retention_setting(self.database, {"days": 60, "cluster_days": 90})

        result = self.service.run()

        # 100 天 > 90 天 → 这次该清；原始条目窗口仍是 60 天，未被改动
        self.assertEqual(result["deleted_detail"]["personal_impacts"], 1)
        self.assertEqual(read_retention_setting(self.database)["days"], 60)

    def test_disabled_setting_touches_nothing(self):
        with self.database.connect() as connection:
            self.add_cluster(connection, "C-old", NOW - timedelta(days=400))

        write_retention_setting(self.database, {"enabled": False})

        result = self.service.run()

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.counts("event_clusters"), 1)
        self.assertEqual(self.counts("personal_impacts"), 1)


if __name__ == "__main__":
    unittest.main()
