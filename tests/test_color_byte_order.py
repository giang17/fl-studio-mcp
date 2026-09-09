"""Colour setters: the byte order FL Studio's scripting API expects.

Runs without FL Studio. Measured on FL 2026 (26.1.5): setChannelColor and
setTrackColor take 0xRRGGBB, and the getters report the same layout, so a
colour set here comes back unchanged from fl_get_channel_info and shows up
on screen as the caller asked for it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CONTROLLER_DIR = Path(__file__).resolve().parents[1] / "fl_controller"

AMBER = {"r": 212, "g": 160, "b": 72}
AMBER_RGB = 0xD4A048


@pytest.fixture
def controller():
    """The controller script, importable thanks to the FL Studio API stubs."""
    sys.path.insert(0, str(CONTROLLER_DIR))
    try:
        import device_FLStudioMCP

        return device_FLStudioMCP
    finally:
        sys.path.remove(str(CONTROLLER_DIR))


def test_channel_color_is_written_as_rgb(controller, monkeypatch):
    import channels

    calls = []
    monkeypatch.setattr(channels, "setChannelColor", lambda i, c, u=0: calls.append((i, c)))

    result = controller.handle_channels_set_color({"index": 2, **AMBER})

    assert calls == [(2, AMBER_RGB)]
    assert result["color"] == "RGB(212, 160, 72)"


def test_mixer_track_color_is_written_as_rgb(controller, monkeypatch):
    import mixer

    calls = []
    monkeypatch.setattr(mixer, "setTrackColor", lambda t, c: calls.append((t, c)))

    result = controller.handle_mixer_set_track_color({"track": 4, **AMBER})

    assert calls == [(4, AMBER_RGB)]
    assert result["color"] == "RGB(212, 160, 72)"


def test_playlist_track_colour_survives_a_round_trip(controller, monkeypatch):
    import playlist

    written = {}
    monkeypatch.setattr(playlist, "trackCount", lambda: 500)
    monkeypatch.setattr(playlist, "setTrackColor", lambda i, c: written.update({i: c}))
    monkeypatch.setattr(playlist, "getTrackColor", lambda i: written[i] - 0x1000000)
    monkeypatch.setattr(playlist, "getTrackName", lambda i: "Bass")

    controller.handle_playlist_set_track({"index": 3, "rgb": AMBER})
    report = controller._playlist_track_report(playlist, 3)

    assert written == {3: AMBER_RGB}
    assert report["color"] == "#d4a048"
