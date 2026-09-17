"""Time-window trend snapshots with explicit insufficient-data states."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone


WINDOW_HOURS = (6, 24, 7 * 24, 30 * 24)
MINIMUM_HISTORY_HOURS = 7 * 24
MAXIMUM_BASELINE_DAYS = 30
MINIMUM_CURRENT_SAMPLE = 5
SURGE_RATIO = 2.0
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
                        status = "rising" if surge_ratio >= SURGE_RATIO else "normal"
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
