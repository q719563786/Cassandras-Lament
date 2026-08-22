import json
import threading
import time
from datetime import datetime, timezone

from .operations import CognitionOperation


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RadarScheduler:
    """Runs collection and cognition tasks with visible, independent state."""

    def __init__(
        self,
        service,
        poll_seconds=30,
        *,
        database=None,
        cognition=None,
        cognition_operation=None,
        backup_service=None,
        retention_service=None,
        learning_callback=None,
        now=lambda: datetime.now(timezone.utc),
    ):
        self.service = service
        self.poll_seconds = float(poll_seconds)
        self.database = database or getattr(service, "database", None)
        self.cognition = cognition
        self.cognition_operation = cognition_operation or (
            CognitionOperation(cognition) if cognition is not None else None
        )
        self.backup_service = backup_service
        self.retention_service = retention_service
        self.learning_callback = learning_callback
        self.now = now
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def paused(self):
        return self._paused.is_set()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def run_once(self):
        """Compatibility entry: immediately run only due external sources."""
        if self.paused:
            return 0
        return self.service.refresh_due_sources()

    def _record(self, task, payload):
        if self.database is None:
            return
        updated_at = _iso(self.now())
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(state_key,value_json,updated_at)
                VALUES (?,?,?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json=excluded.value_json,updated_at=excluded.updated_at
                """,
                (f"task.{task}", json.dumps(payload, sort_keys=True), updated_at),
            )

    def _execute(self, name, callback):
        started_at = _iso(self.now())
        try:
            result = callback()
        except Exception as error:
            payload = {
                "status": "error",
                "started_at": started_at,
                "finished_at": _iso(self.now()),
                "error_type": type(error).__name__,
            }
            self._record(name, payload)
            return payload
        payload = {
            "status": "ok",
            "started_at": started_at,
            "finished_at": _iso(self.now()),
            "result": result,
        }
        self._record(name, payload)
        return payload

    def run_external_once(self):
        if self.paused:
            return {"status": "paused"}
        return self._execute("external", self.service.refresh_due_sources)

    def run_cognition_once(self):
        if self.paused:
            return {"status": "paused"}
        if self.cognition_operation is None:
            return {"status": "disabled"}
        return self._execute(
            "cognition", lambda: self.cognition_operation.run("scheduled")
        )

    def run_trends_once(self):
        if self.paused:
            return {"status": "paused"}
        if self.cognition is None:
            return {"status": "disabled"}
        return self._execute("trends", self.cognition.capture_trends)

    def _task_last_local_date(self, task):
        """读 runtime_state task.<task> 的本地完成日期，没跑过返回 None。"""
        if self.database is None:
            return None
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT value_json FROM runtime_state WHERE state_key=?",
                    (f"task.{task}",),
                ).fetchone()
            if not row:
                return None
            payload = json.loads(row["value_json"])
            finished = str(payload.get("finished_at", ""))
            if not finished:
                return None
            moment = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            return moment.astimezone().date()
        except (ValueError, TypeError, OSError):
            return None

    def _daily_due(self, task, hour):
        """墙钟判断：本地今天已过 hour 点且今天还没跑过 → 到期。

        区别于 monotonic 间隔：跨睡眠/跨天后仍按日历补跑一次（R6）。
        """
        local_now = self.now().astimezone()
        if local_now.hour < max(0, min(int(hour), 23)):
            return False
        return self._task_last_local_date(task) != local_now.date()

    def run_backup_once(self):
        """每日备份：跨过目标时段后补跑一次，成功与否当天不再重试。"""
        if self.paused:
            return {"status": "paused"}
        if self.backup_service is None:
            return {"status": "disabled"}
        from .backup import read_backup_setting

        setting = read_backup_setting(self.database)
        if not setting["enabled"]:
            return {"status": "disabled"}
        if not self._daily_due("backup", setting["hour"]):
            return {"status": "skipped"}
        return self._execute("backup", self.backup_service.run)

    def run_retention_once(self):
        """每日清理：与备份同一时段，避免白天抓取高峰删库页。"""
        if self.paused:
            return {"status": "paused"}
        if self.retention_service is None:
            return {"status": "disabled"}
        from .backup import read_backup_setting

        setting = read_backup_setting(self.database)
        if not self._daily_due("retention", setting["hour"]):
            return {"status": "skipped"}
        return self._execute("retention", self.retention_service.run)

    def run_learning_once(self):
        """误报反馈回灌：6 小时 monotonic 间隔（与墙钟无关）。"""
        if self.paused:
            return {"status": "paused"}
        if self.learning_callback is None:
            return {"status": "disabled"}
        from .system_settings import read_learning_setting

        if not read_learning_setting(self.database).get("enabled", True):
            return {"status": "disabled"}
        return self._execute("learning", self.learning_callback)

    def _run(self):
        next_external = 0.0
        next_cognition = 0.0
        next_trends = 0.0
        next_learning = 0.0
        next_daily = 0.0
        while not self._stop.is_set():
            current = time.monotonic()
            if current >= next_external:
                self.run_external_once()
                next_external = current + self.poll_seconds
            if self.cognition is not None and current >= next_cognition:
                self.run_cognition_once()
                next_cognition = current + 60
            if self.cognition is not None and current >= next_trends:
                self.run_trends_once()
                next_trends = current + 3600
            if current >= next_learning:
                self.run_learning_once()
                next_learning = current + 6 * 3600
            if current >= next_daily:
                # 备份/清理只做"到期与否"检查，真正执行由墙钟判断（R6）。
                self.run_backup_once()
                self.run_retention_once()
                next_daily = current + 300
            waits = [next_external - time.monotonic()]
            if self.cognition is not None:
                waits.extend(
                    [next_cognition - time.monotonic(), next_trends - time.monotonic()]
                )
            waits.append(next_learning - time.monotonic())
            waits.append(next_daily - time.monotonic())
            self._stop.wait(max(0.01, min(max(0.0, value) for value in waits)))

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="YuanJianCognition", daemon=True
        )
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
