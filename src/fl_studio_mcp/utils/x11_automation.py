"""Keyboard, mouse and window automation for FL Studio under Wine/X11.

Some pattern operations (delete, insert, move, transpose, split by channel)
have no scripting API; FL only offers fixed shortcuts and the pattern menu.
This module drives them through xdotool. Under Wine every FL dialog, popup
menu and inline name field is a top-level X window owned by the FL process,
so a sequence can be verified step by step: send a key, wait for the window
FL is expected to open, act on it, wait for it to disappear.

Linux/X11 only. On other platforms `available` is False and the reason says
why, so tools can fail honestly instead of pretending to have done something.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

# Title of FL's main window: "<project>.flp - FL Studio 2026" or "FL Studio 2026".
FL_MAIN_WINDOW_RE = r"( - |^)FL Studio [0-9]+$"

XDOTOOL_TIMEOUT = 10


@dataclass(frozen=True)
class XWindow:
    """A visible X window belonging to FL Studio."""

    id: str
    name: str
    x: int
    y: int
    width: int
    height: int


class X11Automation:
    """Thin, verifiable wrapper around xdotool for the FL Studio process."""

    def __init__(self) -> None:
        self._system = platform.system()
        self.unavailable_reason: str | None = None
        if self._system != "Linux":
            self.unavailable_reason = (
                f"keyboard/menu automation is only implemented for Linux/X11 (xdotool); "
                f"this is {self._system}"
            )
        elif not os.environ.get("DISPLAY"):
            self.unavailable_reason = (
                "DISPLAY is not set (X11 session required, Wayland is not supported)"
            )
        elif shutil.which("xdotool") is None:
            self.unavailable_reason = "xdotool is not installed"

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    # -- low level ----------------------------------------------------------

    def _run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["xdotool", *args],
            capture_output=True,
            text=True,
            timeout=XDOTOOL_TIMEOUT,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"xdotool {' '.join(args)} failed: {result.stderr.strip() or result.returncode}"
            )
        return result.stdout

    def _window_info(self, window_id: str) -> XWindow | None:
        try:
            name = self._run("getwindowname", window_id).rstrip("\n")
            geometry = self._run("getwindowgeometry", "--shell", window_id)
        except RuntimeError:
            return None
        values = dict(line.split("=", 1) for line in geometry.splitlines() if "=" in line)
        try:
            return XWindow(
                id=window_id,
                name=name,
                x=int(values["X"]),
                y=int(values["Y"]),
                width=int(values["WIDTH"]),
                height=int(values["HEIGHT"]),
            )
        except (KeyError, ValueError):
            return None

    # -- windows -------------------------------------------------------------

    def main_window(self) -> XWindow | None:
        """FL Studio's main window, or None when FL is not running/visible."""
        output = self._run(
            "search", "--onlyvisible", "--name", FL_MAIN_WINDOW_RE, check=False
        )
        ids = [line for line in output.splitlines() if line.strip()]
        if not ids:
            return None
        return self._window_info(ids[-1])

    def fl_windows(self) -> list[XWindow]:
        """All visible top-level X windows owned by the FL Studio process.

        Wine gives dialogs, popup menus and inline edit fields their own
        windows, which is what makes the automation verifiable.
        """
        main = self.main_window()
        if main is None:
            return []
        try:
            pid = self._run("getwindowpid", main.id).strip()
        except RuntimeError:
            return [main]
        if not pid:
            return [main]
        output = self._run("search", "--onlyvisible", "--pid", pid, check=False)
        windows = []
        for window_id in output.splitlines():
            window_id = window_id.strip()
            if not window_id:
                continue
            info = self._window_info(window_id)
            # Wine keeps a 1x1 helper window around; it is never a dialog.
            if info is not None and info.width > 1 and info.height > 1:
                windows.append(info)
        return windows

    def find_window(self, name_pattern: str) -> XWindow | None:
        """First visible FL window whose title matches the regex."""
        regex = re.compile(name_pattern)
        for window in self.fl_windows():
            if regex.search(window.name):
                return window
        return None

    def wait_for_window(self, name_pattern: str, timeout: float = 2.0) -> XWindow | None:
        """Poll until a window matching the title regex appears."""
        deadline = time.time() + timeout
        while True:
            window = self.find_window(name_pattern)
            if window is not None or time.time() >= deadline:
                return window
            time.sleep(0.1)

    def wait_for_new_window(
        self, known_ids: set[str], timeout: float = 2.0, min_width: int = 2
    ) -> XWindow | None:
        """Poll until an FL window appears that was not in `known_ids`.

        Popup menus have no title, so they can only be recognised as new
        windows; `min_width` filters out tiny helper windows.
        """
        deadline = time.time() + timeout
        while True:
            for window in self.fl_windows():
                if window.id not in known_ids and window.width >= min_width:
                    return window
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def wait_gone(self, window_id: str, timeout: float = 2.0) -> bool:
        """Poll until the window is no longer visible. True when it disappeared."""
        deadline = time.time() + timeout
        while True:
            if all(window.id != window_id for window in self.fl_windows()):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def window_ids(self) -> set[str]:
        return {window.id for window in self.fl_windows()}

    # -- input ---------------------------------------------------------------

    def activate(self, window: XWindow) -> None:
        self._run("windowactivate", "--sync", window.id)

    def activate_main(self) -> XWindow | None:
        """Bring FL's main window to the foreground; None if FL is not running."""
        main = self.main_window()
        if main is None:
            return None
        self.activate(main)
        time.sleep(0.2)
        return main

    def key(self, *keys: str, delay_ms: int = 80) -> None:
        """Send key names (xdotool syntax, e.g. 'shift+ctrl+Delete', 'Return')."""
        self._run("key", "--delay", str(delay_ms), *keys)

    def type_text(self, text: str, delay_ms: int = 20) -> None:
        self._run("type", "--delay", str(delay_ms), "--", text)

    def click(self, x: int, y: int, button: int = 1) -> None:
        """Click at absolute screen coordinates."""
        self._run("mousemove", str(x), str(y))
        time.sleep(0.15)
        self._run("click", str(button))

    def active_window_name(self) -> str:
        return self._run("getactivewindow", "getwindowname", check=False).rstrip("\n")


_automation: X11Automation | None = None


def get_x11_automation() -> X11Automation:
    """Global automation instance (created lazily)."""
    global _automation
    if _automation is None:
        _automation = X11Automation()
    return _automation
