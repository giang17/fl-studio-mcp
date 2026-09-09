"""Channel rack control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Step parameters (graph editor values per step)
# ---------------------------------------------------------------------------
#
# FL stores step parameters as integers: velocity and pan 0..127 (defaults 100
# and 64), pitch as a MIDI note, shift in ticks 0..PPQ/4 (one step, only
# delays). The tools speak in velocity 0..1, pan -1..1, shift ticks and MIDI
# notes; these helpers convert both ways.

VELOCITY_RAW_MAX = 127
PAN_RAW_CENTRE = 64
PAN_RAW_MAX = 127


def velocity_to_raw(velocity: float) -> int:
    return int(round(velocity * VELOCITY_RAW_MAX))


def raw_to_velocity(raw: int) -> float:
    return round(raw / VELOCITY_RAW_MAX, 3)


def pan_to_raw(pan: float) -> int:
    """-1..1 -> 0..127 with 0.0 exactly on FL's centre value 64."""
    if pan >= 0:
        return PAN_RAW_CENTRE + int(round(pan * (PAN_RAW_MAX - PAN_RAW_CENTRE)))
    return PAN_RAW_CENTRE + int(round(pan * PAN_RAW_CENTRE))


def raw_to_pan(raw: int) -> float:
    if raw >= PAN_RAW_CENTRE:
        return round((raw - PAN_RAW_CENTRE) / (PAN_RAW_MAX - PAN_RAW_CENTRE), 3)
    return round((raw - PAN_RAW_CENTRE) / PAN_RAW_CENTRE, 3)


def _check_list(name: str, values, length: int) -> str | None:
    if not isinstance(values, list):
        return f"Error: {name} must be a list"
    if len(values) != length:
        return f"Error: {name} has {len(values)} entries, expected {length} (one per step)"
    return None


def build_step_param_values(
    length: int,
    velocities: list[float] | None = None,
    pans: list[float] | None = None,
    shifts: list[int] | None = None,
    pitches: list[int] | None = None,
) -> dict[str, list[int]] | str:
    """Validate the per-step lists and convert them to FL's raw integers.

    Returns the mapping the controller expects (parameter name -> raw list),
    or an error string. Nothing is sent when any list is invalid.
    """
    raw: dict[str, list[int]] = {}
    if velocities is not None:
        if (err := _check_list("velocities", velocities, length)):
            return err
        for i, v in enumerate(velocities):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
                return f"Error: velocities[{i}] = {v!r} must be between 0.0 and 1.0"
        raw["velocity"] = [velocity_to_raw(v) for v in velocities]
    if pans is not None:
        if (err := _check_list("pans", pans, length)):
            return err
        for i, v in enumerate(pans):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not -1.0 <= v <= 1.0:
                return f"Error: pans[{i}] = {v!r} must be between -1.0 and 1.0"
        raw["pan"] = [pan_to_raw(v) for v in pans]
    if shifts is not None:
        if (err := _check_list("shifts", shifts, length)):
            return err
        for i, v in enumerate(shifts):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return (
                    f"Error: shifts[{i}] = {v!r} must be a tick count >= 0"
                    " (max PPQ/4 - 2, i.e. 22 at PPQ 96)"
                )
        raw["shift"] = list(shifts)
    if pitches is not None:
        if (err := _check_list("pitches", pitches, length)):
            return err
        for i, v in enumerate(pitches):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 127:
                return f"Error: pitches[{i}] = {v!r} must be a MIDI note 0..127"
        raw["pitch"] = list(pitches)
    return raw


def shift_max(step_ticks: int) -> int:
    """Largest usable shift: one tick less and FL counts the note as the next step."""
    return max(0, int(step_ticks) - 2)


def relative_shifts(raw_shifts: list[int], step_ticks: int) -> list[int | None]:
    """FL reports a step's shift as the absolute tick position of its note.

    -1 means no note (step off). A note one tick before the step's slot
    (the previous step shifted by step_ticks - 1) reads as -1 here; that
    shift is rejected on write, so it only shows up for patterns built by
    hand.
    """
    out: list[int | None] = []
    for i, raw in enumerate(raw_shifts):
        if raw is None or raw < 0:
            out.append(None)
        else:
            out.append(raw - i * step_ticks if step_ticks else raw)
    return out


