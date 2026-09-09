"""Playlist track and marker tools: sequencing against a simulated FL.

Runs without FL Studio. The fake controller mimics what was measured against
FL 2026: a song position, marker jump or loop mode change is applied only
after the command that requested it returns, FL wraps around at both ends
when jumping, and marker names come back in song order.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fl_studio_mcp.tools.playlist import register_playlist_tools, walk_markers

CONTROLLER_DIR = Path(__file__).resolve().parents[1] / "fl_controller"


class RecordingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeFL:
    """Stateful stand-in for the controller script (song mode, 4/4, PPQ 96)."""

    def __init__(self, markers=None, loop_mode="song"):
        self.markers = sorted(markers or [])  # [(ticks, name)]
        self.loop_mode = loop_mode
        self.song_ticks = 0
        self.pattern_ticks = 0
        self.pending = []  # changes FL applies after the callback returns
        self.sent = []
        self.tracks = {i: {"name": f"Track {i}", "muted": False} for i in range(1, 501)}
        self.tracks[1]["name"] = "Drums"

    # -- helpers -----------------------------------------------------------
    def _apply_pending(self):
        for change in self.pending:
            change()
        self.pending = []

    def _position(self):
        return {
            "ticks": self.song_ticks,
            "bar": self.song_ticks // 384 + 1,
            "beat": (self.song_ticks % 384) // 96 + 1,
            "tick": self.song_ticks % 96,
            "transport_ticks": self.pattern_ticks
            if self.loop_mode == "pattern"
            else self.song_ticks,
            "hint": "",
            "is_playing": False,
            "loop_mode": self.loop_mode,
            "ppq": 96,
            "ppb": 384,
        }

    def _jump(self, delta):
        if self.loop_mode != "song":
            return
        ticks = [t for t, _ in self.markers]
        if not ticks:
            return
        if delta > 0:
            later = [t for t in ticks if t > self.song_ticks]
            self.song_ticks = later[0] if later else ticks[0]
        else:
            earlier = [t for t in ticks if t < self.song_ticks]
            self.song_ticks = earlier[-1] if earlier else ticks[-1]

    # -- the controller protocol ------------------------------------------
    def send_command(self, action, params=None, timeout=2.0):
        self._apply_pending()
        params = params or {}
        self.sent.append((action, params))
        if action == "arrangement.getPosition":
            return {"success": True, "position": self._position()}
        if action == "arrangement.getMarkerNames":
            return {"success": True, "names": [n for _, n in self.markers]}
        if action == "arrangement.setPosition":
            ticks = params["ticks"]

            def apply():
                if self.loop_mode == "song":
                    self.song_ticks = ticks
                else:
                    self.pattern_ticks = ticks

            self.pending.append(apply)
            return {"success": True, "requested_ticks": ticks}
        if action == "arrangement.jumpMarker":
            delta = params["delta"]
            self.pending.append(lambda: self._jump(delta))
            return {"success": True, "before_ticks": self.song_ticks}
        if action == "arrangement.addMarker":
            name = params["name"]
            if any(n == name for _, n in self.markers):
                name = f"{name} #2"
            self.markers = sorted(self.markers + [(params["ticks"], name)])
            return {"success": True, "names": [n for _, n in self.markers]}
        if action == "transport.setLoopMode":
            mode = params["mode"]
            self.pending.append(lambda: setattr(self, "loop_mode", mode))
            return {"success": True, "mode": mode}
        if action == "playlist.getTracks":
            tracks = [
                {
                    "index": i,
                    "name": t["name"],
                    "is_unnamed": t["name"] == f"Track {i}",
                    "is_muted": t["muted"],
                    "is_solo": False,
                    "is_selected": False,
                }
                for i, t in self.tracks.items()
                if params.get("include_unnamed") or t["name"] != f"Track {i}"
            ]
            return {
                "success": True,
                "tracks": tracks[: params.get("limit", 50)],
                "track_count": 500,
            }
        if action == "playlist.setTrack":
            track = self.tracks[params["index"]]
            if "name" in params:
                track["name"] = params["name"] or f"Track {params['index']}"
            if params.get("muted") is not None:
                self.pending.append(lambda: track.__setitem__("muted", params["muted"]))
            return {"success": True, "index": params["index"], "changed": []}
        if action == "playlist.getTrack":
            i = params["index"]
            t = self.tracks[i]
            return {
                "success": True,
                "track": {"index": i, "name": t["name"], "is_muted": t["muted"]},
            }
        return {"success": False, "error": f"Unknown action: {action}"}


@pytest.fixture
def make_env(monkeypatch):
    def factory(**fl_kwargs):
        import fl_studio_mcp.utils.connection as connection

        fl = FakeFL(**fl_kwargs)
        monkeypatch.setattr(connection, "get_connection", lambda: fl)
        mcp = RecordingMCP()
        register_playlist_tools(mcp)
        return mcp.tools, fl

    return factory


@pytest.fixture
def controller():
    sys.path.insert(0, str(CONTROLLER_DIR))
    try:
        import device_FLStudioMCP

        return device_FLStudioMCP
    finally:
        sys.path.remove(str(CONTROLLER_DIR))


# -- marker walk -----------------------------------------------------------


def test_walk_reads_positions_only_after_fl_applied_them(make_env):
    _, fl = make_env(markers=[(1536, "Verse"), (4608, "Chorus")])
    fl.song_ticks = 700

    markers, position = walk_markers(fl)
    fl._apply_pending()  # FL applies the restoring setPosition after the callback

    assert [(m["name"], m["ticks"], m["bar"]) for m in markers] == [
        ("Verse", 1536, 5),
        ("Chorus", 4608, 13),
    ]
    assert position["ticks"] == 700
    assert fl.song_ticks == 700, "song position restored"


def test_walk_finds_a_marker_on_the_first_tick_via_the_name_count(make_env):
    _, fl = make_env(markers=[(0, "Intro"), (4608, "Chorus")])

    markers, _ = walk_markers(fl)

    assert [(m["name"], m["ticks"]) for m in markers] == [("Intro", 0), ("Chorus", 4608)]


def test_walk_without_markers_does_not_touch_the_transport(make_env):
    _, fl = make_env()

    markers, _ = walk_markers(fl)

    assert markers == []
    assert [a for a, _ in fl.sent] == ["arrangement.getPosition", "arrangement.getMarkerNames"]


def test_walk_switches_to_song_mode_and_restores_pattern_mode(make_env):
    _, fl = make_env(markers=[(1536, "Verse")], loop_mode="pattern")
    fl.pattern_ticks = 300

    markers, _ = walk_markers(fl)
    fl._apply_pending()

    assert markers[0]["ticks"] == 1536
    assert fl.loop_mode == "pattern"
    assert fl.pattern_ticks == 300, "pattern position restored after switching back"
    assert fl.song_ticks == 0


# -- tools -------------------------------------------------------------------


def test_add_marker_converts_bar_and_beat_to_ticks_and_verifies(make_env):
    tools, fl = make_env()

    result = tools["fl_add_marker"](33, "Chorus")

    assert ("arrangement.addMarker", {"ticks": 12288, "name": "Chorus"}) in fl.sent
    assert result["verified"] is True
    assert result["renamed"] is False
    assert result["marker"]["bar"] == 33

    result = tools["fl_add_marker"](5, "Verse", beat=3)
    assert result["marker"]["ticks"] == 4 * 384 + 2 * 96


def test_add_marker_reports_the_name_fl_assigned(make_env):
    tools, _ = make_env(markers=[(4608, "Chorus")])

    result = tools["fl_add_marker"](33, "Chorus")

    assert result["verified"] is True
    assert result["renamed"] is True
    assert result["marker"]["name"] == "Chorus #2"


@pytest.mark.parametrize(
    "args",
    [(0, "x"), (-1, "x"), (True, "x"), (5, ""), (5, "   "), (5, "x", 0), (5, "x", 5)],
)
def test_add_marker_rejects_bad_input_before_adding(make_env, args):
    tools, fl = make_env()

    assert "error" in tools["fl_add_marker"](*args)
    assert not any(a == "arrangement.addMarker" for a, _ in fl.sent)


def test_jump_to_marker_by_name_is_exact_then_case_insensitive(make_env):
    tools, fl = make_env(markers=[(0, "Intro"), (1536, "verse"), (4608, "Chorus")])

    result = tools["fl_jump_to_marker"](name="chorus")
    assert result["marker"]["name"] == "Chorus"
    assert result["position"]["ticks"] == 4608

    result = tools["fl_jump_to_marker"](name="Bridge")
    assert result["error"] == "No marker named 'Bridge'"
    assert result["markers"] == ["Intro", "verse", "Chorus"]


def test_jump_to_marker_by_delta_reports_the_marker_reached(make_env):
    tools, fl = make_env(markers=[(0, "Intro"), (4608, "Chorus")])
    fl.song_ticks = 4608

    result = tools["fl_jump_to_marker"](delta=1)

    assert result["marker"]["name"] == "Intro", "FL wraps around after the last marker"
    assert result["position"]["ticks"] == 0


def test_jump_to_marker_leaves_song_mode_on(make_env):
    tools, fl = make_env(markers=[(4608, "Chorus")], loop_mode="pattern")

    result = tools["fl_jump_to_marker"](name="Chorus")
    fl._apply_pending()

    assert result["position"]["loop_mode"] == "song"
    assert fl.loop_mode == "song"


@pytest.mark.parametrize("kwargs", [{}, {"name": "A", "delta": 1}, {"delta": 0}, {"delta": True}])
def test_jump_to_marker_needs_exactly_one_valid_target(make_env, kwargs):
    tools, fl = make_env()

    assert "error" in tools["fl_jump_to_marker"](**kwargs)
    assert fl.sent == []


def test_set_playlist_track_reads_the_track_back_in_a_second_call(make_env):
    tools, fl = make_env()

    result = tools["fl_set_playlist_track"](3, name="Bass", color="#3fa7d6", muted=True)

    assert fl.sent[-2][0] == "playlist.setTrack"
    assert fl.sent[-2][1]["rgb"] == {"r": 0x3F, "g": 0xA7, "b": 0xD6}
    assert fl.sent[-1] == ("playlist.getTrack", {"index": 3})
    assert result["track"] == {"index": 3, "name": "Bass", "is_muted": True}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"index": 0, "name": "x"},
        {"index": 3},
        {"index": 3, "color": "blue"},
        {"index": True, "name": "x"},
    ],
)
def test_set_playlist_track_rejects_bad_input(make_env, kwargs):
    tools, fl = make_env()

    assert "error" in tools["fl_set_playlist_track"](**kwargs)
    assert fl.sent == []


def test_list_playlist_tracks_skips_untouched_tracks_by_default(make_env):
    tools, _ = make_env()

    result = tools["fl_list_playlist_tracks"]()

    assert [t["name"] for t in result["tracks"]] == ["Drums"]
    assert result["track_count"] == 500


def test_errors_from_the_controller_are_passed_through(make_env, monkeypatch):
    tools, fl = make_env()
    monkeypatch.setattr(fl, "send_command", lambda *a, **k: {"success": False, "error": "no FL"})

    assert tools["fl_get_position"]() == {"error": "no FL"}
    assert tools["fl_list_markers"]() == {"error": "no FL"}


# -- controller ----------------------------------------------------------------


def test_controller_reports_position_in_bars_beats_and_ticks(controller, monkeypatch):
    import arrangement
    import general

    monkeypatch.setattr(general, "getRecPPQ", lambda: 96)
    monkeypatch.setattr(general, "getRecPPB", lambda: 384)
    monkeypatch.setattr(arrangement, "currentTime", lambda snap: 12288 + 2 * 96 + 5)

    position = controller.handle_arrangement_get_position({})["position"]

    assert (position["bar"], position["beat"], position["tick"]) == (33, 3, 5)


def test_controller_marker_names_stop_at_the_first_empty_name(controller, monkeypatch):
    import arrangement

    names = {0: "Intro", 1: "Chorus"}
    monkeypatch.setattr(arrangement, "getMarkerName", lambda i: names.get(i, ""))

    assert controller.handle_arrangement_get_marker_names({})["names"] == ["Intro", "Chorus"]


def test_controller_track_report_normalizes_signed_bgr_colors(controller, monkeypatch):
    import playlist

    monkeypatch.setattr(playlist, "getTrackName", lambda i: "Track 3")
    monkeypatch.setattr(playlist, "getTrackColor", lambda i: -0xB7AEAA)  # FL reports signed ints

    report = controller._playlist_track_report(playlist, 3)

    assert report["is_unnamed"] is True
    assert report["color"] == "#565148"


def test_controller_set_track_only_toggles_selection_when_needed(controller, monkeypatch):
    import playlist

    calls = []
    monkeypatch.setattr(playlist, "trackCount", lambda: 500)
    monkeypatch.setattr(playlist, "isTrackSelected", lambda i: True)
    monkeypatch.setattr(playlist, "selectTrack", lambda i: calls.append(("select", i)))
    monkeypatch.setattr(playlist, "muteTrack", lambda i, v=-1: calls.append(("mute", i, v)))
    monkeypatch.setattr(playlist, "setTrackColor", lambda i, c: calls.append(("color", i, c)))

    result = controller.handle_playlist_set_track(
        {"index": 3, "selected": True, "muted": True, "rgb": {"r": 0x3F, "g": 0xA7, "b": 0xD6}}
    )

    assert calls == [("color", 3, 0xD6A73F), ("mute", 3, 1)]
    assert result["changed"] == ["color", "muted", "selected"]


@pytest.mark.parametrize("params", [{"ticks": -1}, {"ticks": "0"}, {"ticks": True}, {}])
def test_controller_set_position_rejects_bad_ticks(controller, monkeypatch, params):
    import transport

    monkeypatch.setattr(transport, "setSongPos", lambda *a: pytest.fail("FL was asked to move"))

    assert "error" in controller.handle_arrangement_set_position(params)


def test_controller_add_marker_validates_and_returns_names(controller, monkeypatch):
    import arrangement

    added = []
    monkeypatch.setattr(arrangement, "addAutoTimeMarker", lambda t, n: added.append((t, n)))
    monkeypatch.setattr(arrangement, "getMarkerName", lambda i: {0: "Chorus"}.get(i, ""))

    assert "error" in controller.handle_arrangement_add_marker({"ticks": 5, "name": " "})
    assert "error" in controller.handle_arrangement_add_marker({"ticks": -5, "name": "x"})
    result = controller.handle_arrangement_add_marker({"ticks": 12288, "name": " Chorus "})

    assert added == [(12288, "Chorus")]
    assert result["names"] == ["Chorus"]


def test_controller_pattern_length_is_reported_in_steps_and_bars(controller, monkeypatch):
    import general
    import patterns

    monkeypatch.setattr(patterns, "getPatternLength", lambda i: 64)
    monkeypatch.setattr(general, "getRecPPQ", lambda: 96)
    monkeypatch.setattr(general, "getRecPPB", lambda: 384)

    assert controller._pattern_length(patterns, 1) == {"length_steps": 64, "length_bars": 4.0}
