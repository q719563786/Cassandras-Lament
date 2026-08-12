import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from .config import AppPaths
from .cognition import CognitionController, CognitionService
from .database import Database
from .desktop import DesktopBridge, DesktopUnavailable, PyWebViewDesktop
from .forecasts import ForecastService
from .external_radar import ExternalRadarService
from .http_api import Services, create_server
from .interests import InterestService
from .knowledge import KnowledgeService
from .impacts import ImpactService
from .judgments import LocalHeuristicProvider, build_public_bundle
from .notifications import NotificationService
from .operations import CognitionOperation
from .remote_ai import AiSettingsService, JudgmentQueue
from .secret_store import DpapiSecretStore
from .signals import SignalService
from .radar_scheduler import RadarScheduler
from .runtime import RuntimeClient, RuntimeDiscovery, SingleInstance
from .startup import StartupTask
from .trends import TrendService


@dataclass
class Application:
    server: object
    session_token: str
    external: object
    scheduler: object
    desktop: object

    @classmethod
    def create(cls, data_root, desktop=None, legacy_path=None):
        """Build an application without starting its blocking serve loop."""
        root = Path(data_root)
        database = Database(root / "data" / "yuanjian.db")
        if legacy_path is not None and Path(legacy_path).is_file() and not database.path.exists():
            database.import_legacy(legacy_path)
        else:
            database.initialize()
        session_token = secrets.token_urlsafe(32)
        interests = InterestService(database)
        interests.ensure_defaults()
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
            bundle_loader=lambda cluster_id: build_public_bundle(
                cognition.get_cluster(cluster_id),
                cognition.get_cluster(cluster_id)["items"],
            ),
            local_provider=local_provider,
        )
        forecasts = ForecastService(database)
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
        scheduler = RadarScheduler(
            external,
            database=database,
            cognition=controller,
            cognition_operation=cognition_operation,
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
        )

    @property
    def url(self):
        """Return the tokenized local URL opened for this process only."""
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/?token={self.session_token}"

    def run(self, hidden=False, headless=False):
        """Run the loopback server and either the desktop or explicit smoke shell."""
        self.scheduler.start()
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
            self.server.shutdown()
            server_thread.join(timeout=5)
            self.scheduler.stop()
            self.server.server_close()
        return 0

    def close(self):
        """Close a server that has not entered or has left its serve loop."""
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


def run_application(argv=None):
    """Resolve private paths and run the local application."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ)
    data_dir = data_dir_from_arguments(arguments)
    if data_dir:
        environment["YUANJIAN_DATA_DIR"] = data_dir
    paths = AppPaths.from_environment(environment)
    paths.ensure_directories()
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
        _show_desktop_error(
            "远见无法启动桌面窗口，请安装或修复 Microsoft Edge WebView2 Runtime"
        )
        return 1
    finally:
        discovery.clear()
        instance.release()


if __name__ == "__main__":
    raise SystemExit(run_application())