def normalise_step_params(result: dict) -> dict:
    """Turn a channels.getStepParams response into the tool's normalised form.

    Steps that are off carry None for every parameter.
    """
    steps = list(result.get("steps", []))
    step_ticks = int(result.get("step_ticks") or 0)
    out = {
        "steps": steps,
        "step_ticks": step_ticks,
        "active_steps": sum(1 for b in steps if b),
    }

    def per_step(values, convert):
        return [None if v is None or v < 0 else convert(v) for v in values]

    if "velocity" in result:
        out["velocities"] = per_step(result["velocity"], raw_to_velocity)
    if "pan" in result:
        out["pans"] = per_step(result["pan"], raw_to_pan)
    if "shift" in result:
        out["shifts"] = relative_shifts(result["shift"], step_ticks)
    if "pitch" in result:
        out["pitches"] = per_step(result["pitch"], int)
    return out


def verify_step_params(raw: dict[str, list[int]], readback: dict) -> list[str]:
    """Compare what was sent with what FL reports; returns human-readable diffs.

    Only steps that are on are compared (FL stores nothing for the others).
    Shift is compared relative to the step, like it was sent.
    """
    diffs = []
    active = list(readback.get("steps", []))
    step_ticks = int(readback.get("step_ticks") or 0)
    for name, sent in raw.items():
        got = readback.get(name)
        if got is None:
            diffs.append(f"{name}: not readable")
            continue
        if name == "shift":
            got = relative_shifts(got, step_ticks)
        for i, (a, b) in enumerate(zip(sent, got)):
            if i < len(active) and not active[i]:
                continue
            if a != b:
                diffs.append(f"{name}[{i}]: sent {a}, FL reports {b}")
    return diffs


def humanize_values(
    velocities_raw: list[int],
    shifts_raw: list[int],
    active: list[bool],
    velocity_jitter: float,
    shift_jitter: int,
    step_ticks: int,
    seed: int,
    only_active: bool = True,
) -> tuple[list[int], list[int]]:
    """Deterministic jitter on raw velocity/shift values.

    Velocity moves by up to +-velocity_jitter (fraction of full scale), shift
    by up to +-shift_jitter ticks, both clamped to FL's ranges. Shift cannot
    go below 0 (FL only delays steps), so a step on the grid gets delayed or
    stays put. `shifts_raw` are relative to the step (see relative_shifts).
    """
    import random

    rng = random.Random(seed)
    vel_out = list(velocities_raw)
    shift_out = list(shifts_raw)
    top = shift_max(step_ticks)
    for i in range(len(vel_out)):
        is_on = i < len(active) and bool(active[i])
        if not is_on:
            # Off steps have no values in FL; keep FL's defaults in the list
            # so the write skips them cleanly.
            if vel_out[i] is None or vel_out[i] < 0:
                vel_out[i] = 100
            if i < len(shift_out) and (shift_out[i] is None or shift_out[i] < 0):
                shift_out[i] = 0
            if only_active:
                continue
        if velocity_jitter > 0:
            delta = rng.uniform(-velocity_jitter, velocity_jitter) * VELOCITY_RAW_MAX
            vel_out[i] = max(0, min(VELOCITY_RAW_MAX, int(round(vel_out[i] + delta))))
        if shift_jitter > 0 and i < len(shift_out):
            delta = rng.randint(-shift_jitter, shift_jitter)
            shift_out[i] = max(0, min(top, shift_out[i] + delta))
    return vel_out, shift_out


