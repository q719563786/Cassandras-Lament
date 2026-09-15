import logging
import logging.handlers
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from yuanjian_app.application import (
    Application,
    configure_logging,
    data_dir_from_arguments,
    is_background_mode,
    is_headless_mode,
)
from yuanjian_app.http_api import resolve_static_root


class RecordingDesktop:
    def __init__(self):
        self.run_calls = []

    def run(self, url, hidden=False):
        self.run_calls.append({"url": url, "hidden": hidden})


class RecordingPurge:
    """`impacts.purge_garbage_forecasts()` 的替身，记录启动清理有没有真的被调用。

    加这个替身的原因：`application.py:328` 的启动清理会调
    `self.scheduler.cognition.impacts.purge_garbage_forecasts()`，而这条调用在
    `run()` 里被 `except Exception` + 一行 warning 兜着。原来的 `RecordingScheduler`
    没有 `cognition` 属性，于是**每次全量/覆盖率运行都会静默吞掉一次 AttributeError**：
    测试全绿、日志里留一行"启动时清理垃圾预测失败"，而启动清理这条路径实际上从来没
    被执行过，在覆盖率里永远显示未覆盖。

    把替身补成与真实依赖图同形之后，"有没有被调用"变成一条可断言的可见事实。
    """

    def __init__(self):
        self.calls = 0

    def purge_garbage_forecasts(self):
        self.calls += 1
        return 0


class RecordingScheduler:
    def __init__(self):
        self.running = False
        self.impacts = RecordingPurge()
        self.cognition = SimpleNamespace(impacts=self.impacts)

    def start(self):
        self.running = True

    def stop(self, timeout=5):
        self.running = False

    def wait_for_startup_purge(self, timeout=3.0):
        """启动清理跑在 `run()` 开的后台线程里，等它有界地跑完（不无限轮询）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.impacts.calls:
                return True
            time.sleep(0.005)
        return bool(self.impacts.calls)


class ApplicationTests(unittest.TestCase):
    def test_headless_environment_is_reserved_for_automated_smoke_tests(self):
        self.assertFalse(is_headless_mode({}))
        self.assertTrue(is_headless_mode({"YUANJIAN_HEADLESS": "1"}))

    def test_background_argument_or_environment_starts_hidden(self):
        self.assertTrue(is_background_mode(["--background"], {}))
        self.assertTrue(is_background_mode([], {"YUANJIAN_BACKGROUND": "1"}))
        self.assertFalse(is_background_mode([], {}))

    def test_data_directory_argument_is_optional_and_requires_a_value(self):
        self.assertIsNone(data_dir_from_arguments([]))
        self.assertEqual(
            data_dir_from_arguments(["--data-dir", "C:/temp/yuanjian-v09"]),
            "C:/temp/yuanjian-v09",
        )
        with self.assertRaisesRegex(ValueError, "--data-dir"):
            data_dir_from_arguments(["--data-dir"])

    def test_static_root_supports_source_and_frozen_layouts(self):
        module_file = Path("C:/project/yuanjian_app/http_api.py")
        self.assertEqual(resolve_static_root(module_file), module_file.parent / "static")
        self.assertEqual(
            resolve_static_root(module_file, Path("C:/bundle")),
            Path("C:/bundle/yuanjian_app/static"),
        )

    def test_application_uses_ephemeral_loopback_port_and_session_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            desktop = RecordingDesktop()
            app = Application.create(
                data_root=Path(temp_dir), desktop=desktop, legacy_path=None
            )
            try:
                self.assertEqual(app.server.server_address[0], "127.0.0.1")
                self.assertGreater(app.server.server_address[1], 0)
                self.assertGreaterEqual(len(app.session_token), 32)
                self.assertTrue((Path(temp_dir) / "data" / "yuanjian.db").is_file())
                self.assertGreaterEqual(len(app.external.list_sources()), 2)
            finally:
                app.close()
            self.assertFalse(app.scheduler.running)

    def test_normal_launch_uses_desktop_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            desktop = RecordingDesktop()
            app = Application.create(Path(temp_dir), desktop=desktop, legacy_path=None)
            app.scheduler = RecordingScheduler()
            expected_url = app.url

            app.run()

            self.assertEqual(
                desktop.run_calls, [{"url": expected_url, "hidden": False}]
            )
            self.assertFalse(app.scheduler.running)
            self.assertTrue(
                app.scheduler.wait_for_startup_purge(),
                "启动清理没有被调用 —— 这条路径要么被 except 吞了，要么替身又和真实依赖图脱节了",
            )

    def test_background_launch_starts_desktop_hidden(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            desktop = RecordingDesktop()
            app = Application.create(Path(temp_dir), desktop=desktop, legacy_path=None)
            app.scheduler = RecordingScheduler()

            app.run(hidden=True)

            self.assertTrue(desktop.run_calls[0]["hidden"])
            self.assertTrue(
                app.scheduler.wait_for_startup_purge(),
                "启动清理没有被调用 —— 这条路径要么被 except 吞了，要么替身又和真实依赖图脱节了",
            )

    def test_startup_purge_double_matches_the_real_call_chain(self):
        """替身必须和真实依赖图**同形**，而且这条要在替身上确定性地验，不靠线程时序。

        `application.py:328` 调的是 `scheduler.cognition.impacts.purge_garbage_forecasts()`。
        三层缺任何一层，这里会立刻 `AttributeError`；而在 `run()` 里同样的缺口会被
        `except Exception` 吞成一行 warning，测试照样全绿 —— 那正是这次要堵的洞。
        """
        scheduler = RecordingScheduler()

        purged = scheduler.cognition.impacts.purge_garbage_forecasts()

        self.assertEqual(purged, 0)
        self.assertEqual(scheduler.impacts.calls, 1)


class LoggingConfigurationTests(unittest.TestCase):
    """打包后 console=False，日志必须落到文件里才留得下现场。"""

    def close_rotating_handlers(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                root.removeHandler(handler)
                handler.close()

    def test_logging_writes_to_a_rotating_file_under_the_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs = Path(temp_dir) / "logs"
            try:
                handler = configure_logging(logs)
                logging.getLogger("yuanjian_app.test").warning("轮转日志验证 %s", "ok")
                handler.flush()

                self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
                written = (logs / "yuanjian.log").read_text(encoding="utf-8")
                self.assertIn("轮转日志验证 ok", written)
                self.assertIn("WARNING", written)
            finally:
                # 必须在临时目录被删除之前关掉句柄，否则 Windows 上会因文件
                # 仍被占用而删除失败。
                self.close_rotating_handlers()

    def test_repeated_configuration_does_not_stack_handlers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs = Path(temp_dir) / "logs"
            try:
                first = configure_logging(logs)
                configure_logging(logs)

                rotating = [
                    handler
                    for handler in logging.getLogger().handlers
                    if isinstance(handler, logging.handlers.RotatingFileHandler)
                ]
                self.assertEqual(len(rotating), 1, "重复配置叠加了 handler")
                # 被替换掉的旧 handler 必须已经关闭，否则每次重入都漏一个句柄。
                self.assertIsNone(first.stream)
            finally:
                self.close_rotating_handlers()


if __name__ == "__main__":
    unittest.main()
