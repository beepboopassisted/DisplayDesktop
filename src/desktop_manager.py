"""Per-monitor virtual desktop manager.

Does not use the Windows virtual desktop system; it keeps window sets per monitor and, on a switch,
hides/shows only the windows on that monitor. Desktops are dynamic and each monitor has its own
count; numbering is global across monitors (Monitor 1: 1-2-3, Monitor 2: 4-5-6 ...).

Key invariants:
- A visible eligible window is always in its monitor's current desktop set.
- Only windows in _hidden (hidden by us) are ever re-shown. Windows an app hid itself are dropped.
- Hung windows are never hidden.
- try_hide / show_if_ours are the only hide/show paths.

Window sets are dicts used as insertion-ordered sets ({hwnd: None}).
"""
from __future__ import annotations

import json
import os
import time
from collections import namedtuple
from collections.abc import Callable

from . import native as N
from .last_shown import LastShown
from .layout import Entry, Place, WindowLayout, number_new, setup_key
from .places import REFRESH_MS, WindowPlaces, same_boot
from . import datafolder, paths
from .paths import JOURNAL_FILE, data_dir, is_int, write_json

MAX_DESKTOPS_PER_MONITOR = 10  # a safety cap (a damaged file cannot create thousands of desktops)
STARTUP_GRACE_MS = 120_000
MONITOR_ID_REFRESH_MS = 30_000
SETUP_SETTLE_MS = 2000  # displays attached or detached: their setup's places apply once unchanged this long
RELAUNCH_AFTER_MS = 5000  # Restore Applications On Restart: apps start again this long after DisplayDesktop does
CREATE_NEW_CONSOLE = 0x10  # (a console program gets a console of its own; windowed ones don't care)
CAPTURE_BUDGET_MS = 50  # previews captured per switch at most this long (only windows without one)
PREVIEW_FRESH_S = 5.0  # a window in use gets a new background preview at most this often
PREVIEW_MAX_AGE_S = 60.0  # any shown window's preview is taken again in the background when older
PREVIEW_RETRY_S = 60.0  # a window whose capture gave nothing is not tried again for this long
SLIDE_HOLD_S = 0.6  # no background capture starts this long after a switch begins (its slide runs)
STALE_TRIES = 3  # deletes of a journal left in a folder moved away from, before giving up
LATER_SUFFIX = paths.LATER_SUFFIX  # a crashed run's journal not readable at start, moved aside from ours
OFFLINE_TRIES = 5  # reads of a journal whose folder is offline 2 × PAUSE_GONE_S apart (then 10 × apart)
UNREAD_TELL = 12   # failed reads of a crash journal (one per READ_RETRY_S) before the user is told
SEEN_PRUNE_AT = 512  # `_seen` is cleared of closed windows when it grows past this (then twice its size)
REFUSED_SHOW_MS = 6000  # a refused show is retried every sync this long, then every REFUSED_RETRY_MS
REFUSED_RETRY_MS = 10_000
ACTIVATING_SHOWS = {1, 2, 3, 5, 9, 10}  # SW_* that activate the window (a retry never uses them)
# A saved state (layout/places ShowCmd) -> the placement that shows a window hidden while it was
# placed, in that state (normal: SW_SHOWNOACTIVATE). SW_SHOWMAXIMIZED always activates the window
# (Windows has no other way to maximize); a switch gives the focus back to its last active window.
SHOW_LATER = {N.SW_SHOWMAXIMIZED: N.SW_SHOWMAXIMIZED, N.SW_SHOWMINIMIZED: N.SW_SHOWMINNOACTIVE}

CLASS_BLACKLIST = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                   "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"}
TRANSIENT_CLASSES = {"tooltips_class32", "#32768", "SysShadow", "Auto-Suggest Dropdown"}


SwitchInfo = namedtuple("SwitchInfo", "device ordinal local_index local_count global_number")
WindowEntry = namedtuple("WindowEntry", "handle title")
DesktopEntry = namedtuple("DesktopEntry", "local_index global_number is_current windows")
MonitorEntry = namedtuple("MonitorEntry", "device ordinal desktops")


class MonitorState:
    def __init__(self, device: str):
        self.device = device
        self.desktops: list[dict[int, None]] = [{}]
        self.last_active: list[int] = [0]
        self.current = 0
        # Saved desktop to show again once it has windows (after start or reconnect); -1 = none.
        self.pending_show = -1
        self.pending_until = 0
        self.work = (0, 0, 0, 0)  # last known work area (its windows are moved from here if unplugged)
        self.path = ""  # the monitor's device interface path: the state follows it when \\.\DISPLAYn names change


def display_names(screens, monitor_paths: dict[str, str], name_of) -> dict[str, str]:
    """Device -> display name (native.display_name of its monitor path). Displays with the same
    name get " (2)", " (3)"... left to right, then top to bottom. "" when the monitor is unknown."""
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    for s in sorted(screens, key=lambda s: (s.bounds[0], s.bounds[1])):
        base = name_of(monitor_paths.get(s.device, ""))
        if not base:
            out[s.device] = ""
            continue
        n = seen[base] = seen.get(base, 0) + 1
        out[s.device] = base if n == 1 else f"{base} ({n})"
    return out


class Displaced:
    """A window whose saved monitor is not attached. It stays where Windows put it, and goes
    back when that monitor is attached again, unless the user moves it meanwhile."""

    def __init__(self, entry: Entry):
        self.entry = entry
        self.settled = False
        self.device = ""
        self.desktop = 0
        self.rect = (0, 0, 0, 0)


class _HideContext:
    """State shared by the hides of one operation (switch or move)."""

    def __init__(self, floating: dict[int, list[int]]):
        self.floating = floating
        self.start = time.perf_counter()
        self.journaled: set[int] = set()  # windows already in the journal written ahead of hiding
        self.failed_writes = 0  # journal-ahead writes that failed in this operation (2: no more tries)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000.0


class _Lazy:
    """A value made on first use, then reused (one window scan shared by a whole pass)."""

    def __init__(self, make: Callable):
        self._make, self._done, self._value = make, False, None

    def get(self):
        if not self._done:
            self._value, self._done = self._make(), True
        return self._value


def hidden_windows(w, ours, own_pid: int) -> list[int]:
    """Hidden windows the user may want back, by title (see DesktopManager.hidden_windows). `ours`:
    windows on our desktops or hidden by us (they come back with their desktop)."""
    found = []
    for h in w.enum_windows():
        if h in ours or w.is_window_visible(h) or w.get_root(h) != h or w.get_pid(h) == own_pid:
            continue
        if w.title_length(h) == 0 or w.is_cloaked(h) or w.get_class(h) in CLASS_BLACKLIST:
            continue
        if w.is_hung(h):
            continue  # not responding: it could not be shown now
        found.append(h)
    return sorted(found, key=lambda h: w.get_title(h).lower())


class StartRestorer:
    """Restore Hidden Windows before any desktop exists (Recovery at start): what
    RestoreWindow needs of a DesktopManager. Nothing is ours yet, so a window shown is on screen."""

    def __init__(self, win):
        self._win = win

    def hidden_windows(self) -> list[int]:
        return hidden_windows(self._win, set(), os.getpid())

    def restore_windows(self, windows: list[int]) -> int:
        shown = 0
        for h in windows:
            if self._win.is_window(h) and not self._win.is_hung(h):
                self._win.show_window_safe(h, N.SW_SHOWNA)
                shown += bool(self._win.is_window_visible(h))  # (a refused show, e.g. an elevated app's: not counted)
        return shown


