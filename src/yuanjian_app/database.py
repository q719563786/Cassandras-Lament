import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportResult:
    forecasts: int
    versions: int


class Database:
    """Owns the private SQLite database and one-time legacy import."""

    def __init__(self, path):
        self.path = Path(path)

    def import_legacy(self, source):
        """Copy a legacy ledger once, verify it, then apply local migrations."""
        source = Path(source)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            temporary = self.path.with_suffix(".importing")
            shutil.copy2(source, temporary)
            connection = sqlite3.connect(temporary)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
            if integrity != "ok":
                temporary.unlink(missing_ok=True)
                raise RuntimeError("旧预测账本完整性检查失败")
            os.replace(temporary, self.path)
        self.initialize()
        with self.connect() as connection:
            forecasts = connection.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
            versions = connection.execute(
                "SELECT COUNT(*) FROM forecast_versions"
            ).fetchone()[0]
        return ImportResult(forecasts, versions)

    def initialize(self):
        """Create the current schema and immutable-version safeguards."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            # WAL keeps reads unblocked while the radar thread writes, which
            # matters once 18 preset sources refresh on first launch.
            connection.execute("PRAGMA journal_mode=WAL")
        with self.connect() as connection:
            # Column migrations MUST run before the executescript: old
            # databases that pre-date v5 already have the external_sources
            # and forecasts tables, so CREATE TABLE IF NOT EXISTS is a no-op
            # while CREATE INDEX ON external_sources(region) would fail with
            # "no such column: region" without these additions first.
            self._apply_column_migrations(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS forecasts(
                    forecast_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general'
                );
                CREATE TABLE IF NOT EXISTS forecast_versions(
                    forecast_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    probability REAL NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(forecast_id, version)
                );
                CREATE TABLE IF NOT EXISTS resolutions(
                    forecast_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    probability REAL NOT NULL,
                    brier_score REAL
                );
                CREATE TABLE IF NOT EXISTS schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interest_objects(
                    object_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    importance INTEGER NOT NULL,
                    privacy_level TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interest_links(
                    link_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relationship TEXT NOT NULL,
                    impact_direction TEXT NOT NULL,
                    strength INTEGER NOT NULL,
                    UNIQUE(source_id, target_id, relationship)
                );
                CREATE TABLE IF NOT EXISTS signals(
                    signal_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    domains_json TEXT NOT NULL,
                    reliability TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    status TEXT NOT NULL,
                    interest_ids_json TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    why_it_matters TEXT NOT NULL,
                    recommended_action TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_documents(
                    document_id TEXT PRIMARY KEY,
                    vault_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    modified_at TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    UNIQUE(vault_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS external_sources(
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
                    next_fetch_at TEXT,
                    region TEXT NOT NULL DEFAULT 'global',
                    category TEXT NOT NULL DEFAULT 'general',
                    user_managed INTEGER NOT NULL DEFAULT 0,
                    tier TEXT NOT NULL DEFAULT 'T3'
                );
                CREATE TABLE IF NOT EXISTS watch_rules(
                    rule_id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    domains_json TEXT NOT NULL DEFAULT '[]',
                    interest_ids_json TEXT NOT NULL DEFAULT '[]',
                    importance INTEGER NOT NULL DEFAULT 3,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_items(
                    item_id TEXT PRIMARY KEY,
                    canonical_url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    source_count INTEGER NOT NULL DEFAULT 1,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS external_item_sources(
                    item_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS external_matches(
                    item_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    score REAL NOT NULL,
                    reasons_json TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    PRIMARY KEY(item_id, rule_id)
                );
                CREATE TABLE IF NOT EXISTS external_runs(
                    run_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS event_clusters(
                    cluster_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    evidence_level TEXT NOT NULL DEFAULT 'E1',
                    evidence_hash TEXT NOT NULL,
                    categories_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    needs_judgment INTEGER NOT NULL DEFAULT 1,
                    independent_domains INTEGER NOT NULL DEFAULT 1,
                    primary_source_count INTEGER NOT NULL DEFAULT 0,
                    latest_judgment_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_cluster_items(
                    cluster_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    similarity REAL NOT NULL,
                    merge_reason TEXT NOT NULL,
                    source_domain TEXT NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(cluster_id, item_id)
                );
                CREATE TABLE IF NOT EXISTS event_entities(
                    entity_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    UNIQUE(cluster_id, normalized_name, category)
                );
                CREATE TABLE IF NOT EXISTS trend_snapshots(
                    snapshot_id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    window_hours INTEGER NOT NULL,
                    event_count INTEGER NOT NULL,
                    baseline_count REAL,
                    surge_ratio REAL,
                    status TEXT NOT NULL,
                    UNIQUE(captured_at, category, window_hours)
                );
                CREATE TABLE IF NOT EXISTS judgment_jobs(
                    job_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    request_chars INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    finished_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(cluster_id, evidence_hash, provider)
                );
                CREATE TABLE IF NOT EXISTS judgments(
                    judgment_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(cluster_id, provider, evidence_hash)
                );
                CREATE TABLE IF NOT EXISTS personal_impacts(
                    impact_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    judgment_id TEXT NOT NULL,
                    interest_id TEXT NOT NULL,
                    impact_score REAL NOT NULL,
                    alert_level TEXT NOT NULL,
                    components_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    candidate_json TEXT NOT NULL DEFAULT '{}',
                    muted_until TEXT,
                    importance_override INTEGER,
                    user_label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(cluster_id, judgment_id, interest_id)
                );
                CREATE TABLE IF NOT EXISTS notification_log(
                    notification_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    impact_id TEXT,
                    created_at TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delivery TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    read_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runtime_state(
                    state_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    cluster_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    interest_category TEXT NOT NULL DEFAULT '',
                    source_domains_json TEXT NOT NULL DEFAULT '[]',
                    applied_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_events_pending
                    ON feedback_events(applied_json, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_external_sources_region
                    ON external_sources(region);
                CREATE INDEX IF NOT EXISTS idx_event_clusters_needs_judgment
                    ON event_clusters(needs_judgment);
                CREATE INDEX IF NOT EXISTS idx_event_clusters_status_last_seen
                    ON event_clusters(status, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_event_clusters_first_seen
                    ON event_clusters(first_seen_at);
                CREATE INDEX IF NOT EXISTS idx_judgment_jobs_status_next_attempt
                    ON judgment_jobs(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_personal_impacts_alert_user_muted
                    ON personal_impacts(alert_level, user_label, muted_until);
                CREATE INDEX IF NOT EXISTS idx_notification_log_status_created
                    ON notification_log(status, created_at);
                CREATE TRIGGER IF NOT EXISTS forecast_versions_no_update
                BEFORE UPDATE ON forecast_versions BEGIN
                    SELECT RAISE(ABORT, 'forecast versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS forecast_versions_no_delete
                BEFORE DELETE ON forecast_versions BEGIN
                    SELECT RAISE(ABORT, 'forecast versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS judgments_no_update
                BEFORE UPDATE ON judgments BEGIN
                    SELECT RAISE(ABORT, 'judgments are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS judgments_no_delete
                BEFORE DELETE ON judgments BEGIN
                    SELECT RAISE(ABORT, 'judgments are immutable');
                END;
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (1, CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (2, CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (3, CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (4, CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (5, CURRENT_TIMESTAMP);
                """
            )
            self._apply_column_migrations(connection)

    @staticmethod
    def _apply_column_migrations(connection):
        """Idempotent column additions for databases created before v5.

        SQLite raises on ALTER TABLE ADD COLUMN when the column already
        exists, and initialize() runs on every startup, so each addition is
        guarded by a PRAGMA table_info check. PRAGMA table_info returns an
        empty result set for a missing table (instead of erroring), so we
        first verify the table exists in sqlite_master before attempting
        the migration — fresh databases get the columns via CREATE TABLE
        below and must not hit ALTER on a non-existent table.
        """
        additions = (
            ("forecasts", "category", "TEXT NOT NULL DEFAULT 'general'"),
            ("external_sources", "region", "TEXT NOT NULL DEFAULT 'global'"),
            ("external_sources", "category", "TEXT NOT NULL DEFAULT 'general'"),
            ("external_sources", "user_managed", "INTEGER NOT NULL DEFAULT 0"),
            ("external_sources", "tier", "TEXT NOT NULL DEFAULT 'T3'"),
        )
        existing_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, column, definition in additions:
            if table not in existing_tables:
                continue
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            names = {row[1] for row in rows}
            if column not in names:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    @contextmanager
    def connect(self):
        """Yield a transaction and always close the Windows file handle."""
        connection = sqlite3.connect(self.path, timeout=20.0)
        connection.row_factory = sqlite3.Row
        # 后台写库突发时，界面写操作最多等20秒拿锁而不是10秒后抛 database is locked；
        # WAL 模式下 synchronous=NORMAL 是安全的且提交更快，能显著缩短持锁窗口。
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
