"""The desktop each monitor showed last (showing.json), remembered automatically whenever the user
changes it (switching, opening a desktop, moving a desktop, "Clear Desktops"). When DisplayDesktop
starts, or a monitor is attached, that monitor goes back to this desktop as soon as it has windows.

This is separate from the saved desktops (layout.json): "Save" stores its own copy of what
each monitor shows, "Load" uses that copy, and neither ever changes this file.
Monitors are keyed by the same stable identity as layout.json (native.get_monitor_id).
"""
from __future__ import annotations

import os

from . import paths
from .paths import SHOWING_FILE, is_int, write_json


class LastShown:
    def __init__(self, directory: str):
        self._file = os.path.join(directory, SHOWING_FILE)
        self._showing: dict[str, int] = {}
        self._unread = False
        self.late_read = False  # read after the start could not: the shown desktops apply now
        self._read()

    def take_late_read(self) -> bool:
        late, self.late_read = self.late_read, False
        return late

    def _read(self) -> None:
        """Reads the file. One that is there but can't be read now (held, offline) is read again
        before the next write, and never written over until it is."""
        try:
            if not os.path.exists(self._file):
                folder = os.path.dirname(self._file)  # (offline: can't tell; gone from a drive that answers: none)
                self._unread = not os.path.isdir(folder) and paths.offline(folder)
                return
            data = paths.read_json(self._file)
            read = {str(k): v for k, v in (data.get("Showing") or {}).items() if is_int(v) and v >= 0}
            self._showing = {**read, **self._showing}  # (what this run set since wins)
            self.late_read = self._unread  # (read now after all: the manager applies it, take_late_read)
            self._unread = False
        except OSError as exc:
            self._unread = paths.unreadable(self._file, exc)
        except Exception:  # noqa: BLE001 - damaged: nothing remembered
            self._unread = False

    def relocate(self, directory: str) -> None:
        self._file = os.path.join(directory, SHOWING_FILE)
        self._unread = False  # (another folder: what this run remembers is written there)

    def write_now(self) -> None:
        """After a move: what this run remembers is written to the new folder, merged into what is
        there (this run's values win; a file not readable yet is written once it is)."""
        self._read()
        if self._showing:
            self._write()

    def get(self, monitor_id: str) -> int | None:
        """The desktop index the monitor showed last, if known."""
        return self._showing.get(monitor_id) if monitor_id else None

    def set(self, monitor_id: str, desktop: int) -> None:
        """Remembers what the monitor shows now; writes the file only when it changed."""
        if not monitor_id or self._showing.get(monitor_id) == desktop:
            return
        self._showing[monitor_id] = desktop
        self._write()

    def _write(self) -> None:
        if self._unread:
            if paths.retry_due(self._file):
                self._read()
            if self._unread:
                self._dirty = True  # tried again by flush
                return
        self._dirty = not write_json(self._file, {"Showing": self._showing})

    def flush(self) -> None:
        """A write that failed (the folder paused or briefly unwritable) is made again (every sync)."""
        if getattr(self, "_dirty", False):
            if paths.write_due(self._file):
                self._write()
        elif self._unread and paths.retry_due(self._file):
            self._read()
