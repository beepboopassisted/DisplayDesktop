"""The saved desktops (layout.json): which monitor, desktop and position every window was on,
and which desktop each monitor was showing. Saved only when the user chooses "Save"
in the tray menu (save) or erased by "Clear Desktops" (clear); nothing is recorded automatically.
Used to put windows back when they open again (also after a restart). The desktop each monitor
showed last is remembered separately, automatically (last_shown.py, showing.json).

HWNDs change when an app or Windows restarts, so a window is recognized by its program path,
window class and title. Titles are stored only as hashes (they can contain private text): the whole
title, and each part of it ("Inbox (3) - Webmail - Web Browser" -> "inbox", "webmail", "web
browser"), so a browser window whose page changed is still recognized by its site.
Positions are kept in the window's own DPI coordinates (see native.in_window_dpi_context).
The file format is the same as the C# version's (PascalCase keys), so existing files keep working.
"""
from __future__ import annotations

import functools
import json
import os
import re
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
# Settings, Photos...) is an ApplicationFrameWindow of ApplicationFrameHost.exe. Their windows are
# matched only by exact title, never loosely, or one app could take another app's saved place.
SHARED_HOSTS = frozenset({"applicationframehost.exe"})
NOT_READ_YET = -2  # WindowLayout.save: the save there can't be read right now (not written)
NOT_READABLE = -3  # ... and not because it is in use (a cloud file not available, no read right)
LOCKED = -4        # ... because it is encrypted (Encrypt Settings) and no known key opens it

_FIELDS = ("Exe", "Class", "TitleHash", "MonitorId", "Desktop", "WorkLeft", "WorkTop", "ShowCmd",
           "Left", "Top", "Right", "Bottom")

# How sure a match is, from most to least: the windows are matched in this order, each level for
# all windows before the next, so a weaker match never takes a place a stronger one would get.
PROCESS = 0  # the same app instance (same process, same boot): it may have recreated its window
EXACT = 1    # same program, class and title
SITE = 2     # same program and class, and a title part in common ("Webmail", "report.docx")
LOOSE = 3    # same program and class only (any window of that app)


class Entry:
    """One saved window. Identity semantics: two entries with equal data are still different windows.
    MonitorId: stable monitor identity (native.get_monitor_id); Desktop: desktop index on that monitor
    (0-based); WorkLeft/WorkTop: top-left of the monitor's work area when saved; TitleParts: hashes of
    the title's parts (title_parts)."""
    __slots__ = _FIELDS + ("TitleParts",)

    def __init__(self, Exe: str = "", Class: str = "", TitleHash: str = "", MonitorId: str = "",  # noqa: N803
                 Desktop: int = 0, WorkLeft: int = 0, WorkTop: int = 0, ShowCmd: int = 0, Left: int = 0,  # noqa: N803
                 Top: int = 0, Right: int = 0, Bottom: int = 0, TitleParts: list | None = None):  # noqa: N803
        self.Exe, self.Class, self.TitleHash, self.MonitorId = Exe, Class, TitleHash, MonitorId
        self.Desktop, self.WorkLeft, self.WorkTop, self.ShowCmd = Desktop, WorkLeft, WorkTop, ShowCmd
        self.Left, self.Top, self.Right, self.Bottom = Left, Top, Right, Bottom
        self.TitleParts = [] if TitleParts is None else TitleParts

    def __repr__(self) -> str:
        return "Entry(" + ", ".join(f"{k}={getattr(self, k)!r}" for k in self.__slots__) + ")"

    def to_json(self) -> dict:
        # The C# version's keys, then the title parts (unknown keys are ignored by readers)
        return dict({k: getattr(self, k) for k in _FIELDS}, TitleParts=list(self.TitleParts))

    @staticmethod
    def from_json(d: dict) -> "Entry":
        e = Entry()
        for k in _FIELDS:
            if k in d and d[k] is not None:
                v = d[k]
                default = getattr(e, k)
                if isinstance(default, int) and not is_int(v):
                    raise ValueError(f"bad {k}")
                if isinstance(default, str) and not isinstance(v, str):
                    raise ValueError(f"bad {k}")
                setattr(e, k, v)
        parts = d.get("TitleParts")
        if isinstance(parts, list):
            e.TitleParts = [p for p in parts if isinstance(p, str)]
        return e


Place = namedtuple("Place", "window monitor_id device desktop")  # a window to save: where it is now


EXE_CACHE_MAX = 1024  # program paths kept per (window, process)


@functools.lru_cache(maxsize=1024)  # titles repeat on every 2 s record (room for many open windows' titles)
def title_hash(title: str) -> str:
    """First 12 bytes of SHA-256 of the UTF-8 title, as uppercase hex (same as the C# version)."""
    return _sha256(title.encode("utf-8", "surrogatepass")).hexdigest()[:24].upper()


