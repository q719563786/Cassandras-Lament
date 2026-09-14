"""数据保留：过期原始条目清理 + 结论明细分级清理 + 审计留痕。

原则（2026-09-14 修订）：

- 「结论」= 事件簇 `event_clusters` 与研判 `judgments`，**永远保留**。
  这不只是约定：`judgments` 上有 `judgments_no_delete` 触发器，数据库层
  直接拒绝删除——和不可变预测账本同等保护。
- 「结论明细」= 挂在事件簇下面的派生数据：个人利益影响、通知记录、研判任务、
  实体、簇成员。它们此前**永不删除**，是库体积无限增长的主因之一。
  现在按 `cluster_days` 清理（默认 180 天，比原始条目的 60 天宽），
  既保住近期结论明细，又让这部分有上界。
- **预测账本永不删除**：`forecasts`、`forecast_versions`、`resolutions`。
  校准与评分只读这三张表，因此清理结论明细**不会改变任何历史准确率统计**。
- **趋势快照永不删除**：`trend_snapshots` 是降采样后的聚合结果。
- 审计留痕 `audit_log` 同样不删（体积小、有追责价值）。
- 每次删除的实际行数写入 audit_log。

**已知上界**：由于 `judgments` 受不可变触发器保护，本模块只能收敛约 41% 的库体积；
`judgments`（实测 281MB，占全库约 31%，且占每日增量的约一半）仍会持续增长。
要解决它必须改动"判读不可变"这条产品承诺，属于产品决策，不由本模块擅自处理。

说明：本模块此前在文档里声称有"趋势降采样"，但代码中并未实现，
该表述已移除，避免文档与实现不符。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

DEFAULT_DAYS = 60
MIN_DAYS = 7
MAX_DAYS = 365

DEFAULT_CLUSTER_DAYS = 180
MIN_CLUSTER_DAYS = 30
MAX_CLUSTER_DAYS = 730

# 挂在事件簇下面的**可清理**派生明细。
# 不含 event_clusters 与 judgments —— 它们是结论，judgments 还受触发器保护。
# 不含 forecasts / forecast_versions / resolutions（不可变账本）、
# 不含 trend_snapshots（聚合趋势）、不含 interest_objects（用户登记的私人利益）。
CLUSTER_DETAIL_TABLES = (
    "personal_impacts",
    "notification_log",
    "judgment_jobs",
    "event_entities",
    "event_cluster_items",
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_retention_setting(database, *, default_days=DEFAULT_DAYS) -> dict:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key='settings.retention'"
        ).fetchone()
    payload = {}
    if row:
        try:
            loaded = json.loads(row["value_json"])
            if isinstance(loaded, dict):
                payload = loaded
        except (ValueError, TypeError):
            payload = {}
    days = payload.get("days", default_days)
    try:
        days = max(MIN_DAYS, min(int(days), MAX_DAYS))
    except (TypeError, ValueError):
        days = default_days
    cluster_days = payload.get("cluster_days", DEFAULT_CLUSTER_DAYS)
    try:
        cluster_days = max(MIN_CLUSTER_DAYS, min(int(cluster_days), MAX_CLUSTER_DAYS))
    except (TypeError, ValueError):
        cluster_days = DEFAULT_CLUSTER_DAYS
    return {
        "enabled": bool(payload.get("enabled", True)),
        "days": days,
        "cluster_days": cluster_days,
    }


def write_retention_setting(database, payload: dict, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    current = read_retention_setting(database)
    days = payload.get("days", current["days"])
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("保留天数无效")
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"保留天数需在 {MIN_DAYS}-{MAX_DAYS} 之间")
    cluster_days = payload.get("cluster_days", current["cluster_days"])
    try:
        cluster_days = int(cluster_days)
    except (TypeError, ValueError):
        raise ValueError("结论明细保留天数无效")
    if not MIN_CLUSTER_DAYS <= cluster_days <= MAX_CLUSTER_DAYS:
        raise ValueError(
            f"结论明细保留天数需在 {MIN_CLUSTER_DAYS}-{MAX_CLUSTER_DAYS} 之间"
        )
    updated = {
        "enabled": bool(payload.get("enabled", current["enabled"])),
        "days": days,
        "cluster_days": cluster_days,
    }
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO runtime_state(state_key, value_json, updated_at)
            VALUES ('settings.retention', ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (json.dumps(updated, ensure_ascii=False), _iso(now)),
        )
    return updated


class RetentionService:
    def __init__(self, database, *, now=lambda: datetime.now(timezone.utc)):
        self.database = database
        self.now = now

    def get_setting(self) -> dict:
        return read_retention_setting(self.database)

    def put_setting(self, payload: dict) -> dict:
        return write_retention_setting(self.database, payload, now=self.now())

    def run(self) -> dict:
        setting = read_retention_setting(self.database)
        if not setting["enabled"]:
            return {"status": "disabled", "deleted_items": 0, "expired_clusters": 0}
        now_utc = self.now().astimezone(timezone.utc)
        cutoff = _iso(now_utc - timedelta(days=setting["days"]))
        cluster_cutoff = _iso(now_utc - timedelta(days=setting["cluster_days"]))
        now_text = _iso(now_utc)
        deleted_items = 0
        expired_clusters = 0
        detail_counts = {}
        with self.database.connect() as connection:
            # 阶段一：可再生的原始抓取条目
            stale_ids = [
                row["item_id"]
                for row in connection.execute(
                    "SELECT item_id FROM external_items WHERE published_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if stale_ids:
                connection.executemany(
                    "DELETE FROM external_item_sources WHERE item_id=?",
                    [(item_id,) for item_id in stale_ids],
                )
                connection.executemany(
                    "DELETE FROM external_matches WHERE item_id=?",
                    [(item_id,) for item_id in stale_ids],
                )
                connection.executemany(
                    "DELETE FROM external_items WHERE item_id=?",
                    [(item_id,) for item_id in stale_ids],
                )
                deleted_items = len(stale_ids)

            # 阶段二：过期簇的派生明细（结论本身保留）
            expired_clusters, detail_counts = self._purge_expired_clusters(
                connection, cluster_cutoff
            )
            removed_detail = sum(detail_counts.values())

            # 只有真的删掉了东西才写审计，避免每天留下一条全零的记录
            if deleted_items or removed_detail:
                connection.execute(
                    "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        now_text,
                        "retention_cleanup",
                        "external_items",
                        cutoff,
                        json.dumps(
                            {
                                "deleted_items": deleted_items,
                                "days": setting["days"],
                                "expired_clusters": expired_clusters,
                                "cluster_days": setting["cluster_days"],
                                "deleted_detail": detail_counts,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        return {
            "status": "ok",
            "deleted_items": deleted_items,
            "expired_clusters": expired_clusters,
            "deleted_detail": detail_counts,
            "cutoff": cutoff,
            "cluster_cutoff": cluster_cutoff,
        }

    def _purge_expired_clusters(self, connection, cluster_cutoff):
        """清理过期簇的派生明细，返回（过期簇数量, 各表明细行数）。

        只删 `CLUSTER_DETAIL_TABLES` 里的派生数据；事件簇本身与研判是结论，
        一律保留（judgments 另有触发器保护，想删也删不掉）。

        表名来自模块常量，不是外部输入，所以这里用 f-string 拼表名是安全的
        （SQLite 不支持把表名参数化）。先统计再删除，统计进 audit_log，
        便于事后核对"到底删了什么"。
        """
        expired = connection.execute(
            "SELECT COUNT(*) FROM event_clusters WHERE last_seen_at < ?",
            (cluster_cutoff,),
        ).fetchone()[0]
        if not expired:
            return 0, {}
        scope = "cluster_id IN (SELECT cluster_id FROM event_clusters WHERE last_seen_at < ?)"
        counts = {}
        for table in CLUSTER_DETAIL_TABLES:
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {scope}", (cluster_cutoff,)
            ).fetchone()[0]
        for table in CLUSTER_DETAIL_TABLES:
            if counts[table]:
                connection.execute(f"DELETE FROM {table} WHERE {scope}", (cluster_cutoff,))
        return expired, counts
