"""Playlist track and arrangement navigation tools for FL Studio.

Playlist tracks (name, color, mute, solo, selection) come from FL's
`playlist` module, time markers and the song position from `arrangement`.

Not possible through the FL Studio scripting API: placing pattern or audio
clips in the playlist. Clips still have to be placed by hand in FL.

FL applies song position changes, marker jumps and loop mode switches only
after a controller callback returns, and drops a second change made in the
same callback. The controller therefore exposes single steps and the helpers
here sequence them, one MIDI round trip (about 20 ms) per step.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")

# Upper bound for the marker walk, matching the controller's name scan.
MARKER_LIMIT = 512


class ControllerError(Exception):
    """A controller step failed; carries the error text for the client."""


def _track_index_error(index: object) -> str | None:
    """Playlist tracks are 1-based; FL does not fail on bad indices."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        return f"playlist track index must be a positive integer (1-based), got {index!r}"
    return None


def _parse_hex_color(color: str) -> dict | None:
    """'#RRGGBB' or 'RRGGBB' -> {'r', 'g', 'b'}; None if malformed."""
    match = _HEX_COLOR.match(color.strip())
    if not match:
        return None
    value = int(match.group(1), 16)
    return {"r": (value >> 16) & 0xFF, "g": (value >> 8) & 0xFF, "b": value & 0xFF}


def _step(conn: Any, action: str, params: dict | None = None) -> dict:
    """One controller round trip; raises ControllerError on failure."""
    result = conn.send_command(action, params or {})
    if not result.get("success", False) and "error" in result:
        raise ControllerError(result["error"])
    return result


def _position(conn: Any) -> dict:
    return _step(conn, "arrangement.getPosition")["position"]


