"""Start a piano roll script from FL's menu (the API cannot do it).

`Ctrl+Alt+Y` is FL's *run last script again*, and it stays greyed out until a
piano roll script has been started once per FL session from the piano roll's
*Tools > Scripting* menu. Until then every piano roll tool reports "the script
did not run". This module performs that first start by driving the menu.

Measured on FL Studio 2026 (26.1.5) under Wine/X11:

* The menu triangle sits at (17, 15) inside the piano roll's **own** X window.
  That window exists only while the piano roll is detached; docked, the piano
  roll is drawn inside the main window and the triangle cannot be located.
* Clicking it opens a 185x336 popup. `Home` marks "File", `Down` twice moves to
  "Tools" and `Right` opens its submenu (434x423).
* The submenu has two columns, "Built-in" and "Scripts". `Home` marks the first
  built-in entry; `Right` jumps to the first entry of the Scripts column ("Run
  last script again"), followed by "Open script folder..." and then the
  installed scripts in alphabetical order. `Down` skips the separator.
* `Return` runs the marked entry. ComposeWithLLM runs without any UI, so a
  window appearing afterwards means a *different* script was started.

The scripts FL lists come from three directories (built-in, user settings and
downloaded ones), which is why the position of an entry can be computed but
never guessed: when the listing is ambiguous this module refuses to click.

Linux/X11 only, like `x11_automation`, which does the actual input.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from fl_studio_mcp.utils.x11_automation import X11Automation, XWindow, get_x11_automation

# The piano roll's own X window under Wine: "Piano roll -" plus the target.
PIANO_ROLL_WINDOW_RE = r"^Piano roll"

# Menu triangle, relative to the piano roll window's top-left corner.
# Override with FL_STUDIO_MCP_PR_MENU_OFFSET="x,y" if the layout differs.
DEFAULT_MENU_OFFSET = (17, 15)

# "Tools" is the third entry of the piano roll menu (Home, then Down twice).
TOOLS_ENTRY_FROM_TOP = 2

# The Scripts column starts with "Run last script again" and "Open script
# folder..." before the installed scripts.
SCRIPTS_COLUMN_HEADER_ENTRIES = 2

# FL reads piano roll scripts from a plain file or from a folder holding a
# script of the same name (that is how downloaded scripts are shipped).
SCRIPT_SUFFIX = ".pyscript"


class PRScriptMenuError(Exception):
    """The script could not be started through the menu, with the reason."""


def _menu_offset() -> tuple[int, int]:
    raw = os.environ.get("FL_STUDIO_MCP_PR_MENU_OFFSET")
    if not raw:
        return DEFAULT_MENU_OFFSET
    match = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", raw)
    if not match:
        raise PRScriptMenuError(
            f"FL_STUDIO_MCP_PR_MENU_OFFSET must look like '17,15', got {raw!r}"
        )
    return int(match.group(1)), int(match.group(2))


def _wine_prefix() -> Path:
    return Path(os.environ.get("WINEPREFIX") or Path.home() / ".wine")


def program_scripts_dir() -> Path | None:
    """Directory of the scripts FL ships, or None when it cannot be located."""
    env_dir = os.environ.get("FL_STUDIO_MCP_PROGRAM_DIR")
    if env_dir:
        candidate = Path(env_dir) / "System" / "Config" / "Piano roll scripts"
        return candidate if candidate.is_dir() else None
    program_files = _wine_prefix() / "drive_c" / "Program Files" / "Image-Line"
    candidates = sorted(program_files.glob("FL Studio */System/Config/Piano roll scripts"))
    return candidates[-1] if candidates else None


def script_dirs() -> list[Path]:
    """The directories FL collects piano roll scripts from, in no order."""
    from fl_studio_mcp.utils.midi_connection import get_fl_settings_base

    settings_base = get_fl_settings_base()
    dirs = [settings_base / "Piano roll scripts"]
    # Downloaded scripts live next to the settings tree, under Image-Line.
    if len(settings_base.parents) >= 2:
        dirs.append(settings_base.parents[1] / "Downloads" / "Piano roll scripts")
    program_dir = program_scripts_dir()
    if program_dir is not None:
        dirs.append(program_dir)
    return [d for d in dirs if d.is_dir()]


def _scripts_in(directory: Path) -> list[str]:
    """Script names FL shows for one directory.

    A plain `<name>.pyscript` is one entry, and so is a folder `<name>` that
    holds `<name>.pyscript` - that is how downloaded scripts are packaged. A
    folder that does not follow this shape would become a submenu of unknown
    contents, so it makes the listing ambiguous instead of being ignored.
    """
    names = []
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix == SCRIPT_SUFFIX:
            names.append(entry.stem)
        elif entry.is_dir():
            if (entry / f"{entry.name}{SCRIPT_SUFFIX}").is_file():
                names.append(entry.name)
            elif any(entry.glob(f"*{SCRIPT_SUFFIX}")):
                raise PRScriptMenuError(
                    f"cannot tell how FL lists the scripts in {entry}: the folder holds "
                    f"scripts but not one named {entry.name}{SCRIPT_SUFFIX}"
                )
    return names


def installed_scripts() -> list[str]:
    """Every script FL lists in its Scripts column, in the order it shows them."""
    directories = script_dirs()
    if not directories:
        raise PRScriptMenuError(
            "no piano roll script directory found; set FL_STUDIO_MCP_SETTINGS_DIR "
            "(and FL_STUDIO_MCP_PROGRAM_DIR for FL's own scripts)"
        )
    seen: dict[str, Path] = {}
    for directory in directories:
        for name in _scripts_in(directory):
            if name in seen:
                raise PRScriptMenuError(
                    f"the script {name!r} exists in both {seen[name]} and {directory}; "
                    f"FL's menu order is ambiguous, so the entry cannot be located"
                )
            seen[name] = directory
    return sorted(seen, key=str.lower)


def script_menu_index(script_name: str) -> int:
    """How many `Down` presses lead from the Scripts column's top to the script."""
    scripts = installed_scripts()
    try:
        position = scripts.index(script_name)
    except ValueError:
        raise PRScriptMenuError(
            f"{script_name} is not installed as a piano roll script; expected "
            f"{script_name}{SCRIPT_SUFFIX} in one of: "
            + ", ".join(str(d) for d in script_dirs())
        ) from None
    return SCRIPTS_COLUMN_HEADER_ENTRIES + position


