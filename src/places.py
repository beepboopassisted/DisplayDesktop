"""Last known places (places.json): which monitor, desktop and position every open window is on,
recorded automatically whenever that changes, and one last time whenever DisplayDesktop ends (Exit,
run stop, Windows shutting down, restarting or signing out, an error). Used when DisplayDesktop
starts: windows that are open then, or that open again within the startup grace time, go back to
their last known place, and every monitor gets its desktops back (empty ones included).

A window closed while DisplayDesktop runs is not put back when it opens again: it opens where
Windows creates it. Its record is kept only CLOSED_KEEP_S seconds, because when Windows shuts down
or restarts, apps close while DisplayDesktop is still running, and those windows must still be
restored after the restart. "Updated" (refreshed at least every HEARTBEAT_S seconds while running)
tells at the next start which records were still current when DisplayDesktop stopped.

Several windows of one app (two browser windows on different displays and desktops) each go back
to their own place: within the same boot (a crash, run restart) the windows still exist, so each
record also keeps the window's handle and process and a window is matched to its own record first.
After a reboot the handles are new: then windows are matched by count (layout.WindowLayout.assign):
an app's first window takes its first window's place, and so on.

Rewritten when windows open, close or change desktop or monitor, and every REFRESH_MS otherwise
(positions included), but only when something changed; "Updated" at least every HEARTBEAT_S.

Separate from the saved desktops (layout.json, "Save" / "Load"), which this
never reads or changes. Same matching and window identity as layout.py.
"""
from __future__ import annotations

import time

from . import native as N
from . import paths
from .paths import PLACES_FILE, is_int, write_json
from .layout import Entry, Place, WindowLayout, read_setups, seed_order

CLOSED_KEEP_S = 30
HEARTBEAT_S = 60
REFRESH_MS = 2000   # positions are recorded at least this often (used by DesktopManager)
BOOT_TOLERANCE_S = 30


def same_boot(uptime_ms, written_s, win) -> bool:
    """Whether a file written at uptime `uptime_ms` (and wall time `written_s`) comes from the
    Windows session running now: window handles and process ids only mean something within one
    boot. The uptime must not be ahead of now, and the boot moment (wall time minus uptime) must
    match now's within BOOT_TOLERANCE_S; an uptime check alone accepts an older boot whenever the
    app starts later in the new boot than the file was written in the old one. Without the wall
    time (older files) only the uptime is checked. A wrong "no" is harmless: windows are then
    matched by program and count instead of by handle."""
    if not (isinstance(uptime_ms, int) and not isinstance(uptime_ms, bool)):
        return False
    now_tick = win.tick_count64()
    if uptime_ms > now_tick:
        return False
    if written_s is None:
        return True
    if isinstance(written_s, bool) or not isinstance(written_s, (int, float)):
        return False
    return abs((written_s - uptime_ms / 1000.0) - (win.wall_time() - now_tick / 1000.0)) <= BOOT_TOLERANCE_S


