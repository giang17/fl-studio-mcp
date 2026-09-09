"""Shortcut/menu driven pattern tools, without FL Studio or X11.

The X11 layer is replaced by a scripted fake that records the keys and clicks
the tools send and plays back the windows FL Studio would open (Confirm
dialog, inline name field, popup menu...). The controller is replaced by a
fake that keeps a pattern list and mutates it the way FL does.
"""

from __future__ import annotations

import pytest

from fl_studio_mcp.tools.patterns import register_pattern_tools
from fl_studio_mcp.utils import pattern_ui
from fl_studio_mcp.utils.pattern_ui import MENU_ITEMS_FROM_END, PatternUI
from fl_studio_mcp.utils.x11_automation import XWindow

MAIN = XWindow("main", "song.flp - FL Studio 2026", 1080, 0, 1920, 1080)


class RecordingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeX11:
    """Scripted stand-in for X11Automation.

    `on_key(keys)` and `on_click()` hooks let a test decide what FL "does" in
    response (open a dialog, mutate the fake controller's pattern list).
    """

    available = True
    unavailable_reason = None

    def __init__(self):
        self.windows = [MAIN]
        self.keys: list[tuple[str, ...]] = []
        self.typed: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.activated: list[str] = []
        self.on_key = None
        self.on_click = None

    # windows
    def main_window(self):
        return MAIN

    def fl_windows(self):
        return list(self.windows)

    def window_ids(self):
        return {w.id for w in self.windows}

    def find_window(self, pattern):
        import re

        return next((w for w in self.windows if re.search(pattern, w.name)), None)

    def wait_for_window(self, pattern, timeout=2.0):
        return self.find_window(pattern)

    def wait_for_new_window(self, known, timeout=2.0, min_width=2):
        return next((w for w in self.windows if w.id not in known and w.width >= min_width), None)

    def wait_gone(self, window_id, timeout=2.0):
        return all(w.id != window_id for w in self.windows)

    # input
    def activate(self, window):
        self.activated.append(window.id)

    def activate_main(self):
        self.activated.append(MAIN.id)
        return MAIN

    def key(self, *keys, delay_ms=80):
        self.keys.append(keys)
        if self.on_key:
            self.on_key(keys)

    def type_text(self, text, delay_ms=20):
        self.typed.append(text)

    def click(self, x, y, button=1):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click()

    def open(self, window):
        self.windows.append(window)

    def close(self, window_id):
        self.windows = [w for w in self.windows if w.id != window_id]


class FakeConnection:
    """Controller stand-in with a mutable pattern list (index -> name)."""

    def __init__(self, names):
        self.names = dict(names)
        self.current = min(self.names) if self.names else 1
        self.sent = []
        self.focus_visible = {"piano_roll": False}

    def _renumber(self, ordered):
        self.names = {i + 1: n for i, n in enumerate(ordered)}

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        if action == "patterns.getAll":
            return {
                "success": True,
                "patterns": [{"index": i, "name": n} for i, n in sorted(self.names.items())],
                "current": self.current,
            }
        if action == "patterns.select":
            self.current = params["index"]
            return {
                "success": True,
                "selected": self.current,
                "name": self.names.get(self.current, f"Pattern {self.current}"),
            }
        if action == "patterns.getCurrent":
            return {
                "success": True,
                "index": self.current,
                "name": self.names.get(self.current, f"Pattern {self.current}"),
                "length_steps": 16,
            }
        if action == "ui.getFocus":
            return {"success": True, "visible": dict(self.focus_visible), "focused": {}}
        if action == "pianoroll.open":
            return {"success": True}
        return {"success": True}

    # what FL does
    def fl_delete_current(self):
        ordered = [n for i, n in sorted(self.names.items()) if i != self.current]
        self._renumber(ordered)
        self.current = min(self.current, len(ordered)) or 1

    def fl_insert_before_current(self, name):
        ordered = [n for _, n in sorted(self.names.items())]
        ordered.insert(self.current - 1, name)
        self._renumber(ordered)

    def fl_move_current(self, delta):
        ordered = [n for _, n in sorted(self.names.items())]
        pos = self.current - 1
        new = pos + delta
        if not 0 <= new < len(ordered):
            return
        ordered[pos], ordered[new] = ordered[new], ordered[pos]
        self._renumber(ordered)
        self.current = new + 1


