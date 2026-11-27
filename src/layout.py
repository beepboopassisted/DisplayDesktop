"""The saved desktops (layout.json): which monitor, desktop and position every window was on,
and which desktop each monitor was showing. Saved only when the user chooses "Save"
in the tray menu (save) or erased by "Clear Desktops" (clear); nothing is recorded automatically.
Used to put windows back when they open again (also after a restart). The desktop each monitor
showed last is remembered separately, automatically (last_shown.py, showing.json).

HWNDs change when an app or Windows restarts, so windows are matched by program and window class,
by count: the program's first window goes to the first one's place, the second to the second's,
whatever their titles (Order: when each window first appeared). Store apps all share one program:
they are counted per title, kept only as a hash (titles can contain private text).
Positions are kept in the window's own DPI coordinates (see native.in_window_dpi_context).
"""
from __future__ import annotations

import functools
import json
import os
import time
from collections import namedtuple

from . import native as N
from . import paths
from .paths import LAYOUT_FILE, is_int, write_json

try:  # Python's built-in SHA-256 (not hashlib's OpenSSL: its DLL costs ~0.5 MB and is slower on titles)
    from _sha256 import sha256 as _sha256
except ImportError:  # (3.12+: _sha2)
    try:
        from _sha2 import sha256 as _sha256
    except ImportError:
        from hashlib import sha256 as _sha256

# Programs whose windows belong to many different apps: every Store app's window (Calculator,
# Settings, Photos...) is an ApplicationFrameWindow of ApplicationFrameHost.exe. The title (the app's
# name) is what tells those apps apart: their windows are counted per title too.
SHARED_HOSTS = frozenset({"applicationframehost.exe"})
NOT_READ_YET = -2  # WindowLayout.save: the save there can't be read right now (not written)
NOT_READABLE = -3  # ... and not because it is in use (a cloud file not available, no read right)
LOCKED = -4        # ... because it is encrypted (Encrypt Settings) and no known key opens it

_FIELDS = ("Exe", "Class", "TitleHash", "Display", "Desktop", "WorkLeft", "WorkTop", "ShowCmd",
           "Left", "Top", "Right", "Bottom", "Order")


def setup_key(names) -> tuple:
    """A display setup: the names of the displays attached, sorted (each setup keeps its own places)."""
    return tuple(sorted({n for n in names if n}))


def read_setups(data: dict) -> list[tuple[tuple, dict]]:
    """The setups of a file: (setup, its object) for each {"Displays": [...], ...}; odd ones skipped."""
    out = []
    for s in data.get("Setups") or []:
        if isinstance(s, dict) and isinstance(s.get("Displays"), list):
            out.append((setup_key(str(n) for n in s["Displays"] if isinstance(n, str)), s))
    return out

class Entry:
    """One saved window. Identity semantics: two entries with equal data are still different windows.
    Display: the display's name (unique in its setup); Desktop: desktop index on that display
    (0-based); WorkLeft/WorkTop: top-left of the display's work area when saved; Order: when its window
    first appeared (order_of; only compared between windows of one program); Setup: the display setup
    it belongs to (setup_key; not a field: the file groups entries by setup)."""
    __slots__ = _FIELDS + ("Setup",)

    def __init__(self, Exe: str = "", Class: str = "", TitleHash: str = "", Display: str = "",  # noqa: N803
                 Desktop: int = 0, WorkLeft: int = 0, WorkTop: int = 0, ShowCmd: int = 0, Left: int = 0,  # noqa: N803
                 Top: int = 0, Right: int = 0, Bottom: int = 0, Order: int = 0, Setup: tuple = ()):  # noqa: N803
        self.Exe, self.Class, self.TitleHash, self.Display = Exe, Class, TitleHash, Display
        self.Desktop, self.WorkLeft, self.WorkTop, self.ShowCmd = Desktop, WorkLeft, WorkTop, ShowCmd
        self.Left, self.Top, self.Right, self.Bottom = Left, Top, Right, Bottom
        self.Order = Order
        self.Setup = Setup

    def __repr__(self) -> str:
        return "Entry(" + ", ".join(f"{k}={getattr(self, k)!r}" for k in self.__slots__) + ")"

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in _FIELDS}

    @staticmethod
    def from_json(d: dict, setup: tuple = ()) -> "Entry":
        e = Entry(Setup=setup)
        for k in _FIELDS:
            if k in d and d[k] is not None:
                v = d[k]
                default = getattr(e, k)
                if isinstance(default, int) and not is_int(v):
                    raise ValueError(f"bad {k}")
                if isinstance(default, str) and not isinstance(v, str):
                    raise ValueError(f"bad {k}")
                setattr(e, k, v)
        return e


