"""Drive FL Studio's pattern shortcuts and pattern menu (no scripting API exists).

Everything here was measured against FL Studio 2026 under Wine/X11:

* Shortcuts act on the *selected* pattern and work regardless of which FL
  window has focus: Shift+Ctrl+Del (delete, asks "Confirm"), Shift+Ctrl+Ins
  (insert one *before* the selected pattern, opens an inline "Pattern N name"
  field), Shift+Ctrl+Up/Down (move; a no-op at the ends, no dialog).
* Menu entries without a shortcut (Set time signature, Transpose, Split by
  channel) need the pattern menu, opened by clicking the small triangle left
  of the pattern selector in the toolbar. The menu is a popup window; the
  keyboard highlight starts on the *current pattern* in its left column, so
  counting from the top depends on the pattern count. Counting from the end
  (`End`, then `Up` n times) does not: the right column's tail is fixed.
* `Escape` inside a popup menu or dialog closes only that popup (elsewhere in
  FL it closes the focused window, so never send it blindly).
* Dialogs and inline fields are separate X windows: "Confirm" (delete),
  "Pattern N name" (insert), "Semitones" (transpose), "Time signature change".
  `Return` confirms them all.
"""

from __future__ import annotations

import os
import re
import time

from fl_studio_mcp.utils.x11_automation import (
    FL_MAIN_WINDOW_RE,
    X11Automation,
    XWindow,
    get_x11_automation,
)

# Offset of the pattern-menu triangle from the main window's top-left corner,
# measured on a 1920x1080 FL window with the default toolbar layout.
# Override with FL_STUDIO_MCP_PATTERN_MENU_OFFSET="x,y" if the toolbar differs.
DEFAULT_PATTERN_MENU_OFFSET = (802, 62)

# Pattern menu entries, counted upwards from the last entry (End, Up x n).
MENU_ITEMS_FROM_END = {
    "render_and_replace": 0,
    "render_as_audio_clip": 1,
    "quick_render_as_audio_clip": 2,
    "split_by_channel": 3,
    "move_down": 4,
    "move_up": 5,
    "delete": 6,
    "clone": 7,
    "insert_one": 8,
    "transpose": 9,
    "set_time_signature": 10,
    "open_in_project_browser": 11,
    "random_color": 12,
    "change_color": 13,
    "rename_and_color": 14,
    "select_in_playlist": 15,
}

SHORTCUTS = {
    "delete": "shift+ctrl+Delete",
    "insert_one": "shift+ctrl+Insert",
    "move_up": "shift+ctrl+Up",
    "move_down": "shift+ctrl+Down",
}

CONFIRM_DIALOG_RE = r"^Confirm$"
NAME_FIELD_RE = r" name$"  # "Pattern 7 name"
SEMITONES_FIELD_RE = r"^Semitones$"
WARNING_DIALOG_RE = r"^Warning$"

DIALOG_TIMEOUT = 2.0
MENU_TIMEOUT = 2.0


class PatternUIError(RuntimeError):
    """The UI did not behave as expected; the message says what was seen."""


def pattern_menu_offset() -> tuple[int, int]:
    raw = os.environ.get("FL_STUDIO_MCP_PATTERN_MENU_OFFSET")
    if raw:
        try:
            x, y = (int(part) for part in raw.split(","))
            return x, y
        except ValueError:
            pass
    return DEFAULT_PATTERN_MENU_OFFSET