def _ticks_to_bst(ticks: int, ppq: int, ppb: int) -> dict:
    if not ppq or not ppb:
        return {"bar": None, "beat": None, "tick": None}
    return {"bar": ticks // ppb + 1, "beat": (ticks % ppb) // ppq + 1, "tick": ticks % ppq}


def _ensure_song_mode(conn: Any, position: dict) -> bool:
    """Switch FL to song mode if needed; returns True if it was switched."""
    if position.get("loop_mode") == "song":
        return False
    _step(conn, "transport.setLoopMode", {"mode": "song"})
    return True


def _restore(conn: Any, position: dict, switched: bool) -> None:
    """Put the song position and loop mode back after a walk."""
    _step(conn, "arrangement.setPosition", {"ticks": int(position.get("ticks") or 0)})
    if switched:
        _step(conn, "transport.setLoopMode", {"mode": "pattern"})
        # In pattern mode the transport position belongs to the pattern.
        _step(conn, "arrangement.setPosition", {"ticks": int(position.get("transport_ticks") or 0)})


def walk_markers(conn: Any) -> tuple[list[dict], dict]:
    """Collect the markers in song order by jumping through them.

    FL has no marker enumeration API beyond names by index, so this jumps
    from the song start marker to marker and reads the applied position
    after each jump. A marker on the first tick is never reached by a jump
    from the start; it shows up as one more name than jump targets.
    Returns (markers, position before the walk). Position and loop mode are
    restored afterwards.
    """
    position = _position(conn)
    names = _step(conn, "arrangement.getMarkerNames").get("names", [])
    if not names:
        return [], position
    ppq, ppb = position.get("ppq", 0), position.get("ppb", 0)
    switched = _ensure_song_mode(conn, position)
    times: list[int] = []
    try:
        _step(conn, "arrangement.setPosition", {"ticks": 0})
        last = 0
        for _ in range(MARKER_LIMIT):
            _step(conn, "arrangement.jumpMarker", {"delta": 1})
            ticks = int(_position(conn)["ticks"])
            if ticks <= last:  # FL wraps around after the last marker
                break
            times.append(ticks)
            last = ticks
    finally:
        _restore(conn, position, switched)
    if len(times) < len(names):
        times = [0] * (len(names) - len(times)) + times
    markers = [
        {"index": i, "name": names[i] if i < len(names) else "", "ticks": t}
        | _ticks_to_bst(t, ppq, ppb)
        for i, t in enumerate(times)
    ]
    return markers, position


def _find_marker(markers: list[dict], name: str) -> dict | None:
    """Exact name first, then a unique case-insensitive match."""
    exact = [m for m in markers if m["name"] == name]
    if exact:
        return exact[0]
    lowered = name.lower()
    loose = [m for m in markers if m["name"].lower() == lowered]
    return loose[0] if len(loose) == 1 else None


def register_playlist_tools(mcp: FastMCP) -> None:
    """Register playlist track and arrangement navigation tools."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_list_playlist_tracks(include_unnamed: bool = False, limit: int = 50) -> dict:
        """List playlist tracks with name, color, mute, solo and selection state.

        Playlist tracks are 1-based. By default only tracks that were touched
        are listed (custom name, muted, soloed or selected); untouched tracks
        read back as "Track n". Set include_unnamed=True to list every track
        up to `limit` (FL projects have hundreds of empty tracks).

        Note: the scripting API cannot place pattern or audio clips in the
        playlist. This only manages the tracks themselves.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return {"error": f"limit must be a positive integer, got {limit!r}"}
        try:
            result = _step(
                get_connection(),
                "playlist.getTracks",
                {"include_unnamed": include_unnamed, "limit": limit},
            )
        except ControllerError as e:
            return {"error": str(e)}
        return {"tracks": result.get("tracks", []), "track_count": result.get("track_count")}

    @mcp.tool()
    def fl_set_playlist_track(
        index: int,
        name: str | None = None,
        color: str | None = None,
        muted: bool | None = None,
        solo: bool | None = None,
        selected: bool | None = None,
    ) -> dict:
        """Rename, recolor, mute, solo or select a playlist track (1-based).

        Only the given properties change. An empty name resets the track to
        its default "Track n". Color is a hex string like "#3FA7D6".
        Returns the track as FL reports it afterwards.
        """
        if error := _track_index_error(index):
            return {"error": error}
        params: dict = {"index": index}
        if name is not None:
            params["name"] = name
        if color is not None:
            rgb = _parse_hex_color(color)
            if rgb is None:
                return {"error": f"color must be a hex string like '#RRGGBB', got {color!r}"}
            params["rgb"] = rgb
        if muted is not None:
            params["muted"] = bool(muted)
        if solo is not None:
            params["solo"] = bool(solo)
        if selected is not None:
            params["selected"] = bool(selected)
        if len(params) == 1:
            return {"error": "Nothing to change: give one of name, color, muted, solo, selected"}
        conn = get_connection()
        try:
            _step(conn, "playlist.setTrack", params)
            # FL may apply the change after the callback; read it back separately.
            result = _step(conn, "playlist.getTrack", {"index": index})
        except ControllerError as e:
            return {"error": str(e)}
        return {"track": result.get("track")}

    @mcp.tool()
    def fl_get_position() -> dict:
        """Get the song position: absolute ticks, bar/beat/tick (1-based) and FL's hint.

        Also reports whether FL is playing, the loop mode (song or pattern)
        and the timebase (ppq = ticks per beat, ppb = ticks per bar). In
        pattern mode `transport_ticks` is the position within the pattern.
        """
        try:
            return _position(get_connection())
        except ControllerError as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_list_markers() -> dict:
        """List the time markers of the arrangement in song order.

        Each marker has its index, name, absolute ticks and bar/beat/tick.
        FL has no marker enumeration API, so the markers are visited by
        jumping through them (one short round trip per marker); the song
        position and loop mode are restored afterwards. Do not call while
        recording.
        """
        try:
            markers, position = walk_markers(get_connection())
        except ControllerError as e:
            return {"error": str(e)}
        return {"markers": markers, "position": position}

    @mcp.tool()
    def fl_add_marker(bar: int, name: str, beat: int = 1) -> dict:
        """Add a time marker at a bar (1-based), optionally on a later beat of that bar.

        Bar 33 beat 1 is the start of bar 33. Ticks are derived from the
        project timebase, which ignores time signature markers in the
        playlist. The markers are read back afterwards; `verified` says
        whether FL reports a marker at the requested position. FL keeps
        marker names unique: a second "Chorus" becomes "Chorus #2"
        (`renamed`). Markers cannot be deleted through the API.
        """
        if isinstance(bar, bool) or not isinstance(bar, int) or bar < 1:
            return {"error": f"bar must be a positive integer (1-based), got {bar!r}"}
        if isinstance(beat, bool) or not isinstance(beat, int) or beat < 1:
            return {"error": f"beat must be a positive integer (1-based), got {beat!r}"}
        if not name or not name.strip():
            return {"error": "Marker name must not be empty"}
        conn = get_connection()
        try:
            position = _position(conn)
            ppq, ppb = position.get("ppq", 0), position.get("ppb", 0)
            if not ppq or not ppb:
                return {"error": "Project timebase not available"}
            beats_per_bar = ppb // ppq
            if beat > beats_per_bar:
                return {"error": f"beat must be between 1 and {beats_per_bar}, got {beat}"}
            ticks = (bar - 1) * ppb + (beat - 1) * ppq
            _step(conn, "arrangement.addMarker", {"ticks": ticks, "name": name.strip()})
            markers, _ = walk_markers(conn)
        except ControllerError as e:
            return {"error": str(e)}
        # FL renames a marker whose name is taken ("Chorus" -> "Chorus #2"),
        # so verify by position and report the name FL actually assigned.
        added = next((m for m in markers if m["ticks"] == ticks), None)
        return {
            "marker": added
            or {"name": name.strip(), "ticks": ticks, **_ticks_to_bst(ticks, ppq, ppb)},
            "verified": added is not None,
            "renamed": added is not None and added["name"] != name.strip(),
            "markers": markers,
        }

    @mcp.tool()
    def fl_jump_to_marker(name: str | None = None, delta: int | None = None) -> dict:
        """Move the song position to a marker, by name or relative to the current position.

        Give either `name` (exact, falling back to a unique case-insensitive
        match) or `delta` (+1 = next marker, -1 = previous; FL wraps around
        at both ends). Switches FL to song mode if needed and leaves it
        there. Returns the new position.
        """
        if (name is None) == (delta is None):
            return {"error": "Give exactly one of name or delta"}
        bad_delta = isinstance(delta, bool) or not isinstance(delta, int) or delta == 0
        if delta is not None and bad_delta:
            return {"error": f"delta must be a non-zero integer, got {delta!r}"}
        conn = get_connection()
        try:
            if name is not None:
                markers, position = walk_markers(conn)
                target = _find_marker(markers, name)
                if target is None:
                    return {
                        "error": f"No marker named {name!r}",
                        "markers": [m["name"] for m in markers],
                    }
                _ensure_song_mode(conn, position)
                _step(conn, "arrangement.setPosition", {"ticks": target["ticks"]})
                return {"marker": target, "position": _position(conn)}
            position = _position(conn)
            _ensure_song_mode(conn, position)
            _step(conn, "arrangement.jumpMarker", {"delta": delta})
            new_position = _position(conn)
            markers, _ = walk_markers(conn)
            at = next((m for m in markers if m["ticks"] == new_position.get("ticks")), None)
            return {"marker": at, "position": new_position}
        except ControllerError as e:
            return {"error": str(e)}