class WindowPlaces(WindowLayout):
    FILE_NAME = PLACES_FILE

    def __init__(self, directory: str, win=N, clock=time.time):
        self._clock = clock
        self._closed: dict[int, float] = {}   # id(entry) -> when its window closed (wall clock)
        self._pending: set[int] = set()       # ids of entries from the last session not taken yet
        self._last_write = 0.0
        self._last_payload: tuple | None = None  # what the last write held (None: none yet, or write_next)
        self._ids: dict[int, tuple[int, int]] = {}  # id(entry) -> (window handle, process id)
        # id(entry) -> (its program's command line, its process id when recorded: 0 if not known, its
        # packaged app's id: "" for other programs)
        self._cmds: dict[int, tuple[str, int, str]] = {}
        self._app_cache: dict[int, str] = {}  # window -> its packaged app's id (read once per window)
        self._cmd_cache: dict[tuple[int, int], str] = {}  # (pid, start time) -> command line (once per process)
        self.last_boot = False  # read from a file another boot wrote (Windows restarted since)
        # setup -> display name -> number of desktops (empty ones count too)
        self._counts: dict[tuple, dict[str, int]] = {}
        self._whole_setup = False  # assign_setup: every open place of the setup is a candidate
        self._grace_over = False  # a record without keep_pending was made: last session's places are done
        self._forced = False  # write_next: the next write is wanted now (a late read is tried at once)
        self.late_read = False  # read late, during the startup grace: the manager applies it (take_late_read)
        self._defer = False  # defer_writes: records stay in memory until write_deferred
        self._deferred: float | None = None  # the time of the record waiting to be written
        super().__init__(directory, win)

    def _loaded(self, data: dict, pairs: list[tuple[Entry, dict]]) -> None:
        updated = data.get("Updated")
        updated = updated if (is_int(updated) or isinstance(updated, float)) else 0
        keep = []
        self._counts = {}
        for setup, s in read_setups(data):
            counts = {str(k): v for k, v in (s.get("Desktops") or {}).items() if is_int(v) and v > 0}
            if counts:
                self._counts[setup] = counts
        # Handles are only meaningful within the boot that wrote them (they are reused later)
        this_boot = same_boot(data.get("UptimeMs"), updated if updated else None, self._win)
        self.last_boot = not this_boot and bool(pairs)
        for e, raw in pairs:
            closed = raw.get("Closed") or 0
            if isinstance(closed, (int, float)) and closed and updated - closed > CLOSED_KEEP_S:
                continue  # closed by the user well before DisplayDesktop stopped
            keep.append(e)
            handle, pid = raw.get("Handle"), raw.get("Pid")
            if this_boot and isinstance(handle, int) and isinstance(pid, int) and handle and pid:
                self._ids[id(e)] = (handle, pid)
            cmd, app = raw.get("Command"), raw.get("App")
            cmd = cmd if isinstance(cmd, str) else ""
            app = app if isinstance(app, str) else ""
            if cmd or app:
                self._cmds[id(e)] = (cmd, pid if is_int(pid) else 0, app)
        self._entries = keep
        self._pending = {id(e) for e in keep}

    def _candidates(self) -> list[Entry]:
        # Only places from the last session: a window closed and reopened now is never put back.
        # assign_setup: every place of the setup whose window was open when last recorded.
        setup, pending, closed = self._setup, self._pending, self._closed
        if self._whole_setup:
            return [e for e in self._entries if e.Setup == setup and (id(e) in pending or id(e) not in closed)]
        return [e for e in self._entries if e.Setup == setup and id(e) in pending]

    def assign_setup(self, windows: list[int]) -> list[tuple[int, Entry]]:
        """Displays attached or detached: the open windows matched to the places recorded for the
        setup in use (its own window first, as at a start)."""
        self._whole_setup = True
        try:
            return self.assign(windows)
        finally:
            self._whole_setup = False

    def assign(self, windows: list[int]) -> list[tuple[int, Entry]]:
        # The same window (same boot, same handle and process) first: its own record; then the
        # others by count (WindowLayout.assign)
        result = []
        bound = {id(x) for x in self._bound.values()}
        by_key: dict[tuple[int, int], Entry] = {}  # (handle, pid) -> free entry, built once
        for x in self._candidates():
            if id(x) not in bound and id(x) in self._ids:
                by_key.setdefault(self._ids[id(x)], x)
        for h in windows:
            if h in self._bound:
                continue
            e = by_key.pop((h, self._win.get_pid(h)), None)
            if e is not None:
                self._bound[h] = e
                seed_order(h, e.Order, self._win)  # (its own number back: not renumbered by the next record)
                result.append((h, e))
        result += super().assign([h for h in windows if h not in self._bound])
        for _, e in result:
            self._pending.discard(id(e))
        return result

    def _command_of(self, pid: int) -> str:
        """The command line that started the process (Restore Applications On Restart), read once per process."""
        if not pid:
            return ""
        key = (pid, self._win.process_started(pid))  # (a pid can be used again; with its start time, never)
        cmd = self._cmd_cache.get(key)
        if cmd is None:
            if len(self._cmd_cache) >= 256:
                self._cmd_cache.clear()  # (closed processes' lines go now and then)
            cmd = self._cmd_cache[key] = self._win.process_command_line(pid)
        return cmd

    def _app_of(self, h: int) -> str:
        """The window's packaged app id (Store apps, packaged programs), read once per window."""
        app = self._app_cache.get(h)
        if app is None:
            if len(self._app_cache) >= 1024:
                w = self._win
                self._app_cache = {k: v for k, v in self._app_cache.items() if w.is_window(k)}
            app = self._win.window_app_id(h)
            if app or self._win.get_class(h) != "ApplicationFrameWindow":
                self._app_cache[h] = app  # (a Store app's frame without its app inside yet: asked again)
        return app

    def apps_to_restart(self) -> list[tuple[str, str, str]]:
        """After a Windows restart: (program, command line, packaged app id) of each app that had
        windows in the setup in use when it was last recorded, once per process (a packaged app:
        once per app)."""
        if not self.last_boot:
            return []
        out, seen = [], set()
        for e in self._candidates():
            cmd, pid, app = self._cmds.get(id(e), ("", 0, ""))
            key = ("app", app) if app else pid or cmd
            if (app or (cmd and e.Exe)) and key not in seen:
                seen.add(key)
                out.append((e.Exe, cmd, app))
        return out

    def desktop_count(self, display: str) -> int:
        """How many desktops the display had last time in the setup in use (0 if unknown): empty
        desktops are only ever removed by the user, so they come back too."""
        return self._counts.get(self._setup, {}).get(display, 0)

    def record(self, windows: list[Place], keep_pending: bool, counts: dict[str, int]) -> None:
        """Records where the given windows are now. Kept as well: windows left out that are still
        open (waiting for their monitor), windows closed in the last CLOSED_KEEP_S seconds, and
        (keep_pending, during the startup grace time) places of the last session not taken yet."""
        now = self._clock()
        w = self._win
        anew = {p.window for p in windows}
        for h, e in list(self._bound.items()):
            if h not in anew and not w.is_window(h):
                self._closed.setdefault(id(e), now)
                del self._bound[h]
        taken_anew = {id(e) for h, e in self._bound.items() if h in anew}
        waiting = {id(e) for h, e in self._bound.items() if h not in anew}
        if not keep_pending:
            self._pending.clear()
            self._grace_over = True
        entries = []
        setup = self._setup
        for e in self._entries:
            i = id(e)
            if e.Setup != setup:
                entries.append(e)  # (another setup's places: kept as last recorded there)
                continue
            if i in taken_anew:
                continue
            if i in waiting or i in self._pending or now - self._closed.get(i, -1e18) <= CLOSED_KEEP_S:
                entries.append(e)
        kept = {id(e) for e in entries}
        self._closed = {i: t for i, t in self._closed.items() if i in kept}
        bound = {h: e for h, e in self._bound.items() if id(e) in kept}
        for p in windows:
            e = self._entry_for(p)
            if e is not None:
                entries.append(e)
                bound[p.window] = e
                pid = w.get_pid(p.window)
                self._ids[id(e)] = (p.window, pid)
                self._cmds[id(e)] = (self._command_of(pid), pid, self._app_of(p.window))
        self._entries, self._bound = entries, bound
        kept = {id(e) for e in entries}
        self._ids = {i: v for i, v in self._ids.items() if i in kept}
        self._cmds = {i: v for i, v in self._cmds.items() if i in kept}
        if counts:
            self._counts[setup] = dict(counts)
        self._write_places(now, force=False)

    def heartbeat(self) -> None:
        """Called on every sync: refreshes "Updated" (and forgets closed windows) now and then."""
        now = self._clock()
        if now - self._last_write >= HEARTBEAT_S:
            self._entries = [e for e in self._entries
                             if id(e) not in self._closed or now - self._closed[id(e)] <= CLOSED_KEEP_S]
            kept = {id(e) for e in self._entries}
            self._closed = {i: t for i, t in self._closed.items() if i in kept}
            # (also the ids: a dropped entry's id can be reused by a new entry, which must not inherit
            # its window handle and process)
            self._ids = {i: v for i, v in self._ids.items() if i in kept}
            self._cmds = {i: v for i, v in self._cmds.items() if i in kept}
            self._pending &= kept
            self._write_places(now)

    def _write_places(self, now: float, force: bool = True) -> None:
        if self._defer:
            self._deferred = now  # (written by write_deferred, after the windows are shown at exit)
            return
        if self._unread:
            # places.json there but not read yet (held at start, its folder offline): never written
            # over. Once read, the last session's places not taken yet join this run's (only during
            # the startup grace time, as always), and the write goes on. A forced write (the last
            # snapshot, a new folder) tries the read at once, others at most every READ_RETRY_S.
            mine, counts = list(self._entries), dict(self._counts)
            self._entries, self._counts = [], {}  # (what the read finds; none if the file is gone now)
            forced, self._forced = self._forced, False  # (one read at once: later ones throttled again)
            if not self.retry_read(throttled=not forced):
                self._entries, self._counts = mine, counts
                self._last_write = now  # (the heartbeat waits too: not a read every sync)
                return
            found = bool(self._counts.get(self._setup))
            for setup, mine_counts in counts.items():  # (desktops are never removed by themselves: the larger count)
                theirs = self._counts.setdefault(setup, {})
                for k, v in mine_counts.items():
                    theirs[k] = max(theirs.get(k, 0), v)
            loaded = [] if self._grace_over else self._entries
            self._pending = {id(e) for e in loaded}
            self._entries = mine + loaded
            # During the startup grace the manager then puts the windows it already adopted back to
            # these places, as at a normal start (take_late_read)
            self.late_read = bool(loaded) or found
        self._forced = False
        rows = []
        # A last-session place of a window this run has recorded too (same window, same boot: read
        # late) is kept in memory for the manager to put it back, but never written twice
        ids, pending, closed, cmds = self._ids, self._pending, self._closed, self._cmds
        current = {ids[i] for i in map(id, self._entries) if i not in pending and i in ids}
        for e in self._entries:
            i = id(e)
            if i in pending and ids.get(i) in current:
                continue
            handle, pid = ids.get(i, (0, 0))
            cmd, cmd_pid, app = cmds.get(i, ("", 0, ""))
            rows.append((e, closed.get(i, 0), handle, pid or cmd_pid, cmd, app))
        # What would be written (every value of every record, the counts), compared without building
        # the JSON: it is only made when written
        payload = (tuple((e.Setup, e.Exe, e.Class, e.TitleHash, e.Display, e.Desktop, e.WorkLeft, e.WorkTop,
                          e.ShowCmd, e.Left, e.Top, e.Right, e.Bottom, e.Order, c, h, p, cmd, app)
                         for e, c, h, p, cmd, app in rows),
                   tuple(sorted((k, tuple(sorted(v.items()))) for k, v in self._counts.items())))
        if not force and payload == self._last_payload and now - self._last_write < HEARTBEAT_S:
            return  # nothing changed: no disk write
        if self._last_payload is not None and not paths.write_due(self._file):
            return  # (held by another program a moment ago: no 0.2 s wait every 2 s; forced ones go)
        self._last_write = now
        # {"Updated", "UptimeMs", "Setups": [{"Displays", "Windows", "Desktops"}]}
        setups = {}
        for e, c, h, p, cmd, app in rows:
            setups.setdefault(e.Setup, {"Displays": list(e.Setup), "Windows": []})["Windows"].append(
                dict(e.to_json(), Closed=c, Handle=h, Pid=p, Command=cmd, App=app))
        for key, counts in self._counts.items():
            setups.setdefault(key, {"Displays": list(key), "Windows": []})["Desktops"] = counts
        if write_json(self._file, {"Updated": now, "UptimeMs": self._win.tick_count64(),
                                   "Setups": list(setups.values())}):
            self._last_payload = payload  # (only once written: a failed write is tried again)

    def relocate(self, directory: str) -> None:
        """The data folder moved. Places this run never managed to read stay unread there (the move
        copied that file): read and merged before the first write, never written over."""
        from . import datafolder
        was_unread = self._unread
        super().relocate(directory)
        # (only for a copy of ours: that folder's own data kept is not this run's last session)
        self._unread = was_unread and not datafolder.kept_new

    def defer_writes(self) -> None:
        """Records keep only memory up to date until write_deferred (the last snapshot at exit: its
        disk write waits until every hidden window is shown, a dead share can't hold them up)."""
        self._defer = True

    def write_deferred(self) -> None:
        self._defer = False
        when, self._deferred = self._deferred, None
        if when is not None:
            self._write_places(when)

    def take_late_read(self) -> bool:
        """Once: the file was read late (it could not be read at start) and its places apply."""
        late, self.late_read = self.late_read, False
        return late

    def write_next(self) -> None:
        """The next record is written even if nothing changed (a new folder, the last snapshot)."""
        self._last_payload = None
        self._forced = True

