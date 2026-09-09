# name=FL Studio MCP Controller
# url=https://github.com/karl-andres/fl-studio-mcp
# supportedDevices=FL Studio MCP

"""
FL Studio MIDI Controller Script for MCP Integration.

This script runs inside FL Studio and receives MIDI trigger messages from the
MCP server. When triggered, it reads a command from a JSON file, executes the
corresponding FL Studio API function, and writes the response to another JSON file.

Communication flow:
1. MCP server writes command to mcp_command.json
2. MCP server sends MIDI note 127 (trigger)
3. This script receives the trigger via OnMidiMsg()
4. Script reads command JSON, executes FL Studio API
5. Script writes response to mcp_response.json
6. MCP server reads response
"""

import json
import os
import sys
from pathlib import Path

# FL Studio API modules (available when running inside FL Studio)
import channels
import mixer
import plugins
import transport


def _get_script_dir() -> Path:
    """Get the script directory path.

    FL Studio's Python environment doesn't support __file__, so we construct
    the path based on the platform's standard FL Studio settings location.
    """
    if sys.platform == "darwin":
        # macOS
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    elif sys.platform == "win32":
        # Windows
        userprofile = os.environ.get("USERPROFILE", "~")
        base = Path(userprofile) / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    else:
        # Linux (unlikely but handle it)
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"

    return base / "Hardware" / "FLStudioMCP"


# File paths for JSON communication
SCRIPT_DIR = _get_script_dir()
COMMAND_FILE = SCRIPT_DIR / "mcp_command.json"
RESPONSE_FILE = SCRIPT_DIR / "mcp_response.json"

# MIDI trigger note
TRIGGER_NOTE = 127


def OnInit():
    """Called when the script is loaded."""
    print("FL Studio MCP Controller initialized")
    print(f"Command file: {COMMAND_FILE}")
    print(f"Response file: {RESPONSE_FILE}")


def OnDeInit():
    """Called when the script is unloaded."""
    print("FL Studio MCP Controller deinitialized")


def OnMidiMsg(event):
    """Called when a MIDI message is received."""
    # Check for trigger note (Note On, note 127)
    if event.midiId == 0x90 and event.data1 == TRIGGER_NOTE and event.data2 > 0:
        execute_pending_command()
        event.handled = True


def OnIdle():
    """Called periodically when FL Studio is idle."""
    pass  # Required FL Studio API hook; no polling needed


def execute_pending_command():
    """Read command from JSON file, execute it, and write response."""
    response = {"success": False, "error": None}

    try:
        # Read command file
        if not COMMAND_FILE.exists():
            response["error"] = "No command file found"
            write_response(response)
            return

        command_text = COMMAND_FILE.read_text()
        command = json.loads(command_text)

        action = command.get("action", "")
        params = command.get("params", {})

        # Execute command and get result
        result = dispatch_command(action, params)
        # A handler that returns {"error": ...} has failed; do not mark it a success.
        response = {"success": not result.get("error"), **result}

    except json.JSONDecodeError as e:
        response["error"] = f"Invalid JSON in command file: {e}"
    except Exception as e:
        response["error"] = f"Error executing command: {e}"

    write_response(response)


# ---------------------------------------------------------------------------
# Pattern management (requires newer FL Studio, lazy-imported)
# ---------------------------------------------------------------------------

def _patterns_mod():
    """Lazily import the patterns module (not available on older FL versions)."""
    import patterns  # noqa: PLC0415
    return patterns


def _ui_mod():
    """Lazily import the ui module."""
    import ui  # noqa: PLC0415
    return ui


# FL window indices (midi.wid* constants) by the names the MCP tools accept.
_WINDOWS = {
    "mixer": 0,
    "channel_rack": 1,
    "playlist": 2,
    "piano_roll": 3,
    "browser": 4,
}


def _window_index(params: dict) -> int:
    """Resolve the 'window' parameter (name or index) to an FL window index."""
    window = params.get("window", "piano_roll")
    if isinstance(window, int) and not isinstance(window, bool):
        return window
    index = _WINDOWS.get(str(window).lower())
    if index is None:
        raise ValueError("Unknown window '%s'. Valid: %s" % (window, ", ".join(_WINDOWS)))
    return index


def _window_report(ui, index: int) -> dict:
    """Visibility/focus of one window plus the caption of whatever is focused."""
    name = next((n for n, i in _WINDOWS.items() if i == index), str(index))
    return {
        "window": name,
        "visible": bool(ui.getVisible(index)),
        "focused": bool(ui.getFocused(index)),
        "focused_caption": ui.getFocusedFormCaption(),
    }


def handle_ui_get_focus(params: dict) -> dict:
    """Report which FL windows are visible/focused and the focused window's caption.

    The caption of a focused piano roll is "Piano roll - <channel name>", which
    is the only way to learn which channel an open piano roll targets.
    """
    try:
        ui = _ui_mod()
        return {
            "focused": {n: bool(ui.getFocused(i)) for n, i in _WINDOWS.items()},
            "visible": {n: bool(ui.getVisible(i)) for n, i in _WINDOWS.items()},
            "caption": ui.getFocusedFormCaption(),
            "form_id": ui.getFocusedFormID(),
            "plugin": ui.getFocusedPluginName(),
        }
    except Exception as e:
        return {"error": str(e)}


def handle_ui_show_window(params: dict) -> dict:
    """Show an FL window and (by default) give it FL-internal focus."""
    try:
        ui = _ui_mod()
        index = _window_index(params)
        ui.showWindow(index)
        if params.get("focus", True):
            ui.setFocused(index)
        return _window_report(ui, index)
    except Exception as e:
        return {"error": str(e)}


def handle_ui_hide_window(params: dict) -> dict:
    """Hide an FL window."""
    try:
        ui = _ui_mod()
        index = _window_index(params)
        ui.hideWindow(index)
        return _window_report(ui, index)
    except Exception as e:
        return {"error": str(e)}


def handle_ui_set_focus(params: dict) -> dict:
    """Give an FL window FL-internal focus (without changing its visibility)."""
    try:
        ui = _ui_mod()
        index = _window_index(params)
        ui.setFocused(index)
        return _window_report(ui, index)
    except Exception as e:
        return {"error": str(e)}


