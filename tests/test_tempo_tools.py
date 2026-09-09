"""Tempo tools: server-side validation and the REC event the controller sends.

Runs without FL Studio. The controller script is imported against the FL Studio
API stubs (dev dependency), so the milli-BPM scaling of the REC_Tempo event is
checked here rather than only in FL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fl_studio_mcp.tools.transport import (
    TEMPO_MAX_BPM,
    TEMPO_MIN_BPM,
    register_transport_tools,
)

CONTROLLER_DIR = Path(__file__).resolve().parents[1] / "fl_controller"


class RecordingMCP:
    """Minimal stand-in for FastMCP: collects the functions passed to @mcp.tool()."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeConnection:
    """Answers like the controller script with a 90 BPM 4/4 project."""

    def __init__(self):
        self.sent = []

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        response = {
            "success": True,
            "bpm": 90.0,
            "raw_tempo": 90000,
            "ppq": 96,
            "ppb": 384,
            "beats_per_bar": 4,
        }
        if action == "transport.setTempo":
            response["requested_bpm"] = params["bpm"]
        return response


@pytest.fixture
def env(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    conn = FakeConnection()
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_transport_tools(mcp)
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


def test_get_tempo_reports_tempo_and_timebase(env):
    tools, conn = env

    result = tools["fl_get_tempo"]()

    assert conn.sent == [("transport.getTempo", None)]
    assert result["bpm"] == 90.0
    assert (result["ppq"], result["ppb"]) == (96, 384)
    assert result["time_signature"] == {"numerator": 4, "denominator": None}


def test_get_tempo_passes_controller_errors_through(env, monkeypatch):
    tools, conn = env
    monkeypatch.setattr(
        conn, "send_command", lambda *a, **k: {"success": False, "error": "no response"}
    )

    assert tools["fl_get_tempo"]() == {"error": "no response"}


def test_set_tempo_returns_the_tempo_fl_applied(env):
    tools, conn = env

    result = tools["fl_set_tempo"](90)

    assert conn.sent == [("transport.setTempo", {"bpm": 90.0})]
    assert result["bpm"] == 90.0
    assert result["requested_bpm"] == 90.0


@pytest.mark.parametrize("bpm", [0, 9.9, TEMPO_MAX_BPM + 1, 1000])
def test_set_tempo_rejects_out_of_range_before_talking_to_fl(env, bpm):
    tools, conn = env

    result = tools["fl_set_tempo"](bpm)

    assert "error" in result
    assert conn.sent == []


@pytest.mark.parametrize("bpm", [TEMPO_MIN_BPM, 128, TEMPO_MAX_BPM])
def test_set_tempo_accepts_the_full_fl_range(env, bpm):
    tools, conn = env

    assert "error" not in tools["fl_set_tempo"](bpm)
    assert conn.sent[-1][0] == "transport.setTempo"


def test_controller_sends_tempo_as_milli_bpm(controller, monkeypatch):
    import general
    import midi

    calls = []
    monkeypatch.setattr(general, "processRECEvent", lambda *args: calls.append(args))
    monkeypatch.setattr(controller, "_read_tempo", lambda: {"bpm": 90.5})

    result = controller.handle_transport_set_tempo({"bpm": 90.5})

    assert calls == [(midi.REC_Tempo, 90500, midi.REC_Control | midi.REC_UpdateControl)]
    assert result["requested_bpm"] == 90.5


@pytest.mark.parametrize("bpm", [9, 523, "fast", None])
def test_controller_rejects_out_of_range_tempo(controller, monkeypatch, bpm):
    import general

    monkeypatch.setattr(
        general, "processRECEvent", lambda *args: pytest.fail("FL was asked to set the tempo")
    )

    assert "error" in controller.handle_transport_set_tempo({"bpm": bpm})


def test_controller_derives_the_time_signature_numerator(controller, monkeypatch):
    import general
    import mixer

    monkeypatch.setattr(mixer, "getCurrentTempo", lambda *a: 128000)
    monkeypatch.setattr(general, "getRecPPQ", lambda: 96)
    monkeypatch.setattr(general, "getRecPPB", lambda: 288)

    result = controller.handle_transport_get_tempo({})

    assert result["bpm"] == 128.0
    assert result["beats_per_bar"] == 3


def test_controller_omits_the_numerator_without_a_timebase(controller, monkeypatch):
    import general

    monkeypatch.setattr(general, "getRecPPQ", lambda: 0)

    assert "beats_per_bar" not in controller.handle_transport_get_tempo({})
