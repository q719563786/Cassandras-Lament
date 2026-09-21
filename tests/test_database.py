import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from yuanjian_app.database import Database


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_legacy_fixture(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE forecasts(forecast_id TEXT PRIMARY KEY, status TEXT, window_end TEXT);
        CREATE TABLE forecast_versions(
            forecast_id TEXT, version INTEGER, probability REAL,
            content_sha256 TEXT, content TEXT,
            PRIMARY KEY(forecast_id, version)
        );
        CREATE TABLE resolutions(
            forecast_id TEXT PRIMARY KEY, outcome TEXT, resolved_at TEXT,
            probability REAL, brier_score REAL
        );
        """
    )
    for index, probability in enumerate((0.65, 0.65, 0.80), start=1):
        forecast_id = f"F-{index}"
        connection.execute(
            "INSERT INTO forecasts VALUES (?, 'open', '2026-08-31')", (forecast_id,)
        )
        connection.execute(
            "INSERT INTO forecast_versions VALUES (?, 1, ?, 'hash', ?)",
            (forecast_id, probability, f"title: 预测{index}"),
        )
    connection.commit()
    connection.close()


class DatabaseTests(unittest.TestCase):
    def test_import_legacy_copies_forecasts_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "legacy.db"
            create_legacy_fixture(legacy)
            before = sha256(legacy)
            database = Database(root / "private" / "yuanjian.db")

            result = database.import_legacy(legacy)

            self.assertEqual(result.forecasts, 3)
            self.assertEqual(result.versions, 3)
            self.assertEqual(sha256(legacy), before)
            with database.connect() as connection:
                migrations = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations"
                ).fetchone()[0]
            self.assertEqual(migrations, 8)

    def test_migration_two_creates_sensory_tables_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "yuanjian.db")

            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO interest_objects(object_id, name, category, importance, privacy_level, status) VALUES ('I-test', '测试利益', 'general', 3, 'P1', 'active')"
                )
            database.initialize()

            with database.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                migrations = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                preserved = connection.execute(
                    "SELECT name FROM interest_objects WHERE object_id = 'I-test'"
                ).fetchone()[0]

            self.assertTrue(
                {"interest_objects", "interest_links", "signals", "knowledge_documents"}.issubset(tables)
            )
            self.assertEqual([row[0] for row in migrations], [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertEqual(preserved, "测试利益")

    def test_migration_three_creates_external_radar_tables_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "yuanjian.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO forecasts(forecast_id, status, window_end) VALUES ('F-keep', 'open', '2026-12-31')"
                )

            database.initialize()

            with database.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                preserved = connection.execute(
                    "SELECT status FROM forecasts WHERE forecast_id = 'F-keep'"
                ).fetchone()[0]

            self.assertTrue(
                {
                    "external_sources",
                    "watch_rules",
                    "external_items",
                    "external_item_sources",
                    "external_matches",
                    "external_runs",
                }.issubset(tables)
            )
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertEqual(preserved, "open")

    def test_migration_four_creates_cognition_tables_and_immutable_judgments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "yuanjian.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO forecasts(forecast_id, status, window_end) VALUES ('F-before-v5', 'open', '2027-01-01')"
                )
                connection.execute(
                    """
                    INSERT INTO external_items(
                        item_id, canonical_url, title, summary, published_at,
                        fetched_at, source_id, source_name, language, content_hash,
                        first_seen_at, last_seen_at, source_count, raw_json
                    ) VALUES (
                        'E-before-v5', 'https://example.com/before-v5', '旧外部条目', '', NULL,
                        '2026-08-11T00:00:00Z', 'S-old', '旧来源', 'Chinese', 'hash',
                        '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z', 1, '{}'
                    )
                    """
                )

            database.initialize()
            database.initialize()

            expected = {
                "event_clusters",
                "event_cluster_items",
                "event_entities",
                "trend_snapshots",
                "judgment_jobs",
                "judgments",
                "personal_impacts",
                "notification_log",
                "runtime_state",
            }
            with database.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                migrations = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                preserved = (
                    connection.execute(
                        "SELECT COUNT(*) FROM forecasts WHERE forecast_id='F-before-v5'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM external_items WHERE item_id='E-before-v5'"
                    ).fetchone()[0],
                )
                connection.execute(
                    """
                    INSERT INTO judgments(
                        judgment_id, cluster_id, provider, evidence_hash,
                        content_json, created_at
                    ) VALUES ('J-1', 'C-1', 'local', 'evidence', '{}', '2026-08-11T00:00:00Z')
                    """
                )

            self.assertTrue(expected.issubset(tables))
            self.assertEqual(migrations, [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertEqual(preserved, (1, 1))
            with database.connect() as connection:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "judgments are immutable"
                ):
                    connection.execute(
                        "UPDATE judgments SET provider='changed' WHERE judgment_id='J-1'"
                    )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "judgments are immutable"
                ):
                    connection.execute("DELETE FROM judgments WHERE judgment_id='J-1'")

    def test_v4_database_with_legacy_external_sources_migrates_to_v5(self):
        """Regression: an old database with an external_sources table that
        pre-dates the v5 region/category/user_managed columns must migrate
        successfully. Previously the migration ran AFTER the executescript,
        so the CREATE INDEX ON external_sources(region) would crash with
        'no such column: region' on startup and brick the user's install.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "yuanjian.db"
            connection = sqlite3.connect(db_path)
            try:
                # Bare-minimum v4 schema: external_sources exists but is
                # missing the three v5 columns. All other v5 tables are
                # absent, simulating a real upgrade from a v4 install.
                connection.executescript(
                    """
                    CREATE TABLE forecasts(
                        forecast_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        window_end TEXT NOT NULL
                    );
                    CREATE TABLE external_sources(
                        source_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        refresh_minutes INTEGER NOT NULL DEFAULT 15,
                        reliability_weight REAL NOT NULL DEFAULT 0.6,
                        config_json TEXT NOT NULL DEFAULT '{}',
                        last_attempt_at TEXT,
                        last_success_at TEXT,
                        last_status TEXT NOT NULL DEFAULT 'never',
                        last_error TEXT NOT NULL DEFAULT '',
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        next_fetch_at TEXT
                    );
                    INSERT INTO external_sources(
                        source_id, name, kind, endpoint
                    ) VALUES ('S-legacy', '旧版源', 'rss', 'https://example.com/legacy.xml');
                    INSERT INTO forecasts(
                        forecast_id, status, window_end
                    ) VALUES ('F-legacy', 'open', '2026-12-31');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(db_path)
            database.initialize()  # Must not raise

            with database.connect() as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(external_sources)")
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='external_sources'"
                    )
                }
                preserved_source = connection.execute(
                    "SELECT name FROM external_sources WHERE source_id='S-legacy'"
                ).fetchone()[0]
                preserved_forecast = connection.execute(
                    "SELECT status FROM forecasts WHERE forecast_id='F-legacy'"
                ).fetchone()[0]

            self.assertIn("region", columns)
            self.assertIn("category", columns)
            self.assertIn("user_managed", columns)
            self.assertIn("idx_external_sources_region", indexes)
            self.assertEqual(preserved_source, "旧版源")
            self.assertEqual(preserved_forecast, "open")

    def test_hot_path_indexes_exist_and_are_used(self):
        """热路径查询必须走索引，不能再全表扫描。

        这些索引来自一次真实数据库（约 9 倍于测试规模）上的执行计划审查：
        加索引前相关查询单次 26~110 毫秒且全表扫描，加索引后降到 0.01~0.25 毫秒。
        这里用执行计划本身断言，避免以后有人"清理"掉这些索引。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "yuanjian.db")
            database.initialize()

            expected = {
                "idx_external_items_source",
                "idx_external_items_published",
                "idx_notification_log_cluster_created",
                "idx_judgment_jobs_finished",
                "idx_event_cluster_items_item",
            }

            with database.connect() as connection:
                present = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                # 用空表也能拿到执行计划：关键是计划里不能对大表做全表扫描
                plans = {
                    "external_items": "SELECT item_id FROM external_items WHERE source_id = ?",
                    "notification_log": (
                        "SELECT * FROM notification_log WHERE cluster_id=? AND created_at>=? "
                        "ORDER BY created_at DESC, notification_id DESC LIMIT 1"
                    ),
                    "judgment_jobs": (
                        "SELECT COUNT(*) FROM judgment_jobs WHERE provider!='local' "
                        "AND finished_at IS NOT NULL AND finished_at>=? AND finished_at<?"
                    ),
                    "event_cluster_items": (
                        "SELECT cluster_id FROM event_cluster_items WHERE item_id=?"
                    ),
                }
                for table, sql in plans.items():
                    placeholders = sql.count("?")
                    plan = " | ".join(
                        str(row[-1])
                        for row in connection.execute(
                            "EXPLAIN QUERY PLAN " + sql, tuple([None] * placeholders)
                        )
                    )
                    self.assertNotIn(
                        "SCAN %s" % table,
                        plan,
                        f"{table} 的查询退化成全表扫描：{plan}",
                    )

            missing = expected - present
            self.assertEqual(missing, set(), f"缺少热路径索引：{sorted(missing)}")


class LegacyAlertBackfillMigrationTests(unittest.TestCase):
    """v7：存量 `personal_impacts` 定级回填（迁移契约）。

    这是**唯一**会改写用户既有数据的迁移，所以断言的重点不是"改完好看"，而是：
    先备份、只改两列、身份列一字不动、幂等、重复运行不再产生副作用。
    """

    def _legacy_database(self, root):
        """造一个"尚未回填"的库：schema 已是当前版，但没有 v7 标记 + 一行旧口径影响。"""
        database = Database(root / "data" / "yuanjian.db")
        database.initialize()  # 空库直接记 v7
        with database.connect() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=7")
            connection.execute(
                "INSERT INTO interest_objects(object_id,name,category,importance,"
                "privacy_level,status) VALUES"
                " ('I-default-health','健康安全','health',5,'P1','active')"
            )
            connection.execute(
                "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                "last_seen_at,evidence_level,evidence_hash,categories_json,"
                "latest_judgment_id,created_at,updated_at) VALUES"
                " ('C-1','河源市水务局发布医保报销比例调整公告','',"
                " '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','E3','h1',"
                " '[\"health\"]','J-1','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,"
                "content_json,created_at) VALUES ('J-1','C-1','deepseek_chat','h1',?,"
                " '2026-08-01T00:00:00Z')",
                (
                    json.dumps(
                        {
                            "fact_summary": "河源市水务局发布医保报销比例调整公告",
                            "causal_chain": ["政策发布", "报销变化"],
                            "horizons": ["未来7天"],
                            "confidence": 0.7,
                            "impact_categories": ["health"],
                            "personal_action": "与你直接相关。",
                            # 远程契约里**没有** power_structure —— 这正是 ③ 要补的缺口
                            "gyw": {"risk_signal_hit": []},
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            # 旧口径：只有 5 个字段，importance 是恒定的 0.6，没有结构信号。
            connection.execute(
                "INSERT INTO personal_impacts(impact_id,cluster_id,judgment_id,"
                "interest_id,impact_score,alert_level,components_json,reason,"
                "candidate_json,created_at,updated_at) VALUES"
                " ('P-legacy','C-1','J-1','I-default-health',0.66,'L2',?,"
                " 'legacy','{}','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')",
                (
                    json.dumps(
                        {
                            "confidence": 0.5, "evidence": 0.5, "exposure": 1.0,
                            "importance": 0.6, "urgency": 1.0,
                        }
                    ),
                ),
            )
        return database

    def test_empty_database_records_the_version_without_a_backup(self):
        """空库（含全部测试库）不该为了一个空回填留备份。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "yuanjian.db", backup_dir=root / "backups")
            database.initialize()

            with database.connect() as connection:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])
            self.assertFalse((root / "backups").exists())

    def test_backfill_backs_up_first_then_rewrites_only_the_two_grading_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._legacy_database(root)

            database = Database(
                root / "data" / "yuanjian.db", backup_dir=root / "backups"
            )
            database.initialize()
            report = database.legacy_backfill_report

            # 1) 先备份：备份确实落了盘，路径写进审计
            backups = sorted((root / "backups").glob("yuanjian-*.db"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            try:
                backed_up_level = backup.execute(
                    "SELECT alert_level FROM personal_impacts WHERE impact_id='P-legacy'"
                ).fetchone()[0]
            finally:
                backup.close()
            # 备份是"迁移前"的快照 —— 里面还是旧档位
            self.assertEqual(backed_up_level, "L2")

            # 2) 回填报了真实的数
            self.assertEqual(report["before"]["counts"]["L2"], 1)
            self.assertEqual(report["after"]["counts"]["L4"], 1)
            self.assertEqual(report["level_changed"], 1)
            self.assertEqual(report["updated"], 1)
            # ③：远程研判缺的结构被本机规则引擎补上（该行确实是远程来源）
            self.assertEqual(report["structure_backfilled"], 1)

            with database.connect() as connection:
                row = connection.execute(
                    "SELECT impact_id,alert_level,components_json,impact_score,"
                    "interest_id,cluster_id,judgment_id,created_at,updated_at"
                    " FROM personal_impacts WHERE impact_id='P-legacy'"
                ).fetchone()
                versions = [
                    r[0]
                    for r in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                audit = connection.execute(
                    "SELECT details_json FROM audit_log WHERE action="
                    "'migration.legacy_alert_backfill'"
                ).fetchone()

            # 3) 身份列一字不动
            self.assertEqual(row["interest_id"], "I-default-health")
            self.assertEqual(row["cluster_id"], "C-1")
            self.assertEqual(row["judgment_id"], "J-1")
            self.assertEqual(row["created_at"], "2026-08-01T00:00:00Z")
            self.assertEqual(row["updated_at"], "2026-08-01T00:00:00Z")
            # ⚠ impact_score 本轮**按指令不更新**（只改两列）—— 如实断言现状，
            # 而不是假装它也跟着变了。
            self.assertAlmostEqual(row["impact_score"], 0.66)
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])
            # 4) 两列被换成当前口径，且标明结构来源
            self.assertEqual(row["alert_level"], "L4")
            components = json.loads(row["components_json"])
            self.assertEqual(components["structure_source"], "local_backfill")
            self.assertEqual(components["delay_risk"], "高")
            self.assertIn("structural_intensity", components)
            self.assertIn("importance_base", components)
            # 5) 审计里记下了备份文件名（可回滚的依据）
            self.assertIn("yuanjian-", audit["details_json"])

    def test_backfill_is_idempotent_and_creates_no_second_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._legacy_database(root)
            database = Database(
                root / "data" / "yuanjian.db", backup_dir=root / "backups"
            )
            database.initialize()
            with database.connect() as connection:
                first = connection.execute(
                    "SELECT alert_level,components_json FROM personal_impacts"
                    " WHERE impact_id='P-legacy'"
                ).fetchone()

            # 再跑一次：标记已在，整个回填不该再发生（也不该再备份）
            database.initialize()
            self.assertIsNone(database.legacy_backfill_report)
            self.assertEqual(len(list((root / "backups").glob("yuanjian-*.db"))), 1)
            with database.connect() as connection:
                second = connection.execute(
                    "SELECT alert_level,components_json FROM personal_impacts"
                    " WHERE impact_id='P-legacy'"
                ).fetchone()
            self.assertEqual(first["alert_level"], second["alert_level"])
            self.assertEqual(first["components_json"], second["components_json"])


if __name__ == "__main__":
    unittest.main()
