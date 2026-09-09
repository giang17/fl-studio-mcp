"""Hint panel: the fl_notify tool, the controller handler and the script's run hint.

Runs without FL Studio. The controller script and the piano roll script are
imported against the FL Studio API stubs (dev dependency), so the hint text
and the guarded imports are checked here rather than only in FL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fl_studio_mcp.tools.ui import register_ui_tools

CONTROLLER_DIR = Path(__file__).resolve().parents[1] / "fl_controller"


class RecordingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeConnection:
    def __init__(self):
        self.sent = []

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        return {"message": params["message"], "shown": params["message"], "verified": True}


@pytest.fixture
def env(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    conn = FakeConnection()
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_ui_tools(mcp)
    return mcp.tools, conn


@pytest.fixture
def controller():
    """The controller script, importable thanks to the FL Studio API stubs."""
    sys.path.insert(0, str(CONTROLLER_DIR))
    try:
        import device_FLStudioMCP

        return device_FLStudioMCP
    finally:
        sys.path.remove(str(CONTROLLER_DIR))


# -- server side -----------------------------------------------------------


def test_notify_passes_the_message_through(env):
    tools, conn = env

    result = tools["fl_notify"]("MCP: 12 notes")

    assert conn.sent == [("ui.notify", {"message": "MCP: 12 notes"})]
    assert result["shown"] == "MCP: 12 notes"


def test_notify_allows_an_empty_message_to_clear_the_panel(env):
    tools, conn = env

    tools["fl_notify"]("")

    assert conn.sent == [("ui.notify", {"message": ""})]


def test_notify_rejects_a_non_string(env):
    tools, conn = env

    assert "error" in tools["fl_notify"](12)
    assert conn.sent == []


# -- controller side -------------------------------------------------------


def test_controller_notify_reads_the_hint_back(controller, monkeypatch):
    import ui

    panel = {}
    monkeypatch.setattr(ui, "setHintMsg", lambda m: panel.update(msg=m))
    monkeypatch.setattr(ui, "getHintMsg", lambda: panel.get("msg", ""))

    result = controller.handle_ui_notify({"message": "MCP: 12 notes"})

    assert panel["msg"] == "MCP: 12 notes"
    assert result["shown"] == "MCP: 12 notes"
    assert result["verified"] is True
    assert result["focus_changed"] is False


def test_controller_notify_reports_a_hint_fl_did_not_take(controller, monkeypatch):
    import ui

    monkeypatch.setattr(ui, "setHintMsg", lambda m: None)
    monkeypatch.setattr(ui, "getHintMsg", lambda: "something else")

    result = controller.handle_ui_notify({"message": "MCP: 12 notes"})

    assert result["verified"] is False
    assert result["shown"] == "something else"


def test_controller_notify_needs_a_message(controller):
    assert "error" in controller.handle_ui_notify({})


# -- the hint after a piano roll run ---------------------------------------


class HintConnection:
    """Controller stand-in for the two commands the run hint sends."""

    def __init__(self, pattern="Verse", notify_ok=True):
        self.pattern = pattern
        self.notify_ok = notify_ok
        self.sent = []

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        if action == "patterns.getCurrent":
            return {"success": True, "index": 1, "name": self.pattern}
        if action == "ui.notify":
            if not self.notify_ok:
                return {"success": False, "error": "hint panel unavailable"}
            return {"success": True, "shown": params["message"], "verified": True}
        return {"success": True}


@pytest.fixture
def hint_conn(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    conn = HintConnection()
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    return conn


def _run(added=0, deleted=0):
    return {"ran": True, "response": {"notes_added": added, "notes_deleted": deleted}}


def test_run_hint_names_notes_and_pattern(hint_conn):
    from fl_studio_mcp.tools.piano_roll import _hint_after_run

    assert _hint_after_run(_run(added=12)) is None
    assert hint_conn.sent[-1] == ("ui.notify", {"message": "MCP: 12 notes \u2192 Verse"})


def test_run_hint_counts_deletions_too(hint_conn):
    from fl_studio_mcp.tools.piano_roll import _hint_after_run

    _hint_after_run(_run(added=4, deleted=7))

    assert hint_conn.sent[-1][1]["message"] == "MCP: 4 notes, 7 deleted \u2192 Verse"


def test_run_hint_leaves_out_an_unnamed_pattern(monkeypatch):
    import fl_studio_mcp.utils.connection as connection
    from fl_studio_mcp.tools.piano_roll import _hint_after_run

    conn = HintConnection(pattern="")
    monkeypatch.setattr(connection, "get_connection", lambda: conn)

    _hint_after_run(_run(added=3))

    assert conn.sent[-1][1]["message"] == "MCP: 3 notes"


def test_run_hint_says_so_when_nothing_changed(hint_conn):
    from fl_studio_mcp.tools.piano_roll import _hint_after_run

    _hint_after_run(_run())

    assert hint_conn.sent[-1][1]["message"] == "MCP: no changes \u2192 Verse"


def test_run_hint_reports_a_refusing_controller_instead_of_raising(monkeypatch):
    import fl_studio_mcp.utils.connection as connection
    from fl_studio_mcp.tools.piano_roll import _hint_after_run

    conn = HintConnection(notify_ok=False)
    monkeypatch.setattr(connection, "get_connection", lambda: conn)

    assert _hint_after_run(_run(added=1)) == "hint panel unavailable"


def test_trigger_info_appends_a_failed_hint_and_skips_it_after_a_failed_run(monkeypatch):
    from fl_studio_mcp.tools import piano_roll

    monkeypatch.setattr(piano_roll, "_run_pr_script", lambda: _run(added=2))
    monkeypatch.setattr(piano_roll, "_hint_after_run", lambda run: "no MIDI port")
    assert "Hint not shown: no MIDI port" in piano_roll._get_trigger_info(True, hint=True)

    monkeypatch.setattr(piano_roll, "_run_pr_script", lambda: {"ran": False, "error": "no run"})
    called = []
    monkeypatch.setattr(piano_roll, "_hint_after_run", lambda run: called.append(run))
    piano_roll._get_trigger_info(True, hint=True)
    assert called == [], "a script that did not run has nothing to report"


def test_send_notes_asks_for_a_hint_by_default(monkeypatch):
    from fl_studio_mcp.tools import piano_roll

    seen = []

    def record(auto, hint=False):
        seen.append(hint)
        return ""

    monkeypatch.setattr(piano_roll, "_write_request", lambda r: None)
    monkeypatch.setattr(piano_roll, "_get_trigger_info", record)
    mcp = RecordingMCP()
    piano_roll.register_piano_roll_tools(mcp)
    note = [{"midi": 60, "duration": 1.0}]

    mcp.tools["fl_send_notes"](list(note))
    mcp.tools["fl_send_notes"](list(note), hint=False)

    assert seen == [True, False]