_SEPARATORS = re.compile(r"\s+[-\u2013\u2014|\u00b7\u2022]\s+")   # " - ", " – ", " — ", " | ", " · ", " • "
_COUNTERS = re.compile(r"\(\d+\+?\)|\[\d+\+?\]")              # "(3)", "[12]", "(99+)": unread counts
_MARKERS = "*\u25cf\u2022 "                                      # unsaved-changes marks: "*a.txt", "\u25cf main.py"


def program_parts(titles: list[set[str]]) -> set[str]:
    """The title parts a program puts in its titles (its name), from the parts of its saved and open
    windows' titles: what the titles holding its most frequent part all share (each of them, when odd
    titles are as frequent). A title without it ("Downloads") is left out, so odd titles never make
    the name count as a site; a single title gives all of its parts (nothing to tell its site from
    its name)."""
    # (each distinct title once: several windows or saved places with the same odd title ("Downloads"
    # three times) never outnumber the program's name)
    titles = [set(t) for t in dict.fromkeys(frozenset(t) for t in titles)]
    counts: dict[str, int] = {}
    for parts in titles:
        for p in parts:
            counts[p] = counts.get(p, 0) + 1
    if not counts:
        return set()
    best = max(counts.values())
    if best < 2:
        return set.intersection(*titles)  # (no part repeats: only what every title has, if any)
    # Each most frequent part (a tie: odd titles as frequent as the name) with what its titles share
    out: set[str] = set()
    for top in (p for p, n in counts.items() if n == best):
        out |= set.intersection(*(parts for parts in titles if top in parts))
    return out


def title_parts(title: str) -> list[str]:
    """Hashes of the parts of a title, as browsers and editors build them ("Page - Site - Browser",
    "*notes.txt - Notepad"): lowercase, without unread counters or unsaved-changes marks, so
    "Inbox (3) - Webmail" and "Inbox (5) - Webmail" share "inbox" and "webmail", and "*notes.txt" is
    still "notes.txt"."""
    return list(_title_parts(title))  # a fresh list: entries keep their own


@functools.lru_cache(maxsize=1024)
def _title_parts(title: str) -> tuple[str, ...]:
    parts: list[str] = []
    for seg in _SEPARATORS.split(title):
        seg = " ".join(_COUNTERS.sub(" ", seg).lower().split()).strip(_MARKERS)
        if len(seg) >= 2:
            h = title_hash(seg)
            if h not in parts:
                parts.append(h)
    return tuple(parts)


