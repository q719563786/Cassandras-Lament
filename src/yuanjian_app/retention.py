"""数据保留：分层清理过期原始条目、结论明细、作业流水、采集运行、趋势快照。

原则（2026-09-14 修订）：

- 「结论」= 事件簇 `event_clusters` 与研判 `judgments`，**永远保留**。
  这不只是约定：`judgments` 上有 `judgments_no_delete` 触发器，数据库层
  直接拒绝删除——和不可变预测账本同等保护。
- 「结论明细」= 挂在事件簇下面的派生数据：个人利益影响、通知记录、研判任务、
  实体、簇成员。它们此前**永不删除**，是库体积无限增长的主因之一。
  现在按 `cluster_days` 清理（默认 180 天，比原始条目的 60 天宽）。
  例外：`personal_impacts` 与 `notification_log` 里混着**用户自撰状态**
  （用户标注 `user_label`、`muted_until`、`importance_override`，通知的已读
  标记 `read_at`），且被学习回路当输入，所以只清"无任何用户标注"的行——
  见 `CLUSTER_DETAIL_FILTERS`。抹掉它们等于抹掉用户教给系统的判断。
- 「作业流水」`judgment_jobs`（C 层）：作业一旦成功产出研判，这条流水就完全冗余
  （结果已在 `judgments` 里）。因此清掉「`succeeded` 且对应研判已存在」中
  `created_at` 早于 `job_days`（默认 7 天）的行；另设绝对上限
  `JOB_ABSOLUTE_MAX_DAYS`（180 天），任何状态的僵死作业到期即清。
- 「采集运行」`external_runs`（D 层）：按 `run_days`（默认 90 天）清理。
  **这是未来保护，不是当下回收**：截至 2026-09-14 最老记录是 2026-08-06，
  尚未满 90 天，当下可删 0 行 / 0 MB。
- 「全球态势事件」`situation_events`（F 层，2026-09-20 新增）：按固定
  `SITUATION_KEEP_DAYS`（30 天）清理。判据与 A 层同构 —— 优先用事件自身的
  `occurred_at`，但只在它是 ISO 形状时才用，否则退化用 `last_seen_at`。
  这批数据可从上游（USGS/EONET/GDACS）再生，不是结论，可以清。
- 「趋势快照」`trend_snapshots`（E 层）：按窗口降采样，`SNAPSHOT_KEEP_DAYS`
  给出每个窗口的保留天数；`window_hours=720` **永久保留**（受硬不变量保护）。
  不在 `SNAPSHOT_KEEP_DAYS` 里、也不在保护名单里的窗口一律不动。
- **预测账本永不删除**：`forecasts`、`forecast_versions`、`resolutions`。
- 审计留痕 `audit_log` 不删（体积小、有追责价值），且**只增不减**。
- **审计与节流时钟是两件事，不要混用**：审计只增不减、且只在真的删掉了东西
  时才写一条（避免每天留下全零记录）；阈值触发的 6 小时节流时钟改存
  `runtime_state` 的专用键（`THROTTLE_STATE_KEY`），**每次真正执行过的尝试都
  推进**。若拿"只写于真删时"的审计当时钟，空跑就不会推进时钟，节流形同虚设，
  会每个检查周期重跑一遍全量清理。

**不自动 VACUUM**：删除只把页标记为空闲，物理文件不会缩小
（真实库 `PRAGMA auto_vacuum=0`），因此库级 `db_bytes_before` 与
`db_bytes_after` 正常情况下相等。回收物理空间是用户的显式操作，不由本模块擅自触发。
也正因如此，本模块返回的所有计数都是**逻辑删除行数**，不等于磁盘占用下降量。

**已知天花板（如实记录，勿轻信「能收敛体积」）**：`judgments` 受不可变触发器
保护、不可删除，实测约 281 MB（占全库约 31%，且约占每日增量的一半）。
本模块一次全量清理的净回收是**几十 MB 量级**，而全库日增仍约 26 MB——
清理只能减缓、不能逆转体积增长。要真正收敛必须改动「判读不可变」这条产品承诺，
属于产品决策，不由本模块擅自处理。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

DEFAULT_DAYS = 60
MIN_DAYS = 7
MAX_DAYS = 365

DEFAULT_CLUSTER_DAYS = 180
MIN_CLUSTER_DAYS = 30
MAX_CLUSTER_DAYS = 730

# C 层：作业流水
DEFAULT_JOB_DAYS = 7
MIN_JOB_DAYS = 1
MAX_JOB_DAYS = 180
# 第二条规则：任何状态的作业，创建超过这个天数即清（兜住僵死的 pending/failed）
JOB_ABSOLUTE_MAX_DAYS = 180

# D 层：采集运行
DEFAULT_RUN_DAYS = 90
MIN_RUN_DAYS = 7
MAX_RUN_DAYS = 730

# F 层：全球态势事件（v8 新表 `situation_events`）。
# 与 A 层同属"可从上游再生的原始数据"，所以给一个**固定**窗口而不是用户设置：
# 地图只有 24h/72h/7d 三个展示窗口，30 天足够覆盖且不至于让表无限增长。
# 这里刻意不暴露成 `settings.retention` 的键 —— 团队 2026-09-20 拍板就是「加 30 天」，
# 多一个用户旋钮只会多一处口径不一致。
SITUATION_KEEP_DAYS = 30

# E 层：窗口小时 -> 保留天数。不在此表、也不在保护名单里的窗口一律不动。
SNAPSHOT_KEEP_DAYS = {6: 7, 24: 60, 168: 365}
PROTECTED_SNAPSHOT_WINDOWS = (720,)

# 阈值触发
DEFAULT_THRESHOLD_MB = 2048
DEFAULT_TABLE_THRESHOLD_MB = 500
DEFAULT_MIN_INTERVAL_HOURS = 6

# F3：单簇研判条数上限
DEFAULT_MAX_JUDGMENTS_PER_CLUSTER = 8

# 上面三个「新键」在契约里只给了默认值，未给区间；§3 要求 clamp / 越界报错，
# 故此处补齐边界。取值取「宽到够用、窄到挡住无意义输入」。
MIN_MAX_JUDGMENTS_PER_CLUSTER = 1
MAX_MAX_JUDGMENTS_PER_CLUSTER = 100
MIN_THRESHOLD_MB = 1
MAX_THRESHOLD_MB = 1_000_000
MIN_MIN_INTERVAL_HOURS = 0
MAX_MIN_INTERVAL_HOURS = 168

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

# 逐表追加的"不可清理"条件（簇过期也不删）。多数明细表是纯派生物，簇过期即可清；
# 但下面两张表里**混着用户亲手写下的状态**，删掉就等于抹掉用户的判断，必须保命：
#
# - `personal_impacts`：`user_label` 是用户在界面上标的 `dismissed` /
#   `false_positive`，`muted_until` 是"免打扰到某时刻"，`importance_override`
#   是用户手动调过的重要性。这三者都还被**学习回路当输入**
#   （cognition.py / impacts.py 都会按 `user_label` 过滤）——删掉它们不只是
#   丢展示，而是让系统"忘掉"用户教过的判断。
# - `notification_log`：`read_at` 是用户的已读标记，非空表示这条通知用户看过、
#   处理过；删了就抹掉了"读过"这个事实。
#
# 所以只删"没有任何用户标注"的行（user_label 为空、没静音、没改过重要性；
# 通知没被读过）。其余表沿用原判据，行为不变。
CLUSTER_DETAIL_FILTERS = {
    "personal_impacts": (
        "COALESCE(user_label, '') = ''"
        " AND muted_until IS NULL"
        " AND importance_override IS NULL"
    ),
    "notification_log": "read_at IS NULL",
}

# 阈值节流的时钟：存"上一次真正执行过清理尝试"的时间。
# 与 `settings.retention`（用户设置）分开，语义不同、读写时机也不同。
THROTTLE_STATE_KEY = "retention.last_attempt_at"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_int(value, default, low, high):
    """尽力转 int，失败或越界都回退到边界/默认值，永不抛异常。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


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
        # 以下为 2026-09-14 新增键，只增不改，旧调用方不受影响。
        "job_days": _clamp_int(
            payload.get("job_days", DEFAULT_JOB_DAYS),
            DEFAULT_JOB_DAYS,
            MIN_JOB_DAYS,
            MAX_JOB_DAYS,
        ),
        "run_days": _clamp_int(
            payload.get("run_days", DEFAULT_RUN_DAYS),
            DEFAULT_RUN_DAYS,
            MIN_RUN_DAYS,
            MAX_RUN_DAYS,
        ),
        "max_judgments_per_cluster": _clamp_int(
            payload.get("max_judgments_per_cluster", DEFAULT_MAX_JUDGMENTS_PER_CLUSTER),
            DEFAULT_MAX_JUDGMENTS_PER_CLUSTER,
            MIN_MAX_JUDGMENTS_PER_CLUSTER,
            MAX_MAX_JUDGMENTS_PER_CLUSTER,
        ),
        "threshold_mb": _clamp_int(
            payload.get("threshold_mb", DEFAULT_THRESHOLD_MB),
            DEFAULT_THRESHOLD_MB,
            MIN_THRESHOLD_MB,
            MAX_THRESHOLD_MB,
        ),
        "min_interval_hours": _clamp_int(
            payload.get("min_interval_hours", DEFAULT_MIN_INTERVAL_HOURS),
            DEFAULT_MIN_INTERVAL_HOURS,
            MIN_MIN_INTERVAL_HOURS,
            MAX_MIN_INTERVAL_HOURS,
        ),
    }


