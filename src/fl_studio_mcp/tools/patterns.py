"""Pattern management tools for FL Studio.

Requires FL Studio 2024+ (the `patterns` scripting module). On older versions
the tools return a clear error message instead of failing.

Two kinds of tools live here:

* API tools (list, select, new, clone, rename) go through the controller
  script over MIDI and need neither focus nor a visible window.
* Operations the scripting API does not offer (delete, insert, move,
  transpose, split by channel) are driven through FL's own shortcuts and
  pattern menu with X11 keyboard/mouse automation (Linux/Wine only) and are
  verified through the pattern API afterwards. The time signature is set via
  a marker from the piano roll script, which is what FL's own dialog does.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fl_studio_mcp.utils.pattern_ui import (
    CONFIRM_DIALOG_RE,
    NAME_FIELD_RE,
    SEMITONES_FIELD_RE,
    WARNING_DIALOG_RE,
    PatternUI,
    PatternUIError,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRANSPOSE_LIMIT = 48
TIME_SIGNATURE_NUMERATORS = range(1, 17)
TIME_SIGNATURE_DENOMINATORS = (2, 4, 8, 16)
TIME_SIGNATURE_MARKER_MODE = 8


def _index_error(index: object) -> str | None:
    """Reject pattern indices FL Studio would silently turn into new patterns.

    Pattern indices are 1-based; jumping to index 0 or a negative index makes
    FL create or select unexpected patterns instead of failing.
    """
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        return f"Error: pattern index must be a positive integer (1-based), got {index!r}"
    return None


def _snapshot(conn) -> dict:
    """Current pattern list as {index: name} plus the active pattern.

    Only patterns that differ from their default state are listed (what
    `fl_list_patterns` shows); that is enough to verify counts and order.
    """
    result = conn.send_command("patterns.getAll", {"include_default": False})
    if result.get("error"):
        raise PatternUIError(result["error"])
    return {
        "names": {p["index"]: p["name"] for p in result.get("patterns", [])},
        "current": result.get("current"),
    }


def _select(conn, index: int) -> str:
    """Activate a pattern through the API; returns its name."""
    result = conn.send_command("patterns.select", {"index": index})
    if result.get("error"):
        raise PatternUIError(result["error"])
    return result.get("name", "")


def _wait_for_change(conn, before: dict, timeout: float = 1.5) -> dict:
    """Poll the pattern list until it differs from `before` (or time is up).

    FL applies UI actions asynchronously; reading immediately after a
    keystroke can still show the old state.
    """
    deadline = time.time() + timeout
    while True:
        after = _snapshot(conn)
        if after != before or time.time() >= deadline:
            return after
        time.sleep(0.1)


def _deletion_verified(before: dict, after: dict, index: int) -> bool:
    """Did deleting `index` produce exactly the expected pattern list?

    A listed pattern must be gone with the ones after it moved up by one. An
    unlisted (empty, unnamed) pattern in the middle leaves the names alone but
    still shifts the indices behind it.
    """
    expected = {
        (i - 1 if i > index else i): n for i, n in before["names"].items() if i != index
    }
    if after["names"] != expected:
        return False
    # A pattern behind the last listed one leaves no trace in the list.
    return index <= max(before["names"], default=0)


def register_pattern_tools(mcp: FastMCP) -> None:
    """Register pattern management tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_list_patterns(include_default: bool = False) -> dict:
        """List all patterns in the project.

        By default only patterns that were modified from their default state
        are listed. Set include_default=True to list all 999 potential slots
        (slow, rarely useful).

        Returns each pattern's index (1-based), name, color, length in steps
        (16 per bar in 4/4) and in bars, and selection state, plus the
        currently active pattern.
        """
        conn = get_connection()
        return conn.send_command("patterns.getAll", {"include_default": include_default})

    @mcp.tool()
    def fl_get_current_pattern() -> dict:
        """Get the currently active pattern (index, name, length in steps and bars)."""
        conn = get_connection()
        return conn.send_command("patterns.getCurrent")

    @mcp.tool()
    def fl_select_pattern(index: int) -> str:
        """Select and activate a pattern (1-based index).

        Note: jumping to a non-existent pattern index creates it, so stay
        within sane bounds (use fl_new_pattern for a fresh pattern).
        """
        if error := _index_error(index):
            return error
        conn = get_connection()
        result = conn.send_command("patterns.select", {"index": index})
        if result.get("error"):
            return f"Error: {result['error']}"
        return f"Selected pattern {result.get('selected')} ({result.get('name')})"

    @mcp.tool()
    def fl_new_pattern() -> str:
        """Switch to the next empty pattern (automation-safe, no name prompt).

        FL patterns are virtual; this finds and activates the next unused
        pattern, which is the practical equivalent of creating one. Give it a
        name afterwards with fl_rename_pattern.

        Note: FL reports the length of an empty pattern as the project's
        current default (often the previous pattern's length), not 0.
        """
        conn = get_connection()
        result = conn.send_command("patterns.newEmpty", {})
        if result.get("error"):
            return f"Error: {result['error']}"
        return f"Switched to empty pattern {result.get('selected')} ({result.get('name')})"

    @mcp.tool()
    def fl_clone_pattern(index: int | None = None) -> str:
        """Clone a pattern (default: the currently active one).

        Warning: FL Studio closes the piano roll when cloning, to protect you
        from editing the wrong pattern. Reopen the piano roll afterwards if
        needed.
        """
        if index is not None and (error := _index_error(index)):
            return error
        conn = get_connection()
        params = {"index": index} if index is not None else {}
        result = conn.send_command("patterns.clone", params)
        if result.get("error"):
            return f"Error: {result['error']}"
        return f"Cloned to pattern {result.get('cloned_to')} ({result.get('name')})"

    @mcp.tool()
    def fl_rename_pattern(index: int, name: str) -> str:
        """Rename the pattern at index (1-based). Empty name resets to default."""
        if error := _index_error(index):
            return error
        conn = get_connection()
        result = conn.send_command("patterns.setName", {"index": index, "name": name})
        if result.get("error"):
            return f"Error: {result['error']}"
        return f"Pattern {result.get('index')} is now named '{result.get('name')}'"

    # -- shortcut / menu driven operations (Linux/Wine, X11) -----------------

    @mcp.tool()
    def fl_delete_pattern(index: int) -> dict:
        """Delete a pattern (1-based index) via FL's Shift+Ctrl+Del shortcut.

        Selects the pattern, sends the shortcut, confirms FL's "Confirm"
        dialog and verifies through the pattern API that the pattern is gone.
        Patterns after it move up by one; clips of the pattern disappear from
        the playlist. FL keeps the deletion in its undo history, but do not
        rely on that - call fl_save_undo_point first and double-check the
        index with fl_list_patterns.

        Requires the X11 automation (Linux, FL under Wine, xdotool).
        """
        if error := _index_error(index):
            return {"error": error}
        conn = get_connection()
        ui = PatternUI()
        try:
            ui.require_available()
            before = _snapshot(conn)
            name = _select(conn, index)
            known = ui.press_shortcut("delete")
            dialog = ui.expect_dialog(CONFIRM_DIALOG_RE, known)
            if dialog is not None:
                ui.confirm_dialog(dialog)
            after = _wait_for_change(conn, before)
        except PatternUIError as e:
            return {"error": str(e)}

        result = {
            "deleted": index,
            "name": name,
            "confirm_dialog_seen": dialog is not None,
            "pattern_count": len(after["names"]),
            "current": after["current"],
        }
        if _deletion_verified(before, after, index):
            return {"success": True, **result}
        if dialog is not None and after["names"] == before["names"]:
            # An empty, unnamed pattern behind the last used one: FL deletes
            # it, but nothing observable changes.
            return {
                "success": True,
                "verified": False,
                "note": "Pattern was empty and after the last used pattern; nothing changed.",
                **result,
            }
        return {
            "error": (
                f"FL Studio did not delete pattern {index} ('{name}'): the pattern list "
                "did not change as expected."
                + ("" if dialog else " No 'Confirm' dialog appeared either.")
            ),
            **result,
        }

    @mcp.tool()
    def fl_insert_pattern(name: str | None = None, before: int | None = None) -> dict:
        """Insert a new empty pattern before a pattern (FL's "Insert one").

        FL inserts the new pattern *before* the selected one, so it takes the
        index `before` (default: the currently active pattern) and shifts the
        following patterns down by one. FL opens an inline name field; `name`
        is typed into it, otherwise FL's default ("Pattern N") is kept.
        Verified via the pattern API. To append a pattern at the end use
        fl_new_pattern instead (no automation needed).

        Requires the X11 automation (Linux, FL under Wine, xdotool).
        """
        if before is not None and (error := _index_error(before)):
            return {"error": error}
        conn = get_connection()
        ui = PatternUI()
        try:
            ui.require_available()
            if before is not None:
                _select(conn, before)
            snapshot = _snapshot(conn)
            target = snapshot["current"]
            known = ui.press_shortcut("insert_one")
            dialog = ui.expect_dialog(NAME_FIELD_RE, known)
            if dialog is None:
                after = _wait_for_change(conn, snapshot, timeout=0.5)
                if after == snapshot:
                    return {
                        "error": "FL Studio did not open the pattern name field; "
                        "nothing inserted."
                    }
            else:
                ui.fill_inline_field(dialog, name)
                after = _wait_for_change(conn, snapshot)
            if after == snapshot:
                return {
                    "error": "FL Studio did not insert a pattern; the pattern list is unchanged."
                }
            # FL selects the inserted pattern; an unnamed one is "default" and
            # therefore absent from the list, so ask FL for its name directly.
            inserted = conn.send_command("patterns.getCurrent")
        except PatternUIError as e:
            return {"error": str(e)}

        new_index = inserted.get("index")
        actual_name = inserted.get("name")
        result = {
            "success": True,
            "inserted": new_index,
            "name": actual_name,
            "pattern_count": len(after["names"]),
            "current": after["current"],
        }
        if new_index != target:
            result["note"] = f"expected index {target}, FL inserted at {new_index}"
        if name is not None and actual_name != name:
            result["warning"] = f"requested name {name!r}, FL kept {actual_name!r}"
        return result

    @mcp.tool()
    def fl_move_pattern(index: int, direction: str, steps: int = 1) -> dict:
        """Move a pattern up or down in the pattern list (Shift+Ctrl+Up/Down).

        Args:
            index: 1-based index of the pattern to move
            direction: "up" (towards 1) or "down"
            steps: how many positions (default 1)

        Moves one step at a time and re-reads the pattern list after each;
        stops early at the top/bottom (FL ignores the shortcut there). Returns
        the pattern's new index. Playlist clips follow the pattern.

        Requires the X11 automation (Linux, FL under Wine, xdotool).
        """
        if error := _index_error(index):
            return {"error": error}
        if direction not in ("up", "down"):
            return {"error": f"Error: direction must be 'up' or 'down', got {direction!r}"}
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            return {"error": f"Error: steps must be a positive integer, got {steps!r}"}
        conn = get_connection()
        ui = PatternUI()
        applied = 0
        try:
            ui.require_available()
            before = _snapshot(conn)
            if index not in before["names"]:
                return {"error": f"Pattern {index} is not in use (see fl_list_patterns)."}
            name = _select(conn, index)
            position = index
            for _ in range(steps):
                previous = _snapshot(conn)
                ui.press_shortcut(f"move_{direction}")
                after = _wait_for_change(conn, previous, timeout=1.0)
                if after == previous:
                    break  # top/bottom reached, FL ignores the shortcut
                applied += 1
                position = after["current"]
        except PatternUIError as e:
            return {"error": str(e), "steps_applied": applied}

        expected = index + applied * (1 if direction == "down" else -1)
        result = {
            "success": True,
            "name": name,
            "from": index,
            "to": position,
            "steps_requested": steps,
            "steps_applied": applied,
        }
        if applied < steps:
            end = "top" if direction == "up" else "bottom"
            result["note"] = f"stopped after {applied} step(s): {end} of the list reached"
        if position != expected:
            result["warning"] = f"expected index {expected}, FL reports {position}"
        return result

    @mcp.tool()
    def fl_transpose_pattern(index: int, semitones: int) -> dict:
        """Transpose all notes of a pattern (all channels) via the pattern menu.

        Uses PATTERNS menu > Transpose..., types the semitones into FL's
        inline "Semitones" field and confirms. Note that FL transposes every
        channel in the pattern, drums included - split drums into their own
        pattern first (fl_clone_pattern + fl_clear_piano_roll, or
        fl_split_pattern_by_channel).

        When the piano roll is open, the notes of its target channel are read
        before and after through the piano roll script and compared, and the
        result says whether the transposition was verified. Otherwise the
        result is unverified; check with fl_get_piano_roll_state.

        Requires the X11 automation (Linux, FL under Wine, xdotool).
        """
        if error := _index_error(index):
            return {"error": error}
        if isinstance(semitones, bool) or not isinstance(semitones, int) or semitones == 0:
            return {"error": f"Error: semitones must be a non-zero integer, got {semitones!r}"}
        if abs(semitones) > TRANSPOSE_LIMIT:
            return {"error": f"Error: semitones must be within ±{TRANSPOSE_LIMIT}, got {semitones}"}
        conn = get_connection()
        ui = PatternUI()
        try:
            ui.require_available()
            name = _select(conn, index)
            notes_before = _piano_roll_note_numbers(conn)
            known = ui.choose_menu_item("transpose")
            dialog = ui.expect_dialog(SEMITONES_FIELD_RE, known)
            if dialog is None:
                return {
                    "error": "FL Studio did not open the 'Semitones' field; nothing transposed."
                }
            ui.fill_inline_field(dialog, str(semitones))
        except PatternUIError as e:
            return {"error": str(e)}

        result = {"success": True, "pattern": index, "name": name, "semitones": semitones}
        if notes_before is None:
            result["verified"] = None
            result["note"] = (
                "Not verified: the piano roll was not open (or its script did not run). "
                "Open it on a channel of this pattern to have the notes compared."
            )
            return result
        notes_after = _piano_roll_note_numbers(conn)
        expected = [n + semitones for n in notes_before]
        result["verified"] = notes_after == expected
        result["piano_roll_notes"] = len(notes_before)
        if not result["verified"]:
            result["error"] = (
                "The piano roll's notes do not match the expected transposition "
                f"(before {notes_before[:8]}…, after {(notes_after or [])[:8]}…)."
            )
        return result

    @mcp.tool()
    def fl_split_pattern_by_channel(index: int) -> dict:
        """Split a pattern into one pattern per channel (PATTERNS > Split by channel).

        FL replaces the pattern with one pattern per channel that has notes,
        named "<pattern> - <channel name>", at the same position; it also
        closes the piano roll. Playlist clips of the original end up *stacked
        on one track*; for one track per instrument prefer the clone route
        (fl_clone_pattern, fl_clear_piano_roll on the unwanted channel,
        fl_rename_pattern). Verified via the pattern list.

        Requires the X11 automation (Linux, FL under Wine, xdotool).
        """
        if error := _index_error(index):
            return {"error": error}
        conn = get_connection()
        ui = PatternUI()
        try:
            ui.require_available()
            before = _snapshot(conn)
            if index not in before["names"]:
                return {"error": f"Pattern {index} is not in use (see fl_list_patterns)."}
            name = _select(conn, index)
            known = ui.choose_menu_item("split_by_channel")
            # FL refuses with a "Warning" when fewer than two channels have data.
            warning = ui.expect_dialog(WARNING_DIALOG_RE, known, timeout=0.7)
            if warning is not None:
                ui.confirm_dialog(warning)
                return {
                    "error": (
                        f"FL Studio refused to split pattern {index} ('{name}'): splitting "
                        "only makes sense if the pattern contains at least two channels "
                        "that have data."
                    )
                }
            after = _wait_for_change(conn, before)
        except PatternUIError as e:
            return {"error": str(e)}

        if after == before:
            return {
                "error": (
                    f"FL Studio did not split pattern {index} ('{name}'); the pattern list "
                    "is unchanged."
                )
            }
        created = [
            {"index": i, "name": n}
            for i, n in after["names"].items()
            if n.startswith(f"{name} - ") and n not in before["names"].values()
        ]
        return {
            "success": True,
            "split": index,
            "name": name,
            "created": created,
            "pattern_count": len(after["names"]),
            "current": after["current"],
            "note": "FL closed the piano roll; reopen it with fl_open_piano_roll.",
        }

    @mcp.tool()
    def fl_set_pattern_time_signature(
        numerator: int, denominator: int, index: int | None = None
    ) -> dict:
        """Set a pattern's time signature (what PATTERNS > Set time signature does).

        FL stores a pattern's time signature as a marker at the pattern start;
        this places or replaces that marker through the piano roll script, so
        it works on every platform without menu automation. The pattern
        (default: the active one) is selected and the piano roll shown first.
        Numerator 1-16, denominator 2, 4, 8 or 16 (the values FL's dialog
        offers). Verified by reading the markers back; the pattern length in
        steps changes accordingly.

        Needs the piano roll script (ComposeWithLLM) to have run once in this
        FL session, like all piano roll tools.
        """
        if index is not None and (error := _index_error(index)):
            return {"error": error}
        if (
            isinstance(numerator, bool)
            or not isinstance(numerator, int)
            or numerator not in TIME_SIGNATURE_NUMERATORS
        ):
            return {"error": f"Error: numerator must be 1..16, got {numerator!r}"}
        if (
            isinstance(denominator, bool)
            or not isinstance(denominator, int)
            or denominator not in TIME_SIGNATURE_DENOMINATORS
        ):
            return {"error": f"Error: denominator must be one of 2, 4, 8, 16, got {denominator!r}"}

        from fl_studio_mcp.tools import piano_roll as pr

        conn = get_connection()
        try:
            if index is not None:
                _select(conn, index)
            current = conn.send_command("patterns.getCurrent")
            conn.send_command("pianoroll.open", {})
        except PatternUIError as e:
            return {"error": str(e)}

        pr._write_request(
            {
                "action": "set_time_signature",
                "numerator": numerator,
                "denominator": denominator,
                "time_ticks": 0,
            }
        )
        run = pr._run_pr_script()
        if not run["ran"]:
            pr._remove_queued_actions("set_time_signature")
            return {"error": pr.PR_SCRIPT_NOT_RUN_HINT if run["triggered"] else run["error"]}
        response = run.get("response") or {}
        if response.get("status") == "error":
            return {"error": f"Piano roll script error: {response.get('message')}"}
        outcome = response.get("time_signature")
        if not isinstance(outcome, dict):
            return {
                "error": (
                    "The piano roll script does not know 'set_time_signature'. Update "
                    "ComposeWithLLM.pyscript in FL Studio's 'Piano roll scripts' folder "
                    "from this repository."
                ),
                "raw_response": response,
            }
        if outcome.get("status") == "error":
            return {"error": f"Piano roll script error: {outcome.get('message')}"}
        markers = outcome.get("markers", [])
        verified = any(
            m.get("mode") == TIME_SIGNATURE_MARKER_MODE
            and m.get("time_ticks") == 0
            and m.get("tsnum") == numerator
            and m.get("tsden") == denominator
            for m in markers
        )
        after = conn.send_command("patterns.getCurrent")
        result = {
            "success": verified,
            "pattern": {"index": after.get("index"), "name": after.get("name")},
            "time_signature": f"{numerator}/{denominator}",
            "replaced_marker": bool(outcome.get("replaced")),
            "length_steps_before": current.get("length_steps"),
            "length_steps_after": after.get("length_steps"),
            "markers": markers,
        }
        if not verified:
            result["error"] = "The time signature marker was not found after writing it."
        return result


def _piano_roll_note_numbers(conn) -> list[int] | None:
    """Note numbers of the open piano roll in time order, or None if unavailable.

    Used to verify menu-driven transpositions. Returns None when the piano
    roll is not visible or its script did not run, so callers can report
    "unverified" instead of guessing.
    """
    from fl_studio_mcp.tools import piano_roll as pr

    try:
        focus = conn.send_command("ui.getFocus")
    except Exception:  # noqa: BLE001 - verification is best effort
        return None
    if not focus.get("success") or not focus.get("visible", {}).get("piano_roll"):
        return None
    run = pr._run_pr_script()
    if not run["ran"]:
        return None
    state = pr._read_state() or {}
    notes = state.get("notes") or []
    ordered = sorted(notes, key=lambda n: (n.get("time_ticks", 0), n.get("number", 0)))
    return [int(n["number"]) for n in ordered]