Place = namedtuple("Place", "window display device desktop")  # a window to save: where it is now


EXE_CACHE_MAX = 1024  # program paths kept per (window, process)


@functools.lru_cache(maxsize=1024)  # titles repeat on every 2 s record (room for many open windows' titles)
def title_hash(title: str) -> str:
    """First 12 bytes of SHA-256 of the UTF-8 title, as uppercase hex (same as the C# version)."""
    return _sha256(title.encode("utf-8", "surrogatepass")).hexdigest()[:24].upper()


ORDER_MAX = 1024  # windows whose first sight is kept (closed ones dropped past this)
# (window, its process) -> when DisplayDesktop first saw it (a counter: the spawn order). With the
# process: a handle used again by another window never inherits the old one's number
_order: dict[tuple[int, int], int] = {}
_next_order = [1]


def order_of(h: int, win) -> int:
    """When the window first appeared, as a count (shared by the saved desktops and the last known
    places): the first window of a program goes back to the place of the first one recorded, etc."""
    key = (h, win.get_pid(h))
    o = _order.get(key)
    if o is None:
        if len(_order) >= ORDER_MAX:
            for k in [k for k in _order if not win.is_window(k[0])]:
                del _order[k]
        o = _order[key] = _next_order[0]
        _next_order[0] += 1
    return o


def number_new(windows, win) -> None:
    """Windows seen for the first time together, oldest first (reverse z-order): numbered by their
    process's start (an app started earlier opened its windows earlier), then in that order, so a
    window first noticed by a record (not by a placement) keeps the order it opened in. Windows has
    no creation time for a window: among one process's windows open before DisplayDesktop started,
    z-order is the best guess."""
    new = [h for h in windows if (h, win.get_pid(h)) not in _order]
    if len(new) > 1:
        started: dict[int, int] = {}
        for h in new:
            pid = win.get_pid(h)
            if pid not in started:
                started[pid] = win.process_started(pid) or 0
        new.sort(key=lambda h: started[win.get_pid(h)])  # (stable: one process's keep their order)
    for h in new:
        order_of(h, win)


def seed_order(h: int, order: int, win) -> None:
    """The same window as a recorded one (same boot, same handle and process: DisplayDesktop alone
    restarted): it keeps that record's number, and windows seen later count after it."""
    key = (h, win.get_pid(h))
    if key not in _order and isinstance(order, int) and order > 0:
        _order[key] = order
        _next_order[0] = max(_next_order[0], order + 1)


def group_setups(entries, row) -> dict:
    """Setup -> {"Displays": [...], "Windows": [row(e), ...]}, in the entries' order."""
    setups: dict[tuple, dict] = {}
    for e in entries:
        setups.setdefault(e.Setup, {"Displays": list(e.Setup), "Windows": []})["Windows"].append(row(e))
    return setups


