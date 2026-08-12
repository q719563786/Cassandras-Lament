import tempfile
import unittest
from pathlib import Path

from yuanjian_app.application import (
    Application,
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


class RecordingScheduler:
    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


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

    def test_background_launch_starts_desktop_hidden(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            desktop = RecordingDesktop()
            app = Application.create(Path(temp_dir), desktop=desktop, legacy_path=None)
            app.scheduler = RecordingScheduler()

            app.run(hidden=True)

            self.assertTrue(desktop.run_calls[0]["hidden"])


if __name__ == "__main__":
    unittest.main()