def handle_pianoroll_open(params: dict) -> dict:
    """Retarget the piano roll to a channel and make it FL's focused window.

    An open piano roll ignores channels selected via scripting, and merely
    showing/focusing it does not help either. What does work (verified against
    FL 2026): hide the piano roll, select the channel, show it again - a
    freshly shown piano roll picks up the selected channel. Without "index"
    the piano roll is only shown and focused, its target is left alone.
    """
    try:
        ui = _ui_mod()
        pr = _WINDOWS["piano_roll"]
        index = params.get("index")
        if index is not None:
            index = int(index)
            if index < 0 or index >= channels.channelCount(True):
                return {"error": "Channel index %d out of range (0..%d)"
                        % (index, channels.channelCount(True) - 1)}
            ui.hideWindow(pr)
            channels.selectOneChannel(index, True)
        ui.showWindow(pr)
        ui.setFocused(pr)
        report = _window_report(ui, _WINDOWS["piano_roll"])
        if index is not None:
            report["channel"] = {"index": index, "name": channels.getChannelName(index, True)}
        return report
    except Exception as e:
        return {"error": str(e)}


def _pattern_index_error(patterns, index: int) -> dict | None:
    """Return an error dict for indices outside 1..patternMax, else None.

    FL silently creates or selects unexpected patterns for out-of-range
    indices instead of failing, so validate before calling into the API.
    """
    max_index = patterns.patternMax()
    if index < 1 or index > max_index:
        return {"error": f"Pattern index {index} out of range (1..{max_index})"}
    return None


def _pattern_length(patterns, index: int) -> dict:
    """Pattern length in steps (what getPatternLength reports) and in bars.

    FL counts 4 steps per beat; 64 steps are 4 bars in 4/4. The bar count is
    derived from the project timebase and may be fractional.
    """
    steps = patterns.getPatternLength(index)
    out = {"length_steps": steps}
    try:
        general = _general_mod()
        ppq, ppb = general.getRecPPQ(), general.getRecPPB()
        beats_per_bar = ppb // ppq if ppq else 0
    except Exception:
        beats_per_bar = 0
    if beats_per_bar:
        out["length_bars"] = steps / (4 * beats_per_bar)
    return out


