"""settings.json in the data folder. Only values that differ from the defaults are stored, so with
every setting at its default there is no file. A damaged file or a value of the wrong type counts as
the default. Written atomically (paths.write_json).

The values last read or written are also kept in memory (`_known`): while the data folder can't be
reached (paths.failed) they are used instead of the disk (no blocking reads, no settings lost), and
`write_if_missing` writes them to a new folder that has no settings file (a move away from a folder
that could not be read copies nothing).
"""
from __future__ import annotations

import os

from . import paths
from .paths import SETTINGS_FILE, data_dir, write_json

FILE = SETTINGS_FILE
DEFAULTS: dict[str, bool | str] = {
    "ShowConsole": False,  # the console viewer opens at start
    "StartMenu": False,    # Add To Start Menu: the entry is added (and kept current) at start
    "UseGpu": True,        # Use GPU Acceleration: each monitor's slide runs on the GPU that drives it
    "RestoreApps": False,   # Restore Applications On Restart: after a Windows restart, apps that were open start again
    "ShowAdvanced": False,  # Settings shows its Advanced options
}
_known: dict[str, bool | str] | None = None  # the stored values last read or written (None: not yet)
_dirty = False  # _known has a change not written yet
_read_failed = False  # the last read could not be made (held, offline): retried through paths.retry_due


def _path() -> str:
    return os.path.join(data_dir(), FILE)


def _stored() -> dict[str, bool | str] | None:
    """The stored values; None when the file can't be read now and nothing is known (never written
    back as if empty: that would erase the file's settings)."""
    global _known, _read_failed
    path = _path()
    if _known is not None and (_dirty or paths.paused(os.path.dirname(path))):
        return dict(_known)  # not written yet, or the folder is unreachable: what is known
    if _known is None and _read_failed and not paths.retry_due(path):
        return None  # (unreadable a moment ago: read again at most every READ_RETRY_S, not while paused)
    _read_failed = False
    try:
        data = paths.read_json(path)  # (a byte-order mark, e.g. from Notepad, is fine; encrypted too)
    except FileNotFoundError:
        folder = os.path.dirname(path)
        if not os.path.isdir(folder) and paths.offline(folder):  # (offline: can't tell, not "none")
            _read_failed = True
            return dict(_known) if _known is not None else None
        data = {}  # no file: the defaults
    except ValueError:
        data = {}  # a damaged file (or a newer version's: the tray asked first): the defaults
    except OSError:
        _read_failed = True
        return dict(_known) if _known is not None else None  # can't be read now: what was known
    if not isinstance(data, dict):
        data = {}
    # a value of another type than its default counts as the default (True is not a GPU name, 1 not True)
    _known = {k: v for k, v in data.items() if k in DEFAULTS and type(v) is type(DEFAULTS[k])}
    return dict(_known)


def retry() -> None:
    """Read again at the next use (a key typed only now: the file can be opened)."""
    global _read_failed
    _read_failed = False


def readable() -> bool:
    """The stored settings are known (read now, or known from before)."""
    return _stored() is not None


def get(key: str) -> bool | str:
    data = _stored() or {}
    return {**data, **_pending}.get(key, DEFAULTS[key])  # (a change not merged in yet wins)


def set(key: str, value: bool | str) -> None:  # noqa: A001 - settings.set reads naturally
    global _known, _dirty
    value = bool(value) if isinstance(DEFAULTS[key], bool) else str(value)
    data = _stored()
    if data is None:
        _pending[key] = value  # merged into the file's settings once it can be read (flush)
        _dirty = True
        return
    _pending.pop(key, None)  # (an older change of it, made while unreadable, must not come back)
    data[key] = value
    data = {k: v for k, v in data.items() if v != DEFAULTS[k]}
    _known = dict(data)
    _write_known()


_pending: dict[str, bool | str] = {}  # changes made while the file could not be read (nothing known)


def _write_known() -> None:
    """Writes what is known; if it can't be written now (the folder is paused or unwritable) it stays
    to write (`flush`, from the tray's sync timer): a change must not be lost when the folder comes
    back."""
    global _dirty
    path = _path()
    _dirty = True
    if paths.paused(os.path.dirname(path)) or paths.is_kept(path):
        return  # (kept: waits for the Quit / Recreate Files answer, never written over nor deleted)
    try:
        if _known:
            _dirty = not write_json(path, dict(_known))
        else:
            if os.path.exists(path):
                os.remove(path)
            elif not os.path.isdir(os.path.dirname(path)) and paths.offline(os.path.dirname(path)):
                return  # (its folder offline: "no file" can't be told; the delete is made by flush later)
            _dirty = False
    except OSError:
        pass


def flush() -> None:
    """A settings change that could not be written yet is written now (when the folder allows)."""
    if _pending:  # (_stored reads again at most every READ_RETRY_S, never while the folder is paused)
        changes = dict(_pending)
        _pending.clear()
        if _stored() is None:  # still unreadable
            _pending.update(changes)
            return
        for k, v in changes.items():
            set(k, v)
        return
    if _dirty and _known is not None and paths.write_due(_path()):
        _write_known()


def write_if_missing() -> None:
    """After a move from a folder that could not be read: the settings still known are written to
    the new folder (unless it has a settings file of its own)."""
    global _dirty
    path = _path()
    if _known and not os.path.exists(path):
        # Not written now: kept to write (flush), and read as what is known until then (_dirty)
        _dirty = not write_json(path, dict(_known))


def forget() -> None:
    """The folder changed to one whose settings are used: read them afresh (tests, moves)."""
    global _known, _dirty, _read_failed
    _known = None
    _dirty = False
    _read_failed = False
    _pending.clear()