class DesktopManager:
    def __init__(self, win=N, snapshots=None, directory: str | None = None):
        self._win = win
        if snapshots is None:
            from .snapshots import WindowSnapshots
            snapshots = WindowSnapshots()
        self.snapshots = snapshots
        d = directory or data_dir()
        self._state_file = os.path.join(d, JOURNAL_FILE)
        # Journals recovery reads: this run's, and (the app's own folder only) one a run that had to
        # use the default folder may have left there (datafolder.journals)
        self._journals = [self._state_file] if directory else datafolder.journals(d)
        self._monitors: dict[str, MonitorState] = {}
        self._hidden: dict[int, None] = {}
        self._hidden_at: dict[int, float] = {}  # window -> perf_counter when we last hid it
        self._used_at: dict[int, float] = {}  # window -> time.monotonic() it was last seen in the foreground
        self.previews = True  # window previews are taken (off: Disable Overview previews; see set_previews)
        self._persisted: set[int] = set()
        self._stale_journals: list[str] = []  # ours in folders moved away from (_drop_stale_journal)
        # ours in the default folder while the folder in use is offline (_write_fallback): only for the
        # real data folder, never a test's
        self._fallback_allowed = directory is None and not paths.is_test_mode()
        self._fallback_journal: str | None = None
        self._stale_tries: dict[str, int] = {}  # failed deletes of each
        self._stale_next: dict[str, float] = {}  # stale journal -> time.monotonic() of its next try
        self._given_up: list[str] = []  # stale journals no longer tried while running (erased at exit)
        self._ending = False  # end() runs: it records the run as ended (EndedRuns)
        self._kept_registered: list[str] = []  # stale journals left registered for EndedRuns to skip
        self._seen: set[int] = set()  # windows this run has had on a desktop (a late journal read never re-shows them)
        self._seen_limit = SEEN_PRUNE_AT
        self._recover_later: list[str] = []  # crash journals not readable at start (_recover_later_due)
        self._offline_tries: dict[str, int] = {}  # journal -> reads found its folder offline
        self._unread_tries: dict[str, int] = {}  # journal -> reads that could not open it
        self._alt_journal: str | None = None  # ours under a name of its own (_write_records)
        # a journal read late was damaged, or one stayed unreadable (UNREAD_TELL): the tray tells the
        # user with the "can't be read" dialog, then clears it
        self.late_problem = False
        self._floating_hidden: dict[int, list[int]] = {}
        self._own_pid = os.getpid()
        # This manager, as written into its journal: one read back after a folder move is not a crash's
        self._writer = f"{self._own_pid}-{os.urandom(16).hex()}"  # (32 random hex digits; no uuid module)
        self._monitor_ids: dict[str, str] = {}  # device -> display name (the files' key)
        self._monitor_paths: dict[str, str] = {}  # device -> monitor device interface path
        self._setup: tuple | None = None  # display setup places and showing use (None: before the first sync)
        self._setup_due = False  # displays attached or detached: _apply_setup once they settle
        self._setup_changed_at = 0  # tick of the last change
        self._screens_key: tuple = ()  # display setup the monitor ids were read for
        self._ids_read_at = -10**12     # tick of that read
        self._displaced: dict[int, Displaced] = {}
        self._ended = False  # end() ran: no more changes (see end)
        self._start_tick = win.tick_count64()
        # Restore Applications On Restart (the tray sets it from the setting): after a Windows restart, the
        # apps that were open start again, once, RELAUNCH_AFTER_MS after the start (_relaunch_apps)
        self.restore_apps = False
        self._relaunched = False
        self.log: Callable[[str], None] = lambda text: None  # (the tray: the console)
        # Events: switch_starting(device, old, new) before anything is hidden (animation screenshot);
        # desktop_switched(SwitchInfo) after a switch; edge_reached(device, direction) when there is
        # no desktop further in that direction (nothing changes; the tray shows a bounce).
        self.switch_starting: list[Callable[[str, int, int], None]] = []
        self.desktop_switched: list[Callable[[SwitchInfo], None]] = []
        self.edge_reached: list[Callable[[str, int], None]] = []
        # Why windows a previous run hid may still be hidden (None: no known reason): "damaged" (its
        # journal can't be read). The tray tells the user, who can restore them by hand
        # (hidden_windows / restore_windows: Settings > Restore Hidden Windows).
        self.recovery_problem: str | None = None
        # Each crash record that could not be read this run and why, for Recovery
        # (recovery.build's `journals`): the read's error, or "notJournal", "badRecord", "unread"
        self.journal_problems: dict[str, BaseException | str] = {}
        self._show_later: dict[int, int] = {}  # hidden window -> show command for when it is shown
        self._refused: dict[int, list[int]] = {}  # ours, its show refused -> [tick first refused, next try]
        self._refused_main: dict[int, int] = {}  # a refused floating window -> its main window
        self._recover_previous_session()
        self._layout = WindowLayout(d, win)            # saved desktops: Save / Load only
        self._places = WindowPlaces(d, win, clock=win.wall_time)  # last known places: startup only
        self._places_signature: tuple | None = None
        self._places_at = -10**12  # tick of the last full record
        self._places_grace_over = False
        self._last_shown = LastShown(d)

    # ------------------------------------------------------------ events

    def _fire_starting(self, device: str, old: int, new: int) -> None:
        for cb in list(self.switch_starting):
            cb(device, old, new)

    def _fire_switched(self, info: SwitchInfo) -> None:
        for cb in list(self.desktop_switched):
            cb(info)

    def _fire_edge(self, device: str, direction: int) -> None:
        for cb in list(self.edge_reached):
            cb(device, direction)

    # ------------------------------------------------------------ saved layout

    @property
    def _in_startup_grace(self) -> bool:
        return self._win.tick_count64() - self._start_tick < STARTUP_GRACE_MS

    def _device_of_monitor_id(self, mid: str) -> str | None:
        if not mid:
            return None
        for dev, i in self._monitor_ids.items():
            if i == mid:
                return dev
        return None

    def _locate(self, h: int) -> tuple[MonitorState, int] | None:
        for st in self._monitors.values():
            for i, s in enumerate(st.desktops):
                if h in s:
                    return st, i
        return None

    def _follow_monitors(self, devices: list[str]) -> list[MonitorState]:
        """Display names (\\\\.\\DISPLAYn) can move to other monitors when one is attached: each
        monitor's desktops follow the monitor (its device path), never the name. Returns the states
        of monitors no longer attached."""
        paths_ = self._monitor_paths
        old = self._monitors
        by_path = {st.path: st for st in old.values() if st.path}
        new: dict[str, MonitorState] = {}
        taken: set[int] = set()
        for dev in devices:
            p = paths_.get(dev, "")
            st = by_path.get(p) if p else None
            if st is None:
                same = old.get(dev)  # (a monitor whose path can't be read: kept by its device name)
                if same is not None and (not same.path or not p) and id(same) not in taken:
                    st = same
            if st is None or id(st) in taken:
                continue
            taken.add(id(st))
            st.device = dev
            st.path = p or st.path
            new[dev] = st
        self._monitors = new
        return [st for st in old.values() if id(st) not in taken]

    def _use_setup(self, setup: tuple) -> None:
        """The display setup the last known places and shown desktops are read and recorded for."""
        self._setup = setup
        self._layout.use_setup(setup, self._monitor_ids)
        self._places.use_setup(setup, self._monitor_ids)
        self._last_shown.use_setup(setup)

    def _apply_setup(self) -> None:
        """Displays were attached or detached and have settled: the last known places of this
        display setup apply. Each display gets its desktops back and shows the desktop it showed
        last in this setup; every window recorded there goes back to its display, desktop and
        position. Nothing vanishes unasked: the window in use stays in view (on its recorded
        display), and windows with no place in this setup move along with their display's switch."""
        self._setup_due = False
        self._use_setup(setup_key(self._monitor_ids.values()))
        self._places_signature = None  # (recorded at once, in this setup)
        w = self._win
        names = self._monitor_ids
        final: dict[str, int] = {}
        for dev, st in self._monitors.items():
            st.pending_show = -1
            name = names.get(dev, "")
            for _ in range(min(self._places.desktop_count(name), MAX_DESKTOPS_PER_MONITOR) - len(st.desktops)):
                self._add_desktop(st)
            shown = self._last_shown.get(name)
            final[dev] = min(shown, len(st.desktops) - 1) if shown is not None and shown >= 0 else st.current
        managed = [h for st in self._monitors.values() for s in st.desktops for h in s]
        todo = [h for h in managed if w.is_window(h) and not w.is_hung(h)]
        pairs = self._places.assign_setup(todo) if todo else []
        fg = w.get_foreground()
        placed = []
        for h, e in pairs:
            dev = self._device_of_monitor_id(e.Display)
            st = self._monitors.get(dev) if dev is not None else None
            if st is None:
                continue
            if h == fg and min(max(e.Desktop, 0), MAX_DESKTOPS_PER_MONITOR - 1) != final[dev]:
                e = Entry(**{k: getattr(e, k) for k in Entry.__slots__})
                e.Desktop = final[dev]  # (the window in use: in view, on the desktop its display shows)
            placed.append((h, e, st))
        hiding = self._pass_hide_context()
        self._journal_pass(hiding, [h for h, e, st in placed if self._goes_hidden(e, st)])
        matched: set[int] = set()
        for h, e, st in placed:
            if self._place_at(h, e, st, hiding):
                matched.add(h)
                self._displaced.pop(h, None)
        for dev, st in list(self._monitors.items()):
            target = final.get(dev, st.current)
            if target == st.current or target >= len(st.desktops):
                continue
            cur, dst = st.desktops[st.current], st.desktops[target]
            for h in [h for h in cur if h not in matched and h not in self._hidden]:
                del cur[h]
                dst[h] = None  # (no place here: it stays in view, on the desktop shown now)
            if fg in dst:
                st.last_active[target] = fg
            self._switch_to_core(st, target, remember=False)
        if fg and fg != w.get_foreground() and w.is_window(fg) and fg not in self._hidden and w.is_window_visible(fg):
            w.set_foreground(fg)  # (the switches gave focus to their desktops' windows: it stays with the one in use)

    def _relaunch_apps(self) -> None:
        """Restore Applications On Restart: Windows restarted since places.json was written (never after
        a restart of DisplayDesktop alone), so each app that had windows in this display setup is
        started again, unless it already runs (Windows or a startup entry started it again): a
        packaged app (Store apps, packaged programs) by its app id, as the Start menu does, once per
        app; any other program with its own command line, once per process. Programs whose command
        line could not be read (elevated) are not. Their windows then go back to their places as
        they open (the startup grace). Never in test mode."""
        apps = self._places.apps_to_restart()
        if not apps or paths.is_test_mode():
            return
        from .layout import SHARED_HOSTS
        import subprocess
        w = self._win
        running = w.running_programs()
        running_apps = w.running_app_ids() if any(app for _, _, app in apps) else set()
        for exe, cmd, app in apps:
            name = app.split("!")[-1] if app else os.path.basename(exe)
            try:
                if app:
                    if app in running_apps:
                        continue
                    w.start_app(app)
                else:
                    if exe.lower() in running or os.path.basename(exe).lower() in SHARED_HOSTS \
                            or not os.path.isfile(exe):
                        continue
                    subprocess.Popen(cmd, cwd=os.path.dirname(exe), close_fds=True,
                                     creationflags=CREATE_NEW_CONSOLE)
                self.log(f"Restore Applications On Restart: started {name}.")
            except (OSError, ValueError) as exc:
                self.log(f"Restore Applications On Restart: {name} did not start ({exc}).")

    def _restore(self, windows: list[int], store: WindowLayout) -> None:
        """Puts windows back on their monitor, desktop and position, where the store (last known
        places at startup, saved desktops on "Load") has a place for them; the others are
        left to normal adoption (they stay where Windows created them). Placed quietly (no switch).
        One assign() over all windows: each app's windows take its places by count, first to first
        (layout.WindowLayout.assign)."""
        # Not responding: placing it would block (and it is not bound, so not half-restored)
        todo = [h for h in windows if not store.is_bound(h) and not self._win.is_hung(h)]
        if not todo:
            return  # (every grace sync without a new window: no matching over the saved places)
        hiding = self._pass_hide_context()
        pairs = list(store.assign(todo))
        self._journal_pass(hiding, [h for h, e in pairs
                                    if self._goes_hidden(e, self._monitors.get(self._device_of_monitor_id(e.Display)))])
        for h, entry in pairs:
            self._place_entry(h, entry, hiding)

    @staticmethod
    def _goes_hidden(entry: Entry, st: MonitorState | None) -> bool:
        """_place_at would hide the window: its desktop on that monitor is not the one shown."""
        return st is not None and min(max(entry.Desktop, 0), MAX_DESKTOPS_PER_MONITOR - 1) != st.current

    def _journal_pass(self, hiding: _Lazy, handles: list[int]) -> None:
        """The windows a pass is about to hide, put in the journal in one write (not one per window)."""
        if handles:
            self._journal_first(hiding.get(), handles)

    def _journal_first(self, ctx: _HideContext, handles) -> None:
        self._journal_in(ctx, {c for h in handles for c in (h, *ctx.floating.get(h, ()))})

    def _journal_in(self, ctx: _HideContext, ahead: set[int]) -> None:
        """Journal-ahead write for a hide context; its disk time is not taken from the preview budget."""
        t0 = time.perf_counter()
        w = self._win
        # only windows the hides will really hide (a listed window its own app hides later would be
        # shown by a recovery): visible and responding, or ours already
        listed = {c for c in ahead if c in self._hidden or (w.is_window_visible(c) and not w.is_hung(c))}
        if ctx.failed_writes >= 2:
            ctx.journaled |= listed  # (the journal can't be written now: no more waits in this operation)
        elif self._journal_ahead(listed):
            ctx.journaled |= listed  # (one left out now is journaled by _try_hide if it is hidden after all)
        else:
            ctx.failed_writes += 1  # (not on record: _try_hide tries once more before hiding)
        ctx.start += time.perf_counter() - t0

    def _pass_hide_context(self) -> _Lazy:
        """One hide context for a whole pass (restore, Load, displaced windows), made on the first
        hide: one scan for floating windows, and one CAPTURE_BUDGET_MS of preview captures for the
        pass, not for every window (40 windows put back on hidden desktops would otherwise each get
        a PrintWindow, all in one sync)."""
        return _Lazy(lambda: _HideContext(self._floating_window_map()))

    def _place_entry(self, h: int, entry: Entry, hiding: _Lazy) -> None:
        dev = self._device_of_monitor_id(entry.Display)
        st = self._monitors.get(dev) if dev is not None else None
        if st is None:
            self._displaced[h] = Displaced(entry)
            return
        self._place_at(h, entry, st, hiding)

    def _place_at(self, h: int, entry: Entry, st: MonitorState, hiding: _Lazy) -> bool:
        """Moves a window to the saved desktop and position on the given monitor. Returns False
        (and does nothing) for a window that is not responding. `hiding`: the pass's hide context
        (_pass_hide_context)."""
        w = self._win
        if w.is_hung(h):
            return False
        index = min(max(entry.Desktop, 0), MAX_DESKTOPS_PER_MONITOR - 1)
        while len(st.desktops) <= index:
            self._add_desktop(st)
        before = w.monitor_device_of_window(h)
        moving = before is not None and before != st.device
        # (the pass's floating-window scan only when it changes monitor: its palettes go with it)
        floating = list(hiding.get().floating.get(h, ())) if moving and w.is_window_visible(h) else []
        self._unregister(h)
        self._show_later.pop(h, None)  # (a state an earlier placement left for it no longer applies)
        st.desktops[index][h] = None
        if index != st.current:
            self._try_hide(h, hiding.get())  # hidden first, then moved hidden: never shown on the way
        WindowLayout.apply(h, entry, st.device, w)
        if moving:
            # its floating windows (palettes; hidden with it if its desktop is not shown) go too
            for c in dict.fromkeys(floating + list(self._floating_hidden.get(h, ()))):
                if w.monitor_device_of_window(c) == before:
                    self._reposition_one(c, before, st.device)
        if not w.is_window_visible(h):
            # Moved hidden (only its restored position): its saved state is applied when it is shown,
            # by a placement, so a maximized window maximizes on the monitor it was placed on
            self._show_later[h] = SHOW_LATER.get(entry.ShowCmd, N.SW_SHOWNOACTIVATE)
            if w.is_zoomed(h):
                self._frame_onto(h, st.device)  # (its maximized frame stays where it was otherwise)
        if index == st.current:
            self._show_if_ours(h)
        return True

    def _unregister(self, h: int) -> str | None:
        """Removes a window from every desktop of every monitor. Returns the device it was on."""
        device = None
        for st in self._monitors.values():
            for s in st.desktops:
                if s.pop(h, 0) is None:  # present (values are None)
                    device = st.device
        return device

    def _handle_displaced(self) -> None:
        """Puts displaced windows back once their monitor is attached again. While it is not, a
        displaced window that the user moves (desktop, monitor or position) is released."""
        if not self._displaced:
            return  # (every sync: nothing to do, nothing to make)
        w = self._win
        hiding = self._pass_hide_context()
        self._journal_pass(hiding, [h for h, d in self._displaced.items()
                                    if w.is_window(h) and not w.is_hung(h) and self._goes_hidden(
            d.entry, self._monitors.get(self._device_of_monitor_id(d.entry.Display)))])
        for h, d in list(self._displaced.items()):
            if not w.is_window(h):
                self._displaced.pop(h, None)  # closed while waiting: free its entry
                self._layout.unbind(h)
                continue
            dev = self._device_of_monitor_id(d.entry.Display)
            origin = self._monitors.get(dev) if dev is not None else None
            if origin is not None:
                if self._place_at(h, d.entry, origin, hiding):
                    self._displaced.pop(h, None)  # otherwise (not responding) retry on the next sync
                continue
            at = self._locate(h)
            wp = w.get_placement(h) if at is not None else None
            if at is None or wp is None:
                continue
            r = wp.rcNormalPosition.tup()
            if not d.settled:
                d.settled, d.device, d.desktop, d.rect = True, at[0].device, at[1], r
                continue
            if at[0].device != d.device or at[1] != d.desktop or r != d.rect:
                self._displaced.pop(h, None)  # moved by the user: not moved back later

    def _remember_shown(self, st: MonitorState) -> None:
        """Remembers the desktop the monitor shows now (showing.json), so DisplayDesktop goes back
        to it next time it starts. Only for changes the user makes, never for the desktop that
        startup or "Load" brings back. Not after end(), nor while displays settle (the setup it
        belongs to is not known yet)."""
        if self._ended or self._setup_due:
            return
        self._last_shown.set(self._monitor_ids.get(st.device, ""), st.current)

    def _show_saved_desktops(self) -> None:
        """A monitor whose desktop to show (the last one shown at startup, the saved one after
        "Load") is not showing yet switches to it as soon as that desktop has windows
        again, but only within the grace time."""
        now = self._win.tick_count64()
        for st in list(self._monitors.values()):
            if st.pending_show < 0:
                continue
            target = st.pending_show
            if now > st.pending_until:
                st.pending_show = -1
            elif target < len(st.desktops) and st.desktops[target]:
                st.pending_show = -1
                if st.current != target:
                    self._switch_to_core(st, target, remember=False)

    def save_desktops(self) -> int:
        """Tray "Save": saves where every window is now (monitor, desktop, position) and
        which desktop each monitor shows. Returns the number of windows saved (-1: not written;
        layout.NOT_READ_YET: the save there can't be read right now)."""
        if self._ended:
            return 0
        self.sync()
        w = self._win
        windows: list[Place] = []
        showing: dict[str, int] = {}
        for st in self._monitors.values():
            mid = self._monitor_ids.get(st.device, "")
            if not mid:
                continue
            showing[mid] = st.current
            for i, s in enumerate(st.desktops):
                for h in s:
                    if w.is_window(h):
                        windows.append(Place(h, mid, st.device, i))
        count = self._layout.save(windows, showing)
        if count >= 0:
            for st in self._monitors.values():
                if self._monitor_ids.get(st.device):
                    st.pending_show = -1  # what is shown now is what the user chose to save
        return count

    # ------------------------------------------------------------ persistence and recovery

    def _matches(self, rec: dict) -> bool:
        """A journal record is still the window we hid: same handle, invisible, same process (pid and,
        when recorded, its start time: a pid can be used again after a reboot, a start time can't)
        and same class."""
        w = self._win
        h = rec["Handle"]
        if not w.is_window(h) or w.is_window_visible(h):
            return False
        pid = w.get_pid(h)
        if pid != rec["Pid"] or w.get_class(h) != rec["Class"]:
            return False
        started = rec.get("Started")
        if not (is_int(started) and started):
            return True  # not recorded (older file, or unreadable then)
        now = w.process_started(pid)
        return not now or now == started  # unreadable now (e.g. another elevation): pid + class decide

    def _recover_previous_session(self) -> None:
        """Shows windows a previous run hid and could not restore (crash or kill), from every journal
        it may have left (`_journals`). The clock is not used to tell boots apart here: a clock set
        in the meantime must never leave windows hidden. The uptime rules out most other boots, and
        each window must still be the same process (pid + start time) and class."""
        self.recovery_problem = None  # (only what these journals say: an earlier problem was told)
        for path in self._journals:
            self._recover_journal(path)

    def _recover_later_due(self) -> None:
        """Journals that could not be opened at start (held, a cloud file not available yet, their
        folder offline) are read again now and then (every sync, at most every READ_RETRY_S)."""
        for path in list(self._recover_later):
            if paths.retry_due(path):
                self._recover_later.remove(path)
                before = self.recovery_problem
                self.recovery_problem = None
                self._recover_journal(path)
                if self.recovery_problem:
                    self.late_problem = True  # (the start's check is long done: told from the tray's sync)
                self.recovery_problem = self.recovery_problem or before

    def _recover_journal(self, path: str) -> None:
        """One journal: its windows shown again, then it is deleted (also a damaged one). One that
        can't be read now (held, a cloud file not available yet, its folder offline) is kept for
        `_recover_later_due`: never called damaged, never deleted."""
        w = self._win
        own = False
        read = False
        try:
            folder = os.path.dirname(path)
            if paths.offline(folder):  # (checked first: an offline share is remembered, never probed again)
                if not self._ended:
                    # its folder offline (a move's old folder, an unplugged drive): read later. Not
                    # marked failed (that would make a returning folder look empty to a move);
                    # paths.offline remembers the drive, so the retries don't block
                    self._read_later(path, offline=True)
                return
            if not os.path.exists(path):
                datafolder.forget_left_journal(path)  # (a move's left journal: done)
                return  # (none, or its folder is just gone: nothing to read)
            try:
                journal = paths.read_json(path)  # (crypto.Locked, an OSError: read later, once the key is in)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError) or self._ended:
                    return
                if datafolder.same(path, self._state_file):
                    path = self._move_aside(path)  # (where ours is written: ours must not replace it)
                self._read_later(path)
                return
            except (ValueError, RecursionError) as exc:
                # (can't be read at all, a newer version's too: never a Quit / Recreate Files choice,
                # whose Quit would leave its windows hidden; told in Recovery, Restore Hidden Windows)
                self.recovery_problem = "damaged"  # the user is told (and can restore by hand); deleted below
                self.journal_problems[path] = exc
                journal = None
            read = True  # (read, or found damaged and told: it may be deleted below)
            if self.journal_problems.get(path) == "unread":
                del self.journal_problems[path]  # (read at last)
            windows = journal.get("Windows") if isinstance(journal, dict) else None
            if not isinstance(windows, list):
                self.recovery_problem = "damaged"  # (not an object with a list of windows)
                self.journal_problems.setdefault(path, "notJournal")
            elif journal.get("Writer") == self._writer:
                windows = None  # this run's own (out of date) journal, read after a move: not a crash
                own = True
            else:
                left = datafolder.ended_run(journal.get("Writer"))
                if left is not None:
                    # A run that ended (left in a folder offline then): only the windows it could
                    # not show at its end, never the others (their apps may have hidden them since)
                    windows = [r for r in windows if isinstance(r, dict) and r.get("Handle") in left]
            if isinstance(windows, list) and same_boot(journal.get("UptimeMs"), None, w):
                for rec in windows:
                    try:
                        if rec.get("Handle") in self._hidden:
                            continue  # hidden by this run (a journal read after a move)
                        if rec.get("Handle") in self._seen:
                            continue  # (read late: this run saw it shown since; hidden now = by its app)
                        if self._matches(rec):
                            w.show_window_safe(int(rec["Handle"]), N.SW_SHOWNA)
                    except Exception:  # noqa: BLE001 - a bad record (wrong types: ctypes refuses
                        self.recovery_problem = "damaged"  # them) must not stop the others
                        self.journal_problems.setdefault(path, "badRecord")
                        continue
        except Exception:  # noqa: BLE001 - recovery is best-effort; it must not block startup
            if not read:
                # (something unexpected before it was read: never deleted unread, read again later)
                if not self._ended:
                    self._read_later(path)
                return
        if own and (datafolder.same(path, self._state_file) or self._is_alt(path)):
            return  # ours, in use: replaced by the next journal write, never deleted first (it may fail)
        # Read: deleted (also a damaged journal, and a copy of ours set aside). One that can't be
        # deleted (held) is emptied, or at least erased at exit: a later start must not read it again
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            if not self._empty_journal(path):
                if path not in self._given_up:
                    self._given_up.append(path)
                return
        datafolder.forget_left_journal(path)  # (read and gone: a later start must not look for it)

    def _read_later(self, path: str, offline: bool = False) -> None:
        """Read again later. After UNREAD_TELL failed reads of a journal that is there the user is
        told once (late_problem: Recovery, with Restore Hidden Windows), the
        reads go on. One whose folder is offline is tried every 2 × PAUSE_GONE_S (after OFFLINE_TRIES
        every 10 × PAUSE_GONE_S, except for the folder in use and the default folder), never given up,
        without telling (it may not even exist: its folder's own problem, e.g. the chosen folder
        unreachable, is told already)."""
        tries = self._offline_tries if offline else self._unread_tries
        tries[path] = tries.get(path, 0) + 1
        if not offline and tries[path] == UNREAD_TELL:
            self.late_problem = True
            self.journal_problems[path] = "unread"
        if offline:
            folder = os.path.dirname(path)
            ours = datafolder.same(folder, os.path.dirname(self._state_file)) or \
                datafolder.same(folder, datafolder.default_folder())
            # (not right when the offline memory ends; after OFFLINE_TRIES only every 10 minutes, but
            # never given up: a drive or share that comes back later is still read)
            later = 2 if tries[path] <= OFFLINE_TRIES or ours else 10
            paths.retry_after(path, later * paths.PAUSE_GONE_S)
        if not self._unread(path):
            self._recover_later.append(path)

    def _unread(self, path: str) -> str | None:
        """The entry of `_recover_later` for this path, however it is spelled (case, separators)."""
        return next((p for p in self._recover_later if datafolder.same(p, path)), None)

    @staticmethod
    def _move_aside(path: str) -> str:
        """A crashed run's journal that can't be read now, where ours is written: renamed to a name
        of its own (read later, by this run or the next start: datafolder.journals). Returns where
        it is now (the same path if it can't be moved: held)."""
        aside = f"{path}.{os.urandom(4).hex()}{LATER_SUFFIX}"  # (never over an older one set aside)
        try:
            os.replace(path, aside)
            return aside
        except OSError:
            return path

    def _clear_way(self) -> bool:
        """Before our journal is written or deleted: a crashed run's journal still unread where ours
        goes is moved aside first. False while it can't be (ours must then wait: never over it)."""
        entry = self._unread(self._state_file)
        if entry is None:
            return True
        folder = os.path.dirname(entry)
        if paths.paused_as_gone(folder) or paths.offline(folder):
            return False  # (no disk access on an offline share: it would block every journal write)
        if not os.path.exists(entry):
            self._recover_later.remove(entry)  # (gone meanwhile: nothing left to protect)
            return True
        aside = self._move_aside(entry)
        if aside == entry:
            return False
        self._recover_later[self._recover_later.index(entry)] = aside
        return True

    def _persist_hidden(self) -> None:
        """Called after every change: writes the journal of hidden windows and records the last
        known places. Not after end(): the last snapshot stays as it was taken."""
        if self._ended:
            return
        self._write_journal()
        self._record_places()

    def _record_places(self) -> None:
        """places.json: recorded at once when a window opened, closed or moved to another desktop
        or monitor, and every REFRESH_MS (2 s) otherwise, so moved or resized windows are current
        too (the file is only written when something changed). During the startup grace time,
        places of the last session that no window took yet are kept. Nothing while displays settle
        (attached or detached: Windows moves windows meanwhile; each setup keeps its own record)."""
        if self._setup_due:
            self._places.heartbeat()
            return
        ids, displaced = self._monitor_ids, self._displaced
        rows = []  # (monitor id, desktop, window) of every window recorded (every sync: no Place objects)
        add = rows.append
        devices = []
        counts = {}
        for st in self._monitors.values():
            mid = ids.get(st.device)
            if not mid:
                continue
            counts[mid] = len(st.desktops)
            dev = st.device
            for i, s in enumerate(st.desktops):
                for h in s:  # (displaced ones too: this setup's place; their own setup keeps theirs)
                    add((mid, i, h))
                    devices.append(dev)
        signature = (tuple(rows), tuple(sorted(displaced)), tuple(sorted(counts.items())))
        grace = self._in_startup_grace
        if not grace and not self._places_grace_over:
            self._places_grace_over = True
            self._places_signature = None  # drop the places nobody took: record once more
        tick = self._win.tick_count64()
        if signature != self._places_signature or tick - self._places_at >= REFRESH_MS:
            self._places_signature = signature
            self._places_at = tick
            is_window = self._win.is_window
            self._places.record([Place(h, mid, dev, i) for (mid, i, h), dev in zip(rows, devices) if is_window(h)],
                                keep_pending=grace, counts=counts)
        else:
            self._places.heartbeat()

    def _write_journal(self) -> None:
        if self._persisted == set(self._hidden):
            if not self._hidden and self._alt_journal is not None:
                self._drop_alt_journal()  # (ours under its own name, out of date: deleted when it can be)
            if self._fallback_journal is not None:
                if not self._hidden:
                    self._drop_fallback_journal()
                elif not paths.paused(os.path.dirname(self._state_file)) and paths.write_due(self._state_file):
                    self._write_records(self._hidden)  # (back where it belongs: the fallback then goes)
            if self._stale_journals:
                # (ours matches what is hidden, nothing hidden included: the old ones go; paused
                # folders are skipped inside)
                self._drop_stale_journal()
            return  # nothing changed since the last write
        try:
            if not self._hidden:
                # Nothing hidden: no journal at all. Done only once it is really gone: a stale journal
                # left behind could later show a window its own app hid. A paused (offline) folder is
                # tried again on a later sync, without blocking this one.
                folder = os.path.dirname(self._state_file)
                had_fallback = self._fallback_journal is not None
                self._drop_fallback_journal()  # (local: never blocks; it must not list shown windows)
                if had_fallback:
                    # what was on record (the fallback) is gone: the next hide must write again,
                    # whatever `_persisted` held (until the state file below is really gone too)
                    self._persisted = {0}
                if paths.paused(folder) or paths.offline(folder):
                    return  # (never a delete waiting on an offline share: tried on a later sync)
                if not self._clear_way():
                    # ours is under its own name (the state file is a crashed run's): what is on disk
                    # for us is that file only; its delete is retried until it works
                    self._drop_alt_journal()
                    self._persisted.clear()
                    return
                try:
                    os.remove(self._state_file)
                except FileNotFoundError:
                    if paths.offline(folder):  # (an offline share says "not found" too: not gone)
                        paths.fail(folder, gone=True)
                        return
                except OSError as exc:
                    # Held by another program (a sync client): tried again next sync, nothing paused
                    # (a pause would also stop the next journal). A folder that is gone pauses it; a
                    # journal refused ("access denied", not a sharing violation) for HELD_REFUSED_S
                    # (read-only) is told to the user, and only it is affected (still tried each sync)
                    if paths.offline(folder):
                        paths.fail(folder, gone=True)
                    elif getattr(exc, "winerror", None) != paths.ERROR_ACCESS_DENIED:
                        paths.held_ended(self._state_file)  # (held by another program: not a refusal)
                    elif paths.held_again(self._state_file):
                        paths.report(folder, file=self._state_file)
                    return
                paths.held_ended(self._state_file)
                self._drop_alt_journal()  # (if it can't be yet: retried by the early return above)
                self._drop_fallback_journal()
                self._persisted.clear()  # (the state file is gone: nothing of ours on record there)
                self._drop_stale_journal()
                return
            if self._write_records(self._hidden):
                self._persisted = set(self._hidden)
                self._drop_stale_journal()
        except OSError:
            pass

    def _drop_stale_journal(self, at_exit: bool = False) -> None:
        """Our journals in folders we moved away from, kept there while the new folder had none of
        ours: deleted once ours is in the new folder, or at exit (left, one would be read as a
        crashed run's). One in a paused folder is tried later (never blocks on it); at exit only an
        offline one is left, and one that can't be deleted is emptied."""
        keep = []
        for stale in self._stale_journals:
            if datafolder.same(stale, self._state_file) or self._is_alt(stale) or \
                    (self._fallback_journal is not None and datafolder.same(stale, self._fallback_journal)):
                continue  # (moved back there, or our fallback there now: that is ours)
            if self._unread(stale):
                continue  # (a crashed run's journal, not read yet: never deleted as ours)
            folder = os.path.dirname(stale)
            if not at_exit and (paths.paused(folder) or paths.known_offline(folder)
                                or time.monotonic() < self._stale_next.get(stale, 0.0)):
                keep.append(stale)  # (tried later; the registry is not touched every sync meanwhile)
                continue
            if at_exit:
                if self._hidden:
                    # Windows left hidden: kept as is (it may be their only record; EndedRuns makes a
                    # start show only those from it), and tried again by a later call
                    keep.append(stale)
                    continue
                # Forgotten once erased. One left (offline, refused) stays registered only when end()
                # will record this run as ended (EndedRuns: a start skips it; end() forgets it if that
                # fails); otherwise forgotten anyway (a start must not show from it what apps hid since)
                if self._reachable(folder) and self._erase_journal(stale):
                    datafolder.forget_left_journal(stale)
                elif self._ending and self._fallback_allowed:
                    self._kept_registered.append(stale)
                else:
                    datafolder.forget_left_journal(stale)
                continue
            # Ours is written in the new folder: the next start must not read this one, deleted or not
            datafolder.forget_left_journal(stale)
            try:
                os.remove(stale)
                continue
            except FileNotFoundError:
                if not paths.offline(folder):
                    continue  # gone
                # (offline: "not found" too, but not gone: a failed try like any other)
            except OSError:
                pass
            # Paused (quietly: the app no longer uses it): no new try blocks on it for a while,
            # an offline share included; given up after STALE_TRIES. "Gone" only when it is
            # (a file held for a moment must still be erased at exit)
            if not datafolder.same(folder, os.path.dirname(self._state_file)) and \
                    not datafolder.same(folder, datafolder.default_folder()):
                # (never the folder in use, nor the default folder: the fallback journal goes there)
                paths.note_failure(folder, gone=paths.offline(folder))
            # the next try after a while, by time (not the next sync, 600 ms later), folder paused or not
            self._stale_next[stale] = time.monotonic() + paths.PAUSE_REFUSED_S
            self._stale_tries[stale] = self._stale_tries.get(stale, 0) + 1
            if self._stale_tries[stale] < STALE_TRIES:
                keep.append(stale)
            else:
                self._given_up.append(stale)  # (no more tries while running; erased at exit)
        self._stale_journals = keep
        if at_exit and not self._hidden:  # (else they may hold windows left hidden)
            for stale in self._given_up:
                if self._in_use(stale):
                    continue  # (ours now: never erased as given up)
                if not self._unread(stale) and self._reachable(os.path.dirname(stale)):
                    if self._erase_journal(stale):
                        datafolder.forget_left_journal(stale)  # (a later start no longer looks for it)
            self._given_up = []

    def _journal_data(self, handles) -> dict:
        w = self._win
        records = []
        for h in handles:
            if w.is_window(h):
                pid = w.get_pid(h)
                records.append({"Handle": h, "Pid": pid, "Class": w.get_class(h), "Started": w.process_started(pid)})
        return {"UptimeMs": w.tick_count64(), "Writer": self._writer, "Windows": records}

    def _write_records(self, handles, at_exit: bool = False) -> bool:
        """Writes our journal. While a crashed run's unread journal holds our path and can't be moved
        aside, ours goes to a name of its own that a start also reads (`<JOURNAL>.<writer>.later.json`):
        windows hidden meanwhile are never without a record. `at_exit`: a folder that may be offline
        is not touched (exit must not wait for the network timeout): the default folder instead."""
        data = self._journal_data(handles)
        if at_exit and not self._reachable(os.path.dirname(self._state_file)):
            return self._write_fallback(data)
        if not self._clear_way():
            alt = f"{self._state_file}.{self._writer}{LATER_SUFFIX}"
            if write_json(alt, data):
                if self._alt_journal is not None and not datafolder.same(self._alt_journal, alt):
                    self._retire_alt_journal()  # (an earlier one elsewhere: never left behind)
                self._alt_journal = alt
                self._drop_fallback_journal()
                return True
            return self._write_fallback(data)
        if write_json(self._state_file, data):
            self._drop_alt_journal()
            self._drop_fallback_journal()
            return True
        return self._write_fallback(data)

    def _write_fallback(self, data) -> bool:
        """Our journal could not be written in the folder in use (offline, paused, the file held or
        refused): it goes to the default folder instead, which every start reads too (never over a
        crashed run's unread journal there). Deleted once ours is written in the folder in use
        again. Only for the real data folder (not tests)."""
        folder = os.path.dirname(self._state_file)
        if not self._fallback_allowed:
            return False
        default = datafolder.default_folder()
        if datafolder.same(default, folder) or not os.path.isdir(default):
            return False  # (never makes a default folder a move removed: it would be left behind)
        path = os.path.join(default, JOURNAL_FILE)
        if self._unread(path):
            return False
        if write_json(path, data):
            self._fallback_journal = path
            return True
        return False

    def _drop_fallback_journal(self) -> None:
        path = self._fallback_journal
        if path is None:
            return
        if datafolder.same(path, self._state_file):
            self._fallback_journal = None  # (moved to the default folder: that file is ours now, kept)
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            return  # tried again after the next journal write
        self._fallback_journal = None

    def _in_use(self, path: str) -> bool:
        """One of the journals this run writes now (ours, its alt, the fallback)."""
        return datafolder.same(path, self._state_file) or self._is_alt(path) or \
            (self._fallback_journal is not None and datafolder.same(path, self._fallback_journal))

    def _is_alt(self, path: str) -> bool:
        return self._alt_journal is not None and datafolder.same(path, self._alt_journal)

    def _retire_alt_journal(self) -> None:
        """Ours under its own name in a folder we move away from: handled like our old journal there
        (listed for the next start until ours is written in the new folder, then deleted)."""
        alt, self._alt_journal = self._alt_journal, None
        if alt is None:
            return
        datafolder.add_left_journal(alt)
        # (a folder that failed is not touched while running: erased at exit if it is back by then)
        if not datafolder.left_working:
            if alt not in self._given_up:
                self._given_up.append(alt)
        elif not any(datafolder.same(alt, s) for s in self._stale_journals):
            self._stale_journals.append(alt)

    def _drop_alt_journal(self) -> None:
        """Ours written under a name of its own (_write_records): deleted once it is not needed."""
        alt = self._alt_journal
        if alt is None:
            return
        folder = os.path.dirname(alt)
        if paths.paused(folder) or paths.offline(folder):
            return  # (never blocks on an offline share: tried again later)
        try:
            os.remove(alt)
        except FileNotFoundError:
            if paths.offline(folder):
                return  # (offline: "not found" too, but not gone; tried again next time)
        except OSError:
            return  # tried again next time
        self._alt_journal = None

    def _journal_ahead(self, extra) -> bool:
        """The journal, written BEFORE `extra` windows are hidden: the ones hidden now plus those. A
        window listed but never hidden is harmless (recovery shows only windows that are invisible
        and still the same process). The real list is written after the operation (_persist_hidden)."""
        if self._ended:
            return True
        want = set(self._hidden) | set(extra)
        if want <= self._persisted:
            return True
        try:
            if self._write_records(want):
                self._persisted = want  # (stale journals: never here, mid-switch; see _drop_stale_journal)
                return True
        except OSError:
            pass
        return False

    # ------------------------------------------------------------ hide / show (the only paths)

    def _collect_previews(self) -> None:
        """Installs the background previews finished so far, except those of windows we hid after
        their capture began (half drawn)."""
        if not self.previews:  # (one taken while they were turned off: dropped)
            self.snapshots.collect(lambda h, started: False)
            return
        hidden, hidden_at, is_window = self._hidden, self._hidden_at, self._win.is_window
        self.snapshots.collect(lambda h, started: is_window(h) and h not in hidden
                               and hidden_at.get(h, -1.0) < started)

    def set_previews(self, on: bool) -> None:
        """Disable Overview previews (off): no window is captured any more (none in the background,
        none in a switch) and the previews taken are freed at once; the Overview shows hidden windows
        as icons (visible ones keep their live thumbnails). On: they are taken again."""
        on = bool(on)
        if on == self.previews:
            return
        self.previews = on
        if not on:
            self.snapshots.clear()  # (the worker's list emptied: it goes idle)
            self._used_at.clear()

    def _refresh_previews(self, is_window) -> None:
        """Keeps the previews of the shown desktops' windows current in the background, so a switch
        never waits on a capture: a window without one, one in use since its preview (at most every
        PREVIEW_FRESH_S), and any preview older than PREVIEW_MAX_AGE_S. Nothing while they are off."""
        self._collect_previews()
        for d in (self._used_at, self._hidden_at):
            for h in [h for h in d if not is_window(h)]:
                del d[h]
        if not self.previews:
            return
        now = time.monotonic()
        fg = self._win.get_foreground()
        if fg:
            self._used_at[fg] = now
        shots, wanted = self.snapshots, []
        for st in self._monitors.values():
            for h in st.desktops[st.current]:
                if h in self._hidden:
                    continue
                shot = shots.get(h)
                if shot is None:
                    if not shots.failed_recently(h, PREVIEW_RETRY_S):
                        wanted.append(h)
                elif now - shot.at >= PREVIEW_MAX_AGE_S or \
                        (self._used_at.get(h, 0.0) > shot.at and now - shot.at >= PREVIEW_FRESH_S):
                    wanted.append(h)
        shots.refresh(wanted)

    def _try_hide(self, h: int, ctx: _HideContext) -> bool:
        """Hides a window and records it as hidden by us. Hung windows are skipped (they stay visible
        and sync moves them to the current desktop). Its floating windows are hidden with it. Never
        after end(), not even by an operation that was already running when the app ended."""
        w = self._win
        if self._ended or not w.is_window(h) or not w.is_window_visible(h) or w.is_hung(h):
            return False
        if h not in ctx.journaled:
            # In the journal BEFORE it is hidden: capturing and hiding wait on the window, and if its
            # app hangs there and DisplayDesktop is killed, every window hidden so far is on record
            self._journal_in(ctx, {h, *ctx.floating.get(h, ())})
        # Last chance to capture a preview for the overview: hidden windows can't be captured. Only a
        # window without one waits here; the others' were taken in the background (_refresh_previews)
        if self.previews and ctx.elapsed_ms() < CAPTURE_BUDGET_MS and self.snapshots.get(h) is None \
                and not self.snapshots.failed_recently(h, PREVIEW_RETRY_S):
            self.snapshots.capture(h)
        if not w.show_window(h, N.SW_HIDE):
            return False
        self._hidden[h] = None
        self._hidden_at[h] = time.perf_counter()

        children = ctx.floating.get(h)
        if children:
            hidden_children = []
            for c in children:
                if w.is_window_visible(c) and not w.is_hung(c) and w.show_window(c, N.SW_HIDE):
                    self._hidden[c] = None
                    hidden_children.append(c)
            if hidden_children:
                self._floating_hidden[h] = hidden_children
        return True

    def _show_if_ours(self, h: int) -> None:
        """Shows a window only if we hid it, with the floating windows we hid with it. Windows an app
        hid itself (e.g. "close to tray") are left alone."""
        w = self._win
        if h not in self._hidden:
            return
        entry = self._hidden.pop(h)
        later = self._show_later.pop(h, None)  # (a state it was placed in while hidden)
        if w.is_window(h) and not w.is_window_visible(h):
            hung = w.is_hung(h)  # (decided once: a hung window's show is async, it comes later)
            if later is not None and not hung:
                try:
                    with w.in_window_dpi_context(h):
                        wp = w.get_placement(h)
                        if wp is not None:
                            wp.showCmd = later  # shown by the placement: in that state, where it was placed
                            w.show_by_placement(h, wp)
                except Exception:  # noqa: BLE001 - the plain show below still brings it back
                    pass
            if not w.is_window_visible(h):
                w.show_window_safe(h, N.SW_SHOWNA)  # (no saved state, or the placement failed)
            if not hung and w.is_window(h) and not w.is_window_visible(h):
                # The show did not take (refused; not a hung window, whose async show comes later):
                # still ours, so never taken for a window its own app hid. Kept in the journal; tried
                # again (the syncs while it should be on screen, exit's restore_all). Its floating
                # windows stay hidden with it
                self._hidden[h] = entry
                if later is not None:
                    self._show_later[h] = later
                self._refused.setdefault(h, [w.tick_count64()] * 2)  # (retried by the syncs)
                return
        children = self._floating_hidden.pop(h, None)
        if children:
            for c in children:
                if c in self._hidden:
                    del self._hidden[c]
                    if w.is_window(c) and not w.is_window_visible(c):
                        w.show_window_safe(c, N.SW_SHOWNA)
                        if w.is_window(c) and not w.is_window_visible(c) and not w.is_hung(c):
                            self._hidden[c] = None  # (its show did not take: still ours, retried by the syncs)
                            self._refused.setdefault(c, [w.tick_count64()] * 2)
                            self._refused_main[c] = h  # (retried only while its main window is shown)

    def _retry_refused_shows(self) -> None:
        """Windows we hid whose show did not take (`_refused`, set by _show_if_ours) and that should be
        on screen (on a monitor's shown desktop, or on no desktop: their monitor gone): shown again,
        never activating, at every sync for REFUSED_SHOW_MS, then every REFUSED_RETRY_MS, never given
        up: they stay ours and in the journal (exit shows them), and Restore Hidden Windows lists them
        once overdue. Nothing to do (no set built) while none is refused."""
        if not self._refused:
            return
        monitors = self._monitors.values()
        on = {h for st in monitors for h in st.desktops[st.current]}
        placed = {h for st in monitors for s in st.desktops for h in s}
        children = {c for cs in self._floating_hidden.values() for c in cs}
        now = self._win.tick_count64()
        for h in list(self._refused):
            main = self._refused_main.get(h)
            if main is not None and h in self._hidden and main in self._hidden:
                # (a floating window whose main window is hidden again: it goes with it, never on its own)
                if h not in self._floating_hidden.setdefault(main, []):
                    self._floating_hidden[main].append(h)
                children.add(h)
            if h in self._hidden and self._win.is_window_visible(h) and h not in children:
                del self._hidden[h]  # (on screen already: its app or the user showed it)
                self._show_later.pop(h, None)
            if h not in self._hidden or not self._win.is_window(h) or h in children \
                    or not (h in on or h not in placed):
                del self._refused[h]  # (shown, closed, with its main window, or hidden on purpose again)
                self._refused_main.pop(h, None)
                continue
            first, due = self._refused[h]
            if now < due:
                continue
            later = self._show_later.get(h)
            if later in ACTIVATING_SHOWS and later != N.SW_SHOWMAXIMIZED:
                del self._show_later[h]  # (a retry never activates: never steals focus)
            focused = self._win.get_foreground() if later == N.SW_SHOWMAXIMIZED else 0
            self._show_if_ours(h)
            if focused and self._win.get_foreground() != focused:
                self._win.set_foreground(focused)  # (maximized again, the focus given back: never stolen)
            if h not in self._hidden:
                self._refused.pop(h, None)  # (shown at last)
                self._refused_main.pop(h, None)
            elif h in self._refused:
                self._refused[h][1] = now + (0 if now - first < REFUSED_SHOW_MS else REFUSED_RETRY_MS)

    def _refused_overdue(self) -> set[int]:
        """Refused windows tried for REFUSED_SHOW_MS that should still be on screen (on a monitor's
        shown desktop or on no desktop): Restore Hidden Windows lists them."""
        if not self._refused:
            return set()
        now = self._win.tick_count64()
        monitors = self._monitors.values()
        on = {h for st in monitors for h in st.desktops[st.current]}
        placed = {h for st in monitors for s in st.desktops for h in s}
        return {h for h, (first, _) in self._refused.items()
                if now - first >= REFUSED_SHOW_MS and (h in on or h not in placed)}

    def _new_hide_context(self) -> _HideContext:
        return _HideContext(self._floating_window_map())

    @staticmethod
    def _order_for_capture(s: dict[int, None], focused: int, top_level: list[int]) -> list[int]:
        """The focused window first, then the rest from top to bottom of the Z-order, so the capture
        budget goes to the most visible windows."""
        first = [focused] if focused in s else []
        return list(dict.fromkeys(first + [h for h in top_level if h in s] + list(s)))

    def _floating_window_map(self, top_level: list[int] | None = None) -> dict[int, list[int]]:
        """Maps each main window to the visible floating windows above it that are not managed on their
        own (palettes, tool windows, untitled popups). Transient UI (tooltips, menus) is skipped,
        as is anything without a caption, resizable frame or title."""
        w = self._win
        vis, eligible = w.is_window_visible, self._eligible_shown
        result: dict[int, list[int]] = {}
        for c in filter(vis, w.enum_windows() if top_level is None else top_level):
            if eligible(c):
                continue
            main = w.get_root_anchor(c)
            if not main or main == c:
                continue
            style = w.get_style(c)
            framed = (style & N.WS_CAPTION) == N.WS_CAPTION or (style & N.WS_THICKFRAME) != 0
            if not framed and w.title_length(c) == 0:
                continue
            if w.get_class(c) in TRANSIENT_CLASSES:
                continue
            result.setdefault(main, []).append(c)
        return result

    # ------------------------------------------------------------ eligibility

    def _is_eligible(self, h: int) -> bool:
        return bool(self._win.is_window_visible(h)) and self._eligible_shown(h)

    def _eligible_shown(self, h: int) -> bool:
        """_is_eligible for a window already found visible (loops over every window check that first,
        inline: most top-level windows are invisible). Cheapest and most often failing checks first
        (visible tool windows are the most common); the root last (an enumerated top-level window is
        its own root: it almost never fails)."""
        w = self._win
        ex = w.get_exstyle(h)
        if ex & 0x80 and not ex & 0x40000:  # WS_EX_TOOLWINDOW, not WS_EX_APPWINDOW
            return False
        if w.get_pid(h) == self._own_pid:
            return False
        if w.title_length(h) == 0:
            return False
        if w.is_cloaked(h):
            return False
        return w.get_class(h) not in CLASS_BLACKLIST and w.get_root(h) == h

    # ------------------------------------------------------------ numbering

    def _ordered_monitors(self) -> list[MonitorState]:
        """Monitors sorted by screen layout (left to right, then top to bottom)."""
        bounds = {s.device: s.bounds for s in self._win.screens()}
        big = 2 ** 31 - 1
        return sorted(self._monitors.values(),
                      key=lambda m: (bounds[m.device][0] if m.device in bounds else big,
                                     bounds[m.device][1] if m.device in bounds else 0))

    def _build_info(self, st: MonitorState) -> SwitchInfo:
        ordered = self._ordered_monitors()
        idx = ordered.index(st)
        base = sum(len(m.desktops) for m in ordered[:idx])
        return SwitchInfo(st.device, idx + 1, st.current, len(st.desktops), base + st.current + 1)

    # ------------------------------------------------------------ structure

    @staticmethod
    def _add_desktop(st: MonitorState) -> None:
        st.desktops.append({})
        st.last_active.append(0)

    # ------------------------------------------------------------ sync

    def sync(self) -> None:
        """Syncs state with reality: adopt new windows, drop closed ones, move windows that changed
        monitors, recover hidden windows from monitors that disappeared."""
        if self._ended:
            return
        w = self._win
        screens = w.screens()
        current_devices = [s.device for s in screens]
        if not current_devices:
            # No display at all is only ever transient (display reconfiguration, session switch).
            # Treating it as "every monitor unplugged" would merge away all desktops; wait instead.
            return
        # Reading a monitor's stable id is the slowest part of a sync (~0.5 ms per display), and
        # it only changes with the display setup: read again when that changes, and every 30 s in
        # case a monitor was swapped for another without any visible change.
        key = tuple((s.device, s.bounds, s.hmon) for s in screens)
        tick = w.tick_count64()
        gone_states: list[MonitorState] = []
        if key != self._screens_key or tick - self._ids_read_at >= MONITOR_ID_REFRESH_MS:
            self._screens_key, self._ids_read_at = key, tick
            self._monitor_paths = {d: w.get_monitor_id(d) for d in current_devices}
            self._monitor_ids = display_names(screens, self._monitor_paths, w.display_name)
            gone_states = self._follow_monitors(current_devices)
            setup = setup_key(self._monitor_ids.values())
            if self._setup is None:
                self._use_setup(setup)  # (the first sync: the setup this run starts with)
            elif setup != self._setup:
                self._setup_due, self._setup_changed_at = True, tick
        first = not self._monitors and not gone_states

        for dev in current_devices:
            if dev not in self._monitors:
                # New monitor. At start: as many desktops as it had (empty ones included), and the
                # desktop it showed last time. Attached later: _apply_setup does that for the new
                # setup once the displays settle (never a late switch hiding windows moved onto it)
                st = MonitorState(dev)
                st.path = self._monitor_paths.get(dev, "")
                if first:
                    name = self._monitor_ids.get(dev, "")
                    for _ in range(min(self._places.desktop_count(name), MAX_DESKTOPS_PER_MONITOR) - 1):
                        self._add_desktop(st)
                    shown = self._last_shown.get(name)
                    if shown is not None and shown > 0:
                        st.pending_show = min(shown, MAX_DESKTOPS_PER_MONITOR - 1)
                        st.pending_until = w.tick_count64() + STARTUP_GRACE_MS
                else:
                    self._setup_due, self._setup_changed_at = True, tick
                self._monitors[dev] = st

        gone_states += [self._monitors.pop(d) for d in [d for d in self._monitors if d not in current_devices]]
        for gone in gone_states:
            # Display unplugged: every window that was on it (on any of its desktops) is shown and
            # moved onto the main display, where adoption below puts it on the current desktop
            self._setup_due, self._setup_changed_at = True, tick
            for s in gone.desktops:
                for h in list(s):
                    # Keep track of where it belongs, so it can go back when the monitor returns
                    entry = self._layout.bound_entry(h) or self._places.bound_entry(h)
                    if entry is not None and h not in self._displaced:
                        self._displaced[h] = Displaced(entry)
                    floating = list(self._floating_hidden.get(h, ()))  # (shown with it)
                    self._show_if_ours(h)
                    self._move_to_main_display(h, gone.work, screens)
                    for c in floating:  # its palettes go with it
                        self._move_to_main_display(c, gone.work, screens)
        if self._ids_read_at == tick:
            # Save / Load: the displays attached now (after the unplugged ones' windows took their
            # saved places along as displaced)
            self._layout.use_setup(setup_key(self._monitor_ids.values()), self._monitor_ids)
        for scr in screens:
            if scr.device in self._monitors:
                self._monitors[scr.device].work = scr.work

        # Drop closed windows, and windows that are invisible although we did not hide them
        # (the app hid them itself, e.g. "close to tray"); they must not be re-shown later.
        is_window, vis, hidden = w.is_window, w.is_window_visible, self._hidden
        for st in self._monitors.values():
            for s in st.desktops:
                for h in list(s):
                    if is_window(h) and (h in hidden or vis(h)):
                        continue
                    del s[h]
                    self._hidden.pop(h, None)
                    self._show_later.pop(h, None)
                    self._layout.unbind(h)  # if it opens again, it is matched to its saved place
                    self._displaced.pop(h, None)

        # Windows seen for the first time. Right after DisplayDesktop starts (grace time) they go
        # back to their last known place: open since before, or reopened after a crash or restart.
        # Later, a window opens where Windows creates it. Each app's windows take its places by
        # count (first to first); one with no place left stays where Windows opened it.
        top_level = w.enum_windows()
        if self._places.take_late_read() and self._in_startup_grace:
            # places.json could not be read at start and is read now: what a start does with it is
            # done now (the monitors' desktops back, the windows adopted meanwhile to their places)
            for dev, st in self._monitors.items():
                want = min(self._places.desktop_count(self._monitor_ids.get(dev, "")), MAX_DESKTOPS_PER_MONITOR)
                while len(st.desktops) < want:
                    self._add_desktop(st)
            adopted = [h for m in self._monitors.values() for s in m.desktops for h in s
                       if h not in self._hidden and h not in self._displaced]
            for h in adopted:
                self._places.unbind(h)  # (bound to this run's records of where they were adopted)
            self._restore(adopted, self._places)
        if self._last_shown.take_late_read() and self._in_startup_grace:
            # showing.json read late: each monitor goes back to the desktop it showed, as at a start
            for dev, st in self._monitors.items():
                shown = self._last_shown.get(self._monitor_ids.get(dev, ""))
                if st.pending_show < 0 and st.current == 0 and shown is not None and shown > 0:
                    st.pending_show = min(shown, MAX_DESKTOPS_PER_MONITOR - 1)
                    st.pending_until = w.tick_count64() + STARTUP_GRACE_MS
        eligible = self._eligible_shown
        if self._in_startup_grace:
            managed = {h for m in self._monitors.values() for s in m.desktops for h in s}
            bound, displaced = self._places.is_bound, self._displaced
            fresh = [h for h in filter(vis, top_level) if h not in managed and h not in displaced
                     and not bound(h) and eligible(h)]
            self._restore(fresh, self._places)

        self._handle_displaced()

        # (visibility read again here: the restore above may have hidden windows)
        device_of, monitors = w.monitor_device_of_window, self._monitors
        # A window already on its monitor's current desktop stays as it is whether it is eligible or
        # not: only its monitor is asked (no eligibility reads for the windows in view; no map built).
        # The current sets are bound once per sync (filled in place below: the same dicts)
        shown = tuple((st.desktops[st.current], st) for st in monitors.values())
        for h in filter(vis, top_level):  # (filter: the visibility test per window without a bytecode loop)
            for cur, st in shown:
                if h in cur:
                    break
            else:
                st = None
            if st is not None:
                dev = device_of(h)
                if dev == st.device or not eligible(h):
                    continue
            elif not eligible(h):
                continue
            else:
                dev = device_of(h)
            st = monitors.get(dev) if dev is not None else None
            if st is None:
                continue
            if h not in st.desktops[st.current]:
                # Registered elsewhere? Remove it there (it changed monitors, or its app showed it
                # again while it was hidden)
                self._unregister(h)
                st.desktops[st.current][h] = None
                self._hidden.pop(h, None)

        # Forget floating windows that were closed while hidden, and previews of closed windows
        for h in [h for h in self._hidden if not is_window(h)]:
            del self._hidden[h]
        self.snapshots.purge(w.is_window)
        self._refresh_previews(is_window)
        for main in list(self._floating_hidden):
            children = [c for c in self._floating_hidden[main] if c in self._hidden]
            self._floating_hidden[main] = children
            if not children or main not in self._hidden:
                # Main window no longer hidden by us (closed, or re-shown by its app): release its children
                del self._floating_hidden[main]
                for c in children:
                    self._show_if_ours(c)
        # A state kept for when we show a window means nothing once it is not ours to show (its app
        # showed it, it closed, it was restored by hand)
        for h in [h for h in self._show_later if h not in self._hidden]:
            del self._show_later[h]

        if self._setup_due and w.tick_count64() - self._setup_changed_at >= SETUP_SETTLE_MS:
            self._apply_setup()
        if not self._relaunched and w.tick_count64() - self._start_tick >= RELAUNCH_AFTER_MS:
            self._relaunched = True  # (once, early in the startup grace: their windows go back to their places)
            if self.restore_apps:
                self._relaunch_apps()
        self._retry_refused_shows()
        self._show_saved_desktops()
        # Windows new on a desktop get their spawn number oldest first (reverse z-order) before a record
        # numbers them in desktop order (layout.order_of: an app's windows go back by count)
        seen_before = self._seen
        new = {h for st in self._monitors.values() for s in st.desktops for h in s if h not in seen_before}
        if new:
            number_new([h for h in reversed(top_level) if h in new], w)
        self._persist_hidden()
        # Writes that could not be made (the folder was paused or briefly unwritable) are made now
        self._layout.flush()
        self._last_shown.flush()
        seen = self._seen
        for st in self._monitors.values():
            for s in st.desktops:
                seen.update(s)
        if len(seen) > self._seen_limit:  # (closed windows dropped now and then: it never only grows)
            self._seen = {h for h in self._seen if self._win.is_window(h)}
            self._seen_limit = max(SEEN_PRUNE_AT, 2 * len(self._seen))
        if self._recover_later:
            self._recover_later_due()  # (crash journals that could not be read at start)

    def get_layout(self) -> list[MonitorEntry]:
        """Full layout for the overview (with global numbers)."""
        self.sync()
        w = self._win
        result = []
        number = 0
        for ordinal, st in enumerate(self._ordered_monitors(), start=1):
            desktops = []
            for i, s in enumerate(st.desktops):
                number += 1
                windows = []
                for h in s:
                    if w.is_window(h):
                        t = w.get_title(h)
                        windows.append(WindowEntry(h, t if t else w.get_class(h)))
                desktops.append(DesktopEntry(i, number, i == st.current, windows))
            result.append(MonitorEntry(st.device, ordinal, desktops))
        return result

    # ------------------------------------------------------------ switching

    def switch_relative(self, delta: int) -> None:
        """Next/previous desktop on the monitor under the mouse cursor. Never creates a desktop (that
        is the overview's "+"): past the first or last desktop nothing changes and edge_reached
        fires."""
        dev = self._win.monitor_device_under_cursor()
        if dev is None or self._ended:
            return
        self.sync()
        st = self._monitors.get(dev)
        if st is None:
            return
        target = st.current + delta
        if not 0 <= target < len(st.desktops):
            self._fire_edge(dev, 1 if delta > 0 else -1)
            return
        self._switch_to_core(st, target)

    def switch_to(self, device: str, local_index: int) -> None:
        if self._ended:
            return
        self.sync()
        st = self._monitors.get(device)
        if st is not None and 0 <= local_index < len(st.desktops):
            self._switch_to_core(st, local_index)

    def create_desktop_and_switch(self, device: str) -> None:
        if self._ended:
            return
        self.sync()
        st = self._monitors.get(device)
        if st is None or len(st.desktops) >= MAX_DESKTOPS_PER_MONITOR:
            return
        self._add_desktop(st)
        self._switch_to_core(st, len(st.desktops) - 1)

    def _switch_to_core(self, st: MonitorState, target: int, remember: bool = True) -> None:
        """remember=False: the switch brings back a desktop (startup, "Load"), it is not
        the user's choice, so the last shown desktop stays as it was."""
        w = self._win
        st.pending_show = -1  # a switch (by the user or ours) replaces the saved desktop to show
        if st.current == target:
            if remember:
                self._remember_shown(st)
            self._fire_switched(self._build_info(st))
            return

        self.snapshots.hold(SLIDE_HOLD_S)
        self._collect_previews()  # (taken in the background: the hides below need not wait on any)
        self._fire_starting(st.device, st.current, target)
        if self._ended:
            return  # the app ended during the event (an error in the animation): nothing is hidden

        st.last_active[st.current] = w.get_foreground()
        top_level = w.enum_windows()  # one scan for both
        ctx = _HideContext(self._floating_window_map(top_level))
        order = self._order_for_capture(st.desktops[st.current], st.last_active[st.current], top_level)
        self._journal_first(ctx, order)  # the whole desktop on record in one write, before any is hidden
        for h in order:
            self._try_hide(h, ctx)

        st.current = target
        s = st.desktops[target]
        for h in list(s):
            if not w.is_window(h):
                del s[h]
                self._hidden.pop(h, None)
                continue
            self._show_if_ours(h)

        # Give focus to the most recently active window on the target desktop
        focus = st.last_active[target]
        if not w.is_window(focus) or focus not in s or focus in self._hidden:
            # (never one still hidden: its show did not take)
            focus = next((h for h in s if w.is_window(h) and not w.is_iconic(h) and h not in self._hidden), 0)
        if focus:
            w.set_foreground(focus)

        self._persist_hidden()
        if remember:
            self._remember_shown(st)
        self._fire_switched(self._build_info(st))

    def focus_window(self, h: int) -> None:
        """Goes to a window: switches to its desktop first if needed, restores it if minimized,
        then focuses it."""
        w = self._win
        if self._ended:
            return
        self.sync()
        if not w.is_window(h):
            return
        for st in self._monitors.values():
            for i, s in enumerate(st.desktops):
                if h in s:
                    if st.current != i:
                        st.last_active[i] = h  # give focus to this window after the switch
                        self._switch_to_core(st, i)
                    if h in self._hidden:
                        return  # (its show did not take: never focus an invisible window)
                    if w.is_iconic(h):
                        w.show_window_safe(h, N.SW_RESTORE)
                    w.set_foreground(h)
                    return
        w.set_foreground(h)

    # ------------------------------------------------------------ moving windows and desktops

    def move_window_to_desktop(self, h: int, dst_device: str, dst_local: int) -> None:
        """Moves a window to any desktop on any monitor (overview drag and drop / menu)."""
        w = self._win
        if self._ended:
            return
        self.sync()
        if not w.is_window(h):
            return
        dst = self._monitors.get(dst_device)
        if dst is None or not 0 <= dst_local < len(dst.desktops):
            return
        src_device = self._unregister(h)
        dst.desktops[dst_local][h] = None
        shown = dst.current == dst_local
        if not shown:
            # Hidden first (with its preview), then moved: it never shows up on the other monitor
            self._try_hide(h, self._new_hide_context())
        if src_device is not None and src_device != dst_device:
            self._reposition_window(h, src_device, dst_device)
        if shown:
            self._show_if_ours(h)
        self._persist_hidden()

    def move_desktop_to_monitor(self, src_device: str, src_local: int, dst_device: str) -> None:
        """Moves a desktop with all its windows to another monitor (appended at the end)."""
        if self._ended:
            return
        self.sync()
        if src_device == dst_device:
            return
        src, dst = self._monitors.get(src_device), self._monitors.get(dst_device)
        if src is None or dst is None:
            return
        if not 0 <= src_local < len(src.desktops) or len(dst.desktops) >= MAX_DESKTOPS_PER_MONITOR:
            return
        src.pending_show = dst.pending_show = -1  # desktop numbers change on both monitors
        moved = src.desktops.pop(src_local)
        last = src.last_active.pop(src_local)
        was_current = src.current == src_local
        if not src.desktops:
            self._add_desktop(src)
        if src.current > src_local:
            src.current -= 1
        if src.current >= len(src.desktops):
            src.current = len(src.desktops) - 1

        # The new desktop is not shown on the target monitor: hide its windows first (with their
        # previews), then move them there while hidden (a visible window would show up and take focus)
        ctx = self._new_hide_context()
        self._journal_first(ctx, [h for h in moved if self._win.is_window(h)])
        for h in list(moved):
            if not self._win.is_window(h):
                del moved[h]
                continue
            self._try_hide(h, ctx)
            self._reposition_window(h, src_device, dst_device)
        dst.desktops.append(moved)
        dst.last_active.append(last)

        # If the active desktop was moved off the source monitor, show the remaining active desktop
        if was_current:
            for h in list(src.desktops[src.current]):
                self._show_if_ours(h)
        self._persist_hidden()
        self._remember_shown(src)  # its shown desktop's number may have changed

    def swap_desktops(self, device: str, a: int, b: int) -> None:
        """Swaps two desktops of one monitor (overview: drag a card title onto another card), so
        their numbers change. Nothing is hidden or shown: the desktop on screen stays on screen,
        under its new number."""
        if self._ended:
            return
        self.sync()
        st = self._monitors.get(device)
        if st is None or a == b or not (0 <= a < len(st.desktops) and 0 <= b < len(st.desktops)):
            return
        st.desktops[a], st.desktops[b] = st.desktops[b], st.desktops[a]
        st.last_active[a], st.last_active[b] = st.last_active[b], st.last_active[a]
        if st.current in (a, b):
            st.current = b if st.current == a else a
        st.pending_show = -1  # desktop numbers changed
        self._persist_hidden()
        self._remember_shown(st)

    def delete_desktop(self, device: str, local: int) -> None:
        """Overview trash icon: removes a desktop (desktops are never removed by themselves). Its
        windows go to the desktop on its left (on its right for the first desktop), so no window
        is lost or left hidden. A monitor's only desktop cannot be deleted."""
        if self._ended:
            return
        self.sync()
        st = self._monitors.get(device)
        if st is None or len(st.desktops) < 2 or not 0 <= local < len(st.desktops):
            return
        into = local - 1 if local > 0 else local + 1
        moved = st.desktops[local]
        was_current = st.current == local
        st.desktops[into].update(moved)  # appended after the windows already there
        if not self._win.is_window(st.last_active[into]):
            st.last_active[into] = st.last_active[local]
        del st.desktops[local]
        del st.last_active[local]
        if into > local:
            into -= 1
        if was_current:
            # The desktop on screen goes away: the one its windows joined is shown instead
            st.current = into
            for h in list(st.desktops[into]):
                self._show_if_ours(h)
        else:
            if st.current > local:
                st.current -= 1
            if st.current == into:
                for h in list(moved):
                    self._show_if_ours(h)
            # otherwise they stay hidden: they joined a desktop that is not on screen either
        st.pending_show = -1  # desktop numbers changed
        self._persist_hidden()
        self._remember_shown(st)

    def _move_to_main_display(self, h: int, from_work, screens) -> None:
        """A window of an unplugged display: onto the main display, at the same relative position
        and size where it fits, in the same state (normal, maximized, minimized). Done through the
        window placement, so a minimized window also comes back on the main display. Windows that
        Windows itself already moved somewhere are taken from where they are now. Not responding:
        left alone (changing its placement would block)."""
        w = self._win
        main_px = N.pick_screen(screens)  # (this thread's per-monitor coordinates, like from_work)
        if main_px is None or not w.is_window(h) or w.is_hung(h):
            return
        with w.in_window_dpi_context(h):
            # The placement is in the window's own coordinates (a DPI-unaware window sees scaled
            # ones), so the displays are read again here, in the same coordinates
            local = w.screens()
            main = next((s for s in local if s.device == main_px.device), None) or N.pick_screen(local)
            wp = w.get_placement(h)
            if main is None or wp is None:
                return
            dx, dy = self._workspace_shift(h, local)  # placement -> screen coordinates (and back below)
            l, t, r, b = self._shift(wp.rcNormalPosition.tup(), dx, dy)
            cx, cy = (l + r) // 2, (t + b) // 2
            ml, mt, mr, mb = main.work
            if ml <= cx < mr and mt <= cy < mb:
                return  # already on the main display
            src = next((s.work for s in local
                        if s.bounds[0] <= cx < s.bounds[2] and s.bounds[1] <= cy < s.bounds[3]), None)
            if src is None and from_work and from_work[2] > from_work[0] and from_work[3] > from_work[1]:
                src = self._to_window_units(from_work, main_px.bounds, main.bounds)  # the gone display
            sl, st_, sr, sb = src if src and src[2] > src[0] and src[3] > src[1] else (l, t, r, b)
            width, height = min(r - l, mr - ml), min(b - t, mb - mt)
            rel_x = (l - sl) / (sr - sl) if sr > sl else 0.0
            rel_y = (t - st_) / (sb - st_) if sb > st_ else 0.0
            x = min(max(ml + int(round(rel_x * (mr - ml))), ml), mr - width)
            y = min(max(mt + int(round(rel_y * (mb - mt))), mt), mb - height)
            wp.flags &= N.WPF_RESTORETOMAXIMIZED  # (minimized from maximized: restores maximized)
            wp.rcNormalPosition = N.RECT(*self._shift((x, y, x + width, y + height), -dx, -dy))
            hidden = not w.is_window_visible(h)
            frame = w.get_window_rect(h) if hidden and w.is_zoomed(h) else None
            if hidden:
                wp.showCmd = N.SW_HIDE  # its app hid it (close to tray): moved, never shown
            elif wp.showCmd == N.SW_SHOWMINIMIZED:
                wp.showCmd = N.SW_SHOWMINNOACTIVE  # stays minimized, without taking focus
            elif wp.showCmd != N.SW_SHOWMAXIMIZED:
                wp.showCmd = N.SW_SHOWNOACTIVATE
            w.set_placement(h, wp)
            if frame is not None:  # (a hidden placement moves only the restored position)
                # its overhang is measured against the display it was on; unknown: none (frame = work area)
                real_src = src if src and src[2] > src[0] and src[3] > src[1] else frame
                self._move_maximized_frame(h, frame, real_src, main.work)

    @staticmethod
    def _to_window_units(rect, main_px, main_local):
        """A rectangle in this thread's per-monitor coordinates, in a window's own coordinates,
        given the main display's bounds in both (Windows scales a DPI-unaware or system-aware
        window's view of the whole desktop by one factor; for per-monitor-aware windows the two
        are the same and this changes nothing)."""
        sx = (main_local[2] - main_local[0]) / (main_px[2] - main_px[0]) if main_px[2] > main_px[0] else 1.0
        sy = (main_local[3] - main_local[1]) / (main_px[3] - main_px[1]) if main_px[3] > main_px[1] else 1.0

        def x(v):
            return main_local[0] + int(round((v - main_px[0]) * sx))

        def y(v):
            return main_local[1] + int(round((v - main_px[1]) * sy))

        return x(rect[0]), y(rect[1]), x(rect[2]), y(rect[3])

    def _workspace_shift(self, h: int, screens) -> tuple[int, int]:
        """A window's placement (Get/SetWindowPlacement) is in workspace coordinates unless it is a
        tool window: screen coordinates measured from the top-left of the main display's work area,
        which is not the screen origin when something is docked at the main display's top or left.
        Returns what to add to a placement for screen coordinates (`screens` read in the same DPI
        context as the placement)."""
        if self._win.get_exstyle(h) & N.WS_EX_TOOLWINDOW:
            return 0, 0
        main = next((s for s in screens if s.primary), None)
        return (main.work[0], main.work[1]) if main is not None else (0, 0)

    @staticmethod
    def _shift(rect, dx: int, dy: int) -> tuple[int, int, int, int]:
        return rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy

    @staticmethod
    def _fit(rect, src_work, dst_work) -> tuple[int, int, int, int]:
        """`rect` moved from one work area to another: same relative position, its size kept where
        it fits, inside the target."""
        sl, stp, sr, sb = src_work
        dl, dt, dr, db = dst_work
        sw_, sh_, dw_, dh_ = sr - sl, sb - stp, dr - dl, db - dt
        width = min(rect[2] - rect[0], dw_)
        height = min(rect[3] - rect[1], dh_)
        rel_x = (rect[0] - sl) / sw_ if sw_ > 0 else 0.0
        rel_y = (rect[1] - stp) / sh_ if sh_ > 0 else 0.0
        x = min(max(dl + int(rel_x * dw_), dl), max(dl, dr - width))
        y = min(max(dt + int(rel_y * dh_), dt), max(dt, db - height))
        return x, y, x + width, y + height

    def _reposition_window(self, h: int, src_device: str, dst_device: str) -> None:
        """Moves a window and its floating windows (palettes, Find dialogs: hidden and shown with it)
        to the target monitor (_reposition_one each)."""
        floating = list(self._floating_hidden.get(h, ()))
        if self._win.is_window_visible(h):
            floating += self._floating_window_map().get(h, [])
        self._reposition_one(h, src_device, dst_device)
        for c in dict.fromkeys(floating):
            if self._win.monitor_device_of_window(c) == src_device:
                self._reposition_one(c, src_device, dst_device)

    def _reposition_one(self, h: int, src_device: str, dst_device: str) -> None:
        """Moves a window to the target monitor: same relative position, size kept where it fits,
        same state (normal, maximized, minimized), visible or hidden as it is.

        Done through the window placement, in the window's own DPI coordinates (work areas read in
        the same context), because
        - a minimized window's frame is a placeholder (-32000, -32000): only its restored position
          (rcNormalPosition) says where it goes when restored;
        - a hidden window must stay hidden: show commands (restore, maximize) would show and
          activate a window of a desktop that is not on screen. A placement with SW_HIDE moves it
          without showing it; a maximized one keeps its maximized state, and its maximized frame is
          moved onto the target too (same border insets), so it shows maximized there later.
        Not responding: only its frame is moved, asynchronously (a placement would wait on it)."""
        w = self._win
        if not w.is_window(h):
            return
        if w.is_hung(h):
            scr = {s.device: s for s in w.screens()}
            src, dst, r = scr.get(src_device), scr.get(dst_device), w.get_window_rect(h)
            if src is not None and dst is not None and r is not None:
                x, y, x2, y2 = self._fit(r, src.work, dst.work)
                w.set_window_pos(h, x, y, x2 - x, y2 - y,
                                 N.SWP_NOZORDER | N.SWP_NOACTIVATE | N.SWP_ASYNCWINDOWPOS)
            return
        with w.in_window_dpi_context(h):
            scr = {s.device: s for s in w.screens()}
            src, dst = scr.get(src_device), scr.get(dst_device)
            wp = w.get_placement(h)
            if src is None or dst is None or wp is None:
                return
            visible, zoomed, iconic = w.is_window_visible(h), w.is_zoomed(h), w.is_iconic(h)
            frame = w.get_window_rect(h) if zoomed and not visible else None
            dx, dy = self._workspace_shift(h, scr.values())  # the work areas are screen coordinates
            fitted = self._fit(self._shift(wp.rcNormalPosition.tup(), dx, dy), src.work, dst.work)
            wp.rcNormalPosition = N.RECT(*self._shift(fitted, -dx, -dy))
            wp.flags &= N.WPF_RESTORETOMAXIMIZED  # (minimized from maximized: restores maximized)
            if not visible:
                wp.showCmd = N.SW_HIDE
            elif iconic:
                wp.showCmd = N.SW_SHOWMINNOACTIVE
            elif zoomed:
                wp.showCmd = N.SW_SHOWMAXIMIZED  # maximizes again on the monitor of its new position
            else:
                wp.showCmd = N.SW_SHOWNOACTIVATE
            w.set_placement(h, wp)
            if frame is not None:
                self._move_maximized_frame(h, frame, src.work, dst.work)

    def _frame_onto(self, h: int, device: str) -> None:
        """A hidden maximized window's frame onto the given monitor, if it is on another one."""
        w = self._win
        with w.in_window_dpi_context(h):
            scr = {s.device: s for s in w.screens()}
            frame = w.get_window_rect(h)
            dst = scr.get(device)
            if frame is None or dst is None:
                return
            cx, cy = (frame[0] + frame[2]) // 2, (frame[1] + frame[3]) // 2
            src = next((s for s in scr.values()
                        if s.bounds[0] <= cx < s.bounds[2] and s.bounds[1] <= cy < s.bounds[3]), None)
            if src is not None and src.device != device:
                self._move_maximized_frame(h, frame, src.work, dst.work)

    def _move_maximized_frame(self, h: int, frame, src_work, dst_work) -> None:
        """A hidden maximized window's frame onto another work area (a placement with SW_HIDE moves
        only its restored position): it then shows maximized there. The frame overhangs the work
        area by its (invisible) borders: that overhang is kept."""
        sl, stp, sr, sb = src_work
        dl, dt, dr, db = dst_work
        li, ti, ri, bi = (min(max(v, 0), 64) for v in
                          (sl - frame[0], stp - frame[1], frame[2] - sr, frame[3] - sb))
        self._win.set_window_pos(h, dl - li, dt - ti, (dr + ri) - (dl - li), (db + bi) - (dt - ti),
                                 N.SWP_NOZORDER | N.SWP_NOACTIVATE)

    # ------------------------------------------------------------ tray actions

    def clear_desktops(self) -> None:
        """Tray "Clear Desktops": shows every window we hid, merges each monitor's desktops into one,
        and forgets the saved desktops."""
        if self._ended:
            return
        children = {c for m, cs in self._floating_hidden.items() if m in self._hidden for c in cs}
        for h in [h for h in self._hidden if h not in children]:  # (floating windows come with a hidden main one)
            self._show_if_ours(h)
        # (one whose show did not take stays ours, its floating windows with it: kept in the journal,
        # shown again by the next syncs or exit's restore_all)
        self._floating_hidden = {m: cs for m, cs in self._floating_hidden.items() if m in self._hidden}
        self._displaced.clear()
        for st in self._monitors.values():
            merged: dict[int, None] = {}
            for s in st.desktops:
                merged.update(s)
            st.desktops = [merged]
            st.last_active = [0]
            st.current = 0
            st.pending_show = -1
            self._remember_shown(st)
        self._layout.clear()
        self.sync()  # also removes the crash journal

    def load_desktops(self) -> int:
        """Tray "Load": re-applies the saved desktops now. Nothing is forgotten, and the
        last shown desktops (showing.json) are not changed. Returns 0, or (nothing done) the
        layout.WindowLayout.read_problem() code while the save can't be read."""
        if self._ended:
            return 0
        problem = self._layout.read_problem()
        if problem:
            return problem
        self.sync()
        self._layout.unbind_all()  # reconsider every saved window, wherever it sits now
        self._displaced.clear()
        now = self._win.tick_count64()
        for st in self._monitors.values():
            st.pending_show = -1
            mid = self._monitor_ids.get(st.device)
            shown = self._layout.showing_desktop(mid) if mid else None
            # The desktop shown when saved, the first one included: windows put back there must be
            # in view again, not left behind on whatever desktop the display shows now
            if shown is not None and shown != st.current:
                st.pending_show = min(shown, MAX_DESKTOPS_PER_MONITOR - 1)
                st.pending_until = now + STARTUP_GRACE_MS
        # Every open window: the ones on a desktop (shown or hidden by us) and visible eligible ones.
        # Not every window in _hidden: floating windows (palettes, tool windows) hidden together with
        # their main window are there too, and must not take a saved main window's place.
        managed = [h for st in self._monitors.values() for s in st.desktops for h in s]
        on_desktop = set(managed)
        self._restore([h for h in dict.fromkeys(managed + self._win.enum_windows())
                       if self._win.is_window(h) and (h in on_desktop or self._is_eligible(h))],
                      self._layout)
        self.sync()  # honors pending_show, persists
        return 0

    def snapshot(self, defer: bool = False) -> None:
        """One last record of every window's monitor, desktop and position, and of every desktop,
        taken when DisplayDesktop ends, before the hidden windows are shown: the next start puts
        all of it back. After end() it does nothing: the windows are restored by then, and
        recording them would overwrite the real last snapshot. `defer` (exit): only taken now,
        written by write_snapshot once every window is shown (a dead network folder can't hold them
        up)."""
        if self._ended:
            return
        self._places_signature = None
        self._places_at = -10**12
        self._places.write_next()  # (written even if the same as last time: the file may be gone)
        if defer:
            self._places.defer_writes()
        self._record_places()

    def write_snapshot(self) -> None:
        """Writes the snapshot taken with defer=True (after end(): the windows are shown by then)."""
        self._places.write_deferred()

    def relocate(self, directory: str) -> bool:
        """The data folder moved (Settings > Settings Folder; datafolder.move_to has put the files
        there): every file is read and written there from now on. The saved desktops are taken from
        that folder when it has them (the user chose its data, or they were just copied there);
        everything else this run knows (the journal of hidden windows, places, the shown desktops)
        is written there now: the old folder may not have been readable."""
        old = self._state_file
        self._state_file = os.path.join(directory, JOURNAL_FILE)
        moved = not datafolder.same(old, self._state_file)
        if moved:
            self.journal_problems.clear()  # (told already; Recovery reports the new folder's own)
        # Crashed runs' journals still unread in the folder left: never deleted (by us or move_to:
        # relocate then returns False), and read by the next start there if not by this run
        unread_there = [p for p in self._recover_later
                        if moved and datafolder.same(os.path.dirname(p), os.path.dirname(old))]
        for p in unread_there:
            datafolder.add_left_journal(p)
        if moved and not self._unread(old) and datafolder.left_working and \
                not any(datafolder.same(old, s) for s in self._stale_journals):
            # deleted once ours is written in the new folder (a failed old folder is never touched)
            self._stale_journals.append(old)
        elif moved and not self._unread(old) and not datafolder.left_working and old not in self._given_up:
            # a failed old folder: not touched while running, but ours there is erased at exit if the
            # folder is back by then (a later run moving back there must not read it as a crash's)
            self._given_up.append(old)
        # (moved back to a folder left earlier: ours there is ours again, never erased as given up/stale)
        self._given_up = [p for p in self._given_up if not datafolder.same(p, self._state_file)]
        if moved and self._alt_journal is not None:
            self._retire_alt_journal()  # (ours under its own name there: read by the next start until replaced)
        self._layout.relocate(directory)
        self._places.relocate(directory)
        self._last_shown.relocate(directory)
        # that folder's journals: crashed runs' set aside, ours, and the older name
        self._journals = datafolder.folder_journals(directory)
        if not self._ended:
            # A journal a crashed run left in that folder (moves never copy journals over it): its
            # windows are shown first, before ours is written there
            self._recover_previous_session()
        # Forces one journal write there (or, with nothing hidden, the removal of one we left there)
        self._persisted = {0}
        self._places_signature = None
        self._places.write_next()
        if not self._ended:
            self._layout.adopt_file(datafolder.kept_new)  # its saved desktops, or ours where it has none
            self._persist_hidden()
            self._last_shown.write_now()
        # our journal is there (or nothing is hidden), and the old one may be deleted by move_to
        # (and not while ours is only in the default folder's fallback: that may be the file move_to
        # would delete as the old one)
        return self._persisted == set(self._hidden) and not self._unread(old) and self._fallback_journal is None

    # ------------------------------------------------------------ hidden windows, by hand

    def hidden_windows(self) -> list[int]:
        """Hidden windows the user may want back (Restore Hidden Windows): top-level, titled, not
        DisplayDesktop's own, not cloaked, not the shell's, and not on one of our desktops (those
        come back with their desktop). Windows hidden by a run that crashed with an unreadable
        journal are among them, but so are windows that apps keep hidden: the user chooses."""
        ours = {h for st in self._monitors.values() for s in st.desktops for h in s} | set(self._hidden)
        return hidden_windows(self._win, ours - self._refused_overdue(), self._own_pid)

    def restore_windows(self, windows: list[int]) -> int:
        """Shows the chosen hidden windows and puts them on desktop 1 of their monitor (that desktop
        is shown, so they are on screen). Returns how many were shown (closed or not responding
        ones are skipped)."""
        if self._ended:
            return 0
        # (Restore Hidden Windows used: the crash records it was for are dealt with, never reported again)
        self.journal_problems = {p: why for p, why in self.journal_problems.items() if why == "unread"}
        w = self._win
        shown_on: set[str] = set()
        shown = 0
        hiding = self._pass_hide_context()  # (one window scan for them all, made only if needed)
        for h in windows:
            if not w.is_window(h) or w.is_hung(h):
                continue
            if not w.is_window_visible(h):
                w.show_window_safe(h, N.SW_SHOWNA)
            if not w.is_window_visible(h) and not w.is_hung(h):
                continue  # (refused again: still ours, never counted as shown)
            shown += 1
            if h in self._refused:  # (shown at last, by hand: no longer ours to hide nor retry)
                self._hidden.pop(h, None)
                self._refused.pop(h, None)
                self._refused_main.pop(h, None)
                self._show_later.pop(h, None)
            dev = w.monitor_device_of_window(h)
            st = self._monitors.get(dev) if dev is not None else None
            if st is None or not self._is_eligible(h):
                continue  # shown, but not a window desktops manage (e.g. a tool window)
            self._unregister(h)
            self._hidden.pop(h, None)
            st.desktops[0][h] = None
            if st.current != 0:
                self._try_hide(h, hiding.get())  # (shown again by the switch below)
            shown_on.add(st.device)
        for dev in shown_on:
            st = self._monitors.get(dev)
            if st is not None and st.current != 0:
                self._switch_to_core(st, 0)
        self._persist_hidden()
        return shown

    def reread(self) -> None:
        """Files that could not be read are read again now (an encryption key typed only now: the
        caller clears the read schedule, so crash journals waiting are read at the next sync)."""
        if self._layout._unread:
            self._layout.retry_read()
        if self._places._unread:
            self._places.write_next()  # (its late read is tried at the next record, unthrottled)
        if self._last_shown._unread:
            self._last_shown._read()

    def end(self) -> None:
        """The app ends (exit, --quit, sign-out, crash, failed start): shows every window we hid
        (restore_all), then refuses every later change, so nothing that still runs before the
        process is gone (a timer, a shortcut key, an error message's message loop) can hide a window
        again or record the restored windows over the last snapshot."""
        self._ending = True
        try:
            self.restore_all()
        finally:
            self._ended = True
            recorded = False
            if self._fallback_allowed:
                # The run ended, with these of its windows still hidden (none, normally): a journal of
                # this run left anywhere (a folder offline now) only ever shows those later (the
                # others may be hidden by their own apps by then). Called again (the last-resort
                # exit), the entry is replaced with what is still hidden then
                recorded = datafolder.add_ended_run(self._writer, list(self._hidden))
            if not recorded:
                # (kept for EndedRuns to skip: not there after all)
                for stale in self._kept_registered:
                    datafolder.forget_left_journal(stale)
            self._kept_registered = []

    @property
    def ended(self) -> bool:
        return self._ended

    def restore_all(self) -> None:
        """Restores all windows we hid (exit, --quit, sign-out, crash). Windows that apps hid
        themselves stay hidden. Safe to call more than once."""
        for st in self._monitors.values():
            st.current = 0
        w = self._win
        ours = list(self._hidden)  # (floating windows included: at exit each is shown, even one whose
        # main window's show is refused; never left hidden)
        for h in ours:
            try:
                self._show_if_ours(h)
            except Exception:  # noqa: BLE001 - keep restoring the others
                self._hidden.pop(h, None)
                try:
                    w.show_window_safe(h, N.SW_SHOWNA)  # a plain show, at least
                except Exception:  # noqa: BLE001
                    pass
        # A show can fail without an error (ShowWindow refused): checked by looking
        not_shown = []
        for h in ours:
            try:
                if w.is_window(h) and not w.is_window_visible(h):
                    not_shown.append(h)
            except Exception:  # noqa: BLE001 - can't tell: kept on record
                not_shown.append(h)
        # The windows still hidden stay ours: on record, and tried again by a later call (the
        # last-resort exit runs end() again)
        self._hidden = {h: None for h in not_shown}
        self._floating_hidden.clear()
        self._show_later.clear()
        self._persisted.clear()
        if not_shown:
            # Windows left hidden: every journal of ours is kept (wherever one is, it lists them: they
            # were journaled before they were hidden). Ours is brought down to just those where it
            # can be (best effort; at exit never waiting on a share). At exit, end() records which of
            # this run's windows are still hidden (EndedRuns): a later start shows only those from
            # any journal of this run, never the shown ones (their apps may hide them since)
            try:
                if self._write_records(not_shown, at_exit=self._ending):
                    self._persisted = set(not_shown)
            except Exception:  # noqa: BLE001 - the journals as they are still list them
                pass
        else:
            # Every window is shown: our journal must not outlive the run (a start in the same boot
            # would show what apps hid since). Also while the folder is paused for a refused write;
            # never when it is offline (it would block), nor over a crashed run's journal unread there.
            if self._reachable(os.path.dirname(self._state_file)) and self._clear_way():
                self._erase_journal(self._state_file)
            if self._alt_journal is not None:
                if self._reachable(os.path.dirname(self._alt_journal)):
                    self._erase_journal(self._alt_journal)
                self._alt_journal = None
            if self._fallback_journal is not None:  # (ours in the default folder: a local folder)
                self._erase_journal(self._fallback_journal)
                self._fallback_journal = None
        self._drop_stale_journal(at_exit=True)  # (and ours left in folders moved away from)

    @staticmethod
    def _reachable(folder: str) -> bool:
        """At exit: worth touching. A folder paused as gone is known offline without any disk access
        (probing a dead share waits for the network timeout, and exit must be quick)."""
        return not paths.paused_as_gone(folder) and not paths.offline(folder)

    def _erase_journal(self, path: str) -> bool:
        """Deletes one of our journals at exit; one that can't be deleted is emptied instead. Returns
        whether it is gone or empty now."""
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            return self._empty_journal(path)
        return True

    def _empty_journal(self, path: str) -> bool:
        """A journal that can't be deleted, emptied atomically (temp file, then replace: a kill at
        shutdown never leaves half a file, which a start would call damaged); also while the folder
        is paused for a refusal. Returns whether it worked."""
        tmp = path + ".tmp"
        try:
            from . import crypto
            with open(tmp, "wb") as fh:
                fh.write(crypto.encode(json.dumps({"UptimeMs": self._win.tick_count64(), "Writer": self._writer,
                                                   "Windows": [], paths.FORMAT_KEY: paths.FORMAT})))
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    # ------------------------------------------------------------ test/diagnostic helpers

    @property
    def hidden(self) -> set[int]:
        return set(self._hidden)

    def desktops_of(self, device: str) -> list[list[int]]:
        st = self._monitors.get(device)
        return [list(s) for s in st.desktops] if st else []

    def current_of(self, device: str) -> int:
        st = self._monitors.get(device)
        return st.current if st else -1
