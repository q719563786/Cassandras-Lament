import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from .operations import CognitionOperation
from .text_cleaning import plain_text

_logger = logging.getLogger(__name__)

_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|,;]+")
_POSIX_HOME_PATH = re.compile(r"/(?:Users|home)/[^\s'\"<>|,;]+")


def _redact_paths(text: str) -> str:
    """落库前把消息里的本机路径换成占位符。

    异常消息经常自带完整路径（`FileNotFoundError` 就会把文件名整个带上）。
    状态表只留在本机，但排障需要知道的是"哪一类失败"，不是"文件在哪儿"，
    没有理由把路径写进去。
    """
    return _POSIX_HOME_PATH.sub("<path>", _WINDOWS_PATH.sub("<path>", text))


def _error_location(error) -> str:
    """取异常**最内层**一帧的"函数名:行号"——路径无关，可安全进状态表。

    只给 `function:lineno`，不给文件名/完整堆栈：完整堆栈走日志
    （`_logger.error(..., exc_info=True)`），状态表是会被诊断面板读出来展示的，
    不该把一整段 traceback 怼到界面上。
    """
    tb = getattr(error, "__traceback__", None)
    if tb is None:
        return ""
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"


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
        forecasts=None,
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
        # 预测账本：只有"到期自动归档"这一条每日任务用它，且默认关闭。
        self.forecasts = forecasts
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
            # 完整堆栈进**日志**（不落状态表、更不进界面）：只记类型+消息
            # 在排查"到底是哪一步、哪个值"时等于没有现场。
            _logger.error("定时任务 %s 执行失败", name, exc_info=True)
            payload = {
                "status": "error",
                "started_at": started_at,
                "finished_at": _iso(self.now()),
                "error_type": type(error).__name__,
                # 只记异常类型等于没有现场：不知道是哪个值、哪一步失败。
                # 消息经 plain_text 清洗截断、再用 _redact_paths 抹掉本机路径，
                # 兼顾可诊断与不留路径。
                "error_message": _redact_paths(
                    plain_text(str(error), max_length=300)
                ),
                # 状态表**只**多带一个"函数名:行号"，路径无关；完整堆栈在上面的
                # 日志里。这样诊断面板能指出"死在哪一步"，又不会被 traceback 灌满。
                "error_location": _error_location(error),
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

    def run_situation_once(self):
        """全球态势图层抓取（独立任务名，独立于 external）。

        与 `run_external_once` 分开记录：两者的成功/失败必须能分别回看，
        否则一次地震源抖动会把"新闻采集"的任务状态写脏。
        """
        if self.paused:
            return {"status": "paused"}
        refresh = getattr(self.service, "refresh_situation_layers", None)
        if refresh is None:
            return {"status": "disabled"}
        return self._execute("situation", refresh)

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

    def run_retention_if_threshold(self):
        """体积阈值触发的清理：库文件或单表超阈值时才真正执行。

        与 `run_retention_once`（每日墙钟一次）互补：这里管的是"库涨太快"，
        每日一次来不及的场景。是否到期由 `should_run_by_threshold()` 只读判断，
        真正的节流（默认 6 小时内不重复）由 `RetentionService.run("threshold")`
        内部负责。

        记录用的任务名是 `retention_threshold`，与 `run_retention_once` 的
        `retention` **分开**——否则阈值触发会写脏 `task.retention`，
        让当天的每日清理误判为"已经跑过"而被跳过。
        """
        if self.paused:
            return {"status": "paused"}
        if self.retention_service is None:
            return {"status": "disabled"}
        check = self.retention_service.should_run_by_threshold()
        if not check["needed"]:
            return {
                "status": "skipped",
                "reason": check["reason"],
                "db_bytes": check["db_bytes"],
                "largest_table": check["largest_table"],
                "largest_bytes": check["largest_bytes"],
            }
        return self._execute(
            "retention_threshold",
            lambda: self.retention_service.run("threshold"),
        )

    def run_forecast_archive_once(self):
        """到期不结算的自动归档（每日墙钟一次，**默认关闭**）。

        开关存在 `runtime_state` 的 `settings.forecast_archive`，缺省 `enabled=False`。
        关闭时本方法在一行都不写、一次查询都不做的意义上"不存在自动结算路径"——
        这一点有测试守着（把时钟推后 400 天，`resolutions` 计数不变）。

        为什么走"每日墙钟"而不是每次循环：这是归档，不是监控。一天一次足够，
        而批量结算会在账本上写不可撤销的行，跑得越勤越容易在用户还没反应过来的
        时候把一大片命题判成"无法判定"。
        """
        if self.paused:
            return {"status": "paused"}
        if self.forecasts is None:
            return {"status": "disabled"}
        from .system_settings import read_forecast_archive_setting

        setting = read_forecast_archive_setting(self.database)
        if not setting["enabled"]:
            return {"status": "disabled"}
        # 本地凌晨 3 点之后补跑一次；跨睡眠/跨天仍按日历补跑（与备份/清理同款）。
        if not self._daily_due("forecast_archive", 3):
            return {"status": "skipped"}
        return self._execute(
            "forecast_archive", lambda: self.forecasts.auto_archive_overdue(self.now())
        )

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
        next_situation = 0.0
        next_cognition = 0.0
        next_trends = 0.0
        next_learning = 0.0
        next_daily = 0.0
        next_retention_check = 0.0

        def following(interval):
            # 下次到期从「任务真正结束的时刻」起算，而不是从本轮循环开始时的
            # 时间戳起算。否则任务一旦跑得比间隔还久，deadline 会落在过去，
            # 循环会立刻连续补跑同一任务，把一次超时放大成一阵冲击。
            return time.monotonic() + interval

        while not self._stop.is_set():
            current = time.monotonic()
            # 态势任务**必须排在采集之前**（2026-09-21 修复）。
            #
            # 故障现场：冷启动时所有常规源都已过期（隔夜再开），`refresh_due_sources`
            # 会在**一次调用里**按 source_id 顺序补抓三十多个源 —— 实测 15~30 分钟
            # （真机现场：07:04 起，逐个源按字母序 refresh，到 07:21 还没走完）。
            # 循环是单线程、任务按书写顺序串行，态势块原来排在采集块之后，于是整个
            # 补抓窗口里它一次都轮不到：`task.situation` 从无记录、三个 geojson 源
            # 永远停在 last_status='never'、situation_events 恒为 0，地图页整片空白
            # （"0 / 0 个事件"）。注意这不是抓取失败 —— 全程没有任何异常，所以日志
            # 里干干净净，`_execute` 的吞异常也不是本因；**它是被前面的长任务饿死**。
            #
            # 把态势检查提到采集之前：冷启动时它先跑完（三个源，秒级），地图立刻有
            # 数据，再去补抓新闻。态势源自身 refresh_minutes 是 20~30 分钟，这里
            # 5 分钟查一次是否到期即可（是否真的抓由 next_fetch_at 决定），不额外
            # 增加外网压力。
            if current >= next_situation:
                self.run_situation_once()
                next_situation = following(300)
            if current >= next_external:
                self.run_external_once()
                next_external = following(self.poll_seconds)
            if self.cognition is not None and current >= next_cognition:
                self.run_cognition_once()
                # 认知扫描间隔为5分钟（300秒），远程调用频率由 cognition._remote_due() 严格控制
                # 避免每分钟扫描导致频繁入队和无效API调用
                next_cognition = following(300)
            if self.cognition is not None and current >= next_trends:
                self.run_trends_once()
                next_trends = following(3600)
            if current >= next_learning:
                self.run_learning_once()
                next_learning = following(6 * 3600)
            if current >= next_daily:
                # 备份/清理/到期归档只做"到期与否"检查，真正执行由墙钟判断（R6）。
                self.run_backup_once()
                self.run_retention_once()
                self.run_forecast_archive_once()
                next_daily = following(300)
            if current >= next_retention_check:
                # 体积阈值检查：只读判断很便宜，但"任一表多大"要查 dbstat，
                # 在近 900MB 的库上单次约 1.8 秒，所以按小时而不是按分钟做。
                self.run_retention_if_threshold()
                next_retention_check = following(3600)
            waits = [next_external - time.monotonic(), next_situation - time.monotonic()]
            if self.cognition is not None:
                waits.extend(
                    [next_cognition - time.monotonic(), next_trends - time.monotonic()]
                )
            waits.append(next_learning - time.monotonic())
            waits.append(next_daily - time.monotonic())
            waits.append(next_retention_check - time.monotonic())
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
            self._thread = None
