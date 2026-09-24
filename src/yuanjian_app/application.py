import logging
import logging.handlers
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from .config import AppPaths
from .backup import BackupService
from .cognition import CognitionController, CognitionService
from .database import Database
from .desktop import DesktopBridge, DesktopUnavailable, PyWebViewDesktop
from .diagnostics import DiagnosticsService
from .forecasts import ForecastService, parse_frontmatter
from .external_radar import ExternalRadarService
from .http_api import Services, create_server
from .interests import InterestService
from .knowledge import KnowledgeService
from .impacts import ImpactService
from .judgments import LocalHeuristicProvider, build_public_bundle
from .mobile_export import MobileExportService
from .notifications import NotificationService
from .operations import CognitionOperation
from .remote_ai import AiSettingsService, JudgmentQueue
from .retention import RetentionService
from .secret_store import DpapiSecretStore
from .signals import SignalService
from .radar_scheduler import RadarScheduler
from .runtime import RuntimeClient, RuntimeDiscovery, SingleInstance
from .startup import StartupTask
from .system_settings import SystemSettingsService
from .trends import TrendService
from .update_check import UpdateCheckService


def _is_exportable_privacy(level):
    """隐私级别是否允许进入**任何可能离开本机**的结构。

    白名单式判断：只有明确标成 P2 / P3 的才放行。P1 永不外发；级别缺失或不可
    识别时**保守视为 P1**。写成 `level != "P1"` 是黑名单思路，漏掉 None / 空串
    / 大小写变体就是一个外泄口。
    """
    return str(level or "").strip().upper() in {"P2", "P3"}