def register_channel_tools(mcp: FastMCP) -> None:
    """Register channel rack tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_get_channel_count(global_count: bool = True) -> int:
        """Get the number of channels in the channel rack.

        Args:
            global_count: If True, returns total channels ignoring groups.
                         If False, returns channels in current group only.
        """
        conn = get_connection()
        result = conn.send_command("channels.getCount", {"global_count": global_count})

        if not result.get("success", False) and "error" in result:
            return -1

        return result.get("count", 0)

    @mcp.tool()
    def fl_get_channel_info(index: int, use_global_index: bool = True) -> dict:
        """Get detailed information about a channel.

        Args:
            index: Channel index
            use_global_index: Whether to use global channel indexing
        """
        conn = get_connection()
        result = conn.send_command("channels.getInfo", {
            "index": index,
            "use_global": use_global_index,
        })

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "index": result.get("index", index),
            "name": result.get("name", ""),
            "color": result.get("color", "0x0"),
            "volume": result.get("volume", 0),
            "pan": result.get("pan", 0),
            "pitch": result.get("pitch", 0),
            "is_muted": result.get("is_muted", False),
            "is_solo": result.get("is_solo", False),
            "is_selected": result.get("is_selected", False),
            "target_fx_track": result.get("target_fx_track", 0),
        }

    @mcp.tool()
    def fl_get_all_channels() -> list[dict]:
        """Get information about all channels in the channel rack.

        Returns a list of all channels with their basic properties.
        """
        conn = get_connection()
        result = conn.send_command("channels.getAll")

        if not result.get("success", False) and "error" in result:
            return [{"error": result["error"]}]

        return result.get("channels", [])

    @mcp.tool()
    def fl_get_selected_channel() -> dict | None:
        """Get information about the currently selected channel.

        Returns None if no channel is selected.
        """
        conn = get_connection()
        result = conn.send_command("channels.getSelected")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return result.get("channel")

    @mcp.tool()
    def fl_select_channel(index: int, select: bool = True) -> str:
        """Select or deselect a channel.

        Args:
            index: Channel index (global)
            select: True to select, False to deselect
        """
        conn = get_connection()
        result = conn.send_command("channels.select", {
            "index": index,
            "select": select,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'selected' if select else 'deselected'}"

    @mcp.tool()
    def fl_select_one_channel(index: int) -> str:
        """Select only one channel, deselecting all others.

        Args:
            index: Channel index (global) to select exclusively
        """
        conn = get_connection()
        result = conn.send_command("channels.selectOne", {"index": index})

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' selected exclusively"

    @mcp.tool()
    def fl_trigger_note(
        channel: int,
        note: int,
        velocity: int = 100,
        midi_channel: int = -1
    ) -> str:
        """Trigger a MIDI note on a channel (real-time, does NOT persist to pattern).

        This triggers the note in real-time. To persist notes in a pattern,
        FL Studio must be in record mode, or use the step sequencer functions.

        Args:
            channel: Channel index (global)
            note: MIDI note number (0-127, where 60 = C5/Middle C)
            velocity: Note velocity (1-127, 0 = note off)
            midi_channel: MIDI channel (-1 for default)
        """
        if not 0 <= note <= 127:
            return "Error: Note must be between 0 and 127"
        if not 0 <= velocity <= 127:
            return "Error: Velocity must be between 0 and 127"

        conn = get_connection()
        result = conn.send_command("channels.triggerNote", {
            "channel": channel,
            "note": note,
            "velocity": velocity,
            "midi_channel": midi_channel,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        note_name = note_names[note % 12]
        octave = (note // 12) - 1

        if velocity == 0:
            return f"Note {note_name}{octave} (MIDI {note}) released on channel {channel}"
        return (
            f"Note {note_name}{octave} (MIDI {note}) triggered with velocity {velocity}"
            f" on channel {channel}"
        )

    @mcp.tool()
    def fl_set_channel_volume(index: int, volume: float) -> str:
        """Set the volume of a channel.

        Args:
            index: Channel index (global)
            volume: Volume level from 0.0 (silence) to 1.0 (full)
        """
        if not 0.0 <= volume <= 1.0:
            return "Error: Volume must be between 0.0 and 1.0"

        conn = get_connection()
        result = conn.send_command("channels.setVolume", {
            "index": index,
            "volume": volume,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {index}")
        new_volume = result.get("volume", volume)
        return f"Channel '{channel_name}' volume set to {new_volume:.2f}"

    @mcp.tool()
    def fl_set_channel_pan(index: int, pan: float) -> str:
        """Set the pan position of a channel.

        Args:
            index: Channel index (global)
            pan: Pan from -1.0 (full left) to 1.0 (full right), 0.0 = center
        """
        if not -1.0 <= pan <= 1.0:
            return "Error: Pan must be between -1.0 and 1.0"

        conn = get_connection()
        result = conn.send_command("channels.setPan", {
            "index": index,
            "pan": pan,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {index}")
        new_pan = result.get("pan", pan)
        return f"Channel '{channel_name}' pan set to {new_pan:.2f}"

    @mcp.tool()
    def fl_mute_channel(index: int, muted: bool | None = None) -> str:
        """Mute or unmute a channel.

        Args:
            index: Channel index (global)
            muted: True to mute, False to unmute, None to toggle
        """
        conn = get_connection()
        result = conn.send_command("channels.mute", {
            "index": index,
            "muted": muted,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        is_muted = result.get("is_muted", False)
        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'muted' if is_muted else 'unmuted'}"

    @mcp.tool()
    def fl_solo_channel(index: int, solo: bool | None = None) -> str:
        """Solo or unsolo a channel.

        Args:
            index: Channel index (global)
            solo: True to solo, False to unsolo, None to toggle
        """
        conn = get_connection()
        result = conn.send_command("channels.solo", {
            "index": index,
            "solo": solo,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        is_solo = result.get("is_solo", False)
        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'soloed' if is_solo else 'unsoloed'}"

    @mcp.tool()
    def fl_set_channel_name(index: int, name: str) -> str:
        """Set the name of a channel.

        Args:
            index: Channel index (global)
            name: New name for the channel
        """
        conn = get_connection()
        result = conn.send_command("channels.setName", {
            "index": index,
            "name": name,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return f"Channel {index} renamed to '{name}'"

    @mcp.tool()
    def fl_set_channel_color(index: int, red: int, green: int, blue: int) -> str:
        """Set the color of a channel.

        Args:
            index: Channel index (global)
            red: Red component (0-255)
            green: Green component (0-255)
            blue: Blue component (0-255)
        """
        conn = get_connection()
        result = conn.send_command("channels.setColor", {
            "index": index,
            "r": red,
            "g": green,
            "b": blue,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return f"Channel {index} color set to RGB({red}, {green}, {blue})"

    @mcp.tool()
    def fl_route_channel_to_mixer(channel_index: int, mixer_track: int) -> str:
        """Route a channel to a specific mixer track.

        Args:
            channel_index: Channel index (global)
            mixer_track: Mixer track index to route to
        """
        conn = get_connection()
        result = conn.send_command("channels.routeToMixer", {
            "channel_index": channel_index,
            "mixer_track": mixer_track,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {channel_index}")
        return f"Channel '{channel_name}' routed to mixer track {mixer_track}"

    # Step Sequencer Tools

    @mcp.tool()
    def fl_get_grid_bit(channel: int, position: int) -> bool:
        """Get whether a step is active in the step sequencer.

        Args:
            channel: Channel index (global)
            position: Step position (0-based)
        """
        conn = get_connection()
        result = conn.send_command("channels.getGridBit", {
            "channel": channel,
            "position": position,
        })

        if not result.get("success", False) and "error" in result:
            return False

        return result.get("value", False)

    @mcp.tool()
    def fl_set_grid_bit(channel: int, position: int, value: bool) -> str:
        """Set a step in the step sequencer on or off.

        This allows basic pattern programming for drum-style instruments.

        Args:
            channel: Channel index (global)
            position: Step position (0-based)
            value: True to enable the step, False to disable
        """
        conn = get_connection()
        result = conn.send_command("channels.setGridBit", {
            "channel": channel,
            "position": position,
            "value": value,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {channel}")
        return f"Channel '{channel_name}' step {position} {'enabled' if value else 'disabled'}"

    @mcp.tool()
    def fl_get_step_sequence(
        channel: int,
        steps: int = 16,
        include_params: bool = False,
    ) -> list[bool] | dict:
        """Get the step sequence of a channel in the current pattern.

        By default returns one bool per step (True = step on). With
        include_params=True returns a dict with "steps" plus the per-step
        graph editor values: "velocities" (0..1), "pans" (-1..1, 0 = centre),
        "shifts" (delay in ticks, 0..step_ticks-2) and "pitches" (MIDI note
        per step), plus "step_ticks" (ticks per step = PPQ/4) and
        "active_steps". Steps that are off have no values in FL and read as
        null.

        Args:
            channel: Channel index (global)
            steps: Number of steps to retrieve (default 16)
            include_params: Also read velocity, pan, shift and pitch per step
        """
        if steps < 1:
            return {"error": "steps must be >= 1"} if include_params else []
        conn = get_connection()
        if not include_params:
            result = conn.send_command("channels.getStepSequence", {
                "channel": channel,
                "steps": steps,
            })
            if not result.get("success", False) and "error" in result:
                return []
            return result.get("sequence", [])

        result = conn.send_command("channels.getStepParams", {
            "channel": channel,
            "steps": steps,
            "params": ["velocity", "pan", "shift", "pitch"],
        })
        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}
        out = normalise_step_params(result)
        out["channel_name"] = result.get("channel_name", f"Channel {channel}")
        return out

    @mcp.tool()
    def fl_set_step_sequence(
        channel: int,
        pattern: list[bool],
        velocities: list[float] | None = None,
        pans: list[float] | None = None,
        shifts: list[int] | None = None,
        pitches: list[int] | None = None,
    ) -> str:
        """Set a complete step sequence for a channel in the current pattern.

        Besides the on/off grid, the per-step graph editor values can be set
        in the same call. Each optional list must have exactly one entry per
        step (same length as pattern); values are written for every step, on
        or off, and verified by reading them back from FL.

        Args:
            channel: Channel index (global)
            pattern: One bool per step (True = on, False = off)
            velocities: Velocity per step, 0.0..1.0 (FL default 100/127 = 0.787).
                Accents ~0.9-1.0, ghost notes ~0.3-0.5.
            pans: Pan per step, -1.0 (left) .. 1.0 (right), 0.0 = centre
            shifts: Delay per step in ticks, 0..PPQ/4-2 (at PPQ 96: 0..22).
                Only positive: FL's shift delays a step, it cannot pull it earlier.
                Note that setting the grid resets every step's values first, so
                pass all parameters you want in one call.
            pitches: MIDI note per step (e.g. 42 = closed hi-hat on a GM drum
                channel). Lets one multi-sample channel play different sounds.
        """
        if not isinstance(pattern, list) or not pattern:
            return "Error: pattern must be a non-empty list of booleans"
        raw = build_step_param_values(len(pattern), velocities, pans, shifts, pitches)
        if isinstance(raw, str):
            return raw

        conn = get_connection()
        if "shift" in raw:
            # Check the shift range before touching FL: setting the grid
            # resets the steps' values, so a rejected write would already
            # have wiped them.
            tempo = conn.send_command("transport.getTempo")
            ppq = int(tempo.get("ppq") or 0)
            if ppq:
                top = shift_max(ppq // 4)
                for i, v in enumerate(raw["shift"]):
                    if v > top:
                        return (
                            f"Error: shifts[{i}] = {v} exceeds the usable range"
                            f" 0..{top} ticks ({ppq // 4} ticks per step)"
                        )
        result = conn.send_command("channels.setStepSequence", {
            "channel": channel,
            "pattern": pattern,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        channel_name = result.get("channel_name", f"Channel {channel}")
        active_steps = result.get("active_steps", sum(pattern))
        total_steps = result.get("total_steps", len(pattern))
        summary = (
            f"Channel '{channel_name}' pattern set with {active_steps}/{total_steps} steps active"
        )
        if not raw:
            return summary

        written = conn.send_command("channels.setStepParams", {
            "channel": channel,
            "values": raw,
        })
        if not written.get("success", False) and "error" in written:
            return f"{summary}; step parameters NOT written: {written['error']}"

        readback = conn.send_command("channels.getStepParams", {
            "channel": channel,
            "steps": len(pattern),
            "params": list(raw),
        })
        names = ", ".join(raw)
        if not readback.get("success", False) and "error" in readback:
            return (
                f"{summary}; {names} written to pattern {written.get('pattern')}"
                f" but could not be verified: {readback['error']}"
            )
        diffs = verify_step_params(raw, readback)
        if diffs:
            return (
                f"{summary}; {names} written to pattern {written.get('pattern')}"
                f" but readback differs: " + "; ".join(diffs)
            )
        return (
            f"{summary}; {names} written to pattern {written.get('pattern')}"
            f" and verified (step = {readback.get('step_ticks')} ticks)"
        )

    @mcp.tool()
    def fl_humanize_steps(
        channel: int,
        velocity_jitter: float = 0.15,
        shift_jitter: int = 0,
        seed: int | None = None,
        steps: int = 16,
        only_active: bool = True,
    ) -> dict:
        """Add deterministic random variation to a channel's step velocities and shifts.

        Reads the current per-step values from FL, jitters them and writes
        them back, then reads again to report the result. The same seed on
        the same starting values always gives the same outcome; without a
        seed one is drawn and returned so the run can be repeated.

        Args:
            channel: Channel index (global)
            velocity_jitter: Max velocity change per step as a fraction of full
                scale (0.15 = +-15 percent). 0 leaves velocities alone.
            shift_jitter: Max timing change per step in ticks (a step is
                PPQ/4 ticks, usable delay 0..PPQ/4-2, at PPQ 96 that is 0..22).
                FL can only delay a step, so a step already on the grid is
                delayed or stays put. 0 = off.
            seed: Random seed for reproducible results
            steps: Number of steps to process (default 16)
            only_active: Only touch steps that are switched on (default True)
        """
        import random

        if not 0.0 <= velocity_jitter <= 1.0:
            return {"error": "velocity_jitter must be between 0.0 and 1.0"}
        if isinstance(shift_jitter, bool) or not isinstance(shift_jitter, int) or shift_jitter < 0:
            return {"error": "shift_jitter must be a tick count >= 0"}
        if steps < 1:
            return {"error": "steps must be >= 1"}
        if velocity_jitter == 0 and shift_jitter == 0:
            return {"error": "Nothing to do: velocity_jitter and shift_jitter are both 0"}

        conn = get_connection()
        before = conn.send_command("channels.getStepParams", {
            "channel": channel,
            "steps": steps,
            "params": ["velocity", "shift"],
        })
        if not before.get("success", False) and "error" in before:
            return {"error": before["error"]}

        step_ticks = int(before.get("step_ticks") or 0)
        if shift_jitter > shift_max(step_ticks):
            return {"error": (f"shift_jitter {shift_jitter} exceeds the usable range"
                              f" (0..{shift_max(step_ticks)} ticks at {step_ticks} per step)")}
        if seed is None:
            seed = random.randrange(2**31)
        active = list(before.get("steps", []))
        vel_new, shift_new = humanize_values(
            before.get("velocity", []),
            relative_shifts(before.get("shift", []), step_ticks),
            active, velocity_jitter, shift_jitter, step_ticks, seed, only_active,
        )
        raw: dict[str, list[int]] = {}
        if velocity_jitter > 0:
            raw["velocity"] = vel_new
        if shift_jitter > 0:
            raw["shift"] = shift_new

        written = conn.send_command("channels.setStepParams", {
            "channel": channel,
            "values": raw,
        })
        if not written.get("success", False) and "error" in written:
            return {"error": written["error"], "seed": seed}

        after = conn.send_command("channels.getStepParams", {
            "channel": channel,
            "steps": steps,
            "params": list(raw),
        })
        if not after.get("success", False) and "error" in after:
            return {"error": f"written but not verified: {after['error']}", "seed": seed}

        touched = [i for i in range(len(active)) if not only_active or active[i]]
        out = {
            "channel_name": before.get("channel_name", f"Channel {channel}"),
            "pattern": written.get("pattern"),
            "seed": seed,
            "steps_touched": touched,
            "step_ticks": step_ticks,
            "before": normalise_step_params(before),
            "after": normalise_step_params(after),
        }
        diffs = verify_step_params(raw, after)
        out["verified"] = not diffs
        if diffs:
            out["differences"] = diffs
        return out
