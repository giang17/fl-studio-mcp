"""Which window the Ctrl+Alt+Y trigger is aimed at on Linux/X11.

Under Wine an undocked piano roll is its own top-level X window. Activating
FL's main window instead takes the keyboard focus away from it, and FL then
never routes the keystroke to the piano roll script (issue #10). No FL Studio
and no X server here: xdotool is a recorder, the window lookup a fake.
"""

from __future__ import annotations

import subprocess

import pytest

from fl_studio_mcp.utils import fl_trigger, x11_automation
from fl_studio_mcp.utils.fl_trigger import FLStudioTrigger
from fl_studio_mcp.utils.x11_automation import XWindow

MAIN = XWindow("111", "song.flp - FL Studio 2026", 0, 0, 1660, 946)
PIANO_ROLL = XWindow("222", "Piano roll -", 100, 100, 900, 500)


class FakeX11:
    """Stand-in for X11Automation: knows FL's own windows, nothing else."""

    def __init__(self, windows=(), unavailable_reason=None):
        self.windows = list(windows)
        self.unavailable_reason = unavailable_reason
        self.searched: list[str] = []

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def find_window(self, name_pattern: str) -> XWindow | None:
        import re

        self.searched.append(name_pattern)
        regex = re.compile(name_pattern)
        return next((w for w in self.windows if regex.search(w.name)), None)


class RecordingRun:
    """Records xdotool calls; answers `search` with the given window ids."""

    def __init__(self, search_ids=("111",)):
        self.search_ids = list(search_ids)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[1] == "search":
            if not self.search_ids:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(
                args, 0, stdout="\n".join(self.search_ids) + "\n", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    @property
    def activated(self) -> list[str]:
        return [call[-1] for call in self.calls if call[1] == "windowactivate"]

    @property
    def keys(self) -> list[str]:
        return [call[-1] for call in self.calls if call[1] == "key"]


@pytest.fixture
def trigger():
    return FLStudioTrigger()


def install(monkeypatch, x11, run=None):
    monkeypatch.setattr(x11_automation, "get_x11_automation", lambda: x11)
    run = run or RecordingRun()
    monkeypatch.setattr(fl_trigger.subprocess, "run", run)
    return run


def test_undocked_piano_roll_is_the_trigger_target(trigger, monkeypatch):
    install(monkeypatch, FakeX11([MAIN, PIANO_ROLL]))

    assert trigger._find_trigger_window_linux() == PIANO_ROLL.id


def test_trigger_activates_the_piano_roll_and_not_the_main_window(trigger, monkeypatch):
    run = install(monkeypatch, FakeX11([MAIN, PIANO_ROLL]))

    assert trigger._trigger_linux() is True
    assert run.activated == [PIANO_ROLL.id]
    assert run.keys == ["ctrl+alt+y"]


def test_docked_piano_roll_falls_back_to_the_main_window(trigger, monkeypatch):
    """No piano roll window exists when it is docked - the main window is right."""
    run = install(monkeypatch, FakeX11([MAIN]))

    assert trigger._trigger_linux() is True
    assert run.activated == [MAIN.id]


def test_main_window_is_used_when_x11_automation_is_unavailable(trigger, monkeypatch):
    run = install(monkeypatch, FakeX11(unavailable_reason="DISPLAY is not set"))

    assert trigger._find_trigger_window_linux() == MAIN.id
    assert run.calls[0][1] == "search"


def test_broken_window_lookup_does_not_break_the_trigger(trigger, monkeypatch):
    class ExplodingX11(FakeX11):
        def find_window(self, name_pattern):
            raise RuntimeError("xdotool getwindowname failed")

    install(monkeypatch, ExplodingX11([MAIN, PIANO_ROLL]))

    assert trigger._find_trigger_window_linux() == MAIN.id


def test_piano_roll_lookup_stays_inside_fl_studios_process(trigger, monkeypatch):
    """A browser tab titled "Piano roll" must not be mistaken for FL's window."""
    x11 = FakeX11([MAIN, PIANO_ROLL])
    run = install(monkeypatch, x11)

    trigger._find_trigger_window_linux()

    # The name search goes through X11Automation, which only sees windows of
    # FL's process; no unscoped `xdotool search --name "Piano roll"` is run.
    assert x11.searched == [FLStudioTrigger.LINUX_PIANO_ROLL_RE]
    assert run.calls == []


def test_no_fl_window_at_all_reports_failure(trigger, monkeypatch):
    install(monkeypatch, FakeX11(), RecordingRun(search_ids=[]))

    assert trigger._find_trigger_window_linux() is None
    assert trigger._trigger_linux() is False
