"""诊断面板数据聚合：六个瓦片的扁平字段，直接匹配前端 diag.js 契约。

契约（前端字段访问，缺一个就显示"未知"）：
sources_enabled / sources_total / ai_enabled / ai_jobs_today /
db_bytes / last_backup / backup_enabled / last_run_ms / runtime

另有三个"看得见节流"的补充字段（前端按可选处理，缺失不报错）：
ai_daily_budget / ai_min_interval_seconds / ai_rate_limit_pending
—— 只有「今日已用 X / 上限 Y」看不出"是不是发太快了"，用户要把上限调到极限时
没有一个反馈信号。

v1.4 再补两组"此前没人盯"的事实（前端同样按可选处理）：
trend_health（R-11：rising 占可判定快照的比例，>20% 即阈值失效）、
evidence_levels / primary_source_count / evidence_level_note
（R-15：证据等级分布，以及"尚未标记任何官方来源 → E3/E4 不可达"的明示）。

v1.5.1 再补一个"护栏可不可信"的事实（前端按可选处理）：
table_size_source —— 「单表 > 500 MB」护栏的逐表占用来源，dbstat=精确 / estimate=估算。
交付环境没有 dbstat，这条护栏实际是估算值；不写出来它又会变成静默护栏。

v1.5.2 再补"远程研判到底还活着吗"这一组（前端 diag.js 状态条早已写好，
此前因为后端没这四个键而恒为 undefined，于是「已暂停」「已回退本机」两态永不显示）：
ai_paused / ai_pause_reason / ai_fallback_local / ai_fallback_reason。
远程失效时应用会**静默降级到本机研判**，界面看起来一切正常 —— 这正是
"安静地坏掉"的典型，必须说出来。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from .remote_ai import REMOTE_MIN_INTERVAL_SECONDS, read_ai_setting

_logger = logging.getLogger(__name__)

#: 回退本机判定用的"本轮"窗口。优先取最近一次 cognition 轮的起点；库里没有
#: 轮记录时（刚装、还没跑过）退化成"最近这么多分钟"，避免显示好几天前的陈年回退。
REMOTE_FALLBACK_RECENT_MINUTES = 15

#: 暂停原因（**人话**，不暴露 `paused_auth` / `circuit_open` 这类内部状态名）。
#: 认证失败优先于熔断：前者要用户动作（重填密钥），后者只需要等对端恢复。
_PAUSE_REASON_AUTH = "API 密钥无效或已过期，请到「设置」重新填写"
_PAUSE_REASON_CIRCUIT = "连续多次调用失败，已暂停远程研判以免继续产生费用"
_PAUSE_REASON_UNKNOWN = "远程研判已暂停（原因未记录，可查看任务日志）"

#: 回退原因（`judgment_jobs.last_error` 是 `RemoteProviderError.kind`）→ 人话。
#: 前端会拼成「上次失败：<人话>」，所以这里必须是给用户看的词，不是 kind。
_FALLBACK_REASONS = {
    "network": "网络不通，连不上远程服务",
    "timeout": "连接超时",
    "http_error": "远程服务返回错误",
    "rate_limit": "被对端限流",
    "unsafe_endpoint": "端点地址不被允许外发",
    "auth": "密钥无效",
    "invalid_output": "返回内容无法解析",
}
_FALLBACK_REASON_UNKNOWN = "未知原因"


def _parse_iso(value):
    """解析库里的时间串（`…Z` 或带偏移），无法解析返回 None（读侧不抛）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class DiagnosticsService:
    def __init__(
        self,
        database,
        *,
        external=None,
        ai_settings=None,
        judgment_queue=None,
        backup_service=None,
        trends=None,
    ):
        self.database = database
        self.external = external
        self.ai_settings = ai_settings
        self.judgment_queue = judgment_queue
        self.backup_service = backup_service
        self.trends = trends

    def snapshot(self) -> dict:
        payload = {
            "sources_enabled": 0,
            "sources_total": 0,
            "ai_enabled": False,
            "ai_jobs_today": 0,
            "ai_daily_budget": 0,
            "ai_min_interval_seconds": float(REMOTE_MIN_INTERVAL_SECONDS),
            "ai_rate_limit_pending": 0,
            # v1.5.2：远程研判的"暂停 / 已回退本机"两态（默认不暂停、无回退）。
            "ai_paused": False,
            "ai_pause_reason": "",
            "ai_fallback_local": False,
            "ai_fallback_reason": "",
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
        else:
            # 没注入设置服务（嵌入式/测试）时直接读**同一个落点**（`read_ai_setting`
            # 就是设置页与队列共用的那一处）。留着它，才不会出现"诊断说没开、
            # 队列却在发请求"这种两个真相。
            try:
                payload["ai_enabled"] = bool(read_ai_setting(self.database)["enabled"])
            except Exception:
                payload["ai_enabled"] = False
        if self.judgment_queue is not None:
            try:
                payload["ai_jobs_today"] = int(self.judgment_queue.remote_used_today())
            except Exception:
                payload["ai_jobs_today"] = 0
        try:
            payload["ai_rate_limit_pending"] = int(self._read_rate_limit_pending())
        except Exception:
            payload["ai_rate_limit_pending"] = 0
        # v1.5.2：远程研判是不是已经"安静地坏掉了"。读失败时保持默认
        # （False / ""），不猜 —— 猜成"已暂停"比不说更糟。
        # 门控：这两个状态都只在**远程开着**时才有意义（`ai_enabled` 是同一份快照
        # 里的值，不是另读一次——两个真相就会自相矛盾）。
        try:
            payload.update(self._read_remote_health(payload["ai_enabled"]))
        except Exception:
            _logger.warning("诊断面板：读取远程研判暂停/回退状态失败", exc_info=True)
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
        # ---- v1.4 新增：两个"此前没人盯"的事实 ----
        # R-11：探测器健康度（rising 占可判定快照的比例）。一个多数时间在报警的
        # 探测器等价于没有报警，而此前界面上看不到这个比例。
        try:
            if self.trends is not None:
                payload["trend_health"] = self.trends.stored_health()
            else:
                payload["trend_health"] = None
        except Exception:
            _logger.warning("诊断面板：读取趋势健康度失败", exc_info=True)
            payload["trend_health"] = None
        # R-15：证据等级分布与"官方来源是否标记过"。实测 E3/E4 从未出现，
        # 因为 `primary_source` 从未在任一信息源上标记过 —— 四级体系实际只跑两级，
        # 最窄概率区间（E4 ±0.07）不可达。这是"用户不知道为什么按钮没反应"的典型：
        # 界面上必须说出来，而不是等他自己发现。
        try:
            payload["evidence_levels"] = self._read_evidence_levels()
        except Exception:
            payload["evidence_levels"] = {}
        try:
            payload["primary_source_count"] = self._read_primary_source_count()
        except Exception:
            payload["primary_source_count"] = 0
        payload["evidence_level_note"] = (
            ""
            if payload["primary_source_count"]
            else (
                "尚未标记任何官方来源：E3/E4 需要先在信息源里标记「官方来源」才会出现。"
                "当前证据体系实际只有 E1/E2 两级，最窄的概率区间（E4 ±0.07）不可达。"
            )
        )
        # 「单表 > 500 MB」护栏的占用来源。交付环境没有 dbstat（打包的
        # sqlite3.dll 未编译 SQLITE_ENABLE_DBSTAT_VTAB），该分支实际是估算值。
        # 一个不知道自己"是估算值"的护栏等于没有护栏，所以这里把它显式摊开：
        # dbstat=精确 / estimate=估算。
        try:
            payload["table_size_source"] = self._read_table_size_source()
        except Exception:
            payload["table_size_source"] = None
        return payload

    def _read_table_size_source(self):
        """`dbstat` 虚表在不在 —— 决定「单表 > 500 MB」护栏是精确还是估算。

        刻意做**能力探测**而不是"重跑一次估算取上次结果"：估算一次要 ≈3.7 s
        （真库 26 表），不能塞进一个按需打开的诊断接口；而"护栏可不可信"恰好只
        取决于 `dbstat` 在不在，探测只需 ~1 ms。

        返回 ``"dbstat"``（精确）/ ``"estimate"``（估算）/ ``None``（探测未得出结论，
        例如库被锁 —— 此时不下结论，而不是猜）。
        """
        with self.database.connect() as connection:
            try:
                connection.execute("SELECT 1 FROM dbstat LIMIT 1").fetchone()
            except sqlite3.Error as error:
                # 只有"没有 dbstat 这张虚表"才判为估算；锁/IO 等其它错误不下结论。
                if "dbstat" in str(error).lower():
                    return "estimate"
                return None
        return "dbstat"

    def _read_evidence_levels(self) -> dict:
        """已识别事件簇的证据等级分布。缺失的等级补 0（而不是不出现）——
        "E4 一条都没有"本身就是要说出来的事实。"""
        payload = {"E1": 0, "E2": 0, "E3": 0, "E4": 0}
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT evidence_level, COUNT(*) AS n FROM event_clusters"
                " GROUP BY evidence_level"
            ).fetchall()
        for row in rows:
            key = str(row["evidence_level"] or "E1")
            payload[key] = int(row["n"])
        return payload

    def _read_primary_source_count(self) -> int:
        """有多少个**启用中**的信息源被标记为官方来源（`config_json.primary_source`）。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT config_json FROM external_sources WHERE enabled=1"
            ).fetchall()
        count = 0
        for row in rows:
            try:
                config = json.loads(row["config_json"] or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(config, dict) and config.get("primary_source"):
                count += 1
        return count

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

    def _read_remote_health(self, enabled: bool) -> dict:
        """远程研判的两态：**暂停**（熔断中或鉴权暂停）与**本轮已回退本机**。

        `enabled` = 远程 AI 是否开着（同一份诊断快照里的 `ai_enabled`）。**关掉时
        一律不报故障**：用户是自己关的，报"已暂停（密钥失效）"会把他推去修一个
        他故意关掉的东西 —— 那是误导，不是提示。库里通常还留着关闭之前那批
        `paused_auth` 残留行，所以这个门控不是理论问题（见
        `tests/test_diagnostics.py` 里"已关闭且历史残留"那条）。

        只用既有状态，**不新增状态机**：

        - 暂停 = 存在 `status='paused_auth'` 的远程作业（鉴权失败批量冻结与
          连续失败熔断复用同一个状态，见 `remote_ai.JudgmentQueue._open_circuit`）。
          两种冻结靠 `last_error` 区分：`circuit_open`=熔断，
          `auth`/`auth_paused`=密钥失效。
        - 回退 = 最近一次 cognition 轮的窗口内出现过
          `status='remote_error_fallback_local'` 的作业（即重试耗尽后改用本机研判）。

        为什么回退要绑在"轮"上：不绑窗口的话，几天前那次一次性的连接超时会永远
        挂在界面上，用户以为现在还是坏的 —— 一个不会自愈的告警等于噪声。
        """
        if not enabled:
            # 关闭态：不说暂停、不说原因、也不说回退。前端的"未启用（默认关闭）"
            # 那一态才是此刻的真相。
            return {
                "ai_paused": False,
                "ai_pause_reason": "",
                "ai_fallback_local": False,
                "ai_fallback_reason": "",
            }
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            pauses = connection.execute(
                "SELECT last_error AS reason, COUNT(*) AS n FROM judgment_jobs"
                " WHERE provider!='local' AND status='paused_auth'"
                " GROUP BY last_error"
            ).fetchall()
            window_start = self._fallback_window_start(connection, now)
            fallback = connection.execute(
                "SELECT last_error AS reason, finished_at FROM judgment_jobs"
                " WHERE provider!='local' AND status='remote_error_fallback_local'"
                " ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()

        reasons = {str(row["reason"] or "") for row in pauses}
        paused = bool(reasons)
        if reasons & {"auth", "auth_paused"}:
            pause_reason = _PAUSE_REASON_AUTH
        elif "circuit_open" in reasons:
            pause_reason = _PAUSE_REASON_CIRCUIT
        else:
            pause_reason = _PAUSE_REASON_UNKNOWN

        # 时间比大小用**解析后的 datetime**，不走 SQL 的字符串比较：`_iso()`
        # 在整秒时会省略微秒（`…T00:00:00Z` vs `…T00:00:00.5Z`），字符串序会把
        # 同一秒内的先后判反 —— 而这里判错的方向正是"把刚发生的回退藏起来"。
        fallback_local = False
        fallback_reason = ""
        if fallback is not None:
            finished = _parse_iso(fallback["finished_at"])
            if finished is not None and finished >= window_start:
                fallback_local = True
                kind = str(fallback["reason"] or "")
                fallback_reason = _FALLBACK_REASONS.get(kind, _FALLBACK_REASON_UNKNOWN)
        return {
            "ai_paused": paused,
            "ai_pause_reason": pause_reason if paused else "",
            # 两个不变量：为真时原因必须非空（人话），为假时原因留空。
            "ai_fallback_local": fallback_local,
            "ai_fallback_reason": fallback_reason,
        }

    def _fallback_window_start(self, connection, now) -> datetime:
        """回退判定的窗口起点：最近一次 cognition 轮的起点，缺记录时退化成近期窗口。"""
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key='task.cognition'"
        ).fetchone()
        if row:
            try:
                started = _parse_iso(str(json.loads(row["value_json"]).get("started_at", "")))
            except (ValueError, TypeError, AttributeError):
                started = None
            if started is not None:
                return started
        return now - timedelta(minutes=REMOTE_FALLBACK_RECENT_MINUTES)

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
