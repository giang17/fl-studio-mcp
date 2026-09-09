"""Starting a piano roll script from FL's menu, without FL Studio or X11.

FL greys out its trigger keystroke until a script has been started once per
session, so the first start has to go through the piano roll's Tools >
Scripting menu. The menu position of an entry is computed from the script
directories; the X11 layer here is a scripted fake that plays back the popups
FL would open and records what was clicked and typed.
"""

from __future__ import annotations

import pytest

from fl_studio_mcp.utils import pr_script_menu
from fl_studio_mcp.utils.pr_script_menu import (
    DEFAULT_MENU_OFFSET,
    PRScriptMenu,
    PRScriptMenuError,
)
from fl_studio_mcp.utils.x11_automation import XWindow

PIANO_ROLL = XWindow("pr", "Piano roll - Serum", 1377, 205, 1061, 789)
MAIN = XWindow("main", "song.flp - FL Studio 2026", 1080, 0, 1660, 946)
MENU = XWindow("menu", "", 1381, 232, 185, 336)
TOOLS = XWindow("tools", "", 1564, 270, 434, 423)
DIALOG = XWindow("dialog", "Arpeggiator", 1500, 400, 380, 260)


class FakeX11:
    """Plays back FL's popup menus and records clicks and keys."""

    unavailable_reason = None

    def __init__(self, windows=(MAIN, PIANO_ROLL)):
        self.windows = list(windows)
        self.keys: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.activated: list[str] = []
        self.opens_menu = True
        self.opens_tools = True
        self.opens_on_return: XWindow | None = None

    @property
    def available(self):
        return self.unavailable_reason is None

    # windows
    def fl_windows(self):
        return list(self.windows)

    def window_ids(self):
        return {w.id for w in self.windows}

    def find_window(self, pattern):
        import re

        regex = re.compile(pattern)
        return next((w for w in self.windows if regex.search(w.name)), None)

    def wait_for_new_window(self, known, timeout=2.0, min_width=2):
        return next((w for w in self.windows if w.id not in known and w.width >= min_width), None)

    # input
    def activate(self, window):
        self.activated.append(window.id)

    def click(self, x, y, button=1):
        self.clicks.append((x, y))
        if self.opens_menu:
            self.windows.append(MENU)

    def key(self, *keys, delay_ms=80):
        self.keys.extend(keys)
        if keys == ("Right",) and MENU in self.windows and TOOLS not in self.windows:
            if self.opens_tools:
                self.windows.append(TOOLS)
        elif keys == ("Return",):
            self.windows = [w for w in self.windows if w not in (MENU, TOOLS)]
            if self.opens_on_return is not None:
                self.windows.append(self.opens_on_return)
        elif keys == ("Escape",):
            # A popup swallows Escape first; otherwise FL closes whatever is
            # focused, which is what makes a blind Escape dangerous.
            if TOOLS in self.windows:
                self.windows.remove(TOOLS)
            elif MENU in self.windows:
                self.windows.remove(MENU)
            else:
                focused = next(
                    (w for w in self.windows if self.activated and w.id == self.activated[-1]),
                    None,
                )
                if focused is not None:
                    self.windows.remove(focused)

@pytest.fixture
def scripts(tmp_path, monkeypatch):
    """Three script directories, as FL collects them."""
    settings = tmp_path / "Image-Line" / "FL Studio" / "Settings"
    user = settings / "Piano roll scripts"
    downloads = tmp_path / "Image-Line" / "Downloads" / "Piano roll scripts"
    program = tmp_path / "program" / "Piano roll scripts"
    for directory in (user, downloads, program):
        directory.mkdir(parents=True)

    (user / "ComposeWithLLM.pyscript").write_text("# mcp")
    for name in ("Arpeggiator", "Euclidean", "Humanize"):
        (program / f"{name}.pyscript").write_text("# built-in")
    # Downloaded scripts ship as a folder holding a script of the same name.
    packaged = downloads / "WiseLabs probability sequencer"
    packaged.mkdir()
    (packaged / "WiseLabs probability sequencer.pyscript").write_text("# downloaded")

    monkeypatch.setattr(
        "fl_studio_mcp.utils.midi_connection.get_fl_settings_base", lambda: settings
    )
    monkeypatch.setattr(pr_script_menu, "program_scripts_dir", lambda: program)
    return {"user": user, "downloads": downloads, "program": program}


def test_scripts_are_listed_the_way_fl_shows_them(scripts):
    assert pr_script_menu.installed_scripts() == [
        "Arpeggiator",
        "ComposeWithLLM",
        "Euclidean",
        "Humanize",
        "WiseLabs probability sequencer",
    ]


def test_menu_steps_are_counted_from_the_end_of_the_scripts_column(scripts):
    """The entries above the scripts change state, the tail never does."""
    # Arpeggiator, ComposeWithLLM, Euclidean, Humanize, WiseLabs...
    assert pr_script_menu.script_menu_steps("WiseLabs probability sequencer") == 0
    assert pr_script_menu.script_menu_steps("ComposeWithLLM") == 3
    assert pr_script_menu.script_menu_steps("Arpeggiator") == 4


