"""A simulated Windows for testing the core logic: windows, monitors, z-order, focus, a clock.
Implements the same functions DesktopManager and WindowLayout call on the native module."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass

from src import native as N


@dataclass
class FakeWindow:
    title: str
    cls: str = "AppWindow"
    pid: int = 1000
    exe: str = r"C:\Apps\app.exe"
    visible: bool = True
    rect: tuple = (100, 100, 900, 700)
    show_cmd: int = 1          # placement state: 1 normal, 2 minimized, 3 maximized
    exstyle: int = 0
    style: int = N.WS_CAPTION | N.WS_THICKFRAME
    anchor: int = 0
    hung: bool = False
    cloaked: bool = False
    child_of: int = 0          # nonzero: not a top-level window (GetAncestor(GA_ROOT) != self)
    frame: tuple | None = None  # the real frame while maximized (rect = the restored position)
    dpi_scale: float = 1.0     # how the window sees coordinates: < 1 = a DPI-unaware window on a scaled monitor
    flags: int = 0             # WINDOWPLACEMENT flags (WPF_RESTORETOMAXIMIZED)


@dataclass
class FakeScreen:
    device: str
    bounds: tuple
    work: tuple
    primary: bool = False
    hmon: int = 0


class FakeWin:
    WS_CAPTION = N.WS_CAPTION

    def __init__(self):
        self.windows: dict[int, FakeWindow] = {}
        self.z: list[int] = []
        self.screen_list: list[FakeScreen] = [
            FakeScreen(r"\\.\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True),
            FakeScreen(r"\\.\DISPLAY2", (1920, 0, 3840, 1080), (1920, 0, 3840, 1040)),
        ]
        self.monitor_ids = {r"\\.\DISPLAY1": "MON-A", r"\\.\DISPLAY2": "MON-B"}
        self.foreground = 0
        self.cursor_device = r"\\.\DISPLAY1"
        self.tick = 10_000_000
        self._next = 0x1000
        self.calls: list[tuple] = []
        self._scale = 1.0  # coordinate view of the current DPI context (in_window_dpi_context)

    # ---- coordinates: physical inside, scaled for a DPI-unaware window's context

    def _out(self, r):
        return r if self._scale == 1.0 else tuple(int(round(v * self._scale)) for v in r)

    def _in(self, r):
        return tuple(r) if self._scale == 1.0 else tuple(int(round(v / self._scale)) for v in r)

    def _maximized_frame(self, rect):
        """Where a window maximizes: the work area of the display its restored position is on,
        with an 8 px border overhang."""
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        scr = next((s for s in self.screen_list if s.bounds[0] <= cx < s.bounds[2] and s.bounds[1] <= cy < s.bounds[3]),
                   self.screen_list[0])
        l, t, r, b = scr.work
        return l - 8, t - 8, r + 8, b + 8

    # ---- test helpers

    def add(self, title: str, monitor: int = 0, **kw) -> int:
        h = self._next
        self._next += 0x10
        if "pid" not in kw and hasattr(self, "next_pid"):
            kw["pid"] = self.next_pid  # after a reboot: a new process
        if "rect" not in kw:
            l, t, _, _ = self.screen_list[monitor].work
            kw["rect"] = (l + 100, t + 100, l + 900, t + 700)
        self.windows[h] = FakeWindow(title, **kw)
        self.z.insert(0, h)
        return h

    def close(self, h: int) -> None:
        self.windows.pop(h, None)
        if h in self.z:
            self.z.remove(h)
        if self.foreground == h:
            self.foreground = 0

    def advance(self, ms: int) -> None:
        self.tick += ms

    def reboot(self) -> None:
        """The computer restarts: every window is gone, the uptime starts again, and new windows get
        new handles and process ids (so handles and pids of the last boot mean nothing)."""
        for h in list(self.windows):
            self.close(h)
        self.wall_shift = getattr(self, "wall_shift", 0) + self.tick + 30_000  # the clock keeps going
        self.tick = 60_000
        self.next_pid = getattr(self, "next_pid", 5000) + 1000
        self.boots = getattr(self, "boots", 0) + 1  # every process of the new boot has a new start time

    def set_clock(self, seconds: float) -> None:
        """The wall clock is set forward or back (time sync, the user): uptime and process start
        times do not change."""
        self.wall_shift = getattr(self, "wall_shift", 0) + int(seconds * 1000)

    # ---- the native API

    def enum_windows(self):
        return list(self.z)

    def is_window(self, h):
        return bool(h) and h in self.windows

    def is_window_visible(self, h):
        w = self.windows.get(h)
        return bool(w and w.visible)

    def is_iconic(self, h):
        w = self.windows.get(h)
        return bool(w and w.show_cmd == 2)

    def is_zoomed(self, h):
        w = self.windows.get(h)
        return bool(w and w.show_cmd == 3)

    def is_hung(self, h):
        w = self.windows.get(h)
        return bool(w and w.hung)

    def show_window(self, h, cmd):
        self.calls.append(("show", h, cmd))
        w = self.windows.get(h)
        if not w:
            return False
        was = w.visible
        if cmd == N.SW_HIDE:
            w.visible = False
        else:
            w.visible = True
            if cmd == N.SW_RESTORE:
                w.show_cmd, w.frame = 1, None
            elif cmd == N.SW_MAXIMIZE:
                w.show_cmd, w.frame = 3, self._maximized_frame(w.rect)
            elif cmd == N.SW_SHOWMINNOACTIVE:
                w.show_cmd = 2
        return was

    def show_window_safe(self, h, cmd):
        return self.show_window(h, cmd)

    def get_root(self, h):
        w = self.windows.get(h)
        return w.child_of if w and w.child_of else h

    def get_root_anchor(self, h):
        cur = h
        for _ in range(32):
            w = self.windows.get(cur)
            if not w or not w.anchor:
                break
            cur = w.anchor
        return cur

    def get_style(self, h):
        return self.windows[h].style

    def get_exstyle(self, h):
        return self.windows[h].exstyle

    def title_length(self, h):
        w = self.windows.get(h)
        return len(w.title) if w else 0

    def get_title(self, h):
        w = self.windows.get(h)
        return w.title if w else ""

    def get_class(self, h):
        w = self.windows.get(h)
        return w.cls if w else ""

    def get_pid(self, h):
        w = self.windows.get(h)
        return w.pid if w else 0

    def is_cloaked(self, h):
        return self.windows[h].cloaked

    def get_foreground(self):
        return self.foreground

    def set_foreground(self, h):
        self.calls.append(("fg", h))
        if h in self.windows:
            self.foreground = h
            return True
        return False

    def get_window_rect(self, h):
        w = self.windows.get(h)
        if not w:
            return None
        if w.show_cmd == 2:
            return (-32000, -32000, -31840, -31972)  # a minimized window's frame is a placeholder
        return self._out(w.frame if w.show_cmd == 3 and w.frame else w.rect)

    def set_window_pos(self, h, x, y, w_, h_, flags):
        self.calls.append(("pos", h, x, y, w_, h_, flags))
        w = self.windows[h]
        r = self._in((x, y, x + w_, y + h_))
        if w.show_cmd == 2:
            return True  # moves the minimized placeholder; where it restores to does not change
        if w.show_cmd == 3 and w.frame:
            w.frame = r  # moves the maximized frame (the restored position stays)
        else:
            w.rect = r
        return True

    def screens(self):
        if self._scale == 1.0:
            return list(self.screen_list)
        return [FakeScreen(s.device, self._out(s.bounds), self._out(s.work), s.primary, s.hmon) for s in self.screen_list]

    def get_monitor_id(self, device):
        return self.monitor_ids.get(device, "")

    def get_work_area(self, device):
        for s in self.screen_list:
            if s.device == device:
                return self._out(s.work)
        return None

    def monitor_device_under_cursor(self):
        return self.cursor_device

    def monitor_device_of_window(self, h):
        w = self.windows.get(h)
        if not w or not self.screen_list:
            return None
        r = w.frame if w.show_cmd == 3 and w.frame else w.rect  # (minimized: its restored position)
        cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        for s in self.screen_list:
            if s.bounds[0] <= cx < s.bounds[2] and s.bounds[1] <= cy < s.bounds[3]:
                return s.device

        def dist(s):
            dx = max(s.bounds[0] - cx, 0, cx - s.bounds[2] + 1)
            dy = max(s.bounds[1] - cy, 0, cy - s.bounds[3] + 1)
            return dx * dx + dy * dy

        return min(self.screen_list, key=dist).device

    def _workspace(self, h):
        """Where workspace coordinates start, as Get/SetWindowPlacement use them for windows that are
        not tool windows: the top-left of the main display's work area (in this DPI view)."""
        w = self.windows.get(h)
        main = next((s for s in self.screen_list if s.primary), None)
        if w is None or main is None or w.exstyle & N.WS_EX_TOOLWINDOW:
            return 0, 0
        l, t, _, _ = self._out(main.work)
        return l, t

    def get_placement(self, h):
        w = self.windows.get(h)
        if not w:
            return None
        wp = N.WINDOWPLACEMENT()
        wp.showCmd = w.show_cmd
        wp.flags = w.flags
        ox, oy = self._workspace(h)
        l, t, r, b = self._out(w.rect)
        wp.rcNormalPosition = N.RECT(l - ox, t - oy, r - ox, b - oy)
        return wp

    def set_placement(self, h, wp):
        ox, oy = self._workspace(h)
        l, t, r, b = wp.rcNormalPosition.tup()
        screen = self._in((l + ox, t + oy, r + ox, b + oy))
        self.calls.append(("placement", h, wp.showCmd, screen))
        w = self.windows.get(h)
        if not w:
            return False
        w.rect = screen
        w.flags = wp.flags
        cmd = wp.showCmd
        if cmd == N.SW_HIDE:
            w.visible = False  # stays hidden, in the state it is in (maximized stays maximized)
            return True
        w.show_cmd = {N.SW_SHOWMAXIMIZED: 3, N.SW_SHOWMINNOACTIVE: 2}.get(cmd, 1)
        w.frame = self._maximized_frame(w.rect) if w.show_cmd == 3 else None
        w.visible = True  # SetWindowPlacement with a SHOW command shows the window
        return True

    def show_by_placement(self, h, wp):
        if getattr(self, "placement_fails", False):
            return False  # (a test: SetWindowPlacement refused)
        return self.set_placement(h, wp)

    @contextlib.contextmanager
    def in_window_dpi_context(self, h):
        w = self.windows.get(h)
        before, self._scale = self._scale, (w.dpi_scale if w else 1.0)
        try:
            yield
        finally:
            self._scale = before

    def get_exe_path(self, h):
        w = self.windows.get(h)
        return w.exe if w else ""

    def tick_count64(self):
        return self.tick

    def wall_time(self):
        return 1_700_000_000 + (self.tick + getattr(self, "wall_shift", 0)) / 1000

    def process_started(self, pid):
        """A process's start time: unique to that process in that boot (0 = unknown pid)."""
        return (getattr(self, "boots", 0) + 1) * 10**12 + pid if pid else 0


class FakeSnapshots:
    def __init__(self, capture_s: float = 0.0):
        self.captured: list[int] = []
        self.purged = 0
        self.capture_s = capture_s  # how long one capture takes (PrintWindow can take tens of ms)
        self.refreshed: list[list[int]] = []  # each sync's background request

    def capture(self, h):
        self.captured.append(h)
        if self.capture_s:
            import time
            time.sleep(self.capture_s)

    def get(self, h):
        return None  # (every switch captures; the real store's rules are tested on their own)

    def failed_recently(self, h, within_s):
        return False

    def refresh(self, hs):
        self.refreshed.append(list(hs))

    def hold(self, seconds):
        pass

    def collect(self, keep):
        pass

    def purge(self, is_window=None):
        self.purged += 1

    def clear(self):
        pass
