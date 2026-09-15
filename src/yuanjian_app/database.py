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
                -- 下面五条索引来自一次真实数据库上的执行计划审查。此前这些查询
                -- 全部走全表扫描，在 9~11 万行的表上单次耗时 26~110 毫秒，而它们
                -- 都在热路径上（每条外部条目、每次通知去重、每天的预算统计都要跑）。
                -- 实测加索引后：通知去重 55.7ms -> 0.01ms，条目归属簇 26.5ms -> 0.01ms，
                -- 按来源查条目 110.4ms -> 0.23ms，预算计数 36.8ms -> 0.23ms。
                -- 五个索引合计约 14MB（库本身近 900MB），建索引一次性约 4 秒。
                CREATE INDEX IF NOT EXISTS idx_external_items_source
                    ON external_items(source_id);
                CREATE INDEX IF NOT EXISTS idx_external_items_published
                    ON external_items(published_at);
                CREATE INDEX IF NOT EXISTS idx_notification_log_cluster_created
                    ON notification_log(cluster_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_judgment_jobs_finished
                    ON judgment_jobs(finished_at);
                CREATE INDEX IF NOT EXISTS idx_event_cluster_items_item
                    ON event_cluster_items(item_id);
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
                -- resolutions 存的是预测的**结算结果**（outcome）与 Brier 打分
                -- （brier_score），是「预测账本不可变」里最该被保护的一块：一旦
                -- 可改可删，事后就能悄悄篡改自己的命中率与校准分。此前它连
                -- UPDATE/DELETE 触发器都没有，等于账本只锁了一半。这里补齐与
                -- judgments 同级的 no_update / no_delete 防护（写入仍允许，
                -- 见 forecasts.py 的 resolve()）。
                CREATE TRIGGER IF NOT EXISTS resolutions_no_update
                BEFORE UPDATE ON resolutions BEGIN
                    SELECT RAISE(ABORT, 'resolutions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS resolutions_no_delete
                BEFORE DELETE ON resolutions BEGIN
                    SELECT RAISE(ABORT, 'resolutions are immutable');
                END;
                -- forecasts 是**有意豁免**，不是漏写：它的 status 必须能从
                -- 'open' 流转到 'resolved' / 'void'，而这一流转只能靠对
                -- forecasts 做 UPDATE 完成（forecasts.py 的 resolve() 在写完
                -- resolutions 之后就 `UPDATE forecasts SET status='resolved'`）。
                -- 若在这里加 no_update，整条结算流程会被数据库直接拒绝。
                -- 不可变性由两张内容表承担：forecast_versions（预测内容）与
                -- resolutions（结算结果）；forecasts 本身只是"这条预测当前什么
                -- 状态"的可变索引，不承载不可改的账本内容，所以它没有触发器。
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
        # recursive_triggers 默认是 OFF。OFF 时 `INSERT OR REPLACE` 的冲突消解
        # 内部虽然会删掉旧行，但**不会触发该表的 BEFORE DELETE 触发器**——于是
        # 只要写法从 UPDATE / DELETE 换成 `INSERT OR REPLACE`（撞 PK 或撞 UNIQUE），
        # 就能静默改写不可变表的行，甚至借 UNIQUE 冲突把原行整行抹掉（等于一次
        # 未经拦截的 DELETE），直接绕过 judgments_no_delete /
        # forecast_versions_no_delete 这两个「判读不可变 / 预测账本不可变」的守卫。
        # 打开后 REPLACE 的删除也会触发 delete 触发器，承诺才真正由数据库强制。
        # 实测探针见 build-artifacts/t9b_immutability_probe.py。
        connection.execute("PRAGMA recursive_triggers=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
