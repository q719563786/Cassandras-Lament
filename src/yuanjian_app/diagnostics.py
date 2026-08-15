"""诊断面板数据聚合：六个瓦片的扁平字段，直接匹配前端 diag.js 契约。

契约（前端字段访问，缺一个就显示"未知"）：
sources_enabled / sources_total / ai_enabled / ai_jobs_today /
db_bytes / last_backup / backup_enabled / last_run_ms / runtime
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


class DiagnosticsService:
    def __init__(
        self,
        database,
        *,
        external=None,
        ai_settings=None,
        judgment_queue=None,
        backup_service=None,
    ):
        self.database = database
        self.external = external
        self.ai_settings = ai_settings
        self.judgment_queue = judgment_queue
        self.backup_service = backup_service

    def snapshot(self) -> dict:
        payload = {
            "sources_enabled": 0,
            "sources_total": 0,
            "ai_enabled": False,
            "ai_jobs_today": 0,
            "db_bytes": 0,
            "last_backup": None,
            "backup_enabled": False,
            "last_run_ms": 0,
            "runtime": "本机 127.0.0.1",
        }
        if self.external is not None:
            try:
                sources = self.external.list_sources()
                payload["sources_total"] = len(sources)
                payload["sources_enabled"] = sum(
                    1 for item in sources if item.get("enabled", True)
                )
            except Exception:
                pass
        if self.ai_settings is not None:
            try:
                settings = self.ai_settings.get()
                payload["ai_enabled"] = bool(settings.get("enabled", False))
            except Exception:
                payload["ai_enabled"] = False
        if self.judgment_queue is not None:
            try:
                payload["ai_jobs_today"] = int(self.judgment_queue.remote_used_today())
            except Exception:
                payload["ai_jobs_today"] = 0
        try:
            payload["db_bytes"] = int(self.database.path.stat().st_size)
        except OSError:
            payload["db_bytes"] = 0
        if self.backup_service is not None:
            try:
                latest = self.backup_service.latest()
                payload["last_backup"] = latest["created_at"] if latest else None
            except Exception:
                payload["last_backup"] = None
        try:
            payload["backup_enabled"] = bool(self._read_backup_enabled())
        except Exception:
            payload["backup_enabled"] = False
        try:
            payload["last_run_ms"] = int(self._read_last_run_ms())
        except Exception:
            payload["last_run_ms"] = 0
        return payload

    def _read_backup_enabled(self) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key='settings.backup'"
            ).fetchone()
        if not row:
            return False
        try:
            return bool(json.loads(row["value_json"]).get("enabled", False))
        except (ValueError, TypeError, AttributeError):
            return False

    def _read_last_run_ms(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key='task.cognition'"
            ).fetchone()
        if not row:
            return 0
        try:
            payload = json.loads(row["value_json"])
            started = datetime.fromisoformat(
                str(payload.get("started_at", "")).replace("Z", "+00:00")
            )
            finished = datetime.fromisoformat(
                str(payload.get("finished_at", "")).replace("Z", "+00:00")
            )
            return max(0, int((finished - started).total_seconds() * 1000))
        except (ValueError, TypeError):
            return 0
