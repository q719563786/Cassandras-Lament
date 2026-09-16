"""诊断面板数据聚合：六个瓦片的扁平字段，直接匹配前端 diag.js 契约。

契约（前端字段访问，缺一个就显示"未知"）：
sources_enabled / sources_total / ai_enabled / ai_jobs_today /
db_bytes / last_backup / backup_enabled / last_run_ms / runtime

另有三个"看得见节流"的补充字段（前端按可选处理，缺失不报错）：
ai_daily_budget / ai_min_interval_seconds / ai_rate_limit_pending
—— 只有「今日已用 X / 上限 Y」看不出"是不是发太快了"，用户要把上限调到极限时
没有一个反馈信号。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .remote_ai import REMOTE_MIN_INTERVAL_SECONDS

_logger = logging.getLogger(__name__)


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
            "ai_daily_budget": 0,
            "ai_min_interval_seconds": float(REMOTE_MIN_INTERVAL_SECONDS),
            "ai_rate_limit_pending": 0,
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
                _logger.warning("诊断面板：读取信源列表失败", exc_info=True)
        if self.ai_settings is not None:
            try:
                settings = self.ai_settings.get()
                payload["ai_enabled"] = bool(settings.get("enabled", False))
                # 今日用量必须连着上限一起给，否则"今日 37 次"看不出离天花板还有多远。
                payload["ai_daily_budget"] = int(settings.get("daily_budget", 0))
            except Exception:
                payload["ai_enabled"] = False
                payload["ai_daily_budget"] = 0
        if self.judgment_queue is not None:
            try:
                payload["ai_jobs_today"] = int(self.judgment_queue.remote_used_today())
            except Exception:
                payload["ai_jobs_today"] = 0
        try:
            payload["ai_rate_limit_pending"] = int(self._read_rate_limit_pending())
        except Exception:
            payload["ai_rate_limit_pending"] = 0
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

    def _read_rate_limit_pending(self) -> int:
        """当前有多少远程作业正卡在"限流退避"里（429 专用）。

        定义刻意取**瞬时值**而不是"今日被限流几次"：`judgment_jobs` 只保留最近一次
        失败原因，没有逐次尝试的历史，按天累计只能靠估算。而"现在有几个作业正因为
        429 在等"恰好回答了用户真正要问的那句——**是不是我调太快了**：这个数持续
        大于 0，就说明 12 次/分钟的节流上限仍然高于对端实际能吃的速率。
        """
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM judgment_jobs"
                    " WHERE provider!='local' AND status='retry' AND last_error='rate_limit'"
                ).fetchone()[0]
            )

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
