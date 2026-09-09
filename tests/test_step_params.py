"""Step parameters (velocity, pan, shift, pitch per step) and humanize.

Runs without FL Studio. The server side is tested against a fake connection
that records the commands and answers like the controller; the controller
handlers are imported against the FL Studio API stubs with the step parameter
functions patched to a small in-memory store.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fl_studio_mcp.tools.channels import (
    build_step_param_values,
    humanize_values,
    normalise_step_params,
    pan_to_raw,
    raw_to_pan,
    raw_to_velocity,
    register_channel_tools,
    relative_shifts,
    shift_max,
    velocity_to_raw,
    verify_step_params,
)

CONTROLLER_DIR = Path(__file__).resolve().parents[1] / "fl_controller"
STEP_TICKS = 24  # PPQ 96 / 4


class RecordingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeConnection:
    """Keeps step bits and parameters like FL would, one channel, one pattern.

    Mirrors the verified FL behaviour: setting the grid resets every step's
    values, off steps have no values (-1), and shift is reported as the
    absolute tick position of the step's note.
    """

    def __init__(self, steps=16):
        self.sent = []
        self.bits = [False] * steps
        self.params = {
            "velocity": [100] * steps,
            "pan": [64] * steps,
            "shift": [0] * steps,  # relative to the step, as FL's graph editor shows it
            "pitch": [60] * steps,
        }
        self.fail_actions = set()
        self.lossy = False  # simulate FL storing velocity one lower

    def send_command(self, action, params=None, timeout=2.0):
        params = params or {}
        self.sent.append((action, params))
        if action in self.fail_actions:
            return {"success": False, "error": f"{action} failed"}
        if action == "transport.getTempo":
            return {"success": True, "bpm": 140.0, "ppq": STEP_TICKS * 4, "ppb": STEP_TICKS * 16}
        if action == "channels.setStepSequence":
            pattern = params["pattern"]
            self.bits[: len(pattern)] = pattern
            for name, default in (("velocity", 100), ("pan", 64), ("shift", 0), ("pitch", 60)):
                self.params[name] = [default] * len(self.params[name])
            return {"success": True, "active_steps": sum(pattern),
                    "total_steps": len(pattern), "channel_name": "HiHat"}
        if action == "channels.getStepSequence":
            return {"success": True, "sequence": self.bits[: params["steps"]]}
        if action == "channels.setStepParams":
            skipped = []
            for name, vals in params["values"].items():
                store = self.params[name]
                for i, v in enumerate(vals):
                    if not self.bits[i]:
                        skipped.append(i)
                        continue
                    store[i] = v - 1 if (self.lossy and name == "velocity") else v
            return {"success": True, "pattern": 4, "step_ticks": STEP_TICKS,
                    "written": {k: len(v) for k, v in params["values"].items()},
                    "skipped_off_steps": sorted(set(skipped)), "channel_name": "HiHat"}
        if action == "channels.getStepParams":
            n = params["steps"]
            out = {"success": True, "steps": self.bits[:n], "step_ticks": STEP_TICKS,
                   "channel_name": "HiHat"}
            for name in params["params"]:
                vals = []
                for i in range(n):
                    if not self.bits[i]:
                        vals.append(-1)
                    elif name == "shift":
                        vals.append(i * STEP_TICKS + self.params[name][i])
                    else:
                        vals.append(self.params[name][i])
                out[name] = vals
            return out
        return {"success": False, "error": f"unknown action {action}"}


@pytest.fixture
def env(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    conn = FakeConnection()
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_channel_tools(mcp)
    return mcp.tools, conn


@pytest.fixture
def controller(monkeypatch):
    sys.path.insert(0, str(CONTROLLER_DIR))
    try:
        import device_FLStudioMCP
    finally:
        sys.path.remove(str(CONTROLLER_DIR))

    import channels
    import general
    import patterns

    store = {}  # (channel, step, param) -> value
    calls = []

    def get_param(index, step, param, use_global=False):
        return store.get((index, step, param), {1: 100, 4: 64, 0: 60}.get(param, 0))

    def set_param(index, pat_num, step, param, value, use_global=False):
        calls.append((index, pat_num, step, param, value, use_global))
        store[(index, step, param)] = value

    bits = {}
    monkeypatch.setattr(channels, "getCurrentStepParam", get_param)
    monkeypatch.setattr(channels, "setStepParameterByIndex", set_param)
    monkeypatch.setattr(channels, "getGridBit", lambda i, p, g=False: bits.get((i, p), 0))
    def set_bit(i, p, v, g=False):
        bits[(i, p)] = v

    monkeypatch.setattr(channels, "setGridBit", set_bit)
    monkeypatch.setattr(channels, "getChannelName", lambda i, g=False: "HiHat")
    monkeypatch.setattr(general, "getRecPPQ", lambda: 96)
    monkeypatch.setattr(patterns, "patternNumber", lambda: 4)
    return device_FLStudioMCP, store, calls, bits


# --- value mapping ---------------------------------------------------------


def test_velocity_mapping_roundtrip():
    assert velocity_to_raw(0.0) == 0
    assert velocity_to_raw(1.0) == 127
    assert velocity_to_raw(0.8) == 102
    assert raw_to_velocity(100) == pytest.approx(0.787, abs=1e-3)
    for raw in range(128):
        assert velocity_to_raw(raw_to_velocity(raw)) == raw


def test_pan_mapping_centre_and_extremes():
    assert pan_to_raw(0.0) == 64
    assert pan_to_raw(-1.0) == 0
    assert pan_to_raw(1.0) == 127
    assert raw_to_pan(64) == 0.0
    assert raw_to_pan(0) == -1.0
    assert raw_to_pan(127) == 1.0
    for raw in range(128):
        assert pan_to_raw(raw_to_pan(raw)) == raw


# --- validation ------------------------------------------------------------


def test_build_values_converts_all_lists():
    raw = build_step_param_values(
        4, velocities=[1.0, 0.4, 0.0, 0.787], pans=[0.0, -1.0, 1.0, 0.5],
        shifts=[0, 12, 24, 3], pitches=[42, 42, 46, 42],
    )
    assert raw == {
        "velocity": [127, 51, 0, 100],
        "pan": [64, 0, 127, 96],
        "shift": [0, 12, 24, 3],
        "pitch": [42, 42, 46, 42],
    }


def test_build_values_none_lists_give_empty_mapping():
    assert build_step_param_values(16) == {}


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"velocities": [0.5] * 3}, "3 entries, expected 4"),
        ({"pans": [0.0] * 5}, "5 entries, expected 4"),
        ({"velocities": [0.5, 1.5, 0.5, 0.5]}, "velocities[1]"),
        ({"pans": [0.0, 0.0, -2.0, 0.0]}, "pans[2]"),
        ({"shifts": [0, 0, 0, -1]}, "shifts[3]"),
        ({"shifts": [0, 0, 0.5, 0]}, "shifts[2]"),
        ({"pitches": [42, 128, 42, 42]}, "pitches[1]"),
        ({"velocities": [True, 0.5, 0.5, 0.5]}, "velocities[0]"),
        ({"velocities": "loud"}, "must be a list"),
    ],
)
def test_build_values_rejects_bad_input(kwargs, fragment):
    result = build_step_param_values(4, **kwargs)
    assert isinstance(result, str)
    assert result.startswith("Error")
    assert fragment in result


def test_verify_reports_differences():
    raw = {"velocity": [127, 51], "shift": [0, 12]}
    ok = {"steps": [True, True], "step_ticks": 24, "velocity": [127, 51], "shift": [0, 36]}
    assert verify_step_params(raw, ok) == []
    diffs = verify_step_params(raw, {"steps": [True, True], "step_ticks": 24,
                                     "velocity": [127, 50]})
    assert diffs == ["velocity[1]: sent 51, FL reports 50", "shift: not readable"]


def test_verify_ignores_steps_that_are_off():
    raw = {"velocity": [127, 51, 80]}
    readback = {"steps": [True, False, True], "step_ticks": 24, "velocity": [127, -1, 80]}
    assert verify_step_params(raw, readback) == []


def test_relative_shifts_and_shift_max():
    assert relative_shifts([0, 30, -1, 72], 24) == [0, 6, None, 0]
    assert relative_shifts([23], 24) == [23]
    assert relative_shifts([0, 23], 24) == [0, -1]  # previous step's note one tick early
    assert shift_max(24) == 22
    assert shift_max(1) == 0


# --- fl_set_step_sequence ---------------------------------------------------


def test_set_sequence_without_params_sends_only_bits(env):
    tools, conn = env
    msg = tools["fl_set_step_sequence"](0, [True, False, True, False])
    assert [a for a, _ in conn.sent] == ["channels.setStepSequence"]
    assert msg == "Channel 'HiHat' pattern set with 2/4 steps active"


def test_set_sequence_with_params_writes_and_verifies(env):
    tools, conn = env
    pattern = [True] * 16
    velocities = [1.0 if i % 8 == 0 else (0.7 if i % 4 == 0 else 0.4) for i in range(16)]
    msg = tools["fl_set_step_sequence"](0, pattern, velocities=velocities, pans=[0.0] * 16)
    actions = [a for a, _ in conn.sent]
    assert actions == ["channels.setStepSequence", "channels.setStepParams",
                       "channels.getStepParams"]
    sent_values = conn.sent[1][1]["values"]
    assert sent_values["velocity"][0] == 127
    assert sent_values["velocity"][1] == 51
    assert set(sent_values) == {"velocity", "pan"}
    assert conn.sent[2][1]["params"] == ["velocity", "pan"]
    assert "16/16 steps active" in msg
    assert "velocity, pan written to pattern 4 and verified (step = 24 ticks)" in msg


def test_set_sequence_shift_roundtrip_with_off_steps(env):
    tools, conn = env
    pattern = [True, False, True, True]
    msg = tools["fl_set_step_sequence"](0, pattern, shifts=[0, 5, 12, 22])
    assert "verified" in msg
    assert conn.params["shift"][:4] == [0, 0, 12, 22]  # off step untouched
    actions = [a for a, _ in conn.sent]
    assert actions[0] == "transport.getTempo"  # range check before the grid is touched


def test_set_sequence_rejects_shift_of_a_full_step_before_writing(env):
    tools, conn = env
    conn.params["velocity"][0] = 127
    msg = tools["fl_set_step_sequence"](0, [True, True], shifts=[0, 23])
    assert msg.startswith("Error")
    assert "shifts[1] = 23 exceeds the usable range 0..22" in msg
    assert [a for a, _ in conn.sent] == ["transport.getTempo"]
    assert conn.params["velocity"][0] == 127  # nothing reset


def test_set_sequence_rejects_length_mismatch_before_sending(env):
    tools, conn = env
    msg = tools["fl_set_step_sequence"](0, [True] * 16, velocities=[0.5] * 8)
    assert msg.startswith("Error")
    assert "8 entries, expected 16" in msg
    assert conn.sent == []


def test_set_sequence_reports_readback_difference(env):
    tools, conn = env
    conn.lossy = True
    msg = tools["fl_set_step_sequence"](0, [True, True], velocities=[1.0, 0.5])
    assert "readback differs" in msg
    assert "velocity[0]: sent 127, FL reports 126" in msg


def test_set_sequence_reports_controller_error_for_params(env):
    tools, conn = env
    conn.fail_actions.add("channels.setStepParams")
    msg = tools["fl_set_step_sequence"](0, [True, True], shifts=[0, 12])
    assert "2/2 steps active" in msg
    assert "step parameters NOT written" in msg


# --- fl_get_step_sequence ---------------------------------------------------


def test_get_sequence_default_stays_a_bool_list(env):
    tools, conn = env
    conn.bits[0] = True
    assert tools["fl_get_step_sequence"](0, steps=4) == [True, False, False, False]
    assert conn.sent[0][0] == "channels.getStepSequence"


def test_get_sequence_with_params_is_normalised(env):
    tools, conn = env
    conn.bits[0] = conn.bits[2] = True
    conn.params["velocity"][0] = 127
    conn.params["pan"][2] = 0
    conn.params["shift"][2] = 12
    conn.params["pitch"][2] = 46
    out = tools["fl_get_step_sequence"](0, steps=4, include_params=True)
    assert out["steps"] == [True, False, True, False]
    assert out["active_steps"] == 2
    assert out["velocities"] == [1.0, None, pytest.approx(0.787, abs=1e-3), None]
    assert out["pans"] == [0.0, None, -1.0, None]
    assert out["shifts"] == [0, None, 12, None]
    assert out["pitches"] == [60, None, 46, None]
    assert out["step_ticks"] == 24
    assert out["channel_name"] == "HiHat"


def test_normalise_handles_missing_params():
    out = normalise_step_params({"steps": [True], "step_ticks": 24})
    assert out == {"steps": [True], "step_ticks": 24, "active_steps": 1}


def test_normalise_turns_absolute_shift_into_relative():
    out = normalise_step_params({"steps": [True, True, False], "step_ticks": 24,
                                 "shift": [3, 24, -1]})
    assert out["shifts"] == [3, 0, None]


# --- humanize ---------------------------------------------------------------


def test_humanize_values_is_deterministic_and_clamped():
    vel = [127, 100, 100, 0]
    shift = [0, 0, 24, 0]
    active = [True, True, True, True]
    a = humanize_values(vel, shift, active, 0.2, 12, STEP_TICKS, seed=7)
    b = humanize_values(vel, shift, active, 0.2, 12, STEP_TICKS, seed=7)
    c = humanize_values(vel, shift, active, 0.2, 12, STEP_TICKS, seed=8)
    assert a == b
    assert a != c
    for v in a[0]:
        assert 0 <= v <= 127
    for s in a[1]:
        assert 0 <= s <= shift_max(STEP_TICKS)
    # inputs untouched
    assert vel == [127, 100, 100, 0] and shift == [0, 0, 24, 0]


def test_humanize_values_skips_inactive_steps():
    vel = [100 if i % 2 == 0 else -1 for i in range(8)]  # FL reports -1 for off steps
    shift = [0 if i % 2 == 0 else None for i in range(8)]
    active = [i % 2 == 0 for i in range(8)]
    out_vel, out_shift = humanize_values(vel, shift, active, 0.3, 6, STEP_TICKS, seed=1)
    for i in range(1, 8, 2):
        assert out_vel[i] == 100 and out_shift[i] == 0  # defaults, never written
    assert any(out_vel[i] != 100 for i in range(0, 8, 2))


def test_humanize_values_only_velocity_when_shift_jitter_zero():
    out_vel, out_shift = humanize_values([100] * 4, [3] * 4, [True] * 4, 0.3, 0,
                                         STEP_TICKS, seed=3)
    assert out_shift == [3, 3, 3, 3]
    assert out_vel != [100] * 4


def test_humanize_tool_roundtrip_and_seed(env, monkeypatch):
    tools, conn = env
    conn.bits = [i % 2 == 0 for i in range(16)]
    out = tools["fl_humanize_steps"](0, velocity_jitter=0.2, shift_jitter=6, seed=42)
    assert out["seed"] == 42
    assert out["verified"] is True
    assert out["pattern"] == 4
    assert out["steps_touched"] == list(range(0, 16, 2))
    for i in range(16):
        expected = pytest.approx(0.787, abs=1e-3) if i % 2 == 0 else None
        assert out["before"]["velocities"][i] == expected
    after = out["after"]
    for i in range(1, 16, 2):  # inactive steps have no values in FL
        assert after["velocities"][i] is None
        assert after["shifts"][i] is None
    assert after["velocities"] != out["before"]["velocities"]
    actions = [a for a, _ in conn.sent]
    assert actions == ["channels.getStepParams", "channels.setStepParams",
                       "channels.getStepParams"]
    assert set(conn.sent[1][1]["values"]) == {"velocity", "shift"}
    # the same seed on the same start values gives the same result
    conn2 = FakeConnection()
    conn2.bits = [i % 2 == 0 for i in range(16)]
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn2)
    mcp2 = RecordingMCP()
    register_channel_tools(mcp2)
    again = mcp2.tools["fl_humanize_steps"](0, velocity_jitter=0.2, shift_jitter=6, seed=42)
    assert again["after"] == after


def test_humanize_tool_draws_a_seed_when_none_given(env):
    tools, conn = env
    conn.bits = [True] * 16
    out = tools["fl_humanize_steps"](0, velocity_jitter=0.1)
    assert isinstance(out["seed"], int)
    assert out["verified"] is True
    assert "shift" not in conn.sent[1][1]["values"]


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"velocity_jitter": 1.5}, "velocity_jitter"),
        ({"velocity_jitter": 0.0, "shift_jitter": 0}, "Nothing to do"),
        ({"shift_jitter": -1}, "shift_jitter"),
        ({"shift_jitter": 23}, "exceeds the usable range (0..22 ticks"),
        ({"steps": 0}, "steps"),
    ],
)
def test_humanize_tool_validation(env, kwargs, fragment):
    tools, conn = env
    out = tools["fl_humanize_steps"](0, **kwargs)
    assert fragment in out["error"]
    assert not any(a == "channels.setStepParams" for a, _ in conn.sent)


# --- controller handlers against the stubs ----------------------------------


def test_controller_get_step_params_reads_requested_params(controller):
    dev, store, calls, bits = controller
    bits[(0, 0)] = 1
    store[(0, 0, 1)] = 127
    store[(0, 3, 7)] = 84  # FL reports the absolute tick position (step 3 + 12)
    out = dev.handle_channels_get_step_params({"channel": 0, "steps": 4,
                                               "params": ["velocity", "shift"]})
    assert out["steps"] == [True, False, False, False]
    assert out["velocity"] == [127, 100, 100, 100]
    assert out["shift"] == [0, 0, 0, 84]  # raw, the server makes it relative
    assert "pan" not in out
    assert out["step_ticks"] == 24
    assert out["channel_name"] == "HiHat"


def test_controller_get_step_params_rejects_unknown_param(controller):
    dev, *_ = controller
    out = dev.handle_channels_get_step_params({"channel": 0, "params": ["swing"]})
    assert "Unknown step parameter" in out["error"]


def test_controller_set_step_params_writes_on_current_pattern(controller):
    dev, store, calls, bits = controller
    out = dev.handle_channels_set_step_params({
        "channel": 0, "steps": [True, False, True],
        "values": {"velocity": [127, 51, 90], "pan": [0, 127, 64], "shift": [0, 22, 6],
                   "pitch": [42, 46, 42]},
    })
    assert out["pattern"] == 4
    assert out["written"] == {"velocity": 2, "pan": 2, "shift": 2, "pitch": 2}
    assert out["skipped_off_steps"] == [1]
    assert bits == {(0, 0): 1, (0, 1): 0, (0, 2): 1}
    assert (0, 4, 0, 1, 127, True) in calls
    assert (0, 4, 2, 7, 54, True) in calls  # shift 6 on step 2 -> absolute tick 54
    assert (0, 4, 2, 0, 42, True) in calls
    assert not any(c[2] == 1 for c in calls)  # off step never written
    assert store[(0, 2, 4)] == 64


@pytest.mark.parametrize(
    "values, fragment",
    [
        ({"velocity": [128]}, "velocity[0] = 128 out of range 0..127"),
        ({"shift": [0, 23]}, "shift[1] = 23 out of range 0..22"),
        ({"pan": [-1]}, "pan[0] = -1"),
        ({"pan": [True]}, "pan[0] = True"),
        ({"velocity": 100}, "must be a list"),
        ({"swing": [1]}, "Unknown step parameter"),
    ],
)
def test_controller_set_step_params_validates_before_writing(controller, values, fragment):
    dev, store, calls, bits = controller
    out = dev.handle_channels_set_step_params({"channel": 0, "values": values})
    assert fragment in out["error"]
    assert calls == []
