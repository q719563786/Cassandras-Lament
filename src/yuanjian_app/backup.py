"""每日自动备份：SQLite backup API 在线快照 + 滚动保留 7 份。

设计要点：
- 使用 sqlite3 backup API 而非文件复制，避免抓到写一半的 WAL 页。
- 先写临时文件再 os.replace 原子落盘，失败不产生半份备份。
- 滚动保留最近 7 份，旧的在成功落盘之后才删除。
- 备份目录位于数据根 backups/ 下，永不进入 git 或导出包。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

KEEP_COUNT = 7

#: 只有 `run()` 产出的日期备份才参与保留计数与 `latest()`。
#:
#: **不要再用 `glob("yuanjian-*.db")` 这种宽 glob。** 备份目录里还存在升级前的
#: 手工快照 `yuanjian-pre-v0.4-*` / `yuanjian-pre-v0.5-*`，它们也匹配那个通配符，
#: 而 `'p'(0x70) > '2'(0x32)` —— 按文件名倒序时这些 `pre-*` **全部排在日期备份
#: 之前**，于是长期占住 `KEEP_COUNT=7` 里的名额，把真正的日期备份挤出保留窗口；
#: `latest()` 也因此会返回一个 1.3 MB 的空壳，而不是最新那份 922 MB 的真备份。
#:
#: `pre-*` 是用户的数据，**永不删除**，只是从此不参与保留计数与 `latest()`。
BACKUP_NAME_PATTERN = re.compile(r"^yuanjian-\d{8}T\d{6}Z\.db$")


def _dated_backups(backup_dir: Path) -> list[Path]:
    """列出日期备份，按文件名倒序（时间戳零填充，字典序即时间序）。

    `_rotate()` 与 `latest()` **共用本函数**，避免两处筛选口径再次漂移——
    历史上它们就是各自复制了一份宽 glob，才一起跑偏的。
    """
    return sorted(
        (
            item
            for item in backup_dir.glob("yuanjian-*.db")
            if item.is_file() and BACKUP_NAME_PATTERN.match(item.name)
        ),
        key=lambda item: item.name,
        reverse=True,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class BackupService:
    def __init__(self, database, backup_dir, *, now=lambda: datetime.now(timezone.utc)):
        self.database = database
        self.backup_dir = Path(backup_dir)
        self.now = now

    def _connect(self):
        return sqlite3.connect(self.database.path)

    def run(self) -> dict:
        """产出一份新备份并滚动清理，返回本次备份信息。"""
        current = self.now()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.backup_dir / f"yuanjian-{stamp}.db"
        temporary = target.with_suffix(".tmp")
        source = self._connect()
        try:
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination)
                check = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise RuntimeError("备份完整性检查失败")
            finally:
                destination.close()
        finally:
            source.close()
        if not target.exists():
            os.replace(temporary, target)
        else:
            # 同一秒内重复触发：保留旧份，清掉临时文件即可。
            temporary.unlink(missing_ok=True)
        self._rotate()
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "created_at": _iso(current),
        }

    def _rotate(self):
        backups = _dated_backups(self.backup_dir)
        for stale in backups[KEEP_COUNT:]:
            stale.unlink(missing_ok=True)

    def latest(self) -> dict | None:
        """最近一次成功备份的信息（诊断面板用），没有则 None。

        只认日期备份：`pre-*` 升级前快照不参与，否则这里会返回一个空壳
        （实测 1.3 MB 的 8/11 文件排在 922 MB 的真备份前面）。
        """
        backups = _dated_backups(self.backup_dir)
        if not backups:
            return None
        newest = backups[0]
        stat = newest.stat()
        return {
            "path": str(newest),
            "bytes": stat.st_size,
            "created_at": _iso(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
        }

    def get_setting(self) -> dict:
        return read_backup_setting(self.database)

    def put_setting(self, payload: dict) -> dict:
        return write_backup_setting(self.database, payload, now=self.now())


def read_backup_setting(database, *, default_hour=3) -> dict:
    """读取备份设置（runtime_state.settings.backup）。"""
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key='settings.backup'"
        ).fetchone()
    payload = {}
    if row:
        try:
            loaded = json.loads(row["value_json"])
            if isinstance(loaded, dict):
                payload = loaded
        except (ValueError, TypeError):
            payload = {}
    hour = payload.get("hour", default_hour)
    try:
        hour = max(0, min(int(hour), 23))
    except (TypeError, ValueError):
        hour = default_hour
    return {
        "enabled": bool(payload.get("enabled", False)),
        "hour": hour,
        "keep": int(payload.get("keep", KEEP_COUNT)),
    }


def write_backup_setting(database, payload: dict, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    current = read_backup_setting(database)
    hour = payload.get("hour", current["hour"])
    try:
        hour = max(0, min(int(hour), 23))
    except (TypeError, ValueError):
        raise ValueError("备份目标时段无效（0-23）")
    updated = {
        "enabled": bool(payload.get("enabled", current["enabled"])),
        "hour": hour,
        "keep": current["keep"],
    }
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO runtime_state(state_key, value_json, updated_at)
            VALUES ('settings.backup', ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (json.dumps(updated, ensure_ascii=False), _iso(now)),
        )
    return updated
