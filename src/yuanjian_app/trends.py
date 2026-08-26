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

    def capture(self, at: datetime) -> dict:
        if at.tzinfo is None:
            raise ValueError("趋势时间必须包含时区")
        at = at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        snapshots = self._calculate(at)
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
        return {"captured_at": _iso(at), "snapshots": self._public(snapshots)}

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
