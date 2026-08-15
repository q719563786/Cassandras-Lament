"""数据保留：过期原始条目清理 + 趋势降采样 + 审计留痕。

原则：只删"可再生的原始抓取条目"（external_items 及其关联），
预测账本、判读、事件簇等结论性数据一律不动；删除量写入 audit_log。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

DEFAULT_DAYS = 60
MIN_DAYS = 7
MAX_DAYS = 365


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
    return {"enabled": bool(payload.get("enabled", True)), "days": days}


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
    updated = {
        "enabled": bool(payload.get("enabled", current["enabled"])),
        "days": days,
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
            return {"status": "disabled", "deleted_items": 0}
        cutoff = _iso(self.now().astimezone(timezone.utc) - timedelta(days=setting["days"]))
        now_text = _iso(self.now().astimezone(timezone.utc))
        with self.database.connect() as connection:
            stale_ids = [
                row["item_id"]
                for row in connection.execute(
                    "SELECT item_id FROM external_items WHERE published_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if not stale_ids:
                return {"status": "ok", "deleted_items": 0, "cutoff": cutoff}
            connection.executemany(
                "DELETE FROM external_item_sources WHERE item_id=?",
                [(item_id,) for item_id in stale_ids],
            )
            connection.executemany(
                "DELETE FROM external_matches WHERE item_id=?",
                [(item_id,) for item_id in stale_ids],
            )
            # 簇成员表保留引用行会指向已删条目，簇本身是结论，保留。
            connection.executemany(
                "DELETE FROM external_items WHERE item_id=?",
                [(item_id,) for item_id in stale_ids],
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                (
                    now_text,
                    "retention_cleanup",
                    "external_items",
                    cutoff,
                    json.dumps({"deleted": len(stale_ids), "days": setting["days"]}),
                ),
            )
        return {
            "status": "ok",
            "deleted_items": len(stale_ids),
            "cutoff": cutoff,
        }