class PatternUI:
    """Verified keyboard/menu sequences for pattern operations."""

    def __init__(self, x11: X11Automation | None = None) -> None:
        self.x11 = x11 or get_x11_automation()

    # -- preconditions -----------------------------------------------------

    def require_available(self) -> None:
        if not self.x11.available:
            raise PatternUIError(
                f"Cannot automate FL Studio's pattern menu: {self.x11.unavailable_reason}. "
                "Use the pattern menu in FL Studio manually."
            )

    def _activate_main(self) -> XWindow:
        main = self.x11.activate_main()
        if main is None:
            raise PatternUIError("FL Studio window not found (is FL Studio running and visible?)")
        return main

    # -- shortcuts and menu ------------------------------------------------

    def press_shortcut(self, name: str) -> set[str]:
        """Activate FL and send the shortcut. Returns the window ids seen before."""
        self._activate_main()
        known = self.x11.window_ids()
        self.x11.key(SHORTCUTS[name])
        return known

    def open_menu(self) -> XWindow:
        """Open the pattern menu by clicking its toolbar triangle."""
        main = self._activate_main()
        known = self.x11.window_ids()
        dx, dy = pattern_menu_offset()
        self.x11.click(main.x + dx, main.y + dy)
        menu = self.x11.wait_for_new_window(known, timeout=MENU_TIMEOUT, min_width=200)
        if menu is None:
            raise PatternUIError(
                "The pattern menu did not open after clicking the toolbar triangle at "
                f"offset {dx},{dy}. If the toolbar layout was changed, re-measure the "
                "triangle and set FL_STUDIO_MCP_PATTERN_MENU_OFFSET=\"x,y\"."
            )
        return menu

    def choose_menu_item(self, item: str) -> set[str]:
        """Open the pattern menu and activate `item`. Returns window ids seen before."""
        steps = MENU_ITEMS_FROM_END[item]
        menu = self.open_menu()
        known = self.x11.window_ids() - {menu.id}
        self.x11.key("End", *(["Up"] * steps), "Return", delay_ms=60)
        if not self.x11.wait_gone(menu.id, timeout=MENU_TIMEOUT):
            self.x11.key("Escape")
            raise PatternUIError(f"The pattern menu stayed open after choosing '{item}'.")
        return known

    # -- dialogs -------------------------------------------------------------

    def _unexpected_dialogs(self, known: set[str]) -> list[XWindow]:
        return [
            window
            for window in self.x11.fl_windows()
            if window.id not in known
            and window.name
            and not re.search(FL_MAIN_WINDOW_RE, window.name)
        ]

    def expect_dialog(
        self, name_pattern: str, known: set[str], timeout: float = DIALOG_TIMEOUT
    ) -> XWindow | None:
        """Wait for the dialog FL should open now.

        Returns None when it did not appear (the caller then verifies whether
        FL acted without asking, e.g. "Remember my choice" on the delete
        confirmation). A *different* titled window is closed with Escape and
        reported as an error instead of being guessed at.
        """
        dialog = self.x11.wait_for_window(name_pattern, timeout=timeout)
        if dialog is not None:
            return dialog
        unexpected = self._unexpected_dialogs(known)
        if unexpected:
            self.x11.activate(unexpected[0])
            self.x11.key("Escape")
            raise PatternUIError(
                f"FL Studio opened an unexpected window '{unexpected[0].name}' "
                f"(expected one matching {name_pattern!r}); closed it with Escape."
            )
        return None

    def ensure_no_dialog(self, known: set[str], settle: float = 0.5) -> None:
        """Fail if FL opened any dialog where none is expected."""
        time.sleep(settle)
        unexpected = self._unexpected_dialogs(known)
        if unexpected:
            self.x11.activate(unexpected[0])
            self.x11.key("Escape")
            raise PatternUIError(
                f"FL Studio opened an unexpected window '{unexpected[0].name}'; "
                "closed it with Escape."
            )

    def confirm_dialog(self, dialog: XWindow) -> None:
        """Press Return (Ok/Accept/✓) in the dialog and wait for it to close."""
        self.x11.activate(dialog)
        self.x11.key("Return")
        if not self.x11.wait_gone(dialog.id, timeout=DIALOG_TIMEOUT):
            raise PatternUIError(f"The '{dialog.name}' window did not close after Return.")

    def cancel_dialog(self, dialog: XWindow) -> None:
        self.x11.activate(dialog)
        self.x11.key("Escape")
        self.x11.wait_gone(dialog.id, timeout=DIALOG_TIMEOUT)

    def fill_inline_field(self, dialog: XWindow, text: str | None) -> None:
        """Type into FL's inline edit field (name / semitones) and confirm.

        The field is pre-filled and selected for names, empty for semitones;
        Ctrl+A first makes both cases behave the same.
        """
        self.x11.activate(dialog)
        if text is not None:
            self.x11.key("ctrl+a")
            self.x11.type_text(text)
        self.x11.key("Return")
        if not self.x11.wait_gone(dialog.id, timeout=DIALOG_TIMEOUT):
            self.cancel_dialog(dialog)
            raise PatternUIError(
                f"The '{dialog.name}' field did not accept the input {text!r}; cancelled it."
            )
