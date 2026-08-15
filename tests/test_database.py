import hashlib
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
            self.assertEqual(migrations, 5)

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
            self.assertEqual([row[0] for row in migrations], [1, 2, 3, 4, 5])
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
            self.assertEqual(versions, [1, 2, 3, 4, 5])
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
            self.assertEqual(migrations, [1, 2, 3, 4, 5])
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


if __name__ == "__main__":
    unittest.main()
