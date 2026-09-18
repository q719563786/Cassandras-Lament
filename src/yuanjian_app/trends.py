"""Time-window trend snapshots with explicit insufficient-data states."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from statistics import NormalDist


WINDOW_HOURS = (6, 24, 7 * 24, 30 * 24)
MINIMUM_HISTORY_HOURS = 7 * 24
MAXIMUM_BASELINE_DAYS = 30
MINIMUM_CURRENT_SAMPLE = 5
#: 旧口径的固定倍数（当前/基线 >= 2.0 即算 rising）。v1.4 起**不再用它判定**，
#: 但 `surge_ratio` 仍然照算并落库 —— 比值本身是有用的事实，只是不该由它定阈值。
LEGACY_SURGE_RATIO = 2.0
#: 允许的 rising 占比上限（健康指标阈值）。
#:
#: 真库实测：14,315 个快照里 rising 6,892（48%）；只看可判定的（rising + normal）
#: **占 73%**。一个多数时间在报警的探测器等价于没有报警 —— 这个数字此前没有任何
#: 地方在盯它。现在它既是诊断面板上的一个指标，也是阈值本身的标定目标：
#: 单次比较的显著性水平 = RISING_BUDGET / 本轮比较次数（见 `_rising_threshold`）。
RISING_BUDGET = 0.20
# 查询下限：最大基线窗口(30天) + 最大趋势窗口(30天) + 1天余量，避免全表扫描
_LOOKBACK_DAYS = MAXIMUM_BASELINE_DAYS + max(WINDOW_HOURS) // 24 + 1


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TrendService:
    def __init__(self, database):
        self.database = database

    def _events(self, at):
        lower = _iso(at - timedelta(days=_LOOKBACK_DAYS))
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT first_seen_at,categories_json FROM event_clusters
                WHERE status='active' AND first_seen_at<=? AND first_seen_at>=?
                ORDER BY first_seen_at
                """,
                (_iso(at), lower),
            ).fetchall()
        events = []
        for row in rows:
            try:
                categories = json.loads(row["categories_json"])
            except json.JSONDecodeError:
                categories = ["general"]
            if not isinstance(categories, list) or not categories:
                categories = ["general"]
            events.append((_parse(row["first_seen_at"]), tuple(map(str, categories))))
        return events

    def _calculate(self, at):
        events = self._events(at)
        categories = sorted({category for _, values in events for category in values})
        # 本轮一共要做多少次"这是不是上升"的判断：类别数 × 窗口数。
        # 多重比较修正的分母就是它（见 `_rising_threshold`）。
        comparisons = max(1, len(categories) * len(WINDOW_HOURS))
        snapshots = []
        for category in categories:
            times = [seen for seen, values in events if category in values]
            for window_hours in WINDOW_HOURS:
                current_start = at - timedelta(hours=window_hours)
                current_count = sum(current_start < seen <= at for seen in times)
                historical_times = [seen for seen in times if seen <= current_start]
                baseline_count = None
                surge_ratio = None
                if not historical_times:
                    status = "accumulating"
                else:
                    history_start = max(
                        min(historical_times), at - timedelta(days=MAXIMUM_BASELINE_DAYS)
                    )
                    history_hours = (current_start - history_start).total_seconds() / 3600
                    if history_hours < MINIMUM_HISTORY_HOURS:
                        status = "accumulating"
                    elif current_count < MINIMUM_CURRENT_SAMPLE:
                        status = "low_sample"
                    else:
                        baseline_events = sum(
                            history_start <= seen <= current_start for seen in historical_times
                        )
                        baseline_count = baseline_events * window_hours / history_hours
                        surge_ratio = (
                            current_count / baseline_count
                            if baseline_count > 0
                            else float(current_count)
                        )
                        threshold = self._rising_threshold(baseline_count, comparisons)
                        status = "rising" if current_count > threshold else "normal"
                snapshots.append(
                    {
                        "captured_at": _iso(at),
                        "category": category,
                        "window_hours": window_hours,
                        "event_count": current_count,
                        "baseline_count": (
                            round(baseline_count, 6) if baseline_count is not None else None
                        ),
                        "surge_ratio": (
                            round(surge_ratio, 6) if surge_ratio is not None else None
                        ),
                        "status": status,
                    }
                )
        return snapshots

    @staticmethod
    def _rising_threshold(expected, comparisons):
        """按比较次数做多重比较修正后的"上升"阈值（泊松的正态近似）。

        **为什么不再用固定倍数**（旧 `SURGE_RATIO = 2.0`）：
        旧口径每轮要对「约 14 个类别 × 4 个窗口 = 最多 56 次比较」各判一次"是否翻倍"，
        却按**单次比较**的尺度取固定倍数 —— 没有做多重比较校正。比较次数越多，
        "至少有一次误报"的概率越高，于是阈值整体失效：真库实测 rising 占可判定
        快照的 73%。

        现在：单次比较的显著性水平 = `RISING_BUDGET / 比较次数`，阈值取
        `期望 + z·√期望`。因为比较次数被显式计入分母，**期望的 rising 占比就落在
        RISING_BUDGET 之内** —— 健康指标因此有明确的比较基准，而不是随手定的数字。
        """
        if expected is None or expected <= 0:
            return 0.0
        level = 1.0 - (RISING_BUDGET / max(1, comparisons))
        # 比较次数极少时（例如只有 1 个类别 1 个窗口）level 会接近 0.8，
        # z≈0.84，阈值仍然明显高于期望 —— 不会退化成"稍有波动就算上升"。
        z = NormalDist().inv_cdf(min(0.999999, max(0.5, level)))
        return expected + z * math.sqrt(expected)

    @staticmethod
    def _health(snapshots) -> dict:
        """探测器健康度：`rising` 占**可判定**快照（rising + normal）的比例。

        高于 RISING_BUDGET 即为**阈值失效**（"一直在报警"）：诊断面板据此显示警示。
        只统计可判定的（accumulating / low_sample 没有基线，不属于"判过了说没问题"，
        也不属于"判过了说在上升"）。
        """
        judgeable = [
            item for item in snapshots if item.get("status") in ("rising", "normal")
        ]
        rising = [item for item in judgeable if item.get("status") == "rising"]
        share = round(len(rising) / len(judgeable), 6) if judgeable else None
        return {
            "judgeable": len(judgeable),
            "rising": len(rising),
            "rising_share": share,
            "budget": RISING_BUDGET,
            "threshold_failed": share is not None and share > RISING_BUDGET,
        }

    def stored_health(self) -> dict:
        """**只读**地从最近一次已落盘的采样里算健康度（诊断面板用，绝不写库）。

        诊断面板不该为了显示一个数字而触发一次采集 —— 那是"读路径写库"，
        本仓已经因为同样的原因删过一次首页的写库调用。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(captured_at) FROM trend_snapshots"
            ).fetchone()
            latest = row[0] if row else None
            if not latest:
                payload = self._health([])
                payload["captured_at"] = None
                return payload
            rows = connection.execute(
                "SELECT status FROM trend_snapshots WHERE captured_at=?", (latest,)
            ).fetchall()
        payload = self._health([{"status": item["status"]} for item in rows])
        payload["captured_at"] = latest
        return payload

    def _last_captured_at(self, at):
        """最近一次（早于 at）已落盘的采样时刻，用于口径对比；无则返回 None。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(captured_at) FROM trend_snapshots WHERE captured_at < ?",
                (_iso(at),),
            ).fetchone()
        return row[0] if row else None

    def _prior_category_set(self, prior_at):
        """上次采样覆盖的检测源类别集合。"""
        if not prior_at:
            return set()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT category FROM trend_snapshots WHERE captured_at = ?",
                (prior_at,),
            ).fetchall()
        return {row["category"] for row in rows}

    def _detect_sampling_shift(self, at, snapshots):
        """采样口径护栏（v1.1 第一波）。

        检测源集合相对上次采样发生变化时返回 (True, reason)，否则 (False, "")。
        两类触发：① 检测源启用/停用导致类别集合增减；② 断网恢复或回填导致距
        上次采样的间隔异常拉长。两种情况都会让 baseline 口径不可比，界面应提示
        「采样口径有变动，解读需谨慎」。
        """
        prior_at = self._last_captured_at(at)
        current = {s["category"] for s in snapshots}
        if prior_at is None:
            return False, ""  # 首次采样，无口径可比
        prior = self._prior_category_set(prior_at)
        added = sorted(current - prior)
        removed = sorted(prior - current)
        reasons = []
        if added or removed:
            parts = []
            if added:
                parts.append("新增检测源类别：" + "、".join(added))
            if removed:
                parts.append("停用检测源类别：" + "、".join(removed))
            reasons.append("；".join(parts))
        # 断网恢复 / 回填：间隔超过 2 天即视为异常
        try:
            gap_hours = (at - _parse(prior_at)).total_seconds() / 3600
        except Exception:
            gap_hours = 0
        if gap_hours > 2 * 24:
            reasons.append("采样中断后恢复：距上次采样约 %.0f 小时" % gap_hours)
        if reasons:
            return True, "采样口径有变动——" + "；".join(reasons)
        return False, ""

    def capture(self, at: datetime) -> dict:
        if at.tzinfo is None:
            raise ValueError("趋势时间必须包含时区")
        at = at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        snapshots = self._calculate(at)
        # 采样口径护栏：检测源集合变化 / 采样中断恢复时标注 sampling_shift，
        # 提示界面「采样口径有变动，解读需谨慎」。
        sampling_shift, sampling_shift_reason = self._detect_sampling_shift(at, snapshots)
        with self.database.connect() as connection:
            for snapshot in snapshots:
                identity = (
                    f"{snapshot['captured_at']}|{snapshot['category']}|"
                    f"{snapshot['window_hours']}"
                )
                snapshot_id = "T-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                connection.execute(
                    """
                    INSERT INTO trend_snapshots(
                        snapshot_id,captured_at,category,window_hours,event_count,
                        baseline_count,surge_ratio,status
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(captured_at,category,window_hours) DO UPDATE SET
                        event_count=excluded.event_count,
                        baseline_count=excluded.baseline_count,
                        surge_ratio=excluded.surge_ratio,
                        status=excluded.status
                    """,
                    (
                        snapshot_id,
                        snapshot["captured_at"],
                        snapshot["category"],
                        snapshot["window_hours"],
                        snapshot["event_count"],
                        snapshot["baseline_count"],
                        snapshot["surge_ratio"],
                        snapshot["status"],
                    ),
                )
        return {
            "captured_at": _iso(at),
            "snapshots": self._public(snapshots),
            # 健康指标（R-11）：rising 占可判定快照的比例。> RISING_BUDGET 即阈值失效。
            "health": self._health(snapshots),
            "sampling_shift": sampling_shift,
            "sampling_shift_reason": sampling_shift_reason,
        }

    def summary(self, at: datetime) -> list[dict]:
        return self.capture(at)["snapshots"]

    @staticmethod
    def _public(snapshots):
        output = []
        for snapshot in snapshots:
            item = dict(snapshot)
            if item["baseline_count"] is None:
                item.pop("baseline_count")
            if item["surge_ratio"] is None:
                item.pop("surge_ratio")
            output.append(item)
        return output