@pytest.fixture
def env(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    conn = FakeConnection({1: "Intro", 2: "Verse", 3: "Chorus"})
    x11 = FakeX11()
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    monkeypatch.setattr(pattern_ui, "get_x11_automation", lambda: x11)
    monkeypatch.setattr(pattern_ui, "DIALOG_TIMEOUT", 0.0)
    monkeypatch.setattr(pattern_ui, "MENU_TIMEOUT", 0.0)
    import fl_studio_mcp.tools.patterns as patterns

    monkeypatch.setattr(
        patterns, "_wait_for_change", lambda conn, before, timeout=0: patterns._snapshot(conn)
    )
    monkeypatch.setattr(patterns.time, "sleep", lambda s: None)
    mcp = RecordingMCP()
    register_pattern_tools(mcp)
    return mcp.tools, conn, x11


# -- delete ---------------------------------------------------------------------


def test_delete_confirms_dialog_and_verifies_list(env):
    tools, conn, x11 = env
    confirm = XWindow("confirm", "Confirm", 0, 0, 359, 207)

    def on_key(keys):
        if keys == ("shift+ctrl+Delete",):
            x11.open(confirm)
        elif keys == ("Return",) and "confirm" in x11.window_ids():
            x11.close("confirm")
            conn.fl_delete_current()

    x11.on_key = on_key
    result = tools["fl_delete_pattern"](2)

    assert result["success"] is True
    assert result["confirm_dialog_seen"] is True
    assert conn.names == {1: "Intro", 2: "Chorus"}
    assert x11.activated[-1] == "confirm"


def test_delete_without_dialog_still_verifies(env):
    """'Remember my choice' set: FL deletes without asking."""
    tools, conn, x11 = env
    x11.on_key = lambda keys: conn.fl_delete_current() if "Delete" in keys[0] else None

    result = tools["fl_delete_pattern"](3)

    assert result["success"] is True
    assert result["confirm_dialog_seen"] is False
    assert conn.names == {1: "Intro", 2: "Verse"}


def test_delete_reports_when_nothing_happened(env):
    tools, conn, x11 = env

    result = tools["fl_delete_pattern"](2)

    assert "error" in result
    assert "No 'Confirm' dialog" in result["error"]
    assert conn.names == {1: "Intro", 2: "Verse", 3: "Chorus"}


def test_delete_closes_unexpected_dialog_and_fails(env):
    tools, conn, x11 = env
    other = XWindow("warn", "Warning", 0, 0, 300, 100)
    x11.on_key = lambda keys: x11.open(other) if "Delete" in keys[0] else None

    result = tools["fl_delete_pattern"](2)

    assert "unexpected window 'Warning'" in result["error"]
    assert x11.keys[-1] == ("Escape",)
    assert x11.activated[-1] == "warn"


def test_delete_rejects_bad_index_before_touching_fl(env):
    tools, conn, x11 = env

    assert "error" in tools["fl_delete_pattern"](0)
    assert conn.sent == []
    assert x11.keys == []


# -- insert -----------------------------------------------------------------------


def test_insert_types_name_into_inline_field(env):
    tools, conn, x11 = env
    field = XWindow("field", "Pattern 2 name", 0, 0, 251, 51)

    def on_key(keys):
        if keys == ("shift+ctrl+Insert",):
            conn.fl_insert_before_current("Pattern 2")
            x11.open(field)
        elif keys == ("Return",):
            conn.names[conn.current] = x11.typed[-1] if x11.typed else "Pattern 2"
            x11.close("field")

    x11.on_key = on_key
    result = tools["fl_insert_pattern"](name="Bridge", before=2)

    assert result == {
        "success": True,
        "inserted": 2,
        "name": "Bridge",
        "pattern_count": 4,
        "current": 2,
    }
    assert conn.names == {1: "Intro", 2: "Bridge", 3: "Verse", 4: "Chorus"}
    assert ("ctrl+a",) in x11.keys
    assert x11.typed == ["Bridge"]


def test_insert_without_name_keeps_fl_default(env):
    tools, conn, x11 = env
    field = XWindow("field", "Pattern 1 name", 0, 0, 251, 51)

    def on_key(keys):
        if keys == ("shift+ctrl+Insert",):
            conn.fl_insert_before_current("Pattern 1")
            x11.open(field)
        elif keys == ("Return",):
            x11.close("field")

    x11.on_key = on_key
    conn.current = 1
    result = tools["fl_insert_pattern"]()

    assert result["success"] is True
    assert result["name"] == "Pattern 1"
    assert x11.typed == []
    assert ("ctrl+a",) not in x11.keys


def test_insert_fails_when_no_field_opens(env):
    tools, conn, x11 = env

    result = tools["fl_insert_pattern"](name="X")

    assert "did not open the pattern name field" in result["error"]
    assert len(conn.names) == 3


# -- move -------------------------------------------------------------------------


def test_move_down_two_steps(env):
    tools, conn, x11 = env
    x11.on_key = lambda keys: conn.fl_move_current(1) if keys == ("shift+ctrl+Down",) else None

    result = tools["fl_move_pattern"](1, "down", steps=2)

    assert result["from"] == 1 and result["to"] == 3
    assert result["steps_applied"] == 2
    assert conn.names == {1: "Verse", 2: "Chorus", 3: "Intro"}


def test_move_stops_at_the_top(env):
    tools, conn, x11 = env
    x11.on_key = lambda keys: conn.fl_move_current(-1) if keys == ("shift+ctrl+Up",) else None

    result = tools["fl_move_pattern"](2, "up", steps=5)

    assert result["success"] is True
    assert result["to"] == 1
    assert result["steps_applied"] == 1
    assert "top of the list" in result["note"]


def test_move_validates_arguments(env):
    tools, conn, x11 = env

    assert "error" in tools["fl_move_pattern"](1, "sideways")
    assert "error" in tools["fl_move_pattern"](1, "up", steps=0)
    assert x11.keys == []


# -- pattern menu -----------------------------------------------------------------


def test_menu_is_opened_at_toolbar_offset_and_navigated_from_the_end(env, monkeypatch):
    tools, conn, x11 = env
    monkeypatch.setenv("FL_STUDIO_MCP_PATTERN_MENU_OFFSET", "800,60")
    menu = XWindow("menu", "", 0, 0, 348, 393)
    x11.on_click = lambda: x11.open(menu)

    def on_key(keys):
        if keys[-1] == "Return" and "menu" in x11.window_ids():
            x11.close("menu")
            conn._renumber(["Intro", "Verse - Drums", "Verse - Piano", "Chorus"])

    x11.on_key = on_key
    result = tools["fl_split_pattern_by_channel"](2)

    assert x11.clicks == [(MAIN.x + 800, MAIN.y + 60)]
    ups = ["Up"] * MENU_ITEMS_FROM_END["split_by_channel"]
    assert x11.keys[0] == ("End", *ups, "Return")
    assert result["success"] is True
    assert result["created"] == [
        {"index": 2, "name": "Verse - Drums"},
        {"index": 3, "name": "Verse - Piano"},
    ]


def test_menu_not_opening_is_an_error_with_a_hint(env):
    tools, conn, x11 = env

    result = tools["fl_transpose_pattern"](1, 2)

    assert "pattern menu did not open" in result["error"]
    assert "FL_STUDIO_MCP_PATTERN_MENU_OFFSET" in result["error"]
    assert x11.keys == []


def test_split_reports_fl_warning_for_single_channel_pattern(env):
    tools, conn, x11 = env
    menu = XWindow("menu", "", 0, 0, 348, 393)
    warning = XWindow("warn", "Warning", 0, 0, 533, 205)
    x11.on_click = lambda: x11.open(menu)

    def on_key(keys):
        if keys[-1] == "Return" and "menu" in x11.window_ids():
            x11.close("menu")
            x11.open(warning)
        elif keys == ("Return",) and "warn" in x11.window_ids():
            x11.close("warn")

    x11.on_key = on_key
    result = tools["fl_split_pattern_by_channel"](1)

    assert "at least two channels" in result["error"]
    assert "warn" not in x11.window_ids()


# -- transpose ---------------------------------------------------------------------


def _script_transpose(x11, conn, states):
    """Menu -> Semitones field -> typed value; piano roll states are served in order."""
    menu = XWindow("menu", "", 0, 0, 348, 393)
    field = XWindow("semi", "Semitones", 0, 0, 202, 51)
    x11.on_click = lambda: x11.open(menu)

    def on_key(keys):
        if keys[-1] == "Return" and "menu" in x11.window_ids():
            x11.close("menu")
            x11.open(field)
        elif keys == ("Return",) and "semi" in x11.window_ids():
            x11.close("semi")

    x11.on_key = on_key


def test_transpose_verifies_notes_through_piano_roll(env, monkeypatch):
    tools, conn, x11 = env
    conn.focus_visible["piano_roll"] = True
    _script_transpose(x11, conn, None)
    states = iter([[60, 64, 67], [62, 66, 69]])
    import fl_studio_mcp.tools.patterns as patterns

    monkeypatch.setattr(patterns, "_piano_roll_note_numbers", lambda conn: next(states))

    result = tools["fl_transpose_pattern"](1, 2)

    assert result["success"] is True
    assert result["verified"] is True
    assert x11.typed == ["2"]


def test_transpose_flags_mismatch(env, monkeypatch):
    tools, conn, x11 = env
    _script_transpose(x11, conn, None)
    states = iter([[60], [60]])
    import fl_studio_mcp.tools.patterns as patterns

    monkeypatch.setattr(patterns, "_piano_roll_note_numbers", lambda conn: next(states))

    result = tools["fl_transpose_pattern"](1, -5)

    assert result["verified"] is False
    assert "error" in result
    assert x11.typed == ["-5"]


def test_transpose_is_unverified_without_piano_roll(env, monkeypatch):
    tools, conn, x11 = env
    _script_transpose(x11, conn, None)
    import fl_studio_mcp.tools.patterns as patterns

    monkeypatch.setattr(patterns, "_piano_roll_note_numbers", lambda conn: None)

    result = tools["fl_transpose_pattern"](1, 1)

    assert result["success"] is True
    assert result["verified"] is None
    assert "Not verified" in result["note"]


@pytest.mark.parametrize("semitones", [0, 49, -49, 1.5, True])
def test_transpose_rejects_bad_semitones(env, semitones):
    tools, conn, x11 = env

    assert "error" in tools["fl_transpose_pattern"](1, semitones)
    assert x11.clicks == []


# -- time signature --------------------------------------------------------------------


def test_time_signature_goes_through_piano_roll_script(env, monkeypatch):
    tools, conn, x11 = env
    import fl_studio_mcp.tools.piano_roll as pr

    written = []
    monkeypatch.setattr(pr, "_write_request", lambda req: written.append(req))
    monkeypatch.setattr(
        pr,
        "_run_pr_script",
        lambda: {
            "triggered": True,
            "ran": True,
            "response": {
                "status": "success",
                "time_signature": {
                    "status": "success",
                    "replaced": 1,
                    "markers": [
                        {"time_ticks": 0, "mode": 8, "tsnum": 7, "tsden": 8, "name": "7/8"}
                    ],
                },
            },
            "error": None,
        },
    )

    result = tools["fl_set_pattern_time_signature"](7, 8, index=3)

    assert written == [
        {"action": "set_time_signature", "numerator": 7, "denominator": 8, "time_ticks": 0}
    ]
    assert ("patterns.select", {"index": 3}) in conn.sent
    assert ("pianoroll.open", {}) in conn.sent
    assert result["success"] is True
    assert result["time_signature"] == "7/8"
    assert result["replaced_marker"] is True
    assert x11.keys == []  # no menu automation involved


def test_time_signature_fails_when_marker_missing(env, monkeypatch):
    tools, conn, x11 = env
    import fl_studio_mcp.tools.piano_roll as pr

    monkeypatch.setattr(pr, "_write_request", lambda req: None)
    monkeypatch.setattr(
        pr,
        "_run_pr_script",
        lambda: {
            "triggered": True,
            "ran": True,
            "response": {"status": "success", "time_signature": {"markers": []}},
            "error": None,
        },
    )

    result = tools["fl_set_pattern_time_signature"](3, 4)

    assert result["success"] is False
    assert "marker was not found" in result["error"]


def test_time_signature_explains_old_script(env, monkeypatch):
    tools, conn, x11 = env
    import fl_studio_mcp.tools.piano_roll as pr

    monkeypatch.setattr(pr, "_write_request", lambda req: None)
    monkeypatch.setattr(
        pr,
        "_run_pr_script",
        lambda: {"triggered": True, "ran": True, "response": {"status": "success"}, "error": None},
    )

    result = tools["fl_set_pattern_time_signature"](3, 4)

    assert "does not know 'set_time_signature'" in result["error"]


def test_time_signature_removes_its_request_when_script_did_not_run(env, monkeypatch):
    tools, conn, x11 = env
    import fl_studio_mcp.tools.piano_roll as pr

    removed = []
    monkeypatch.setattr(pr, "_write_request", lambda req: None)
    monkeypatch.setattr(pr, "_remove_queued_actions", lambda action: removed.append(action))
    monkeypatch.setattr(
        pr,
        "_run_pr_script",
        lambda: {"triggered": True, "ran": False, "response": None, "error": "no run"},
    )

    result = tools["fl_set_pattern_time_signature"](3, 4)

    assert "error" in result
    assert removed == ["set_time_signature"]


@pytest.mark.parametrize("num, den", [(0, 4), (17, 4), (4, 3), (4, 32), (True, 4)])
def test_time_signature_rejects_values_fl_does_not_offer(env, num, den):
    tools, conn, x11 = env

    assert "error" in tools["fl_set_pattern_time_signature"](num, den)
    assert conn.sent == []


# -- platform gate -----------------------------------------------------------------------


def test_tools_fail_honestly_without_x11(env):
    tools, conn, x11 = env
    x11.available = False
    x11.unavailable_reason = "this is Windows"

    for call in (
        lambda: tools["fl_delete_pattern"](1),
        lambda: tools["fl_insert_pattern"](),
        lambda: tools["fl_move_pattern"](1, "down"),
        lambda: tools["fl_transpose_pattern"](1, 1),
        lambda: tools["fl_split_pattern_by_channel"](1),
    ):
        result = call()
        assert "this is Windows" in result["error"]
    assert x11.keys == [] and x11.clicks == []


def test_pattern_ui_uses_env_offset_or_default(monkeypatch):
    monkeypatch.delenv("FL_STUDIO_MCP_PATTERN_MENU_OFFSET", raising=False)
    assert pattern_ui.pattern_menu_offset() == pattern_ui.DEFAULT_PATTERN_MENU_OFFSET
    monkeypatch.setenv("FL_STUDIO_MCP_PATTERN_MENU_OFFSET", "12,34")
    assert pattern_ui.pattern_menu_offset() == (12, 34)
    monkeypatch.setenv("FL_STUDIO_MCP_PATTERN_MENU_OFFSET", "garbage")
    assert pattern_ui.pattern_menu_offset() == pattern_ui.DEFAULT_PATTERN_MENU_OFFSET


def test_pattern_ui_requires_available_automation():
    x11 = FakeX11()
    x11.available = False
    x11.unavailable_reason = "xdotool is not installed"
    with pytest.raises(pattern_ui.PatternUIError, match="xdotool is not installed"):
        PatternUI(x11).require_available()