class PRScriptMenu:
    """Runs a piano roll script through FL's menu and verifies that it ran."""

    def __init__(self, x11: X11Automation | None = None) -> None:
        self.x11 = x11 or get_x11_automation()

    def _piano_roll_window(self) -> XWindow:
        window = self.x11.find_window(PIANO_ROLL_WINDOW_RE)
        if window is None:
            raise PRScriptMenuError(
                "no piano roll window found. The menu can only be reached while the "
                "piano roll is open and detached (its menu > Detached); a docked piano "
                "roll has no window of its own"
            )
        return window

    def _close_menus(self, levels: int) -> None:
        """Best effort: leave no popup behind after a failed run."""
        for _ in range(levels):
            try:
                self.x11.key("Escape")
                time.sleep(0.1)
            except Exception:  # noqa: BLE001 - already failing, do not mask the cause
                return

    def _await_script(self, ran, known_ids: set[str], timeout: float) -> None:
        """Wait for proof that the script ran, or explain what happened instead."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ran():
                return
            stray = next(
                (w for w in self.x11.fl_windows() if w.id not in known_ids and w.width > 1),
                None,
            )
            if stray is not None:
                self._close_menus(2)
                raise PRScriptMenuError(
                    f"a window opened instead of the script running ({stray.name or 'untitled'}, "
                    f"{stray.width}x{stray.height}); the menu entry was probably a different "
                    f"script - nothing was confirmed, check FL Studio"
                )
            time.sleep(0.1)
        raise PRScriptMenuError(
            "the menu entry was activated but the script left no trace; "
            "run it once from the piano roll's Tools > Scripting menu"
        )

    def run_script(self, script_name: str, ran, timeout: float = 5.0) -> dict:
        """Start `script_name` from the piano roll's Tools > Scripting menu.

        `ran` is polled for proof that the script executed (the caller knows
        what to look at, e.g. the state export's mtime). Raises
        PRScriptMenuError with the reason when anything is unclear - the entry
        is only clicked when its position is known.
        """
        if not self.x11.available:
            raise PRScriptMenuError(self.x11.unavailable_reason or "X11 is unavailable")

        offset = _menu_offset()
        steps = script_menu_index(script_name)
        piano_roll = self._piano_roll_window()

        before_menus = self.x11.window_ids()
        self.x11.activate(piano_roll)
        time.sleep(0.2)
        self.x11.click(piano_roll.x + offset[0], piano_roll.y + offset[1])
        menu = self.x11.wait_for_new_window(before_menus, timeout=2.0)
        if menu is None:
            raise PRScriptMenuError(
                "the piano roll menu did not open; the triangle was expected at "
                f"{offset[0]},{offset[1]} inside the piano roll window "
                "(override with FL_STUDIO_MCP_PR_MENU_OFFSET)"
            )

        with_menu = self.x11.window_ids()
        self.x11.key("Home")
        for _ in range(TOOLS_ENTRY_FROM_TOP):
            self.x11.key("Down")
        self.x11.key("Right")
        tools = self.x11.wait_for_new_window(with_menu, timeout=2.0)
        if tools is None:
            self._close_menus(1)
            raise PRScriptMenuError("the Tools submenu did not open")

        # Home marks the first built-in entry, Right jumps to the Scripts column.
        self.x11.key("Home")
        self.x11.key("Right")
        for _ in range(steps):
            self.x11.key("Down")

        known_ids = self.x11.window_ids()
        self.x11.key("Return")
        self._await_script(ran, known_ids, timeout)
        return {"script": script_name, "menu_steps": steps}
