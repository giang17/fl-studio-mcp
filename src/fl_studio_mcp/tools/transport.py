"""Transport control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

# FL Studio's project tempo range. Out-of-range values are clamped silently by
# the REC event, so they are rejected here and in the controller script.
TEMPO_MIN_BPM = 10.0
TEMPO_MAX_BPM = 522.0


def _tempo_result(result: dict) -> dict:
    """Shape a controller tempo response for the MCP client."""
    ppq = result.get("ppq", 0)
    return {
        "bpm": result.get("bpm"),
        "ppq": ppq,
        "ppb": result.get("ppb", 0),
        "raw_tempo": result.get("raw_tempo"),
        # FL only exposes the numerator (bar length in beats) to controller
        # scripts; the denominator comes from the piano roll (fl_get_pr_context).
        "time_signature": {
            "numerator": result.get("beats_per_bar"),
            "denominator": None,
        },
    }


def register_transport_tools(mcp: FastMCP) -> None:
    """Register transport control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_play() -> str:
        """Start or pause FL Studio playback.

        Toggles between play and pause states. If stopped, starts playback.
        If playing, pauses playback.
        """
        conn = get_connection()
        result = conn.send_command("transport.start")

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        is_playing = result.get("is_playing", False)
        return f"Playback {'started' if is_playing else 'paused'}"

    @mcp.tool()
    def fl_stop() -> str:
        """Stop FL Studio playback completely.

        Stops playback and resets the playback position.
        """
        conn = get_connection()
        result = conn.send_command("transport.stop")

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return "Playback stopped"

    @mcp.tool()
    def fl_record() -> str:
        """Toggle recording mode in FL Studio.

        When enabled, incoming MIDI and audio will be recorded.
        """
        conn = get_connection()
        result = conn.send_command("transport.record")

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        is_recording = result.get("is_recording", False)
        return f"Recording {'enabled' if is_recording else 'disabled'}"

    @mcp.tool()
    def fl_get_transport_status() -> dict:
        """Get current transport status including playback and recording state.

        Returns information about whether FL Studio is playing, recording,
        the current position, and loop mode.
        """
        conn = get_connection()
        result = conn.send_command("transport.getStatus")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "is_playing": result.get("is_playing", False),
            "is_recording": result.get("is_recording", False),
            "position": result.get("position", ""),
            "loop_mode": result.get("loop_mode", "pattern"),
        }

    @mcp.tool()
    def fl_set_song_position(position: float, mode: int = 2) -> str:
        """Set the playback position in FL Studio.

        Args:
            position: The position value. Interpretation depends on mode.
            mode: Position format:
                  0 = Percentage (0.0 to 1.0)
                  1 = Time in milliseconds
                  2 = Time in seconds (default)
                  3 = Position in ticks
                  4 = Position as bars:steps:ticks (encoded)
        """
        conn = get_connection()
        result = conn.send_command("transport.setPosition", {
            "position": position,
            "mode": mode,
        })

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        new_pos = result.get("position", "")
        return f"Position set to {new_pos}"

    @mcp.tool()
    def fl_get_song_length() -> dict:
        """Get the total length of the current song/pattern.

        Returns the length in multiple formats for convenience.
        """
        conn = get_connection()
        result = conn.send_command("transport.getLength")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "ticks": result.get("ticks", 0),
            "seconds": result.get("seconds", 0),
            "milliseconds": result.get("milliseconds", 0),
        }

    @mcp.tool()
    def fl_set_loop_mode(mode: str) -> str:
        """Set the loop mode between pattern and song.

        Args:
            mode: Either "pattern" or "song"
        """
        if mode not in ("pattern", "song"):
            return "Error: mode must be 'pattern' or 'song'"

        conn = get_connection()
        result = conn.send_command("transport.setLoopMode", {"mode": mode})

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return f"Loop mode set to {mode}"

    @mcp.tool()
    def fl_set_playback_speed(speed: float) -> str:
        """Set the playback speed multiplier.

        Args:
            speed: Speed multiplier from 0.25 (quarter speed) to 4.0 (4x speed).
                   1.0 is normal speed.
        """
        if not 0.25 <= speed <= 4.0:
            return "Error: Speed must be between 0.25 and 4.0"

        conn = get_connection()
        result = conn.send_command("transport.setPlaybackSpeed", {"speed": speed})

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return f"Playback speed set to {speed}x"

    @mcp.tool()
    def fl_get_tempo() -> dict:
        """Get the project tempo and timebase (read-only).

        Returns the tempo in BPM, the timebase (ppq = ticks per quarter note,
        ppb = ticks per bar), the raw milli-BPM value FL reports, and the time
        signature. Only the numerator (beats per bar) is available here, and it
        ignores time signature markers in the playlist; the denominator is read
        from the open piano roll by fl_get_pr_context.
        """
        conn = get_connection()
        result = conn.send_command("transport.getTempo")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return _tempo_result(result)

    @mcp.tool()
    def fl_set_tempo(bpm: float) -> dict:
        """Set the project tempo and return the tempo FL actually applied.

        Call fl_save_undo_point first if the change should be revertible.

        Args:
            bpm: Tempo in beats per minute, 10 to 522 (FL Studio's range).
        """
        if not TEMPO_MIN_BPM <= bpm <= TEMPO_MAX_BPM:
            return {
                "error": (
                    f"bpm must be between {TEMPO_MIN_BPM:g} and {TEMPO_MAX_BPM:g}, got {bpm}"
                )
            }

        conn = get_connection()
        result = conn.send_command("transport.setTempo", {"bpm": float(bpm)})

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        applied = _tempo_result(result)
        applied["requested_bpm"] = result.get("requested_bpm", float(bpm))
        return applied