def _forecast_privacy_levels(forecasts):
    """一次查询取出 {forecast_id: privacy_level}，避免逐条 get_forecast 的 N+1。

    `list_forecasts()` 的摘要里**不含** privacy_level（它写在版本的 content
    frontmatter 里），所以这里必须自己解析，不能用现成摘要。
    """
    levels = {}
    try:
        with forecasts.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.forecast_id, v.content
                FROM forecasts f
                JOIN forecast_versions v ON v.forecast_id = f.forecast_id
                JOIN (
                    SELECT forecast_id, MAX(version) AS latest FROM forecast_versions
                    GROUP BY forecast_id
                ) l ON l.forecast_id = v.forecast_id AND l.latest = v.version
                """
            ).fetchall()
    except Exception:
        return levels
    for row in rows:
        try:
            levels[row["forecast_id"]] = parse_frontmatter(row["content"]).get(
                "privacy_level"
            )
        except Exception:
            levels[row["forecast_id"]] = None
    return levels


def _load_self_reported_signals(interests, limit=12):
    """读取用户主动记录的个人近况（「告诉远见」的输入）。

    **local-only，任何外发结构都不得包含本函数的返回值。**

    这是库里最直接的个人画像（用户亲口说的工作、收入、健康、家庭处境），
    而外部 AI 在本设计里只该看到公开证据包。P0-1 之后它已从
    `_make_personal_context_loader()` 的返回结构中移除；将来若做「本机模型
    个性化」，请直接调用本函数，不要绕回远程队列。
    """
    entries = []
    try:
        with interests.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT summary, why_it_matters, received_at FROM signals
                WHERE source_type='manual'
                  AND IFNULL(status,'new')!='dismissed'
                ORDER BY received_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
    except Exception:
        return entries
    for row in rows:
        entry = {"recorded_at": str(row["received_at"])[:19], "situation": row["summary"]}
        if row["why_it_matters"]:
            entry["relevance"] = row["why_it_matters"]
        entries.append(entry)
    return entries


def _make_personal_context_loader(interests, forecasts):
    """构建「个人上下文」加载器：利益地图 + 近期预测。

    ⚠ **当前没有任何远程路径会调用它。** P0-1（2026-09-15）之后远程研判只发送
    `build_public_bundle()` 的公开证据包（`PRIVACY.md` 的承诺），`JudgmentQueue`
    还在出口处硬剥离 `personal_context`。保留下这个函数是为了将来可能的
    「本机模型做个性化」路径复用。

    **谁要复用它，必须先满足下面两条**，否则就是重新把个人画像推给外部：

    1. **P1 一律不出本机**：返回结构里的利益对象与预测都已按 `privacy_level`
       过滤（白名单，只放行 P2/P3；级别缺失保守当作 P1）。历史缺陷正是这里
       完全不看 `privacy_level`。
    2. **`manual` signals 永不外发**：用户主动记录的个人近况已整段移出本函数，
       改由 `_load_self_reported_signals()` 提供，那个函数是 local-only 的。

    返回 dict；全部内容都被隐私过滤掉时返回 None，调用方应跳过注入。
    """
    def loader(cluster_id):
        all_objects = interests.list_objects()
        exportable = [
            o for o in all_objects if _is_exportable_privacy(o.get("privacy_level"))
        ]
        allowed_ids = {o["object_id"] for o in exportable}
        id_to_name = {o["object_id"]: o["name"] for o in all_objects}
        # 两端都必须是可外发对象才保留这条关系，否则名字虽隐，关系本身仍会
        # 暴露一个 P1 对象的存在与结构。列表上限放在过滤**之后**，避免被
        # 前 20 条 P1 关系把有效内容挤空。
        resolved_links = [
            {
                "source": id_to_name.get(l["source_id"], l["source_id"]),
                "target": id_to_name.get(l["target_id"], l["target_id"]),
                "relationship": l["relationship"],
                "impact": l["impact_direction"],
                "strength": l["strength"],
            }
            for l in interests.list_links()
            if l["source_id"] in allowed_ids and l["target_id"] in allowed_ids
        ][:20]

        levels = _forecast_privacy_levels(forecasts)
        recent = [
            f
            for f in forecasts.list_forecasts()[0]
            if _is_exportable_privacy(levels.get(f["forecast_id"]))
        ][:5]

        if not exportable and not resolved_links and not recent:
            # 全被隐私过滤掉，等于没有可外发的个人上下文。
            return None
        return {
            "interests": {
                "objects": [
                    {
                        "name": o["name"],
                        "category": o["category"],
                        "importance": o["importance"],
                    }
                    for o in exportable[:20]
                ],
                "links": resolved_links,
            },
            "recent_forecasts": [
                {
                    "title": f["title"],
                    "category": f["category"],
                    "probability": f["probability"],
                    "status": f["status"],
                    "alert_level": f["alert_level"],
                    "window_end": f["window_end"],
                }
                for f in recent
            ],
        }

    return loader


@dataclass
class Application:
    server: object
    session_token: str
    external: object
    scheduler: object
    desktop: object
    queue: object = None

    @classmethod
    def create(cls, data_root, desktop=None, legacy_path=None):
        """Build an application without starting its blocking serve loop."""
        root = Path(data_root)
        # 传入备份目录：v7 的存量定级回填会改写 `personal_impacts`，迁移机制要求
        # "先落一份备份再动数据"，备份落点就是项目一贯的 `<数据根>/backups`。
        database = Database(root / "data" / "yuanjian.db", backup_dir=root / "backups")
        if legacy_path is not None and Path(legacy_path).is_file() and not database.path.exists():
            database.import_legacy(legacy_path)
        else:
            database.initialize()
        session_token = secrets.token_urlsafe(32)
        interests = InterestService(database)
        interests.ensure_defaults()
        forecasts = ForecastService(database)
        signals = SignalService(database, interests)
        knowledge = KnowledgeService(database)
        cognition = CognitionService(database)
        external = ExternalRadarService(
            database, on_item_stored=cognition.process_item
        )
        external.ensure_public_defaults()
        trends = TrendService(database)
        local_provider = LocalHeuristicProvider()
        ai_settings = AiSettingsService(
            database, DpapiSecretStore(root / "secrets" / "ai-token.dpapi")
        )
        queue = JudgmentQueue(
            database,
            providers={"local": local_provider},
            bundle_loader=lambda cluster_id: (
                lambda cluster: build_public_bundle(cluster, cluster["items"])
            )(cognition.get_cluster(cluster_id)),
            local_provider=local_provider,
            personal_context_loader=_make_personal_context_loader(interests, forecasts),
        )
        # 熔断状态是**进程内**的，重启就归零；不把它从库里捡回来，上一轮冻住的
        # 那批 `paused_auth` 作业就永远解不了冻（真库 2026-09-23 一路堆到 256 条）。
        # 捡回来只是恢复"熔断打开"这个事实 + 放一条探测作业，不会立刻重发请求。
        try:
            queue.rehydrate_circuit_state()
        except Exception:
            logging.getLogger(__name__).warning(
                "启动时恢复熔断遗留状态失败（冻结作业会继续留在队列里，不影响启动）",
                exc_info=True,
            )
        impacts = ImpactService(database, interests, forecasts)
        notifications = NotificationService(database)
        controller = CognitionController(
            database,
            cognition,
            trends,
            queue,
            impacts,
            notifications,
            ai_settings,
        )
        cognition_operation = CognitionOperation(controller)
        backup_service = BackupService(database, root / "backups")
        retention_service = RetentionService(database)
        system_settings = SystemSettingsService(database)
        diagnostics = DiagnosticsService(
            database,
            external=external,
            ai_settings=ai_settings,
            judgment_queue=queue,
            backup_service=backup_service,
            trends=trends,
        )
        mobile_export = MobileExportService(root / "mobile")
        scheduler = RadarScheduler(
            external,
            database=database,
            cognition=controller,
            cognition_operation=cognition_operation,
            backup_service=backup_service,
            retention_service=retention_service,
            learning_callback=controller.apply_feedback_learning,
            forecasts=forecasts,
        )
        startup = (
            StartupTask(executable=Path(sys.executable))
            if getattr(sys, "frozen", False)
            else None
        )
        desktop_bridge = DesktopBridge()
        server = create_server(
            "127.0.0.1",
            0,
            session_token,
            Services(
                forecasts,
                interests,
                signals,
                knowledge,
                external,
                cognition,
                trends,
                controller,
                notifications,
                impacts,
                startup,
                ai_settings,
                cognition_operation,
                desktop_bridge,
                system_settings=system_settings,
                diagnostics=diagnostics,
                backup_service=backup_service,
                retention_service=retention_service,
                mobile_export=mobile_export,
                scheduler=scheduler,
                update_check=UpdateCheckService(),
            ),
        )
        if desktop is None:
            desktop = PyWebViewDesktop(
                monitor=scheduler,
                run_cognition=lambda: cognition_operation.run("tray"),
                request_shutdown=server.shutdown,
            )
        desktop_bridge.bind(desktop)
        return cls(
            server=server,
            session_token=session_token,
            external=external,
            scheduler=scheduler,
            desktop=desktop,
            queue=queue,
        )

    @property
    def url(self):
        """Return the tokenized local URL opened for this process only."""
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/?token={self.session_token}"

    def run(self, hidden=False, headless=False):
        """Run the loopback server and either the desktop or explicit smoke shell."""
        self.scheduler.start()
        # 启动时清理v1.0自动确认产生的垃圾预测（后台线程，不阻塞启动）
        def _purge_garbage_startup():
            try:
                purged = self.scheduler.cognition.impacts.purge_garbage_forecasts()
                if purged:
                    import logging
                    logging.getLogger(__name__).info("清理了 %d 条自动确认垃圾预测", purged)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("启动时清理垃圾预测失败", exc_info=True)
        threading.Thread(target=_purge_garbage_startup, name="YuanJianPurge", daemon=True).start()
        server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="YuanJianHttp",
            daemon=True,
        )
        server_thread.start()
        try:
            if headless:
                server_thread.join()
            else:
                self.desktop.run(self.url, hidden=hidden)
        except KeyboardInterrupt:
            return 0
        finally:
            # 关键：先立即停止任务队列，清空所有待处理远程任务，防止退出后继续调用API
            if self.queue:
                self.queue.shutdown()
            # 停止调度器，不再发起新的认知扫描
            self.scheduler.stop(timeout=3)
            # 关闭HTTP服务器
            self.server.shutdown()
            server_thread.join(timeout=3)
            self.server.server_close()
        return 0

    def close(self):
        """Close a server that has not entered or has left its serve loop."""
        if self.queue:
            self.queue.shutdown()
        self.scheduler.stop()
        self.server.server_close()


def is_headless_mode(env):
    """Allow a non-GUI process only for the packaged smoke harness."""
    return env.get("YUANJIAN_HEADLESS") == "1"


def is_background_mode(argv, env):
    return "--background" in set(argv or ()) or env.get("YUANJIAN_BACKGROUND") == "1"


def data_dir_from_arguments(argv):
    arguments = list(argv or ())
    if "--data-dir" not in arguments:
        return None
    index = arguments.index("--data-dir")
    if index + 1 >= len(arguments) or arguments[index + 1].startswith("--"):
        raise ValueError("--data-dir requires a directory path")
    return arguments[index + 1]


def _show_desktop_error(message):
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "远见", 0x10)
    else:
        print(message, file=sys.stderr)


def configure_logging(logs_dir, level=logging.INFO):
    """把日志落到数据目录下的 logs/，并返回所用的 handler。

    打包后的程序是 `console=False`，没有控制台；此前 `logging` 调用没有配置
    任何 handler，等于写进虚空。应用内的诊断中心与 runtime_state 能覆盖业务
    层错误，但进程启动失败、GUI 层异常这些没有现场。

    用 RotatingFileHandler 限制体积：这是长期常驻的程序，日志不能无限增长。
    日志目录位于 %LOCALAPPDATA%\\YuanJian 之下，不进仓库也不外发。
    """
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(logs_dir / "yuanjian.log"),
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    # 重复调用不应叠加 handler（测试与重入都会走到这里），旧的要关掉，
    # 否则会漏文件句柄。
    for existing in list(root.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    return handler


def run_application(argv=None):
    """Resolve private paths and run the local application."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ)
    data_dir = data_dir_from_arguments(arguments)
    if data_dir:
        environment["YUANJIAN_DATA_DIR"] = data_dir
    paths = AppPaths.from_environment(environment)
    paths.ensure_directories()
    configure_logging(paths.root / "logs")
    logging.getLogger(__name__).info("远见启动，数据目录 %s", paths.root)
    legacy = environment.get("YUANJIAN_LEGACY_DB")
    background = is_background_mode(arguments, environment)
    headless = is_headless_mode(environment)
    runtime_root = paths.root / "runtime"
    instance = SingleInstance(runtime_root / "yuanjian.lock")
    discovery = RuntimeDiscovery(runtime_root / "runtime.json")
    if not instance.acquire():
        existing = discovery.read_valid()
        if existing:
            RuntimeClient(existing).show_window()
        return 0
    try:
        application = Application.create(paths.root, legacy_path=legacy)
        discovery.publish(
            os.getpid(),
            application.server.server_address[1],
            application.session_token,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        return application.run(hidden=background, headless=headless)
    except DesktopUnavailable:
        logging.getLogger(__name__).error("桌面窗口不可用（WebView2 缺失或损坏）", exc_info=True)
        _show_desktop_error(
            "远见无法启动桌面窗口，请安装或修复 Microsoft Edge WebView2 Runtime"
        )
        return 1
    finally:
        discovery.clear()
        instance.release()


if __name__ == "__main__":
    raise SystemExit(run_application())