def handle_patterns_get_count(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        return {
            "count": patterns.patternCount(),
            "current": patterns.patternNumber(),
            "max": patterns.patternMax(),
        }
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_get_all(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        include_default = params.get("include_default", False)
        max_index = patterns.patternMax()
        out = []
        for i in range(1, max_index + 1):
            is_default = patterns.isPatternDefault(i)
            if is_default and not include_default:
                continue
            out.append({
                "index": i,
                "name": patterns.getPatternName(i),
                "color": patterns.getPatternColor(i),
                **_pattern_length(patterns, i),
                "is_default": is_default,
                "is_selected": patterns.isPatternSelected(i),
            })
        return {"patterns": out, "current": patterns.patternNumber()}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_get_current(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        i = patterns.patternNumber()
        return {
            "index": i,
            "name": patterns.getPatternName(i),
            **_pattern_length(patterns, i),
        }
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_select(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        index = int(params.get("index", 0))
        error = _pattern_index_error(patterns, index)
        if error:
            return error
        patterns.jumpToPattern(index)
        return {"selected": index, "name": patterns.getPatternName(index)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_new_empty(params: dict) -> dict:
    """Select the first/next empty pattern (automation-safe: no name prompt).

    Note: patterns are virtual in FL; this jumps to an unused one, which is
    the closest equivalent to "create new pattern".
    """
    try:
        patterns = _patterns_mod()
        FFNEP_DontPromptName = 1 << 1  # from midi module flags
        patterns.findFirstNextEmptyPat(FFNEP_DontPromptName)
        i = patterns.patternNumber()
        return {"selected": i, "name": patterns.getPatternName(i)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_clone(params: dict) -> dict:
    """Clone a pattern. NOTE: this closes the piano roll (FL behaviour)."""
    try:
        patterns = _patterns_mod()
        index = params.get("index")
        if index is None:
            patterns.clonePattern()
            new_index = patterns.patternNumber()
        else:
            error = _pattern_index_error(patterns, int(index))
            if error:
                return error
            patterns.clonePattern(int(index))
            new_index = patterns.patternNumber()
        return {"cloned_to": new_index, "name": patterns.getPatternName(new_index)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_set_name(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        index = int(params.get("index", 0))
        name = str(params.get("name", ""))
        error = _pattern_index_error(patterns, index)
        if error:
            return error
        patterns.setPatternName(index, name)
        return {"index": index, "name": patterns.getPatternName(index)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Playlist tracks and arrangement markers (lazy-imported)
# ---------------------------------------------------------------------------
#
# FL applies transport changes (song position, marker jumps, loop mode) only
# after the script callback returns, and drops a second change made in the
# same callback. Every handler here therefore does one step and reads only
# the state FL has already applied; the server sequences the steps.

# transport.getSongPos / setSongPos mode: absolute ticks.
SONGLENGTH_ABSTICKS = 2
# Upper bound for the marker name scan; FL returns "" past the last marker.
MARKER_NAME_LIMIT = 512


def _playlist_mod():
    """Lazily import the playlist module."""
    import playlist  # noqa: PLC0415
    return playlist


def _arrangement_mod():
    """Lazily import the arrangement module."""
    import arrangement  # noqa: PLC0415
    return arrangement


def _timebase() -> tuple:
    """Return (ppq, ppb): ticks per beat and ticks per bar."""
    general = _general_mod()
    return general.getRecPPQ(), general.getRecPPB()


def _ticks_to_bst(ticks: int, ppq: int, ppb: int) -> dict:
    """Split absolute ticks into a 1-based bar, beat and the tick remainder."""
    if not ppq or not ppb:
        return {"bar": None, "beat": None, "tick": None}
    return {
        "bar": ticks // ppb + 1,
        "beat": (ticks % ppb) // ppq + 1,
        "tick": ticks % ppq,
    }


def _color_to_rgb(color: int) -> dict:
    """FL colors are 0x--BBGGRR (reported as signed ints)."""
    return {"r": color & 0xFF, "g": (color >> 8) & 0xFF, "b": (color >> 16) & 0xFF}


def _playlist_track_index_error(playlist, index) -> dict | None:
    count = playlist.trackCount()
    if isinstance(index, bool) or not isinstance(index, int) or index < 1 or index > count:
        return {"error": f"Playlist track index {index!r} out of range (1..{count})"}
    return None


def _is_default_track_name(index: int, name: str) -> bool:
    """Unnamed playlist tracks read back as "Track <n>"."""
    return name.strip() == f"Track {index}"


def _playlist_track_report(playlist, index: int) -> dict:
    name = playlist.getTrackName(index)
    color = playlist.getTrackColor(index) & 0xFFFFFF
    return {
        "index": index,
        "name": name,
        "is_unnamed": _is_default_track_name(index, name),
        "color": "#%02x%02x%02x" % (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF),
        "is_muted": bool(playlist.isTrackMuted(index)),
        "is_solo": bool(playlist.isTrackSolo(index)),
        "is_selected": bool(playlist.isTrackSelected(index)),
    }


def handle_playlist_get_tracks(params: dict) -> dict:
    """List playlist tracks; unnamed, untouched tracks are skipped by default."""
    try:
        playlist = _playlist_mod()
        include_unnamed = bool(params.get("include_unnamed", False))
        limit = int(params.get("limit", 50))
        count = playlist.trackCount()
        out = []
        for i in range(1, count + 1):
            report = _playlist_track_report(playlist, i)
            if (
                not include_unnamed
                and report["is_unnamed"]
                and not (report["is_muted"] or report["is_solo"] or report["is_selected"])
            ):
                continue
            out.append(report)
            if len(out) >= limit:
                break
        return {"tracks": out, "track_count": count}
    except ImportError:
        return {"error": "playlist module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_playlist_get_track(params: dict) -> dict:
    try:
        playlist = _playlist_mod()
        index = params.get("index")
        if error := _playlist_track_index_error(playlist, index):
            return error
        return {"track": _playlist_track_report(playlist, index)}
    except ImportError:
        return {"error": "playlist module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_playlist_set_track(params: dict) -> dict:
    """Set name, color, mute, solo and selection of one playlist track.

    Reads nothing back: FL may apply the change after this callback returns,
    so the server fetches the track state in a second call.
    """
    try:
        playlist = _playlist_mod()
        index = params.get("index")
        if error := _playlist_track_index_error(playlist, index):
            return error
        changed = []
        if "name" in params:
            playlist.setTrackName(index, str(params["name"]))
            changed.append("name")
        if params.get("rgb") is not None:
            rgb = params["rgb"]
            r, g, b = int(rgb["r"]), int(rgb["g"]), int(rgb["b"])
            # FL Studio uses BGR format
            playlist.setTrackColor(index, (b << 16) | (g << 8) | r)
            changed.append("color")
        if params.get("muted") is not None:
            playlist.muteTrack(index, 1 if params["muted"] else 0)
            changed.append("muted")
        if params.get("solo") is not None:
            playlist.soloTrack(index, 1 if params["solo"] else 0)
            changed.append("solo")
        if params.get("selected") is not None:
            # selectTrack only toggles, so compare with the current state first
            if bool(playlist.isTrackSelected(index)) != bool(params["selected"]):
                playlist.selectTrack(index)
            changed.append("selected")
        return {"index": index, "changed": changed}
    except ImportError:
        return {"error": "playlist module not available"}
    except Exception as e:
        return {"error": str(e)}


def _position_report(arrangement) -> dict:
    """The song position FL has applied, in absolute ticks and bar:beat:tick.

    `ticks` is the arrangement time (unclamped, also beyond the song end);
    `transport_ticks` is transport.getSongPos, which follows the pattern in
    pattern mode and is clamped to the song length in song mode.
    """
    ppq, ppb = _timebase()
    ticks = arrangement.currentTime(0)
    report = {"ticks": ticks, **_ticks_to_bst(ticks, ppq, ppb)}
    report.update({
        "transport_ticks": transport.getSongPos(SONGLENGTH_ABSTICKS),
        "hint": transport.getSongPosHint(),
        "is_playing": transport.isPlaying() == 1,
        "loop_mode": "song" if transport.getLoopMode() == 1 else "pattern",
        "ppq": ppq,
        "ppb": ppb,
    })
    return report


def _marker_names(arrangement) -> list:
    """Marker names in song order; FL returns "" past the last marker."""
    names = []
    for i in range(MARKER_NAME_LIMIT):
        try:
            name = arrangement.getMarkerName(i)
        except Exception:
            break
        if not name:
            break
        names.append(name)
    return names


def handle_arrangement_get_position(params: dict) -> dict:
    try:
        arrangement = _arrangement_mod()
        return {"position": _position_report(arrangement)}
    except ImportError:
        return {"error": "arrangement module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_arrangement_set_position(params: dict) -> dict:
    """Set the song position in absolute ticks; FL applies it after this call."""
    ticks = params.get("ticks")
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        return {"error": f"ticks must be a non-negative integer, got {ticks!r}"}
    try:
        transport.setSongPos(ticks, SONGLENGTH_ABSTICKS)
        return {"requested_ticks": ticks}
    except Exception as e:
        return {"error": str(e)}


def handle_arrangement_jump_marker(params: dict) -> dict:
    """Jump `delta` markers (song mode only); FL applies it after this call.

    Returns the position before the jump, i.e. the state FL had applied.
    """
    delta = params.get("delta")
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
        return {"error": f"delta must be a non-zero integer, got {delta!r}"}
    try:
        arrangement = _arrangement_mod()
        before = arrangement.currentTime(0)
        arrangement.jumpToMarker(delta, False)
        return {"before_ticks": before}
    except ImportError:
        return {"error": "arrangement module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_arrangement_get_marker_names(params: dict) -> dict:
    try:
        arrangement = _arrangement_mod()
        return {"names": _marker_names(arrangement)}
    except ImportError:
        return {"error": "arrangement module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_arrangement_add_marker(params: dict) -> dict:
    """Add a time marker at absolute ticks; the name list updates at once."""
    name = str(params.get("name", "")).strip()
    ticks = params.get("ticks")
    if not name:
        return {"error": "Marker name must not be empty"}
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        return {"error": f"ticks must be a non-negative integer, got {ticks!r}"}
    try:
        arrangement = _arrangement_mod()
        arrangement.addAutoTimeMarker(ticks, name)
        return {"name": name, "ticks": ticks, "names": _marker_names(arrangement)}
    except ImportError:
        return {"error": "arrangement module not available"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Undo / redo safety net
# ---------------------------------------------------------------------------

def _general_mod():
    import general  # noqa: PLC0415
    return general


def _midi_mod():
    """Lazily import the midi module (REC event ids and flags)."""
    import midi  # noqa: PLC0415
    return midi


def handle_general_undo(params: dict) -> dict:
    try:
        general = _general_mod()
        result = general.undoUp()
        return {"undone": True, "result": result,
                "history_pos": general.getUndoHistoryPos(),
                "history_count": general.getUndoHistoryCount()}
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_general_redo(params: dict) -> dict:
    try:
        general = _general_mod()
        result = general.undoDown()
        return {"redone": True, "result": result,
                "history_pos": general.getUndoHistoryPos(),
                "history_count": general.getUndoHistoryCount()}
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


# UF flags (see FL API docs for general.saveUndo)
_UNDO_FLAGS = {
    "none": 0, "ee": 1, "pr": 2, "playlist": 4, "knob": 32,
    "audio_rec": 256, "auto_clip": 512, "pr_marker": 1024,
    "pl_marker": 2048, "plugin": 4096, "ss_looping": 8192, "reset": 65536,
}


def handle_general_save_undo_point(params: dict) -> dict:
    try:
        general = _general_mod()
        name = str(params.get("name", "MCP undo point"))
        flags_in = params.get("flags", ["pr", "playlist", "knob", "ss_looping"])
        if isinstance(flags_in, str):
            flags_in = [flags_in]
        unknown = [str(f) for f in flags_in if str(f).lower() not in _UNDO_FLAGS]
        if unknown:
            # Ignoring a flag silently would create an undo point that does not
            # cover what the caller assumed.
            return {"error": "Unknown undo flag(s): %s. Valid: %s"
                    % (", ".join(unknown), ", ".join(_UNDO_FLAGS))}
        flags = 0
        for f in flags_in:
            flags |= _UNDO_FLAGS[str(f).lower()]
        general.saveUndo(name, flags)
        return {"saved": True, "name": name, "flags": flags,
                "history_pos": general.getUndoHistoryPos(),
                "history_count": general.getUndoHistoryCount()}
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_general_get_undo_status(params: dict) -> dict:
    try:
        general = _general_mod()
        return {
            "history_count": general.getUndoHistoryCount(),
            "history_pos": general.getUndoHistoryPos(),
            "history_last": general.getUndoHistoryLast(),
            "level_hint": general.getUndoLevelHint(),
            "changed_flag": general.getChangedFlag(),
            "safe_to_edit": general.safeToEdit(),
        }
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


def write_response(response: dict):
    """Write response to JSON file."""
    try:
        RESPONSE_FILE.write_text(json.dumps(response, indent=2))
    except Exception as e:
        print(f"Error writing response: {e}")


def dispatch_command(action: str, params: dict) -> dict:
    """Route command to appropriate handler and return result."""

    # Transport commands
    if action == "transport.start":
        return handle_transport_start()
    elif action == "transport.stop":
        return handle_transport_stop()
    elif action == "transport.record":
        return handle_transport_record()
    elif action == "transport.getStatus":
        return handle_transport_get_status()
    elif action == "transport.setPosition":
        return handle_transport_set_position(params)
    elif action == "transport.getLength":
        return handle_transport_get_length()
    elif action == "transport.setLoopMode":
        return handle_transport_set_loop_mode(params)
    elif action == "transport.setPlaybackSpeed":
        return handle_transport_set_playback_speed(params)
    elif action == "transport.getTempo":
        return handle_transport_get_tempo(params)
    elif action == "transport.setTempo":
        return handle_transport_set_tempo(params)

    # Mixer commands
    elif action == "mixer.getTrackCount":
        return handle_mixer_get_track_count()
    elif action == "mixer.getTrackInfo":
        return handle_mixer_get_track_info(params)
    elif action == "mixer.getAllTracks":
        return handle_mixer_get_all_tracks(params)
    elif action == "mixer.setTrackVolume":
        return handle_mixer_set_track_volume(params)
    elif action == "mixer.setTrackPan":
        return handle_mixer_set_track_pan(params)
    elif action == "mixer.muteTrack":
        return handle_mixer_mute_track(params)
    elif action == "mixer.soloTrack":
        return handle_mixer_solo_track(params)
    elif action == "mixer.armTrack":
        return handle_mixer_arm_track(params)
    elif action == "mixer.setTrackName":
        return handle_mixer_set_track_name(params)
    elif action == "mixer.setTrackColor":
        return handle_mixer_set_track_color(params)
    elif action == "mixer.setStereoSep":
        return handle_mixer_set_stereo_sep(params)

    # Channel commands
    elif action == "channels.getCount":
        return handle_channels_get_count(params)
    elif action == "channels.getInfo":
        return handle_channels_get_info(params)
    elif action == "channels.getAll":
        return handle_channels_get_all()
    elif action == "channels.getSelected":
        return handle_channels_get_selected()
    elif action == "channels.select":
        return handle_channels_select(params)
    elif action == "channels.selectOne":
        return handle_channels_select_one(params)
    elif action == "channels.triggerNote":
        return handle_channels_trigger_note(params)
    elif action == "channels.setVolume":
        return handle_channels_set_volume(params)
    elif action == "channels.setPan":
        return handle_channels_set_pan(params)
    elif action == "channels.mute":
        return handle_channels_mute(params)
    elif action == "channels.solo":
        return handle_channels_solo(params)
    elif action == "channels.setName":
        return handle_channels_set_name(params)
    elif action == "channels.setColor":
        return handle_channels_set_color(params)
    elif action == "channels.routeToMixer":
        return handle_channels_route_to_mixer(params)

    # Step sequencer commands
    elif action == "channels.getGridBit":
        return handle_channels_get_grid_bit(params)
    elif action == "channels.setGridBit":
        return handle_channels_set_grid_bit(params)
    elif action == "channels.getStepSequence":
        return handle_channels_get_step_sequence(params)
    elif action == "channels.setStepSequence":
        return handle_channels_set_step_sequence(params)
    elif action == "channels.getStepParams":
        return handle_channels_get_step_params(params)
    elif action == "channels.setStepParams":
        return handle_channels_set_step_params(params)

    # Plugin commands
    elif action == "plugins.isValid":
        return handle_plugins_is_valid(params)
    elif action == "plugins.getName":
        return handle_plugins_get_name(params)
    elif action == "plugins.getParamCount":
        return handle_plugins_get_param_count(params)
    elif action == "plugins.getParams":
        return handle_plugins_get_params(params)
    elif action == "plugins.getParamValue":
        return handle_plugins_get_param_value(params)
    elif action == "plugins.setParamValue":
        return handle_plugins_set_param_value(params)
    elif action == "plugins.getPresetCount":
        return handle_plugins_get_preset_count(params)
    elif action == "plugins.nextPreset":
        return handle_plugins_next_preset(params)
    elif action == "plugins.prevPreset":
        return handle_plugins_prev_preset(params)
    elif action == "patterns.getCount":
        return handle_patterns_get_count(params)
    elif action == "patterns.getAll":
        return handle_patterns_get_all(params)
    elif action == "patterns.getCurrent":
        return handle_patterns_get_current(params)
    elif action == "patterns.select":
        return handle_patterns_select(params)
    elif action == "patterns.newEmpty":
        return handle_patterns_new_empty(params)
    elif action == "patterns.clone":
        return handle_patterns_clone(params)
    elif action == "patterns.setName":
        return handle_patterns_set_name(params)
    elif action == "general.undo":
        return handle_general_undo(params)
    elif action == "general.redo":
        return handle_general_redo(params)
    elif action == "general.saveUndoPoint":
        return handle_general_save_undo_point(params)
    elif action == "general.getUndoStatus":
        return handle_general_get_undo_status(params)

    # UI / window handling
    elif action == "ui.getFocus":
        return handle_ui_get_focus(params)
    elif action == "ui.showWindow":
        return handle_ui_show_window(params)
    elif action == "ui.hideWindow":
        return handle_ui_hide_window(params)
    elif action == "ui.setFocus":
        return handle_ui_set_focus(params)
    elif action == "pianoroll.open":
        return handle_pianoroll_open(params)
    elif action == "plugins.getColor":
        return handle_plugins_get_color(params)
    elif action == "playlist.getTracks":
        return handle_playlist_get_tracks(params)
    elif action == "playlist.getTrack":
        return handle_playlist_get_track(params)
    elif action == "playlist.setTrack":
        return handle_playlist_set_track(params)
    elif action == "arrangement.getPosition":
        return handle_arrangement_get_position(params)
    elif action == "arrangement.setPosition":
        return handle_arrangement_set_position(params)
    elif action == "arrangement.jumpMarker":
        return handle_arrangement_jump_marker(params)
    elif action == "arrangement.getMarkerNames":
        return handle_arrangement_get_marker_names(params)
    elif action == "arrangement.addMarker":
        return handle_arrangement_add_marker(params)

    else:
        return {"error": f"Unknown action: {action}"}


# =============================================================================
# Transport Handlers
# =============================================================================


def handle_transport_start() -> dict:
    """Toggle play/pause."""
    transport.start()
    return {"is_playing": transport.isPlaying() == 1}


def handle_transport_stop() -> dict:
    """Stop playback."""
    transport.stop()
    return {"stopped": True}


def handle_transport_record() -> dict:
    """Toggle recording."""
    transport.record()
    return {"is_recording": transport.isRecording() == 1}


def handle_transport_get_status() -> dict:
    """Get transport status."""
    loop_mode = transport.getLoopMode()
    return {
        "is_playing": transport.isPlaying() == 1,
        "is_recording": transport.isRecording() == 1,
        "position": transport.getSongPosHint(),
        "loop_mode": "song" if loop_mode == 1 else "pattern",
    }


def handle_transport_set_position(params: dict) -> dict:
    """Set playback position."""
    position = params.get("position", 0)
    mode = params.get("mode", 2)  # Default: seconds
    transport.setSongPos(position, mode)
    return {"position": transport.getSongPosHint()}


def handle_transport_get_length() -> dict:
    """Get song length."""
    return {
        "ticks": transport.getSongLength(3),
        "seconds": transport.getSongLength(2),
        "milliseconds": transport.getSongLength(1),
    }


def handle_transport_set_loop_mode(params: dict) -> dict:
    """Set loop mode."""
    mode = params.get("mode", "pattern")
    current_mode = transport.getLoopMode()
    target_mode = 1 if mode.lower() == "song" else 0

    if current_mode != target_mode:
        transport.setLoopMode()

    return {"mode": mode}


def handle_transport_set_playback_speed(params: dict) -> dict:
    """Set playback speed."""
    speed = params.get("speed", 1.0)
    transport.setPlaybackSpeed(speed)
    return {"speed": speed}


# FL Studio's project tempo range; processRECEvent clamps out-of-range values
# silently, so reject them here instead.
TEMPO_MIN_BPM = 10.0
TEMPO_MAX_BPM = 522.0
# Both mixer.getCurrentTempo() and the REC_Tempo event work in milli-BPM:
# 90 BPM is 90000.
TEMPO_SCALE = 1000


def _read_tempo() -> dict:
    """Read tempo and timebase, deriving the time signature numerator."""
    general = _general_mod()
    raw = mixer.getCurrentTempo()
    ppq = general.getRecPPQ()
    ppb = general.getRecPPB()
    info = {
        "bpm": round(float(raw) / TEMPO_SCALE, 3),
        "raw_tempo": raw,
        "ppq": ppq,
        "ppb": ppb,
    }
    # PPB is PPQ times the beats in a bar, so it yields the numerator of the
    # project time signature (playlist time signature markers are ignored, see
    # the getRecPPB docs). FL exposes no denominator to controller scripts; the
    # piano roll script reports it (flp.score.tsden) via fl_get_pr_context.
    if ppq:
        info["beats_per_bar"] = ppb // ppq
    return info


def handle_transport_get_tempo(params: dict) -> dict:
    """Report tempo, timebase and the time signature numerator."""
    try:
        return _read_tempo()
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_transport_set_tempo(params: dict) -> dict:
    """Set the project tempo through the REC_Tempo event and read it back."""
    try:
        bpm = float(params.get("bpm"))
    except (TypeError, ValueError):
        return {"error": "bpm must be a number"}
    if not TEMPO_MIN_BPM <= bpm <= TEMPO_MAX_BPM:
        return {"error": "bpm must be between %g and %g" % (TEMPO_MIN_BPM, TEMPO_MAX_BPM)}
    try:
        general = _general_mod()
        midi = _midi_mod()
        general.processRECEvent(
            midi.REC_Tempo,
            int(round(bpm * TEMPO_SCALE)),
            midi.REC_Control | midi.REC_UpdateControl,
        )
        result = _read_tempo()
        result["requested_bpm"] = bpm
        return result
    except ImportError:
        return {"error": "general or midi module not available"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# Mixer Handlers
# =============================================================================


def handle_mixer_get_track_count() -> dict:
    """Get number of mixer tracks."""
    return {"count": mixer.trackCount()}


def handle_mixer_get_track_info(params: dict) -> dict:
    """Get info about a mixer track."""
    track = params.get("track", 0)
    return {
        "index": track,
        "name": mixer.getTrackName(track),
        "volume": mixer.getTrackVolume(track),
        "volume_db": mixer.getTrackVolume(track, 1),
        "pan": mixer.getTrackPan(track),
        "stereo_separation": mixer.getTrackStereoSep(track),
        "is_muted": mixer.isTrackMuted(track) == 1,
        "is_solo": mixer.isTrackSolo(track) == 1,
        "is_armed": mixer.isTrackArmed(track) == 1,
        "color": hex(mixer.getTrackColor(track)),
    }


def handle_mixer_get_all_tracks(params: dict) -> dict:
    """Get info about all mixer tracks."""
    include_empty = params.get("include_empty", False)
    tracks = []
    track_count = mixer.trackCount()

    for i in range(track_count):
        name = mixer.getTrackName(i)

        # Skip empty tracks if requested
        if not include_empty and (not name or name.startswith("Insert ")):
            if i != 0:  # Always include master
                continue

        tracks.append({
            "index": i,
            "name": name if name else ("Master" if i == 0 else f"Insert {i}"),
            "volume": mixer.getTrackVolume(i),
            "pan": mixer.getTrackPan(i),
            "is_muted": mixer.isTrackMuted(i) == 1,
            "is_solo": mixer.isTrackSolo(i) == 1,
        })

    return {"tracks": tracks}


def handle_mixer_set_track_volume(params: dict) -> dict:
    """Set mixer track volume."""
    track = params.get("track", 0)
    volume = params.get("volume", 0.8)
    mixer.setTrackVolume(track, volume)
    return {
        "volume": mixer.getTrackVolume(track),
        "volume_db": mixer.getTrackVolume(track, 1),
    }


def handle_mixer_set_track_pan(params: dict) -> dict:
    """Set mixer track pan."""
    track = params.get("track", 0)
    pan = params.get("pan", 0.0)
    mixer.setTrackPan(track, pan)
    return {"pan": mixer.getTrackPan(track)}


def handle_mixer_mute_track(params: dict) -> dict:
    """Mute/unmute mixer track."""
    track = params.get("track", 0)
    muted = params.get("muted")  # None = toggle

    if muted is None:
        mixer.muteTrack(track, -1)
    else:
        mixer.muteTrack(track, 1 if muted else 0)

    return {
        "is_muted": mixer.isTrackMuted(track) == 1,
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_solo_track(params: dict) -> dict:
    """Solo/unsolo mixer track."""
    track = params.get("track", 0)
    solo = params.get("solo")  # None = toggle
    mode = params.get("mode", 3)

    if solo is None:
        mixer.soloTrack(track, -1, mode)
    else:
        mixer.soloTrack(track, 1 if solo else 0, mode)

    return {
        "is_solo": mixer.isTrackSolo(track) == 1,
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_arm_track(params: dict) -> dict:
    """Toggle arm state of mixer track."""
    track = params.get("track", 0)
    mixer.armTrack(track)
    return {
        "is_armed": mixer.isTrackArmed(track) == 1,
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_set_track_name(params: dict) -> dict:
    """Set mixer track name."""
    track = params.get("track", 0)
    name = params.get("name", "")
    mixer.setTrackName(track, name)
    return {"name": name}


def handle_mixer_set_track_color(params: dict) -> dict:
    """Set mixer track color."""
    track = params.get("track", 0)
    r = params.get("r", 0)
    g = params.get("g", 0)
    b = params.get("b", 0)
    # FL Studio uses BGR format
    color = (b << 16) | (g << 8) | r
    mixer.setTrackColor(track, color)
    return {"color": f"RGB({r}, {g}, {b})"}


def handle_mixer_set_stereo_sep(params: dict) -> dict:
    """Set mixer track stereo separation."""
    track = params.get("track", 0)
    separation = params.get("separation", 0.0)
    mixer.setTrackStereoSep(track, separation)
    return {"separation": separation}


# =============================================================================
# Channel Handlers
# =============================================================================


def handle_channels_get_count(params: dict) -> dict:
    """Get number of channels."""
    global_count = params.get("global_count", True)
    return {"count": channels.channelCount(global_count)}


def handle_channels_get_info(params: dict) -> dict:
    """Get info about a channel."""
    index = params.get("index", 0)
    use_global = params.get("use_global", True)

    return {
        "index": index,
        "name": channels.getChannelName(index, use_global),
        "color": hex(channels.getChannelColor(index, use_global)),
        "volume": channels.getChannelVolume(index, use_global),
        "pan": channels.getChannelPan(index, use_global),
        "pitch": channels.getChannelPitch(index, useGlobalIndex=use_global),
        "is_muted": channels.isChannelMuted(index, use_global) == 1,
        "is_solo": channels.isChannelSolo(index, use_global) == 1,
        "is_selected": channels.isChannelSelected(index, use_global) == 1,
        "target_fx_track": channels.getTargetFxTrack(index, use_global),
    }


def handle_channels_get_all() -> dict:
    """Get info about all channels."""
    channels_list = []
    count = channels.channelCount(True)

    for i in range(count):
        channels_list.append({
            "index": i,
            "name": channels.getChannelName(i, True),
            "is_muted": channels.isChannelMuted(i, True) == 1,
            "is_selected": channels.isChannelSelected(i, True) == 1,
            "target_fx_track": channels.getTargetFxTrack(i, True),
        })

    return {"channels": channels_list}


def handle_channels_get_selected() -> dict:
    """Get currently selected channel."""
    index = channels.selectedChannel(canBeNone=True, indexGlobal=True)

    if index is None or index < 0:
        return {"channel": None}

    return {
        "channel": {
            "index": index,
            "name": channels.getChannelName(index, True),
            "volume": channels.getChannelVolume(index, True),
            "pan": channels.getChannelPan(index, True),
            "is_muted": channels.isChannelMuted(index, True) == 1,
            "is_solo": channels.isChannelSolo(index, True) == 1,
        }
    }


def handle_channels_select(params: dict) -> dict:
    """Select/deselect a channel."""
    index = params.get("index", 0)
    select = params.get("select", True)
    channels.selectChannel(index, 1 if select else 0, True)
    return {
        "selected": select,
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_select_one(params: dict) -> dict:
    """Select only one channel, deselecting others."""
    index = params.get("index", 0)
    channels.selectOneChannel(index, True)
    return {"channel_name": channels.getChannelName(index, True)}


def handle_channels_trigger_note(params: dict) -> dict:
    """Trigger a MIDI note on a channel."""
    channel = params.get("channel", 0)
    note = params.get("note", 60)
    velocity = params.get("velocity", 100)
    midi_channel = params.get("midi_channel", -1)
    channels.midiNoteOn(channel, note, velocity, midi_channel)
    return {"triggered": True, "note": note, "velocity": velocity}


def handle_channels_set_volume(params: dict) -> dict:
    """Set channel volume."""
    index = params.get("index", 0)
    volume = params.get("volume", 0.8)
    channels.setChannelVolume(index, volume, True)
    return {
        "volume": channels.getChannelVolume(index, True),
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_set_pan(params: dict) -> dict:
    """Set channel pan."""
    index = params.get("index", 0)
    pan = params.get("pan", 0.0)
    channels.setChannelPan(index, pan, True)
    return {
        "pan": channels.getChannelPan(index, True),
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_mute(params: dict) -> dict:
    """Mute/unmute channel."""
    index = params.get("index", 0)
    muted = params.get("muted")  # None = toggle

    if muted is None:
        channels.muteChannel(index, -1, True)
    else:
        channels.muteChannel(index, 1 if muted else 0, True)

    return {
        "is_muted": channels.isChannelMuted(index, True) == 1,
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_solo(params: dict) -> dict:
    """Solo/unsolo channel."""
    index = params.get("index", 0)
    solo = params.get("solo")  # None = toggle

    if solo is None:
        channels.soloChannel(index, -1, True)
    else:
        channels.soloChannel(index, 1 if solo else 0, True)

    return {
        "is_solo": channels.isChannelSolo(index, True) == 1,
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_set_name(params: dict) -> dict:
    """Set channel name."""
    index = params.get("index", 0)
    name = params.get("name", "")
    channels.setChannelName(index, name, True)
    return {"name": name}


def handle_channels_set_color(params: dict) -> dict:
    """Set channel color."""
    index = params.get("index", 0)
    r = params.get("r", 0)
    g = params.get("g", 0)
    b = params.get("b", 0)
    # FL Studio uses BGR format
    color = (b << 16) | (g << 8) | r
    channels.setChannelColor(index, color, True)
    return {"color": f"RGB({r}, {g}, {b})"}


def handle_channels_route_to_mixer(params: dict) -> dict:
    """Route channel to mixer track."""
    channel_index = params.get("channel_index", 0)
    mixer_track = params.get("mixer_track", 0)
    channels.setTargetFxTrack(channel_index, mixer_track, True)
    return {
        "channel_name": channels.getChannelName(channel_index, True),
        "mixer_track": mixer_track,
    }


# =============================================================================
# Step Sequencer Handlers
# =============================================================================


def handle_channels_get_grid_bit(params: dict) -> dict:
    """Get whether a step is active."""
    channel = params.get("channel", 0)
    position = params.get("position", 0)
    return {"value": channels.getGridBit(channel, position, True) == 1}


def handle_channels_set_grid_bit(params: dict) -> dict:
    """Set a step on or off."""
    channel = params.get("channel", 0)
    position = params.get("position", 0)
    value = params.get("value", False)
    channels.setGridBit(channel, position, 1 if value else 0, True)
    return {
        "value": value,
        "channel_name": channels.getChannelName(channel, True),
    }


def handle_channels_get_step_sequence(params: dict) -> dict:
    """Get step sequence for a channel."""
    channel = params.get("channel", 0)
    steps = params.get("steps", 16)
    sequence = []

    for i in range(steps):
        sequence.append(channels.getGridBit(channel, i, True) == 1)

    return {"sequence": sequence}


def handle_channels_set_step_sequence(params: dict) -> dict:
    """Set complete step sequence for a channel."""
    channel = params.get("channel", 0)
    pattern = params.get("pattern", [])

    for i, value in enumerate(pattern):
        channels.setGridBit(channel, i, 1 if value else 0, True)

    active_steps = sum(pattern)
    return {
        "active_steps": active_steps,
        "total_steps": len(pattern),
        "channel_name": channels.getChannelName(channel, True),
    }


# Step parameter types of channels.setStepParameterByIndex / getCurrentStepParam
# (midi.pPitch, pVelocity, pPan, pShift, ...). Kept literal: the values are
# part of the FL API contract and the handlers must not depend on the midi
# module for a table lookup.
STEP_PARAMS = {
    "pitch": 0,      # MIDI note, default 60
    "velocity": 1,   # 0..127, default 100
    "release": 2,    # 0..127, default 64
    "fine_pitch": 3, # cents 0..240, default 120
    "pan": 4,        # 0..127, default 64 = centre
    "mod_x": 5,      # 0..127, default 64
    "mod_y": 6,      # 0..127, default 64
    "shift": 7,      # ticks 0..PPQ/4 (one step), default 0
}


def _step_ticks() -> int:
    """Ticks per step: FL's step sequencer runs 4 steps per beat."""
    return _general_mod().getRecPPQ() // 4


def _step_param_max(name: str, step_ticks: int) -> int:
    if name == "shift":
        # Verified live (FL 2026): a note one tick before the next step's
        # slot already counts as that next step for setGridBit and masks its
        # note when reading, so the last usable shift is step_ticks - 2.
        return step_ticks - 2
    if name == "fine_pitch":
        return 240
    return 127


def _step_param_names(params: dict) -> "list | dict":
    names = params.get("params", ["velocity", "pan", "shift"])
    if isinstance(names, str):
        names = [names]
    unknown = [str(n) for n in names if str(n) not in STEP_PARAMS]
    if unknown:
        return {"error": "Unknown step parameter(s): %s. Valid: %s"
                % (", ".join(unknown), ", ".join(STEP_PARAMS))}
    return [str(n) for n in names]


def handle_channels_get_step_params(params: dict) -> dict:
    """Read grid bits plus per-step parameters (graph editor values) of a channel.

    Values are FL's raw integers (velocity/pan 0..127, shift as the absolute
    tick position of the note); the server normalises them.
    """
    channel = params.get("channel", 0)
    steps = int(params.get("steps", 16))
    names = _step_param_names(params)
    if isinstance(names, dict):
        return names
    if steps < 1:
        return {"error": "steps must be >= 1"}
    try:
        out = {
            "steps": [channels.getGridBit(channel, i, True) == 1 for i in range(steps)],
            "step_ticks": _step_ticks(),
            "channel_name": channels.getChannelName(channel, True),
        }
        # Raw FL values. For "shift" FL reports the absolute tick position
        # of the first note it finds from one tick before the step's slot on;
        # -1 means no note. The server turns that into per-step values.
        for name in names:
            param = STEP_PARAMS[name]
            out[name] = [int(channels.getCurrentStepParam(channel, i, param, True))
                         for i in range(steps)]
        return out
    except Exception as e:
        return {"error": str(e)}


def handle_channels_set_step_params(params: dict) -> dict:
    """Write per-step parameters (and optionally grid bits) on the current pattern.

    params["values"] maps parameter names to lists of raw integers, one per
    step from step 0; "shift" is relative to the step (0..step_ticks-2) and
    written as the absolute tick position FL expects. All lists are validated
    before anything is written, so a bad value leaves the pattern untouched.
    Steps that are off are skipped (FL ignores such writes anyway).
    """
    channel = params.get("channel", 0)
    values = params.get("values", {}) or {}
    bits = params.get("steps")
    if not isinstance(values, dict):
        return {"error": "values must be a mapping of parameter name -> list"}
    names = _step_param_names({"params": list(values)})
    if isinstance(names, dict):
        return names
    try:
        step_ticks = _step_ticks()
        for name in names:
            vals = values[name]
            if not isinstance(vals, list):
                return {"error": "%s must be a list" % name}
            top = _step_param_max(name, step_ticks)
            for i, v in enumerate(vals):
                if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > top:
                    return {"error": "%s[%d] = %r out of range 0..%d" % (name, i, v, top)}
        pattern = _patterns_mod().patternNumber()
        if bits is not None:
            for i, value in enumerate(bits):
                channels.setGridBit(channel, i, 1 if value else 0, True)
        written = {}
        skipped = []
        for name in names:
            param = STEP_PARAMS[name]
            count = 0
            for i, v in enumerate(values[name]):
                if not channels.getGridBit(channel, i, True):
                    # FL ignores parameter writes on steps that are off.
                    if i not in skipped:
                        skipped.append(i)
                    continue
                if name == "shift":
                    v = i * step_ticks + v
                channels.setStepParameterByIndex(channel, pattern, i, param, v, True)
                count += 1
            written[name] = count
        # Refresh the graph editor if it is open; the value is stored either way.
        refresh = getattr(channels, "updateGraphEditor", None)
        if refresh is not None:
            try:
                refresh()
            except Exception:
                pass
        return {
            "pattern": pattern,
            "written": written,
            "skipped_off_steps": skipped,
            "step_ticks": step_ticks,
            "channel_name": channels.getChannelName(channel, True),
        }
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# Plugin Handlers
# =============================================================================


def handle_plugins_is_valid(params: dict) -> dict:
    """Check if plugin exists at location."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        valid = plugins.isValid(index, slot_index, True)
    else:
        valid = plugins.isValid(index, -1, use_global)

    return {"valid": valid == 1}


def handle_plugins_get_name(params: dict) -> dict:
    """Get plugin name."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        name = plugins.getPluginName(index, slot_index, True)
    else:
        name = plugins.getPluginName(index, -1, use_global)

    return {"name": name}


def handle_plugins_get_param_count(params: dict) -> dict:
    """Get number of plugin parameters."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        count = plugins.getParamCount(index, slot_index, True)
    else:
        count = plugins.getParamCount(index, -1, use_global)

    return {"count": count}


def handle_plugins_get_params(params: dict) -> dict:
    """Get all plugin parameters."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)
    max_params = params.get("max_params", 50)

    if slot_index >= 0:
        param_count = plugins.getParamCount(index, slot_index, True)
    else:
        param_count = plugins.getParamCount(index, -1, use_global)

    param_list = []
    for i in range(min(param_count, max_params)):
        try:
            if slot_index >= 0:
                name = plugins.getParamName(i, index, slot_index, True)
                value = plugins.getParamValue(i, index, slot_index, True)
                value_str = plugins.getParamValueString(i, index, slot_index, True)
            else:
                name = plugins.getParamName(i, index, -1, use_global)
                value = plugins.getParamValue(i, index, -1, use_global)
                value_str = plugins.getParamValueString(i, index, -1, use_global)

            param_list.append({
                "index": i,
                "name": name,
                "value": value,
                "value_string": value_str,
            })
        except Exception as e:
            print(f"Warning: could not read param {i}: {e}")
            continue

    return {"params": param_list}


def handle_plugins_get_param_value(params: dict) -> dict:
    """Get specific parameter value."""
    param_index = params.get("param_index", 0)
    plugin_index = params.get("plugin_index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        name = plugins.getParamName(param_index, plugin_index, slot_index, True)
        value = plugins.getParamValue(param_index, plugin_index, slot_index, True)
        value_str = plugins.getParamValueString(param_index, plugin_index, slot_index, True)
    else:
        name = plugins.getParamName(param_index, plugin_index, -1, use_global)
        value = plugins.getParamValue(param_index, plugin_index, -1, use_global)
        value_str = plugins.getParamValueString(param_index, plugin_index, -1, use_global)

    return {
        "index": param_index,
        "name": name,
        "value": value,
        "value_string": value_str,
    }


def handle_plugins_set_param_value(params: dict) -> dict:
    """Set plugin parameter value."""
    param_index = params.get("param_index", 0)
    value = params.get("value", 0.0)
    plugin_index = params.get("plugin_index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        name = plugins.getParamName(param_index, plugin_index, slot_index, True)
        plugins.setParamValue(value, param_index, plugin_index, slot_index, True)
        new_value = plugins.getParamValue(param_index, plugin_index, slot_index, True)
        value_str = plugins.getParamValueString(param_index, plugin_index, slot_index, True)
    else:
        name = plugins.getParamName(param_index, plugin_index, -1, use_global)
        plugins.setParamValue(value, param_index, plugin_index, -1, use_global)
        new_value = plugins.getParamValue(param_index, plugin_index, -1, use_global)
        value_str = plugins.getParamValueString(param_index, plugin_index, -1, use_global)

    return {
        "name": name,
        "value": new_value,
        "value_string": value_str,
    }


def handle_plugins_get_preset_count(params: dict) -> dict:
    """Get number of plugin presets."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        count = plugins.getPresetCount(index, slot_index, True)
    else:
        count = plugins.getPresetCount(index, -1, use_global)

    return {"count": count}


def handle_plugins_next_preset(params: dict) -> dict:
    """Switch to next preset."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        plugin_name = plugins.getPluginName(index, slot_index, True)
        plugins.nextPreset(index, slot_index, True)
    else:
        plugin_name = plugins.getPluginName(index, -1, use_global)
        plugins.nextPreset(index, -1, use_global)

    return {"plugin_name": plugin_name}


def handle_plugins_prev_preset(params: dict) -> dict:
    """Switch to previous preset."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        plugin_name = plugins.getPluginName(index, slot_index, True)
        plugins.prevPreset(index, slot_index, True)
    else:
        plugin_name = plugins.getPluginName(index, -1, use_global)
        plugins.prevPreset(index, -1, use_global)

    return {"plugin_name": plugin_name}


def handle_plugins_get_color(params: dict) -> dict:
    """Get plugin color."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        color = plugins.getColor(index, slot_index, True)
    else:
        color = plugins.getColor(index, -1, use_global)

    return {"color": hex(color)}