def test_sorting_ignores_case(scripts):
    (scripts["user"] / "aaa test.pyscript").write_text("# lowercase")

    assert pr_script_menu.installed_scripts()[0] == "aaa test"
    assert pr_script_menu.script_menu_steps("ComposeWithLLM") == 3


def test_backup_files_are_not_menu_entries(scripts):
    (scripts["user"] / "ComposeWithLLM.pyscript.bak-20260909").write_text("# backup")

    assert pr_script_menu.installed_scripts().count("ComposeWithLLM") == 1


def test_a_folder_that_does_not_name_its_script_is_refused(scripts):
    odd = scripts["downloads"] / "Some pack"
    odd.mkdir()
    (odd / "different name.pyscript").write_text("# unknown shape")

    with pytest.raises(PRScriptMenuError, match="cannot tell how FL lists"):
        pr_script_menu.installed_scripts()


def test_the_same_script_in_two_directories_is_refused(scripts):
    (scripts["program"] / "ComposeWithLLM.pyscript").write_text("# duplicate")

    with pytest.raises(PRScriptMenuError, match="menu order is ambiguous"):
        pr_script_menu.installed_scripts()


def test_a_missing_script_names_the_directories(scripts):
    with pytest.raises(PRScriptMenuError, match="not installed as a piano roll script"):
        pr_script_menu.script_menu_steps("NotThere")


def test_run_script_walks_the_menu_and_confirms_the_run(scripts):
    x11 = FakeX11()

    result = PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)

    assert result == {"script": "ComposeWithLLM", "menu_steps_from_end": 3}
    assert x11.activated == [PIANO_ROLL.id]
    assert x11.clicks == [
        (PIANO_ROLL.x + DEFAULT_MENU_OFFSET[0], PIANO_ROLL.y + DEFAULT_MENU_OFFSET[1])
    ]
    # Home + Down x2 + Right opens Tools; Home + Right jumps into the Scripts
    # column, End marks its last script, three Ups reach ComposeWithLLM.
    assert x11.keys == [
        "Home", "Down", "Down", "Right",
        "Home", "Right", "End", "Up", "Up", "Up",
        "Return",
    ]  # fmt: skip


def test_menu_offset_can_be_overridden(scripts, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_PR_MENU_OFFSET", "40, 22")
    x11 = FakeX11()

    PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)

    assert x11.clicks == [(PIANO_ROLL.x + 40, PIANO_ROLL.y + 22)]


def test_a_broken_offset_override_is_reported(scripts, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_PR_MENU_OFFSET", "middle")

    with pytest.raises(PRScriptMenuError, match="FL_STUDIO_MCP_PR_MENU_OFFSET"):
        PRScriptMenu(FakeX11()).run_script("ComposeWithLLM", lambda: True)


def test_a_docked_piano_roll_has_no_window_to_click(scripts):
    x11 = FakeX11(windows=(MAIN,))

    with pytest.raises(PRScriptMenuError, match="detached"):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)

    assert x11.clicks == []


def test_a_menu_that_does_not_open_is_reported(scripts):
    x11 = FakeX11()
    x11.opens_menu = False

    with pytest.raises(PRScriptMenuError, match="menu did not open"):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)


def test_a_missing_tools_submenu_closes_the_menu_again(scripts):
    x11 = FakeX11()
    x11.opens_tools = False

    with pytest.raises(PRScriptMenuError, match="Tools submenu did not open"):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)

    assert "Escape" in x11.keys
    assert MENU not in x11.windows
    # A popup is never activated: it holds the keyboard grab, and
    # `windowactivate --sync` on a window the WM never activates hangs.
    assert x11.activated == [PIANO_ROLL.id]


def test_a_dialog_after_return_means_the_wrong_entry(scripts):
    """Every other script opens a window; ComposeWithLLM runs without any UI."""
    x11 = FakeX11()
    x11.opens_on_return = DIALOG

    with pytest.raises(PRScriptMenuError, match="a window opened instead"):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: False, timeout=0.3)

    # Aimed at the dialog and stopped once it was gone: a blind second Escape
    # would close the piano roll itself.
    assert x11.keys.count("Escape") == 1
    assert x11.activated[-1] == DIALOG.id
    assert DIALOG not in x11.windows


def test_a_silent_run_is_not_reported_as_success(scripts):
    x11 = FakeX11()

    with pytest.raises(PRScriptMenuError, match="left no trace"):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: False, timeout=0.3)


def test_x11_unavailable_is_reported_before_anything_is_clicked(scripts):
    x11 = FakeX11()
    x11.unavailable_reason = "DISPLAY is not set (X11 session required)"

    with pytest.raises(PRScriptMenuError, match="DISPLAY is not set"):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)

    assert x11.clicks == []


def test_the_entry_is_never_clicked_when_its_position_is_unknown(scripts):
    """Refusing beats guessing: a wrong entry would run a foreign script."""
    (scripts["program"] / "ComposeWithLLM.pyscript").write_text("# duplicate")
    x11 = FakeX11()

    with pytest.raises(PRScriptMenuError):
        PRScriptMenu(x11).run_script("ComposeWithLLM", lambda: True)

    assert x11.clicks == []
    assert x11.keys == []
