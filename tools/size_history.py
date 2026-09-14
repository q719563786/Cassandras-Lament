"""记录远见数据库的体积，追加到 CSV，用于长期观察膨胀趋势。

为什么需要它：库体积是五个评估维度里**唯一会随时间单方向恶化**的指标，
其它维度（可靠性、速度、安全、可维护性）都相对稳定。没有历史数值，
"是不是在失控"只能靠感觉。

用法（默认数据目录取 %LOCALAPPDATA%\\YuanJian，可用 --data-dir 指定）：

    python tools/size_history.py
    python tools/size_history.py --data-dir "D:\\远见\\数据"

输出：在数据目录下写 `logs/size-history.csv`，每次追加一行。
只读数据库，不做任何写操作。
"""

import argparse
import csv
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "recorded_at",
    "db_bytes",
    "db_mb",
    "detail_bytes",  # 可清理的派生明细占用
    "protected_bytes",  # 受不可变保护、无法清理的部分
    "largest_table",
    "row_total",
    "backup_count",
    "backup_bytes",
]

# 可清理的派生明细（与 retention.py 的 CLUSTER_DETAIL_TABLES 对齐）
DETAIL_TABLES = (
    "personal_impacts",
    "notification_log",
    "judgment_jobs",
    "event_entities",
    "event_cluster_items",
)
# 受不可变触发器保护、永远不删的表（Judgments 占大头）
PROTECTED_TABLES = ("judgments", "forecast_versions", "forecasts", "resolutions")


def default_data_dir():
    configured = os.environ.get("YUANJIAN_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("无法确定数据目录，请用 --data-dir 指定")
    return Path(local) / "YuanJian"


def measure(data_dir):
    database_path = data_dir / "data" / "yuanjian.db"
    if not database_path.is_file():
        raise SystemExit("找不到数据库：%s" % database_path)

    connection = sqlite3.connect("file:%s?mode=ro" % str(database_path).replace("\\", "/"), uri=True)
    try:
        try:
            sizes = {
                name: (size or 0)
                for name, size in connection.execute(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
                )
            }
        except sqlite3.Error:
            sizes = {}

        row_total = 0
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            try:
                row_total += connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            except sqlite3.Error:
                pass
    finally:
        connection.close()

    detail_bytes = sum(sizes.get(name, 0) for name in DETAIL_TABLES)
    protected_bytes = sum(sizes.get(name, 0) for name in PROTECTED_TABLES)
    largest = max(sizes.items(), key=lambda item: item[1])[0] if sizes else "unknown"

    backups = sorted((data_dir / "backups").glob("*.db")) if (data_dir / "backups").is_dir() else []
    backup_bytes = sum(path.stat().st_size for path in backups)

    db_bytes = database_path.stat().st_size
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "db_bytes": db_bytes,
        "db_mb": round(db_bytes / 1048576, 1),
        "detail_bytes": detail_bytes,
        "protected_bytes": protected_bytes,
        "largest_table": largest,
        "row_total": row_total,
        "backup_count": len(backups),
        "backup_bytes": backup_bytes,
    }


def append_record(data_dir, record):
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "size-history.csv"
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(record)
    return path


def summarize(record, history_path):
    print("库体积      %.1f MB" % record["db_mb"])
    print("  可清理明细 %.1f MB" % (record["detail_bytes"] / 1048576))
    print("  受保护部分 %.1f MB（判读与预测账本，不可清理）" % (record["protected_bytes"] / 1048576))
    print("  最大表     %s" % record["largest_table"])
    print("  总行数     %d" % record["row_total"])
    print("备份        %d 份 / %.1f GB" % (record["backup_count"], record["backup_bytes"] / 2**30))
    print("已追加到    %s" % history_path)


def main():
    parser = argparse.ArgumentParser(description="记录远见数据库体积")
    parser.add_argument("--data-dir", default=None, help="数据目录，默认 %%LOCALAPPDATA%%\\YuanJian")
    parser.add_argument("--print-only", action="store_true", help="只打印，不写入 CSV")
    arguments = parser.parse_args()

    data_dir = Path(arguments.data_dir).expanduser() if arguments.data_dir else default_data_dir()
    record = measure(data_dir)
    if arguments.print_only:
        summarize(record, data_dir / "logs" / "size-history.csv")
        return 0
    path = append_record(data_dir, record)
    summarize(record, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
