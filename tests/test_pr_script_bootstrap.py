"""The trigger falls back to starting the script from FL's menu.

Right after an FL Studio start the trigger keystroke does nothing, because FL
has no "last script" yet. `_run_pr_script` then starts ComposeWithLLM from the
menu once - and that run reads the same request file, so the queued request is
served by it. FL Studio, X11 and the menu are all replaced by fakes here.
"""

from __future__ import annotations

import json

import pytest

from fl_studio_mcp.tools import piano_roll


class FakeTrigger:
    """Sends a keystroke that FL ignores, like a fresh FL Studio session."""

    is_supported = True
    platform = "Linux"
    keystroke = "Ctrl+Alt+Y"

    def __init__(self, on_trigger=None):
        self.on_trigger = on_trigger
        self.calls = 0

    def activate_window(self):
        return True

    def trigger(self, delay=0.0):
        self.calls += 1
        if self.on_trigger:
            self.on_trigger()
        return True


@pytest.fixture
def scripts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(piano_roll, "_get_fl_scripts_dir", lambda: tmp_path)
    monkeypatch.setattr(piano_roll, "_focus_piano_roll", lambda: None)
    return tmp_path


def script_run(scripts_dir, response=None):
    """What the piano roll script does: consume the queue, export, answer."""
    request = scripts_dir / "mcp_request.json"
    if request.exists():
        request.write_text("[]")
    (scripts_dir / "piano_roll_state.json").write_text(json.dumps({"notes": []}))
    if response is not None:
        (scripts_dir / "mcp_response.json").write_text(json.dumps(response))


def install_menu(monkeypatch, on_start=None, problem=None):
    """Replace the menu automation; returns the recorded start attempts."""
    calls = []

    def start(timeout=piano_roll.PR_SCRIPT_TIMEOUT):
        calls.append(timeout)
        if problem is not None:
            return problem
        if on_start:
            on_start()
        return None

    monkeypatch.setattr(piano_roll, "_start_pr_script_from_menu", start)
    return calls


def test_a_dead_trigger_starts_the_script_from_the_menu(scripts_dir, monkeypatch):
    response = {"status": "success", "notes_added": 3}
    piano_roll._write_request({"action": "add_notes"})
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())
    starts = install_menu(monkeypatch, on_start=lambda: script_run(scripts_dir, response))

    run = piano_roll._run_pr_script(timeout=0.5)

    assert run["ran"] is True
    assert run["response"] == response
    assert run["bootstrapped"] is True
    assert len(starts) == 1


def test_the_menu_is_left_alone_when_the_keystroke_works(scripts_dir, monkeypatch):
    trigger = FakeTrigger(lambda: script_run(scripts_dir, {"status": "success"}))
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: trigger)
    starts = install_menu(monkeypatch)

    run = piano_roll._run_pr_script(timeout=0.5)

    assert run["ran"] is True
    assert "bootstrapped" not in run
    assert starts == []


def test_a_failed_menu_start_is_reported_with_its_reason(scripts_dir, monkeypatch):
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())
    install_menu(monkeypatch, problem="no piano roll window found")

    run = piano_roll._run_pr_script(timeout=0.3)

    assert run["ran"] is False
    assert "no piano roll window found" in run["error"]
    assert "did not run" in run["error"]


def test_a_silent_menu_start_does_not_claim_success(scripts_dir, monkeypatch):
    """The menu reported success but nothing showed up in the files."""
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())
    install_menu(monkeypatch)

    run = piano_roll._run_pr_script(timeout=0.3)

    assert run["ran"] is False
    assert run["error"] == piano_roll._not_run_error()


def test_the_fallback_can_be_switched_off(scripts_dir, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_AUTO_BOOTSTRAP", "0")
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())
    starts = install_menu(monkeypatch)

    run = piano_roll._run_pr_script(timeout=0.3)

    assert run["ran"] is False
    assert starts == []


def test_callers_can_opt_out_per_call(scripts_dir, monkeypatch):
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())
    starts = install_menu(monkeypatch)

    piano_roll._run_pr_script(timeout=0.3, bootstrap=False)

    assert starts == []


def test_an_empty_queue_is_proven_by_the_state_export_alone(scripts_dir, monkeypatch):
    """Without a queued request the script writes no response, only the export."""
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())
    install_menu(monkeypatch, on_start=lambda: script_run(scripts_dir))

    run = piano_roll._run_pr_script(timeout=1.5)

    assert run["ran"] is True
    assert run["response"] is None
    assert run["bootstrapped"] is True


class FakeMenu:
    """Stands in for PRScriptMenu; records the script and reports what it did."""

    def __init__(self, ran_after_call=True, raises=None):
        self.ran_after_call = ran_after_call
        self.raises = raises
        self.started: list[str] = []

    def run_script(self, script_name, ran, timeout=5.0):
        self.started.append(script_name)
        if self.raises is not None:
            raise self.raises
        return {"script": script_name, "menu_steps": 3}


def test_the_tool_reports_a_started_script(scripts_dir, monkeypatch):
    from fl_studio_mcp.utils import pr_script_menu

    menu = FakeMenu()
    monkeypatch.setattr(pr_script_menu, "PRScriptMenu", lambda *a, **k: menu)
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: FakeTrigger())

    problem = piano_roll._start_pr_script_from_menu(timeout=0.2)

    assert problem is None
    assert menu.started == ["ComposeWithLLM"]


def test_the_tool_passes_the_menus_reason_through(scripts_dir, monkeypatch):
    from fl_studio_mcp.utils import pr_script_menu

    menu = FakeMenu(raises=pr_script_menu.PRScriptMenuError("the Tools submenu did not open"))
    monkeypatch.setattr(pr_script_menu, "PRScriptMenu", lambda *a, **k: menu)

    problem = piano_roll._start_pr_script_from_menu(timeout=0.2)

    assert problem == "the Tools submenu did not open"


def test_unexpected_x11_trouble_stays_a_message(scripts_dir, monkeypatch):
    from fl_studio_mcp.utils import pr_script_menu

    menu = FakeMenu(raises=RuntimeError("xdotool click failed"))
    monkeypatch.setattr(pr_script_menu, "PRScriptMenu", lambda *a, **k: menu)

    problem = piano_roll._start_pr_script_from_menu(timeout=0.2)

    assert problem == "RuntimeError: xdotool click failed"
