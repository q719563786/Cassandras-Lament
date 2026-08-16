import unittest

from yuanjian_app.desktop import DesktopBridge, DesktopLifecycle, PyWebViewDesktop


class RecordingWindow:
    def __init__(self):
        self.calls = []

    def hide(self):
        self.calls.append("hide")

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def destroy(self):
        self.calls.append("destroy")


class RecordingTray:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")


class RecordingMonitor:
    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class EventHook:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class GuiWindow(RecordingWindow):
    def __init__(self):
        super().__init__()
        self.events = type("Events", (), {"closing": EventHook()})()


class FakeWebView:
    def __init__(self):
        self.window = GuiWindow()
        self.create_calls = []
        self.start_calls = []

    def create_window(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.window

    def start(self, **kwargs):
        self.start_calls.append(kwargs)


class GuiTray(RecordingTray):
    def __init__(self, name, title, image, menu):
        super().__init__()
        self.name = name
        self.title = title
        self.image = image
        self.menu = menu

    def run_detached(self):
        self.calls.append("run_detached")


class FakePystray:
    Icon = GuiTray

    class MenuItem:
        def __init__(self, text, action, **kwargs):
            self.text = text
            self.action = action
            self.kwargs = kwargs

    class Menu:
        def __init__(self, *items):
            self.items = items


class FakeImage:
    @staticmethod
    def new(mode, size, color):
        return {"mode": mode, "size": size, "color": color}


class FakeDraw:
    def __init__(self, image):
        self.image = image

    def rounded_rectangle(self, *args, **kwargs):
        return None

    def line(self, *args, **kwargs):
        return None


class FakeImageDraw:
    Draw = FakeDraw


class DesktopLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.window = RecordingWindow()
        self.tray = RecordingTray()
        self.monitor = RecordingMonitor()
        self.shutdown_calls = []
        self.cognition_calls = []
        self.lifecycle = DesktopLifecycle(
            window=self.window,
            tray=self.tray,
            monitor=self.monitor,
            run_cognition=lambda: self.cognition_calls.append("run") or {"status": "ok"},
            request_shutdown=lambda: self.shutdown_calls.append("shutdown"),
        )

    def test_close_exits_app_instead_of_hiding_to_tray(self):
        """Regression: closing the window must trigger safe shutdown, not
        hide to tray. Non-technical users closing the window expected the
        process to exit; the previous hide-to-tray behavior left the
        loopback server and port listener alive, surprising users and
        locking dist dlls on the next build (PermissionError on
        ClrLoader.dll). The tray '退出远见' item still routes through
        request_exit for tray-first users.
        """
        self.assertFalse(self.lifecycle.close_to_tray())

        self.assertEqual(self.tray.calls, ["stop"])
        self.assertEqual(self.shutdown_calls, ["shutdown"])
        self.assertEqual(self.window.calls, ["destroy"])

    def test_show_window_restores_the_existing_window(self):
        self.lifecycle.show_window()

        self.assertEqual(self.window.calls, ["show", "restore"])

    def test_tray_exit_stops_tray_and_requests_safe_shutdown_once(self):
        self.lifecycle.request_exit()
        self.lifecycle.request_exit()

        self.assertEqual(self.tray.calls, ["stop"])
        self.assertEqual(self.shutdown_calls, ["shutdown"])
        self.assertEqual(self.window.calls, ["destroy"])

    def test_close_allows_window_destruction_after_exit_begins(self):
        self.lifecycle.request_exit()

        self.assertTrue(self.lifecycle.close_to_tray())
        self.assertEqual(self.window.calls, ["destroy"])

    def test_toggle_monitoring_returns_new_running_state(self):
        self.assertFalse(self.lifecycle.toggle_monitoring())
        self.assertTrue(self.monitor.paused)
        self.assertTrue(self.lifecycle.toggle_monitoring())
        self.assertFalse(self.monitor.paused)

    def test_manual_cognition_uses_the_shared_callback(self):
        self.assertEqual(self.lifecycle.run_cognition_once(), {"status": "ok"})
        self.assertEqual(self.cognition_calls, ["run"])


class DesktopBridgeTests(unittest.TestCase):
    def test_bridge_forwards_controls_to_the_bound_desktop(self):
        target = type(
            "Target",
            (),
            {
                "show_window": lambda self: setattr(self, "shown", True),
                "toggle_monitoring": lambda self: False,
                "request_exit": lambda self: setattr(self, "exited", True),
            },
        )()
        bridge = DesktopBridge()
        bridge.bind(target)

        bridge.show_window()
        self.assertFalse(bridge.toggle_monitoring())
        bridge.request_exit()

        self.assertTrue(target.shown)
        self.assertTrue(target.exited)


class PyWebViewDesktopTests(unittest.TestCase):
    def test_run_creates_edge_window_and_tray_without_browser_fallback(self):
        webview = FakeWebView()
        monitor = RecordingMonitor()
        shell = PyWebViewDesktop(
            monitor=monitor,
            run_cognition=lambda: {"status": "ok"},
            request_shutdown=lambda: None,
            gui_loader=lambda: (webview, FakePystray, FakeImage, FakeImageDraw),
        )

        shell.run("http://127.0.0.1:4567/?token=x", hidden=True)

        created = webview.create_calls[0]
        self.assertEqual(created["title"], "远见 · 外部认知大脑")
        self.assertEqual(created["url"], "http://127.0.0.1:4567/?token=x")
        self.assertEqual((created["width"], created["height"]), (1180, 780))
        self.assertEqual(created["min_size"], (900, 620))
        self.assertTrue(created["hidden"])
        self.assertEqual(webview.start_calls, [{"gui": "edgechromium"}])
        self.assertIn("run_detached", shell.lifecycle.tray.calls)

        self.assertFalse(webview.window.events.closing.handlers[0]())
        self.assertEqual(webview.window.calls, ["destroy"])


if __name__ == "__main__":
    unittest.main()
