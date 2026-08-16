"""Desktop lifecycle rules that do not depend on GUI libraries."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any


class DesktopUnavailable(RuntimeError):
    """Raised when the native Windows desktop shell cannot be initialized."""


class DesktopBridge:
    """Break the HTTP-server/desktop construction cycle without global state."""

    def __init__(self) -> None:
        self._target = None

    def bind(self, target: Any) -> None:
        self._target = target

    def _bound(self) -> Any:
        if self._target is None:
            raise RuntimeError("桌面窗口尚未就绪")
        return self._target

    def show_window(self) -> None:
        self._bound().show_window()

    def toggle_monitoring(self) -> bool:
        return self._bound().toggle_monitoring()

    def request_exit(self) -> None:
        self._bound().request_exit()


class DesktopLifecycle:
    """Coordinate one window, one tray icon, and safe application shutdown."""

    def __init__(
        self,
        window: Any,
        tray: Any,
        monitor: Any,
        run_cognition: Callable[[], dict],
        request_shutdown: Callable[[], None],
    ) -> None:
        self.window = window
        self.tray = tray
        self.monitor = monitor
        self._run_cognition = run_cognition
        self._request_shutdown = request_shutdown
        self.exiting = False

    def close_to_tray(self) -> bool:
        """Closing the window exits the app — non-technical users expect
        closing the window to stop the process. The previous hide-to-tray
        behavior left the loopback server and port listener alive,
        surprising users who closed the window assuming the app had quit,
        and it locked dist dlls on the next build (PermissionError on
        ClrLoader.dll). The tray icon's '退出远见' item still routes
        through request_exit for users who prefer the tray.
        """
        if self.exiting:
            return True
        self.request_exit()
        return False  # request_exit already destroyed the window

    def show_window(self) -> None:
        self.window.show()
        self.window.restore()

    def run_cognition_once(self) -> dict:
        return self._run_cognition()

    def toggle_monitoring(self) -> bool:
        """Toggle automatic monitoring and return whether it is now running."""
        if self.monitor.paused:
            self.monitor.resume()
            return True
        self.monitor.pause()
        return False

    def request_exit(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        try:
            self.tray.stop()
        finally:
            try:
                self._request_shutdown()
            finally:
                self.window.destroy()


def _load_gui_modules():
    import pystray
    import webview
    from PIL import Image, ImageDraw

    return webview, pystray, Image, ImageDraw


def _create_tray_image(image_module, draw_module):
    image = image_module.new("RGBA", (64, 64), (15, 23, 42, 255))
    draw = draw_module.Draw(image)
    draw.rounded_rectangle(
        (8, 8, 56, 56), radius=13, fill=(37, 99, 235, 255)
    )
    draw.line((18, 42, 29, 29, 37, 36, 48, 20), fill="white", width=6)
    return image


class PyWebViewDesktop:
    """Concrete pywebview window and pystray integration for Windows."""

    def __init__(
        self,
        monitor: Any,
        run_cognition: Callable[[], dict],
        request_shutdown: Callable[[], None],
        gui_loader: Callable[[], tuple] = _load_gui_modules,
    ) -> None:
        self.monitor = monitor
        self._run_cognition = run_cognition
        self._request_shutdown = request_shutdown
        self._gui_loader = gui_loader
        self.lifecycle = None

    def run(self, url: str, hidden: bool = False) -> None:
        tray = None
        try:
            webview, pystray, image_module, draw_module = self._gui_loader()
            window = webview.create_window(
                title="远见 · 外部认知大脑",
                url=url,
                width=1180,
                height=780,
                min_size=(900, 620),
                hidden=bool(hidden),
            )

            def show_window(*_args):
                self.show_window()

            def run_cognition(*_args):
                threading.Thread(
                    target=self._run_cognition,
                    name="YuanJianManualCognition",
                    daemon=True,
                ).start()

            def toggle_monitoring(*_args):
                self.toggle_monitoring()

            def request_exit(*_args):
                self.request_exit()

            menu = pystray.Menu(
                pystray.MenuItem("显示远见", show_window, default=True),
                pystray.MenuItem("立即运行认知", run_cognition),
                pystray.MenuItem("暂停 / 恢复后台监控", toggle_monitoring),
                pystray.MenuItem("退出远见", request_exit),
            )
            tray = pystray.Icon(
                "YuanJian",
                _create_tray_image(image_module, draw_module),
                "远见 · 后台监控中",
                menu,
            )
            self.lifecycle = DesktopLifecycle(
                window=window,
                tray=tray,
                monitor=self.monitor,
                run_cognition=self._run_cognition,
                request_shutdown=self._request_shutdown,
            )
            window.events.closing += self.lifecycle.close_to_tray
            tray.run_detached()
            webview.start(gui="edgechromium")
        except DesktopUnavailable:
            raise
        except Exception as error:
            raise DesktopUnavailable(str(error)) from error
        finally:
            if tray is not None and not (
                self.lifecycle is not None and self.lifecycle.exiting
            ):
                tray.stop()

    def show_window(self) -> None:
        if self.lifecycle is None:
            raise DesktopUnavailable("桌面窗口尚未就绪")
        self.lifecycle.show_window()

    def toggle_monitoring(self) -> bool:
        if self.lifecycle is None:
            raise DesktopUnavailable("桌面窗口尚未就绪")
        return self.lifecycle.toggle_monitoring()

    def request_exit(self) -> None:
        if self.lifecycle is None:
            self._request_shutdown()
            return
        self.lifecycle.request_exit()