class WindowLayout:
    FILE_NAME = LAYOUT_FILE

    def __init__(self, directory: str, win=N):
        self._win = win
        self._file = os.path.join(directory, self.FILE_NAME)
        self._exe_cache: dict[tuple[int, int], str] = {}  # (window, pid) -> program path
        self._entries: list[Entry] = []
        self._showing: dict[str, int] = {}
        self._bound: dict[int, Entry] = {}  # live window -> its entry
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
        mine = json.dumps({"Windows": [e.to_json() for e in self._entries], "Showing": self._showing},
                          sort_keys=True)
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
            raw = data.get("Windows") or []
            entries = [Entry.from_json(e) for e in raw]
            showing = {str(k): int(v) for k, v in (data.get("Showing") or {}).items()
                       if is_int(v)}
            self._entries, self._showing = entries, showing
            self._unread = False
            self._held_since = None
            self._loaded(data, list(zip(entries, raw)))
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

    def showing_desktop(self, monitor_id: str) -> int | None:
        """The desktop index the monitor was showing when saved, if any."""
        return self._showing.get(monitor_id)

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

    def _pid_of(self, e: Entry) -> int:
        """The process that had this entry's window, if known and still meaningful (0 if not)."""
        return 0

    def assign(self, windows: list[int], level: int = LOOSE) -> list[tuple[int, Entry]]:
        """Matches newly seen windows to free entries of the same program and class, up to the
        given level (PROCESS, EXACT, SITE or LOOSE), and binds them. All windows are considered
        together: the best pairs are taken first, so the order in which windows happened to open
        does not matter. A pair is better when (in this order) it is the same app instance (same
        process), the title is the same, more title parts are shared (parts every entry of that
        program has, like the app name, do not count), the place is on the monitor the window is on
        now, and it is closer to where the window is now. So identical windows (three "Untitled -
        Notepad") each get a place of their own: copies are never stacked on one place.
        Store apps (SHARED_HOSTS) share one process and only ever match their exact title."""
        w = self._win
        bound = {id(e) for e in self._bound.values()}
        free = [e for e in self._candidates() if id(e) not in bound]
        titles: dict[tuple[str, str], list[set[str]]] = {}
        by_app: dict[tuple[str, str], list] = {}  # (program, class) -> [(entry, its title parts)], in `free` order
        for e in free:
            key, eparts = (e.Exe.lower(), e.Class), set(e.TitleParts)
            titles.setdefault(key, []).append(eparts)
            by_app.setdefault(key, []).append((e, eparts))
        # (every part of the saved titles: an open window counts only if it shares one, so repeated
        # odd open titles ("Downloads" twice) never outnumber the program's name)
        saved = {key: set().union(*sets) for key, sets in titles.items()}
        seen = []
        for h in windows:
            if h in self._bound:
                continue
            exe, cls, title = self._exe_of(h).lower(), w.get_class(h), w.get_title(h)
            parts = set(title_parts(title))
            seen.append((h, exe, cls, title, parts))
            if (exe, cls) in titles and parts & saved.get((exe, cls), set()):
                titles[(exe, cls)].append(parts)  # (the windows' titles count too, those like a saved one)
        # The parts the program puts in its titles (its name): never a site
        common = {key: program_parts(sets) for key, sets in titles.items()}
        pairs = []
        monitor_ids: dict[str, str] = {}  # device -> monitor id (read once per display, not per window)
        for h, exe, cls, title, parts in seen:
            same_app = by_app.get((exe, cls))
            if not same_app:
                continue  # (no saved entry of that program and class)
            shared_host = os.path.basename(exe) in SHARED_HOSTS
            pid = w.get_pid(h)
            th = title_hash(title)
            with w.in_window_dpi_context(h):
                wp = w.get_placement(h)
            now = wp.rcNormalPosition.tup() if wp is not None else (0, 0, 0, 0)
            dev = w.monitor_device_of_window(h)
            here = ""
            if dev:
                here = monitor_ids.get(dev)
                if here is None:
                    here = monitor_ids[dev] = w.get_monitor_id(dev)
            name = common[(exe, cls)]
            for e, eparts in same_app:
                exact = e.TitleHash == th
                if shared_host and not exact:
                    continue
                same_process = not shared_host and bool(pid) and self._pid_of(e) == pid
                shared = len((parts & eparts) - name)
                kind = PROCESS if same_process else EXACT if exact else SITE if shared else LOOSE
                if kind > level:
                    continue
                score = (same_process, exact, shared, bool(here) and e.MonitorId == here,
                         -(abs(e.Left - now[0]) + abs(e.Top - now[1])))
                pairs.append((score, h, e))
        pairs.sort(key=lambda p: p[0], reverse=True)  # stable: ties keep window/entry order
        taken_h: set[int] = set()
        taken_e: set[int] = set()
        result = []
        for _, h, e in pairs:
            if h in taken_h or id(e) in taken_e:
                continue
            taken_h.add(h)
            taken_e.add(id(e))
            self._bound[h] = e
            result.append((h, e))
        return result

    def _candidates(self) -> list[Entry]:
        """Entries a newly seen window may be matched to."""
        return self._entries

    def _entry_for(self, p: Place) -> Entry | None:
        """An entry for where the window is now (None if its placement cannot be read)."""
        w = self._win
        with w.in_window_dpi_context(p.window):
            wp = w.get_placement(p.window)
            work = w.get_work_area(p.device)
        if wp is None or not work or work == (0, 0, 0, 0) or not p.monitor_id:
            return None
        r = wp.rcNormalPosition
        title = w.get_title(p.window)
        return Entry(Exe=self._exe_of(p.window), Class=w.get_class(p.window),
                     TitleHash=title_hash(title), TitleParts=title_parts(title), MonitorId=p.monitor_id,
                     Desktop=p.desktop, WorkLeft=work[0], WorkTop=work[1], ShowCmd=wp.showCmd,
                     Left=r.left, Top=r.top, Right=r.right, Bottom=r.bottom)

    # ---- saving (manual only)

    def save(self, windows: list[Place], showing: dict[str, int], attached_monitor_ids: set[str]) -> int:
        """Tray "Save": replaces the memory of the attached monitors with where the
        given windows are now and which desktop each monitor shows, and writes layout.json. Memory of
        monitors that are not attached is kept (their windows cannot be arranged now). Returns the
        number of windows saved, -1 if layout.json could not be written, or NOT_READ_YET while the
        save there can't be read (writing would drop the other monitors' save: try again shortly)."""
        problem = self.read_problem()
        if problem:
            return problem
        anew = {p.window for p in windows}
        taken_anew = {id(e) for h, e in self._bound.items() if h in anew}
        entries = [e for e in self._entries
                   if e.MonitorId not in attached_monitor_ids and id(e) not in taken_anew]
        kept = {id(e) for e in entries}
        bound = {h: e for h, e in self._bound.items() if id(e) in kept}

        count = 0
        for p in windows:
            e = self._entry_for(p)
            if e is None:
                continue
            entries.append(e)
            bound[p.window] = e
            count += 1

        new_showing = {k: v for k, v in self._showing.items() if k not in attached_monitor_ids}
        new_showing.update(showing)
        if not write_json(self._file, {"Windows": [e.to_json() for e in entries], "Showing": new_showing}):
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
        return write_json(self._file, {"Windows": [e.to_json() for e in self._entries], "Showing": self._showing})

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