def _require_int(payload, key, current_value, low, high, message):
    """取值 -> int -> 校验区间；越界抛 ValueError（与既有 days 行为一致）。"""
    raw = payload.get(key, current_value)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(message)
    if not low <= value <= high:
        raise ValueError(f"{message}（需在 {low}-{high} 之间）")
    return value


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
    job_days = _require_int(
        payload,
        "job_days",
        current["job_days"],
        MIN_JOB_DAYS,
        MAX_JOB_DAYS,
        "作业流水保留天数无效",
    )
    run_days = _require_int(
        payload,
        "run_days",
        current["run_days"],
        MIN_RUN_DAYS,
        MAX_RUN_DAYS,
        "采集运行保留天数无效",
    )
    max_judgments_per_cluster = _require_int(
        payload,
        "max_judgments_per_cluster",
        current["max_judgments_per_cluster"],
        MIN_MAX_JUDGMENTS_PER_CLUSTER,
        MAX_MAX_JUDGMENTS_PER_CLUSTER,
        "单簇研判条数上限无效",
    )
    threshold_mb = _require_int(
        payload,
        "threshold_mb",
        current["threshold_mb"],
        MIN_THRESHOLD_MB,
        MAX_THRESHOLD_MB,
        "清理阈值无效",
    )
    min_interval_hours = _require_int(
        payload,
        "min_interval_hours",
        current["min_interval_hours"],
        MIN_MIN_INTERVAL_HOURS,
        MAX_MIN_INTERVAL_HOURS,
        "最小清理间隔无效",
    )
    updated = {
        "enabled": bool(payload.get("enabled", current["enabled"])),
        "days": days,
        "cluster_days": cluster_days,
        "job_days": job_days,
        "run_days": run_days,
        "max_judgments_per_cluster": max_judgments_per_cluster,
        "threshold_mb": threshold_mb,
        "min_interval_hours": min_interval_hours,
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

    # -- 只读检查 ---------------------------------------------------------

    def _database_bytes(self) -> int:
        """库文件的物理字节数；取不到返回 0。"""
        try:
            return int(self.database.path.stat().st_size)
        except (OSError, AttributeError, TypeError):
            return 0

    @staticmethod
    def _freelist_count(connection) -> int:
        """在既有连接上读空闲页数；取不到返回 0。

        `PRAGMA auto_vacuum=0` 且本机制不执行 VACUUM，所以删掉的行不会让物理
        文件变小，只会变成可复用的空闲页。**空闲页增量才是清理真正生效的唯一
        可见证据**，`db_bytes_before == db_bytes_after` 是预期行为而不是 bug。
        """
        try:
            row = connection.execute("PRAGMA freelist_count").fetchone()
        except sqlite3.Error:
            return 0
        if row is None:
            return 0
        return int(row[0] or 0)

    def _free_pages_now(self) -> int:
        """自开一个连接取空闲页基线。

        只用于进入清理事务**之前**取基线。清理事务内部必须用同一个
        `connection` 读，绝不能在事务里再开连接——写事务持有锁，新连接拿
        不到锁会直接卡死。
        """
        try:
            with self.database.connect() as connection:
                return self._freelist_count(connection)
        except sqlite3.Error:
            return 0

    def _largest_table_bytes(self):
        """返回（占用最大的对象名, 字节数）。dbstat 不可用时返回 ("", 0)。"""
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT name, SUM(pgsize) AS bytes FROM dbstat
                    GROUP BY name ORDER BY bytes DESC LIMIT 1
                    """
                ).fetchone()
        except sqlite3.Error:
            return "", 0
        if row is None:
            return "", 0
        return str(row["name"]), int(row["bytes"] or 0)

    def should_run_by_threshold(self) -> dict:
        """只读检查是否需要按阈值清理，**不做任何删除**。

        判定：库文件体积 > `threshold_mb`，**或** 任一表 > 500 MB。
        返回:

        .. code-block:: python

            {"needed": bool, "reason": str, "db_bytes": int,
             "largest_table": str, "largest_bytes": int}
        """
        setting = read_retention_setting(self.database)
        db_bytes = self._database_bytes()
        largest_table, largest_bytes = self._largest_table_bytes()
        db_limit = setting["threshold_mb"] * 1024 * 1024
        table_limit = DEFAULT_TABLE_THRESHOLD_MB * 1024 * 1024
        reasons = []
        if db_bytes > db_limit:
            reasons.append(
                f"库文件 {db_bytes} 字节超过阈值 {db_limit} 字节"
                f"（{setting['threshold_mb']} MB）"
            )
        if largest_bytes > table_limit:
            reasons.append(
                f"表 {largest_table} 占用 {largest_bytes} 字节超过 "
                f"{DEFAULT_TABLE_THRESHOLD_MB} MB"
            )
        return {
            "needed": bool(reasons),
            "reason": "；".join(reasons),
            "db_bytes": db_bytes,
            "largest_table": largest_table,
            "largest_bytes": largest_bytes,
        }

    @staticmethod
    def _parse_moment(value):
        """把 ISO 字符串解析成 UTC datetime；解析不了返回 None。"""
        if not value:
            return None
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _audit_last_cleanup_at(self, fallback=None):
        """旧口径：`audit_log` 里最后一次 `retention_cleanup` 的时间。

        仅用于升级兼容——旧库还没有节流时钟键时，拿它当一次回退基准，避免
        升级后的第一次阈值检查把"上次真删"当成"从未清理"而立刻重跑。
        """
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT MAX(occurred_at) FROM audit_log "
                    "WHERE action='retention_cleanup'"
                ).fetchone()
        except sqlite3.Error:
            return fallback
        value = row[0] if row is not None else None
        moment = self._parse_moment(value)
        return fallback if moment is None else moment

    def _last_attempt_at(self, fallback=None):
        """上一次**真正执行**过清理尝试的时间；取不到返回 fallback。

        时钟存在 `runtime_state` 的专用键（``THROTTLE_STATE_KEY``）里，**不复用
        `audit_log`**。原因：审计是"只在真的删掉了东西时才写"（有意设计，避免
        每天留一条全零记录），而节流要的是"每次尝试都前进"的时钟。两者语义不同，
        共用同一个字段就会退化成死循环——当判定该清理、跑完却什么都没删时
        （例如 `judgments` 触顶后长期无可删数据），审计不写、时钟不动，于是每个
        阈值检查周期都重跑一遍 A~E 全表扫描 + dbstat（真库单次约 1.8 秒），
        而调度器每小时检查一次。时钟与审计必须分开。

        兼容：旧库还没有这个键时，回退到旧的 `audit_log` 口径（
        `_audit_last_cleanup_at`），让升级后的第一次检查仍按"上次真删"判定；
        两者都没有（全新库）则返回 fallback，表示"从未清理过"、允许执行一次。
        """
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT value_json FROM runtime_state WHERE state_key=?",
                    (THROTTLE_STATE_KEY,),
                ).fetchone()
        except sqlite3.Error:
            return self._audit_last_cleanup_at(fallback)
        if row is not None:
            raw = row["value_json"]
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                value = raw
            moment = self._parse_moment(value)
            if moment is not None:
                return moment
        return self._audit_last_cleanup_at(fallback)

    # -- 执行 -------------------------------------------------------------

    def run(self, trigger="scheduled") -> dict:
        """按 A → B → C → D → E → F 顺序执行分层清理。

        参数
        ----
        trigger : ``"scheduled"`` | ``"threshold"`` | ``"manual"``
            - ``scheduled``：每日定时清理（上游已做「今天是否跑过」的墙钟判断）。
            - ``threshold``：体积阈值触发；受 ``min_interval_hours`` 约束，
              间隔内返回 ``status="skipped"`` 且不做删除。间隔以
              ``runtime_state`` 里的节流时钟为准（``THROTTLE_STATE_KEY``），**每次
              真正执行的尝试都会推进它**，所以空跑也会重置下一个 6 小时窗口；被
              跳过的检查不推进时钟，因此不会把窗口错误地向后推。
            - ``manual``：人工强制，**绕过**最小间隔检查。

        行为约定
        --------
        - 设置为 ``enabled=False`` 时直接返回 ``status="disabled"``，不删任何东西。
        - **单事务**：A~F 全部删除在同一个事务里完成，任一层抛异常整体回滚，
          不留半删状态。审计行与节流时钟也在同一事务内写入。
        - **只有真的删了东西才写 audit_log**（沿用既有约定，避免每天留全零记录）；
          节流时钟与此无关，每次尝试都写。
        - **不自动 VACUUM**：删除不缩小物理文件，库级 before/after 正常相等。
          返回值为逻辑删除行数，不等于磁盘占用下降量。
        - **效果证据看空闲页，不看体积**：库是 `PRAGMA auto_vacuum=0`，删掉的行
          只会变成可复用的空闲页。所以判断清理是否真的生效，看
          `free_pages_after > free_pages_before`；`db_bytes_before ==
          db_bytes_after` **是预期行为而非 bug**，不要拿它说事。
          `free_pages_before` 在进入清理事务**之前**自开连接读取，
          `free_pages_after` 则在事务内用**同一个 connection** 读——写事务持锁
          期间另开连接会拿不到锁而死等。
        - **A 层判据（2026-09-14 修正）**：删除的依据取
          ``COALESCE(CASE WHEN published_at LIKE '____-__-__T%' THEN
          published_at END, first_seen_at)``。即优先用发布日期，但**只有它
          是 ISO 形状时才用**；`published_at` 为 NULL 或非 ISO 形状（历史遗留
          的 RFC 2822 等）时一律退化用 `first_seen_at`。
          旧判据 `published_at < ?` 有两处永假：NULL 比较得 NULL，RFC 2822
          首字符 `'T'` > `'2'` 按字典序恒大于截止值。**不解析 RFC 2822 回写、
          也不 backfill NULL**——那是伪造发布日期，`first_seen_at` 才是诚实的
          兜底。代价是这条 SELECT 用不上 `idx_external_items_published`，
          退化为全表扫描（实测约 96k 行 / 数十毫秒，每日一次可接受）。
        """
        setting = read_retention_setting(self.database)
        now_utc = self.now().astimezone(timezone.utc)
        cutoff = _iso(now_utc - timedelta(days=setting["days"]))
        cluster_cutoff = _iso(now_utc - timedelta(days=setting["cluster_days"]))
        situation_cutoff = _iso(now_utc - timedelta(days=SITUATION_KEEP_DAYS))

        result = {
            "status": "ok",
            "trigger": trigger,
            "deleted_items": 0,
            "expired_clusters": 0,
            "deleted_detail": {},
            "deleted_jobs": 0,
            "deleted_runs": 0,
            "deleted_situation": 0,
            "downsampled_snapshots": {},
            "cutoff": cutoff,
            "cluster_cutoff": cluster_cutoff,
            "situation_cutoff": situation_cutoff,
            "db_bytes_before": 0,
            "db_bytes_after": 0,
            "free_pages_before": 0,
            "free_pages_after": 0,
        }
        if not setting["enabled"]:
            result["status"] = "disabled"
            return result

        if trigger == "threshold":
            interval = timedelta(hours=setting["min_interval_hours"])
            last = self._last_attempt_at(fallback=None)
            if (
                interval > timedelta(0)
                and last is not None
                and now_utc - last < interval
            ):
                result["status"] = "skipped"
                return result

        now_text = _iso(now_utc)
        job_cutoff = _iso(now_utc - timedelta(days=setting["job_days"]))
        job_absolute_cutoff = _iso(
            now_utc - timedelta(days=JOB_ABSOLUTE_MAX_DAYS)
        )
        run_cutoff = _iso(now_utc - timedelta(days=setting["run_days"]))

        db_bytes_before = self._database_bytes()
        free_pages_before = self._free_pages_now()
        deleted_items = 0
        expired_clusters = 0
        detail_counts = {}
        deleted_jobs = 0
        deleted_runs = 0
        deleted_situation = 0
        downsampled = {window: 0 for window in SNAPSHOT_KEEP_DAYS}
        db_bytes_after = db_bytes_before
        free_pages_after = free_pages_before

        with self.database.connect() as connection:
            # 阶段 A：可再生的原始抓取条目
            #
            # 判据必须是「发布日期」和「首次看到时间」里那个**真的能用的**：
            #
            # - `published_at` 为 NULL 时，`NULL < ?` 结果是 NULL，永远为假
            #   ——库里 80% 的行就是这样，旧判据把它们永久焊死。
            # - `published_at` 不是 ISO 形状时（历史遗留的 RFC 2822，如
            #   "Thu, 06 Aug 2026 13:34:05 GMT"），按字典序比会因为首字符
            #   'T'(0x54) > '2'(0x32) 而恒大于截止值，也永远删不掉。
            #
            # 这两种情况一律退化用 `first_seen_at`。它的语义是"我们第一次
            # 看到这条的时间"，拿它兜底是诚实的；**不解析 RFC 2822 回写、
            # 也不 backfill NULL**，那是伪造发布日期。`first_seen_at` 是
            # NOT NULL 且实测无空缺，`COALESCE` 一定取得到值。
            stale_ids = [
                row["item_id"]
                for row in connection.execute(
                    """
                    SELECT item_id FROM external_items
                    WHERE COALESCE(
                        CASE WHEN published_at LIKE '____-__-__T%'
                             THEN published_at END,
                        first_seen_at
                    ) < ?
                    """,
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

            # 阶段 B：过期簇的派生明细（结论本身保留）
            expired_clusters, detail_counts = self._purge_expired_clusters(
                connection, cluster_cutoff
            )

            # 阶段 C：作业流水
            deleted_jobs = self._purge_judgment_jobs(
                connection, job_cutoff, job_absolute_cutoff
            )

            # 阶段 D：采集运行
            deleted_runs = self._purge_external_runs(connection, run_cutoff)

            # 阶段 E：趋势快照按窗口降采样
            downsampled = self._downsample_snapshots(connection, now_utc)

            # 阶段 F：过期的全球态势事件（30 天，可从上游再生）
            deleted_situation = self._purge_situation_events(
                connection, situation_cutoff
            )

            removed_total = (
                deleted_items
                + sum(detail_counts.values())
                + deleted_jobs
                + deleted_runs
                + deleted_situation
                + sum(downsampled.values())
            )
            db_bytes_after = self._database_bytes()
            # 用同一个 connection 读，绝不在写事务里新开连接（拿不到锁会卡死）。
            free_pages_after = self._freelist_count(connection)

            # 节流时钟：**每次真正执行过的尝试都推进**，无论本次是否删了东西。
            # 与下面的审计写入刻意分开——审计只在真删时写（保持既有语义），
            # 时钟则必须每次前进，否则空跑会让 6 小时节流失效、每周期重跑全扫。
            # 写在同一个事务里：失败回滚时时钟一并回滚，不会出现"尝试失败却把
            # 6 小时窗口耗掉"的情况。被节流跳过的检查根本没走到这里，不推进时钟，
            # 以免把 6 小时窗口错误地向后推（见 MinIntervalTests）。
            connection.execute(
                "INSERT INTO runtime_state(state_key, value_json, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(state_key) DO UPDATE SET"
                " value_json=excluded.value_json, updated_at=excluded.updated_at",
                (THROTTLE_STATE_KEY, json.dumps(now_text), now_text),
            )

            # 只有真的删掉了东西才写审计，避免每天留下一条全零的记录
            if removed_total:
                details = {
                    "trigger": trigger,
                    "deleted_items": deleted_items,
                    "days": setting["days"],
                    "expired_clusters": expired_clusters,
                    "cluster_days": setting["cluster_days"],
                    "deleted_detail": detail_counts,
                    "deleted_jobs": deleted_jobs,
                    "job_days": setting["job_days"],
                    "deleted_runs": deleted_runs,
                    "run_days": setting["run_days"],
                    "deleted_situation": deleted_situation,
                    "situation_days": SITUATION_KEEP_DAYS,
                    "downsampled_snapshots": downsampled,
                    "db_bytes_before": db_bytes_before,
                    "db_bytes_after": db_bytes_after,
                    "free_pages_before": free_pages_before,
                    "free_pages_after": free_pages_after,
                }
                if trigger == "threshold":
                    details["threshold_mb"] = setting["threshold_mb"]
                connection.execute(
                    "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        now_text,
                        "retention_cleanup",
                        "external_items",
                        cutoff,
                        json.dumps(details, ensure_ascii=False),
                    ),
                )

        result.update(
            {
                "deleted_items": deleted_items,
                "expired_clusters": expired_clusters,
                "deleted_detail": detail_counts,
                "deleted_jobs": deleted_jobs,
                "deleted_runs": deleted_runs,
                "deleted_situation": deleted_situation,
                "downsampled_snapshots": downsampled,
                "db_bytes_before": db_bytes_before,
                "db_bytes_after": db_bytes_after,
                "free_pages_before": free_pages_before,
                "free_pages_after": free_pages_after,
            }
        )
        return result

    def _purge_expired_clusters(self, connection, cluster_cutoff):
        """清理过期簇的派生明细，返回（过期簇数量, 各表明细行数）。

        只删 `CLUSTER_DETAIL_TABLES` 里的派生数据；事件簇本身与研判是结论，
        一律保留（judgments 另有触发器保护，想删也删不掉）。

        **逐表判据不同**：多数明细表是纯派生物，簇过期即可清；但
        `personal_impacts` / `notification_log` 里混着用户自撰状态（用户标注、
        静音、重要性覆盖、已读标记），这些还被学习回路当输入，删掉等于抹掉
        用户的判断——所以对这两张表额外叠加 `CLUSTER_DETAIL_FILTERS` 里的
        "无任何用户标注"条件，只清从未被用户碰过的行。其余表行为不变。

        表名来自模块常量，不是外部输入，所以这里用 f-string 拼表名是安全的
        （SQLite 不支持把表名参数化）。先统计再删除，统计进 audit_log，
        便于事后核对"到底删了什么"。
        """

        def where_for(table):
            scope = (
                "cluster_id IN (SELECT cluster_id FROM event_clusters"
                " WHERE last_seen_at < ?)"
            )
            extra = CLUSTER_DETAIL_FILTERS.get(table)
            return scope if extra is None else f"{scope} AND {extra}"

        expired = connection.execute(
            "SELECT COUNT(*) FROM event_clusters WHERE last_seen_at < ?",
            (cluster_cutoff,),
        ).fetchone()[0]
        if not expired:
            return 0, {}
        counts = {}
        for table in CLUSTER_DETAIL_TABLES:
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where_for(table)}",
                (cluster_cutoff,),
            ).fetchone()[0]
        for table in CLUSTER_DETAIL_TABLES:
            if counts[table]:
                connection.execute(
                    f"DELETE FROM {table} WHERE {where_for(table)}", (cluster_cutoff,)
                )
        return expired, counts

    def _purge_judgment_jobs(self, connection, job_cutoff, absolute_cutoff):
        """C 层：清理作业流水，返回实际删除行数。两条规则取并集。

        (a) `status='succeeded'` 且所属簇已有研判，且 `created_at < job_cutoff`
            ——作业成果已经落在 `judgments` 里，这条流水完全冗余。
        (b) 任何状态的作业，`created_at < absolute_cutoff`（180 天）
            ——兜住永远不会再有结果的僵死作业。

        「所属簇已有研判」用 EXISTS + 索引查，不用 IN 子查询：
        `judgments` 上有 `UNIQUE(cluster_id, provider, evidence_hash)`，
        其隐式索引以 cluster_id 为前缀，EXISTS 能直接命中，避免全表扫描。
        先统计后删除。

        收益要分成两笔账看，别混在一起：

        - **一次性回收**：截至 2026-09-14 真实库实测可删 37,667 行 ≈ 10.2 MB。
          这是一次性的历史欠账，跑完第一次就没有了。
        - **稳态日抑制**：约 **0.95 MB/日**。作业流水近似日增 0.95 MB，
          `job_days=7` 的窗口把超过 7 天的冗余流水持续清掉。**这才是 C 层
          真正的长期价值**——它把"流水无限增长"变成"流水在 7 天窗口内封顶"，
          而不是赚一笔一次性的钱。
        """
        condition = """
            (status = 'succeeded' AND created_at < ?
             AND EXISTS (SELECT 1 FROM judgments j
                         WHERE j.cluster_id = judgment_jobs.cluster_id))
            OR created_at < ?
        """
        params = (job_cutoff, absolute_cutoff)
        count = connection.execute(
            f"SELECT COUNT(*) FROM judgment_jobs WHERE {condition}", params
        ).fetchone()[0]
        if count:
            connection.execute(
                f"DELETE FROM judgment_jobs WHERE {condition}", params
            )
        return count

    def _purge_external_runs(self, connection, run_cutoff):
        """D 层：清理过期的采集运行记录，返回实际删除行数。

        **未来保护**：截至 2026-09-14 最老记录是 2026-08-06，未满 90 天，
        当下可删 0 行 / 0 MB。不要把它当成即时回收。
        """
        count = connection.execute(
            "SELECT COUNT(*) FROM external_runs WHERE started_at < ?",
            (run_cutoff,),
        ).fetchone()[0]
        if count:
            connection.execute(
                "DELETE FROM external_runs WHERE started_at < ?", (run_cutoff,)
            )
        return count

    def _purge_situation_events(self, connection, situation_cutoff):
        """F 层：清理过期的全球态势事件，返回实际删除行数。

        判据与 A 层**刻意同构**，因为两张表的"时间"都有同一个坑：

        - `occurred_at` 可能为 NULL（上游没给时间）。
        - 它也可能是非 ISO 形状的字符串。按字典序比会恒大于截止值而永远删不掉。

        所以优先用 `occurred_at`，但**只有它是 ISO 形状时才用**；否则退化用
        `last_seen_at`。两个都为空的行（COALESCE 得 NULL）比较结果为 NULL、
        永不命中 —— 这是**保守方向**（宁可不删），符合"删不掉的后果只是多占点空间"。

        这批数据可从上游再生，不是结论，所以进清理；但它**不是**用户自撰状态，
        不需要像 `personal_impacts` 那样叠加"无用户标注"条件。
        """
        condition = """
            COALESCE(
                CASE WHEN occurred_at LIKE '____-__-__T%' THEN occurred_at END,
                last_seen_at
            ) < ?
        """
        count = connection.execute(
            f"SELECT COUNT(*) FROM situation_events WHERE {condition}",
            (situation_cutoff,),
        ).fetchone()[0]
        if count:
            connection.execute(
                f"DELETE FROM situation_events WHERE {condition}", (situation_cutoff,)
            )
        return count

    def _downsample_snapshots(self, connection, now_utc):
        """E 层：按窗口降采样趋势快照，返回 {窗口小时: 删除行数}。

        每个窗口按 `SNAPSHOT_KEEP_DAYS` 保留；`PROTECTED_SNAPSHOT_WINDOWS`
        （720h）**永久保留**，不在表内也不在保护名单里的窗口一律不动。
        返回字典始终包含 `SNAPSHOT_KEEP_DAYS` 的所有窗口键（即使该窗口删了 0 行），
        便于调用方一眼看清每个窗口的处置结果。
        """
        counts = {}
        for window_hours, keep_days in SNAPSHOT_KEEP_DAYS.items():
            if window_hours in PROTECTED_SNAPSHOT_WINDOWS:
                continue
            cutoff = _iso(now_utc - timedelta(days=keep_days))
            count = connection.execute(
                "SELECT COUNT(*) FROM trend_snapshots "
                "WHERE window_hours = ? AND captured_at < ?",
                (window_hours, cutoff),
            ).fetchone()[0]
            if count:
                connection.execute(
                    "DELETE FROM trend_snapshots "
                    "WHERE window_hours = ? AND captured_at < ?",
                    (window_hours, cutoff),
                )
            counts[window_hours] = count
        return counts
