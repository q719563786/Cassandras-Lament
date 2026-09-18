"""系统设置读写：反馈学习开关（backup/retention 设置在各自模块内）。

所有设置存 runtime_state，键名 settings.*；前端 PUT 后立即生效，
不依赖重启。learning.enabled=false 时调度器跳过误报回灌。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_key(database, key: str) -> dict:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key=?",
            (key,),
        ).fetchone()
    if not row:
        return {}
    try:
        loaded = json.loads(row["value_json"])
        return loaded if isinstance(loaded, dict) else {}
    except (ValueError, TypeError):
        return {}


def _write_key(database, key: str, payload: dict, *, now=None) -> None:
    now = now or datetime.now(timezone.utc)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO runtime_state(state_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (key, json.dumps(payload, ensure_ascii=False), _iso(now)),
        )


def read_learning_setting(database) -> dict:
    payload = _read_key(database, "settings.learning")
    return {"enabled": bool(payload.get("enabled", True))}


def write_learning_setting(database, payload: dict, *, now=None) -> dict:
    if "enabled" not in payload:
        raise ValueError("缺少 enabled 字段")
    updated = {"enabled": bool(payload.get("enabled"))}
    _write_key(database, "settings.learning", updated, now=now)
    return updated


class SystemSettingsService:
    """HTTP 边界的统一门面：GET/PUT 设置端点都走这里。"""

    def __init__(self, database):
        self.database = database

    def get_learning(self) -> dict:
        return read_learning_setting(self.database)

    def put_learning(self, payload: dict) -> dict:
        return write_learning_setting(self.database, payload)

    def get_forecast_archive(self) -> dict:
        return read_forecast_archive_setting(self.database)

    def put_forecast_archive(self, payload: dict) -> dict:
        return write_forecast_archive_setting(self.database, payload)


#: 到期不结算的自动归档规则：**默认关闭**。
#:
#: 为什么必须是"默认关闭"而不是"默认开启、可关掉"：账本不可变，批量写进去的
#: `indeterminate` 删不掉也改不了。系统在用户没表态的情况下替他把成百上千条命题
#: 一次性判定为"无法判定"——那是一次不可撤销的写入，不能由默认值代劳。
#: 用户明确打开之后，才走这条路径。
FORECAST_ARCHIVE_DEFAULT_DAYS = 30
FORECAST_ARCHIVE_MIN_DAYS = 7
FORECAST_ARCHIVE_MAX_DAYS = 180


def read_forecast_archive_setting(database) -> dict:
    payload = _read_key(database, "settings.forecast_archive")
    try:
        days = int(payload.get("days", FORECAST_ARCHIVE_DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = FORECAST_ARCHIVE_DEFAULT_DAYS
    return {
        "enabled": bool(payload.get("enabled", False)),
        "days": max(FORECAST_ARCHIVE_MIN_DAYS, min(days, FORECAST_ARCHIVE_MAX_DAYS)),
    }


def write_forecast_archive_setting(database, payload: dict, *, now=None) -> dict:
    if "enabled" not in payload:
        raise ValueError("缺少 enabled 字段")
    raw = payload.get("days", FORECAST_ARCHIVE_DEFAULT_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise ValueError("天数必须是整数") from None
    if not (FORECAST_ARCHIVE_MIN_DAYS <= days <= FORECAST_ARCHIVE_MAX_DAYS):
        raise ValueError(
            f"天数必须在 {FORECAST_ARCHIVE_MIN_DAYS}–{FORECAST_ARCHIVE_MAX_DAYS} 之间"
        )
    updated = {"enabled": bool(payload.get("enabled")), "days": days}
    _write_key(database, "settings.forecast_archive", updated, now=now)
    return updated