class WindowLayout:
    FILE_NAME = LAYOUT_FILE

    def __init__(self, directory: str, win=N):
        self._win = win
        self._file = os.path.join(directory, self.FILE_NAME)
        self._exe_cache: dict[tuple[int, int], str] = {}  # (window, pid) -> program path
        self._entries: list[Entry] = []  # every setup's (Entry.Setup)
        self._showing: dict[tuple, dict[str, int]] = {}  # setup -> display name -> desktop shown
        self._setup: tuple = ()  # the display setup attached now (use_setup)
        self._names: dict[str, str] = {}  # device -> display name, in that setup
        self._bound: dict[int, Entry] = {}  # live window -> its entry (of the setup in use)
        self._load_file()

    def relocate(self, directory: str) -> None:
        """The data folder moved: read and write the file there from now on (WindowPlaces too,
        through FILE_NAME)."""
        self._file = os.path.join(directory, self.FILE_NAME)
        self._unread = False  # (the old folder's file no longer matters)
        self._locked = False  # (nor what its last read found)

    def adopt_file(self, theirs_chosen: bool = False) -> None:
        """After a move: the new folder's saved desktops are used when it has them (the user chose
        that folder's data; when ours were copied there, they are the same and nothing changes).
        Otherwise the saved desktops still known are written there (the old folder could not be
        read), and so is a Save or Clear not written yet, unless the user chose that folder's own
        data (`theirs_chosen`). layout.json is otherwise only written by Save and Clear."""
        self._unread = False  # (another folder: what memory holds is this run's own)
        if getattr(self, "_dirty", False) and not theirs_chosen:
            self._dirty = not self._write()  # (an unwritten Clear must not come back from the copy)
            return
        self._dirty = False
        if not os.path.exists(self._file):
            if theirs_chosen:
                # that folder's data has no saved desktops: none (the old save is not carried over)
                self._entries, self._showing = [], {}
                self._bound.clear()
                self._loaded({}, [])
            elif self._entries or self._showing:
                self._dirty = not self._write()  # (a failed write is made again by flush)
            return
        mine = json.dumps(self._data(), sort_keys=True)
        try:
            theirs = json.dumps(paths.read_json(self._file), sort_keys=True)
        except (OSError, ValueError):
            theirs = None
        if theirs != mine:
            self._bound.clear()  # (entries of another save: windows are matched afresh on Load)
            self._load_file()
            if self._unread and theirs_chosen:
                # its own save, not readable yet: not ours either (read later, by retry_read)
                self._entries, self._showing = [], {}
                self._loaded({}, [])

    def _load_file(self) -> None:
        """Reads the file. One that can't be read now (held, or its folder offline: then it looks
        missing) leaves memory as it is and `_unread` set: nothing is written over it until it is
        read (retry_read). Only a file missing from a folder that is there means "nothing saved"."""
        self._locked = False  # (the last read decides: set again below if it fails so)
        try:
            if not os.path.exists(self._file):
                # (the folder gone from a drive that answers: nothing saved, a write makes it again;
                # only an offline drive or share means "can't read now")
                folder = os.path.dirname(self._file)
                self._unread = not os.path.isdir(folder) and paths.offline(folder)
                self._held_since, self._held = None, False  # (not "in use": not there)
                return
            data = paths.read_json(self._file)
            pairs, showing = [], {}
            for setup, s in read_setups(data):
                pairs += [(Entry.from_json(e, setup), e) for e in s.get("Windows") or []]
                shown = {str(k): int(v) for k, v in (s.get("Showing") or {}).items() if is_int(v)}
                if shown:
                    showing[setup] = shown
            self._entries, self._showing = [e for e, _ in pairs], showing
            self._unread = False
            self._held_since = None
            self._loaded(data, pairs)
        except OSError as exc:
            self._unread = paths.unreadable(self._file, exc)
            # In use (a retry may work) only while refused for less than HELD_REFUSED_S: open() gives
            # the same PermissionError for a file held and one with no read right
            now = time.monotonic()
            from . import crypto
            self._locked = isinstance(exc, crypto.Locked)  # (encrypted: the key is needed)
            if not isinstance(exc, PermissionError):
                self._held_since = None
                self._held = False
            else:
                self._held_since = getattr(self, "_held_since", None) or now
                self._held = now - self._held_since < paths.HELD_REFUSED_S
        except Exception:  # noqa: BLE001 - damaged: nothing saved
            self._unread = False
            self._entries, self._showing = [], {}
            self._loaded({}, [])

    def _loaded(self, data: dict, pairs: list[tuple[Entry, dict]]) -> None:
        """Hook for subclasses: the file was read (data) or could not be ({})."""

    # ---- queries

    def showing_desktop(self, display: str) -> int | None:
        """The desktop index the display was showing when saved (in the setup in use), if any."""
        return self._showing.get(self._setup, {}).get(display)

    def use_setup(self, setup: tuple, names: dict[str, str]) -> None:
        """The displays attached now (setup_key; `names`: device -> display name): matching,
        saving and recording use that setup's places only; every other setup's stay as they are."""
        self._names = dict(names)
        if setup != self._setup:
            self._setup = setup
            self._bound.clear()  # (entries of another setup)

    def is_bound(self, h: int) -> bool:
        return h in self._bound

    def bound_entry(self, h: int) -> Entry | None:
        return self._bound.get(h)

    def unbind(self, h: int) -> None:
        """The window is gone (closed, or hidden by its own app): its entry stays saved,
        free to be matched by the next window that opens with the same identity."""
        self._bound.pop(h, None)

    def unbind_all(self) -> None:
        """Every entry can be matched afresh (tray "Load")."""
        self._bound.clear()

    def _exe_of(self, h: int) -> str:
        """The window's program path. A window's process never changes, so it is read once per
        (window, pid) instead of opening the process on every record."""
        key = (h, self._win.get_pid(h))
        exe = self._exe_cache.get(key)
        if exe is None:
            exe = self._win.get_exe_path(h)
            if exe:  # not cached when it could not be read: try again next time
                if len(self._exe_cache) >= EXE_CACHE_MAX:  # (closed windows' entries go now and then)
                    # (only closed windows' entries: a busy desktop never asks every window's program again)
                    w = self._win
                    self._exe_cache = {k: v for k, v in self._exe_cache.items() if w.is_window(k[0])}
                    if len(self._exe_cache) >= EXE_CACHE_MAX:
                        self._exe_cache.clear()  # (that many windows open at once: start over)
                self._exe_cache[key] = exe
        return exe

    # ---- matching

    def _app_key(self, exe: str, cls: str, title_hash_: str) -> tuple:
        """What windows are counted by: program and class (Store apps, SHARED_HOSTS: their title too)."""
        exe = exe.lower()
        return (exe, cls, title_hash_) if os.path.basename(exe) in SHARED_HOSTS else (exe, cls)

    def assign(self, windows: list[int]) -> list[tuple[int, Entry]]:
        """Matches newly seen windows to free entries of the same program and class by count, never
        by title: the program's first window (in the order they first appeared, order_of) takes the
        free place recorded first (Order), the second the next, and so on. A window with no place
        left (more windows than places) is not matched: it stays where Windows opened it. Store apps
        (SHARED_HOSTS: one program for all of them) are counted per title (the app's name)."""
        w = self._win
        number_new(reversed(windows), w)  # (first seen together: the one lowest in z-order is the oldest)
        bound = {id(e) for e in self._bound.values()}
        free: dict[tuple, list[Entry]] = {}
        for e in self._candidates():
            if id(e) not in bound:
                free.setdefault(self._app_key(e.Exe, e.Class, e.TitleHash), []).append(e)
        for places in free.values():
            places.sort(key=lambda e: e.Order)  # (stable: equal ones keep the file's order)
        new: dict[tuple, list[int]] = {}
        for h in windows:
            if h in self._bound:
                continue
            key = self._app_key(self._exe_of(h), w.get_class(h), title_hash(w.get_title(h)))
            if key in free:
                new.setdefault(key, []).append(h)
        result = []
        for key, hs in new.items():
            hs.sort(key=lambda h: order_of(h, w))
            for h, e in zip(hs, free[key]):
                self._bound[h] = e
                result.append((h, e))
        return result

    def _candidates(self) -> list[Entry]:
        """Entries a newly seen window may be matched to: the setup in use's."""
        setup = self._setup
        return [e for e in self._entries if e.Setup == setup]

    def _entry_for(self, p: Place) -> Entry | None:
        """An entry for where the window is now (None if its placement cannot be read)."""
        w = self._win
        with w.in_window_dpi_context(p.window):
            wp = w.get_placement(p.window)
            work = w.get_work_area(p.device)
        if wp is None or not work or work == (0, 0, 0, 0) or not p.display:
            return None
        r = wp.rcNormalPosition
        title = w.get_title(p.window)
        return Entry(Exe=self._exe_of(p.window), Class=w.get_class(p.window),
                     TitleHash=title_hash(title), Display=p.display, Setup=self._setup,
                     Desktop=p.desktop, WorkLeft=work[0], WorkTop=work[1], ShowCmd=wp.showCmd,
                     Left=r.left, Top=r.top, Right=r.right, Bottom=r.bottom, Order=order_of(p.window, w))

    # ---- saving (manual only)

    def save(self, windows: list[Place], showing: dict[str, int]) -> int:
        """Tray "Save": replaces the save of the display setup in use with where the given windows
        are now and which desktop each display shows, and writes layout.json. Other setups' saves are
        kept. Returns the number of windows saved, -1 if layout.json could not be written, or
        NOT_READ_YET while the save there can't be read (writing would drop the other setups' saves:
        try again shortly)."""
        problem = self.read_problem()
        if problem:
            return problem
        setup = self._setup
        entries = [e for e in self._entries if e.Setup != setup]
        bound: dict[int, Entry] = {}

        count = 0
        for p in windows:
            e = self._entry_for(p)
            if e is None:
                continue
            entries.append(e)
            bound[p.window] = e
            count += 1

        new_showing = {k: v for k, v in self._showing.items() if k != setup}
        if showing:
            new_showing[setup] = dict(showing)
        if not write_json(self._file, self._data(entries, new_showing)):
            return -1  # not written: the last real save stays what Load uses
        self._entries, self._bound, self._showing = entries, bound, new_showing
        return count

    def clear(self) -> None:
        """Forgets every saved window and shown desktop (tray "Clear Desktops"). If the file can't be
        written now, it is written by a later sync (flush): the cleared save must not come back."""
        self._entries.clear()
        self._showing.clear()
        self._bound.clear()
        self._unread = False  # (cleared: whatever the file held is replaced, read or not)
        self._dirty = not self._write()

    def flush(self) -> None:
        """A Clear whose write failed is written again (every sync) until it is; a file that could
        not be read is read again."""
        if getattr(self, "_dirty", False):
            if not paths.write_due(self._file):
                return  # (held by another program a moment ago: tried again in READ_RETRY_S)
            self._unread = False  # (a Clear replaces whatever the file held)
            self._dirty = not self._write()
        elif self._unread:
            self.retry_read(throttled=True)

    def retry_read(self, throttled: bool = False) -> bool:
        """The file was there but could not be read (held, a cloud file not available yet, its
        folder offline): read again, never while the folder is paused, and `throttled` (from the
        sync timer) at most every paths.READ_RETRY_S. Until it is read nothing overwrites it.
        Returns whether it is read now."""
        if self._unread:
            due = paths.retry_due(self._file) if throttled else not paths.paused(os.path.dirname(self._file))
            if due:
                self._load_file()
        return not self._unread

    def read_problem(self) -> int:
        """Before Save / Load: 0 when the save there is read (read again now if it wasn't), else -1
        (its folder offline), NOT_READ_YET (in use: try again shortly) or NOT_READABLE (it can't be
        read: a retry won't help by itself)."""
        folder = os.path.dirname(self._file)
        if self._unread and not paths.offline(folder):
            self._load_file()  # (asked for by the user: read now, also while paused for a refused write)
        if not self._unread:
            return 0
        if paths.offline(folder):
            return -1
        if getattr(self, "_locked", False):
            return LOCKED
        return NOT_READ_YET if getattr(self, "_held", True) else NOT_READABLE

    def _write(self) -> bool:
        if self._unread and not self.retry_read(throttled=True):
            return False  # never written over a file not read yet: it would erase what it holds
        return write_json(self._file, self._data())

    def _data(self, entries: list[Entry] | None = None, showing: dict | None = None) -> dict:
        """layout.json: {"Setups": [{"Displays": [...], "Windows": [...], "Showing": {...}}]}."""
        setups = group_setups(self._entries if entries is None else entries, Entry.to_json)
        for key, shown in (self._showing if showing is None else showing).items():
            setups.setdefault(key, {"Displays": list(key), "Windows": []})["Showing"] = shown
        return {"Setups": list(setups.values())}

    # ---- applying

    @staticmethod
    def apply(h: int, e: Entry, device: str, win) -> bool:
        """Moves the window to its saved position and state (normal, maximized or minimized)
        on the given display, shifted if that display moved in the layout. A window already there
        is not touched. A hidden window stays hidden (placement with SW_HIDE: only its position
        moves; the caller applies the saved state when it shows it). The caller skips windows that
        are not responding."""
        with win.in_window_dpi_context(h):
            work = win.get_work_area(device)
            wp = win.get_placement(h)
            if not work or work == (0, 0, 0, 0) or wp is None:
                return False
            dx, dy = work[0] - e.WorkLeft, work[1] - e.WorkTop
            rc = (e.Left + dx, e.Top + dy, e.Right + dx, e.Bottom + dy)
            if wp.showCmd == e.ShowCmd and wp.rcNormalPosition.tup() == rc:
                return False
            wp.flags = 0
            if not win.is_window_visible(h):
                wp.showCmd = N.SW_HIDE  # a show command would show (and maybe activate) it
            elif e.ShowCmd == N.SW_SHOWMAXIMIZED:
                wp.showCmd = N.SW_SHOWMAXIMIZED
            elif e.ShowCmd == N.SW_SHOWMINIMIZED:
                wp.showCmd = N.SW_SHOWMINNOACTIVE  # without taking focus
            else:
                wp.showCmd = N.SW_SHOWNOACTIVATE
            wp.rcNormalPosition = N.RECT(*rc)
            return win.set_placement(h, wp)
