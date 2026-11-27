"""Live test of the core against real windows (run explicitly: python -B src/tests/live_core.py).

A real DesktopManager hides/shows/places real test windows (opened by winhelper.py in another
process) on the real monitors. It is scoped to the helper processes, so none of the user's own
windows are touched; the data folder is a temporary one. Everything is restored and deleted at the end.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))  # the repo (src\tests -> ..\..)

from src import native as N  # noqa: E402
from src.desktop_manager import DesktopManager  # noqa: E402
from src.paths import FORMAT, JOURNAL_FILE  # noqa: E402
from src.testing import ScopedWin  # noqa: E402
from fakes import all_showing, all_windows  # noqa: E402
from moving import move_window  # noqa: E402

N.enable_per_monitor_dpi()
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
_SetEvent = N._f(N.kernel32, "SetEvent", N.BOOL, N.H)
_GetDpiForMonitor = N._f(N._load("shcore.dll"), "GetDpiForMonitor", ctypes.c_long, N.H, ctypes.c_int,
                         ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint))


def dpi_of_monitor(hmon: int) -> int:
    x, y = ctypes.c_uint(96), ctypes.c_uint(96)
    return x.value if _GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) == 0 else 96
_OpenEventW = N._f(N.kernel32, "OpenEventW", N.H, N.DWORD, N.BOOL, ctypes.c_wchar_p)

_AttachThreadInput = N._f(N.user32, "AttachThreadInput", N.BOOL, N.DWORD, N.DWORD, N.BOOL)
_GetCurrentThreadId = N._f(N.kernel32, "GetCurrentThreadId", N.DWORD)
_GetWindowThreadProcessId = N._f(N.user32, "GetWindowThreadProcessId", N.DWORD, N.H, ctypes.c_void_p)
_BringWindowToTop = N._f(N.user32, "BringWindowToTop", N.BOOL, N.H)


def force_foreground(h: int) -> bool:
    """SetForegroundWindow from a background test process. Windows' foreground lock refuses it
    (the real app is allowed because it just received the hotkey), so attach to the input of the
    current foreground thread first."""
    me = _GetCurrentThreadId()
    fg = N.get_foreground()
    other = _GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(other and other != me and _AttachThreadInput(me, other, True))
    try:
        _BringWindowToTop(h)
        N.set_foreground(h)
    finally:
        if attached:
            _AttachThreadInput(me, other, False)
    return N.get_foreground() == h


class TestWin(ScopedWin):
    def set_foreground(self, h):
        return force_foreground(h)


results: list[tuple[bool, str]] = []
LAUNCHER = os.path.join(HERE, "..", "..", "DisplayDesktop.pyw")


def _run(command: str) -> str:
    r = subprocess.run([sys.executable, "-B", LAUNCHER, command], capture_output=True, text=True, timeout=60)
    return r.stdout


@contextlib.contextmanager
def real_app_stopped():
    """The user's own DisplayDesktop must not run during a live test: it manages the test windows
    too, and in its first two minutes it puts windows of the same program and title back where it
    last saw them (it recorded earlier test windows), moving them mid-test. It is stopped the clean
    way (run stop: every window shown), and started again afterwards if it was running."""
    running = "is running" in _run("status")
    if running:
        print("Stopping DisplayDesktop for the test (it is started again afterwards).", flush=True)
        _run("stop")
    try:
        yield
    finally:
        if running:
            _run("start")
            print("DisplayDesktop started again.", flush=True)


def check(ok, what: str) -> bool:
    results.append((bool(ok), what))
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    return bool(ok)


def wait_for(pred, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


class Helper:
    def __init__(self, tmp, name, rect, titles, extra=()):
        self.out = os.path.join(tmp, name + ".json")
        self.event = "WSTestHelperQuit_" + name + "_" + str(os.getpid())
        self.proc = subprocess.Popen([PYTHONW, "-B", os.path.join(HERE, "winhelper.py"), "--out", self.out,
                                      "--rect", ",".join(map(str, rect)), "--quit-event", self.event,
                                      "--titles", *titles, *extra])
        if not wait_for(lambda: os.path.exists(self.out), 15):
            raise RuntimeError("helper did not start")
        with open(self.out, encoding="utf-8") as fh:
            info = json.load(fh)
        self.pid = info["pid"]
        self.windows = info["windows"]
        self.floating = info["floating"]

    def all(self):
        return list(self.windows.values()) + ([self.floating] if self.floating else [])

    def quit(self):
        ev = _OpenEventW(0x0002, False, self.event)
        if ev:
            _SetEvent(ev)
            N.CloseHandle(ev)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            pass


def placement(h):
    with N.in_window_dpi_context(h):
        wp = N.get_placement(h)
    return wp.showCmd, wp.rcNormalPosition.tup()


_WINEVENTPROC = ctypes.WINFUNCTYPE(None, N.H, N.DWORD, N.H, ctypes.c_long, ctypes.c_long, N.DWORD, N.DWORD)
_SetWinEventHook = N._f(N.user32, "SetWinEventHook", N.H, N.DWORD, N.DWORD, N.H, _WINEVENTPROC, N.DWORD,
                        N.DWORD, N.DWORD)
_UnhookWinEvent = N._f(N.user32, "UnhookWinEvent", N.BOOL, N.H)


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", N.H), ("message", N.UINT), ("wParam", N.WPARAM), ("lParam", N.LPARAM),
                ("time", N.DWORD), ("pt", N.POINT), ("private", N.DWORD)]


_PeekMessageW = N._f(N.user32, "PeekMessageW", N.BOOL, ctypes.POINTER(_MSG), N.H, N.UINT, N.UINT, N.UINT)
_DispatchMessageW = N._f(N.user32, "DispatchMessageW", N.LRESULT, ctypes.POINTER(_MSG))
EVENT_OBJECT_SHOW = 0x8002


class ShowWatch:
    """Records every time a window of a process becomes visible (EVENT_OBJECT_SHOW), so a window
    that flashes on screen for a moment is caught too. Events arrive through this thread's
    messages: call pump() before reading `shown`."""

    def __init__(self, pid: int):
        self.shown: list[int] = []
        self._proc = _WINEVENTPROC(lambda hk, ev, hwnd, obj, child, tid, t: self.shown.append(hwnd) if obj == 0 else None)
        self._hook = _SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, 0, self._proc, pid, 0, 0)

    def pump(self, seconds: float = 0.3) -> None:
        end = time.monotonic() + seconds
        msg = _MSG()
        while time.monotonic() < end:
            while _PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                _DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)

    def close(self) -> None:
        if self._hook:
            _UnhookWinEvent(self._hook)
            self._hook = 0


def main() -> int:
    screens = N.screens()
    primary = next(s for s in screens if s.primary)
    other = next((s for s in screens if not s.primary), None)
    dev = primary.device
    tmp = tempfile.mkdtemp(prefix="ws-live-")
    helpers: list[Helper] = []
    managers: list[DesktopManager] = []
    print(f"monitors: {[(s.device, dpi_of_monitor(s.hmon)) for s in screens]}")

    others_before = {h for h in N.enum_windows() if N.is_window_visible(h)}
    try:
        h1 = Helper(tmp, "h1", primary.work, ["WS Test Alpha", "WS Test Beta", "WS Test Gamma"], ["--floating"])
        helpers.append(h1)
        pids = {h1.pid}
        win = TestWin(pids)
        mgr = DesktopManager(win=win, directory=tmp)
        managers.append(mgr)
        alpha, beta, gamma = (h1.windows[t] for t in ("WS Test Alpha", "WS Test Beta", "WS Test Gamma"))
        journal = os.path.join(tmp, JOURNAL_FILE)

        # --- adoption
        mgr.sync()
        check(sorted(mgr.desktops_of(dev)[0]) == sorted([alpha, beta, gamma]),
              "adoption: the 3 test windows are on the primary monitor's current desktop")
        check(h1.floating not in mgr.desktops_of(dev)[0], "floating tool window is not managed on its own")

        # --- switch away: hide (+ floating palette), journal, snapshots
        force_foreground(beta)
        mgr.create_desktop_and_switch(dev)
        check(mgr.current_of(dev) == 1, "new desktop created and shown")
        check(not any(N.is_window_visible(h) for h in h1.all()), "all test windows (and the floating palette) hidden")
        check(mgr.hidden == set(h1.all()), "hidden set = exactly the windows we hid")
        with open(journal, encoding="utf-8") as fh:
            j = json.load(fh)
        check({r["Handle"] for r in j["Windows"]} == set(h1.all())
              and all(r["Pid"] == h1.pid and r["Class"] == "WSTestWindow" for r in j["Windows"])
              and 0 < j["UptimeMs"] <= N.tick_count64(), "display.desktop.crash.json lists them with pid, class and uptime")
        shots = [mgr.snapshots.get(h) for h in (alpha, beta, gamma)]
        check(all(s is not None and 0 < max(s.w, s.h) <= 480 for s in shots),
              "preview snapshots captured (scaled to <= 480 px)")
        check(not os.path.exists(os.path.join(tmp, "display.desktop.layout.json")), "no layout.json written by switching")

        # --- switch back
        mgr.switch_to(dev, 0)
        check(all(N.is_window_visible(h) for h in h1.all()), "switch back: all test windows visible again")
        check(not os.path.exists(journal), "display.desktop.crash.json deleted when nothing is hidden")
        check(len(mgr.desktops_of(dev)) == 2, "the empty desktop is kept")
        check(N.get_foreground() == beta, "focus returns to the window that was active")

        # --- close to tray: an app that hides its own window
        N.show_window(alpha, N.SW_HIDE)
        mgr.sync()
        check(alpha not in mgr.desktops_of(dev)[0], "a window hidden by its own app is dropped")
        mgr.create_desktop_and_switch(dev)
        mgr.switch_to(dev, 0)
        check(not N.is_window_visible(alpha), "and it is NOT re-shown by a switch")
        N.show_window(alpha, N.SW_SHOWNA)
        mgr.sync()
        check(alpha in mgr.desktops_of(dev)[0], "when the app shows it again it is adopted again")

        # --- crash recovery from the journal (a new manager = a restarted app)
        mgr.create_desktop_and_switch(dev)
        check(not N.is_window_visible(alpha), "windows hidden before the simulated crash")
        mgr2 = DesktopManager(win=win, directory=tmp)  # the old one is never restored ("killed")
        managers.append(mgr2)
        check(all(N.is_window_visible(h) for h in h1.all()), "restart after a crash: journal restores every window")
        check(not os.path.exists(journal), "journal consumed")
        mgr._hidden.clear()  # the dead manager forgets them
        managers.remove(mgr)
        mgr = mgr2
        mgr.sync()

        # --- saved layout with real placements (DPI-correct)
        N.set_window_pos(alpha, primary.work[0] + 150, primary.work[1] + 120, 700, 500,
                         N.SWP_NOZORDER | N.SWP_NOACTIVATE)
        force_foreground(beta)
        time.sleep(0.1)
        move_window(mgr, mgr._win, beta, +1)  # beta on desktop 2 (shown)
        mgr.switch_to(dev, 0)
        alpha_place = placement(alpha)
        beta_place = placement(beta)
        n = mgr.save_desktops()
        check(n == 3, f"save: 3 windows saved (got {n})")
        with open(os.path.join(tmp, "display.desktop.layout.json"), encoding="utf-8") as fh:
            lay = json.load(fh)
        check(all_showing(lay).get(mgr._monitor_ids.get(dev)) == 0 and len(all_windows(lay)) == 3,
              "display.desktop.layout.json: 3 entries and the shown desktop")
        check(all("WS Test" not in json.dumps(e) for e in all_windows(lay)), "display.desktop.layout.json holds no titles")

        N.set_window_pos(alpha, primary.work[0] + 600, primary.work[1] + 400, 500, 400,
                         N.SWP_NOZORDER | N.SWP_NOACTIVATE)
        mgr.move_window_to_desktop(beta, dev, 0)
        mgr.load_desktops()
        check(placement(alpha) == alpha_place, "restore desktops: alpha back at its saved position")
        check(beta in mgr.desktops_of(dev)[1] and not N.is_window_visible(beta),
              "restore desktops: beta back on desktop 2 (hidden, desktop 1 shown)")

        # --- the computer restarts: the app closes while DisplayDesktop still runs, then opens
        # its windows again after DisplayDesktop started: they go back to their last known places
        mgr.restore_all()
        h1.quit()
        helpers.remove(h1)
        mgr.sync()
        h1b = Helper(tmp, "h1b", primary.work, ["WS Test Alpha", "WS Test Beta", "WS Test Gamma"])
        helpers.append(h1b)
        win._pids.add(h1b.pid)
        mgr3 = DesktopManager(win=win, directory=tmp)
        managers.append(mgr3)
        mgr3.sync()
        # (by count, never by title: the app's n-th window, in the order DisplayDesktop first saw them,
        # takes its n-th recorded place; windows already open when it started are counted in z-order)
        from src.layout import order_of
        reopened = sorted(h1b.windows.values(), key=lambda h: order_of(h, win))
        bound = [mgr3._places.bound_entry(h) for h in reopened]
        check(all(e is not None for e in bound) and len({id(e) for e in bound}) == len(reopened)
              and [e.Order for e in bound] == sorted(e.Order for e in bound),
              "reopened windows take the recorded places by count (first to first), one each")
        check(all(e is not None and h in mgr3.desktops_of(dev)[e.Desktop]
                  and N.is_window_visible(h) == (e.Desktop == mgr3.current_of(dev)) for h, e in zip(reopened, bound)),
              "reopened windows are put back on their places' desktops (hidden where not shown)")
        check(all(e is not None and placement(h)[1] == (e.Left, e.Top, e.Right, e.Bottom) for h, e in zip(reopened, bound)),
              "reopened windows are put back at their places' positions")
        check(sorted(placement(h)[1] for h in reopened if placement(h)[1] in (alpha_place[1], beta_place[1]))
              == sorted((alpha_place[1], beta_place[1])), "the saved positions are all taken again")
        mgr3.restore_all()

        # --- hung window: never hidden
        h2 = Helper(tmp, "h2", primary.work, ["WS Test Hung"], ["--hang-after", "0.5", "--hang", "9"])
        helpers.append(h2)
        win._pids.add(h2.pid)
        hung = h2.windows["WS Test Hung"]
        if check(wait_for(lambda: N.is_hung(hung), 12), "helper window is reported not responding"):
            mgr3.sync()
            mgr3.create_desktop_and_switch(dev)
            check(N.is_window_visible(hung) and hung not in mgr3.hidden, "a hung window is never hidden")
            mgr3.sync()
            check(hung in mgr3.desktops_of(dev)[mgr3.current_of(dev)], "it follows to the current desktop")
            mgr3.switch_to(dev, 0)
            check(N.is_window_visible(hung), "still visible after switching back")
        wait_for(lambda: not N.is_hung(hung), 15)

        # --- other monitor (mixed DPI): move a window and a whole desktop
        if other is not None:
            mgr3.sync()
            g2 = h1b.windows["WS Test Gamma"]
            mgr3.move_window_to_desktop(g2, other.device, 0)
            check(N.monitor_device_of_window(g2) == other.device and N.is_window_visible(g2),
                  f"window moved to {other.device} (DPI {dpi_of_monitor(other.hmon)})")
            fr = N.get_window_rect(g2)
            check(other.work[0] <= fr[0] and fr[2] <= other.work[2] + 16, "and it fits inside that monitor")
            force_foreground(a2)
            time.sleep(0.1)
            move_window(mgr3, mgr3._win, a2, +1)  # a2 alone on desktop 2 of the primary
            mgr3.move_desktop_to_monitor(dev, 1, other.device)
            check(N.monitor_device_of_window(a2) == other.device and not N.is_window_visible(a2),
                  "a whole desktop moved to the other monitor (hidden there, not active)")
            mgr3.switch_to(other.device, len(mgr3.desktops_of(other.device)) - 1)
            check(N.is_window_visible(a2) and not N.is_window_visible(g2), "switching there shows it")
            mgr3.restore_all()
            check(N.is_window_visible(a2) and N.is_window_visible(g2), "restore all shows both")

            # --- a desktop that is not on screen, with a maximized window, moved to the other
            # monitor: the window must never show up on the way (no flash, no focus); and a
            # minimized window moved there must restore there
            h5 = Helper(tmp, "h5", primary.work, ["WS Move Max", "WS Move Min"])
            helpers.append(h5)
            win._pids.add(h5.pid)
            hmax, hmin = h5.windows["WS Move Max"], h5.windows["WS Move Min"]
            N.show_window(hmax, N.SW_MAXIMIZE)
            N.show_window(hmin, 6)  # SW_MINIMIZE
            time.sleep(0.3)
            mgr3.sync()
            cur = mgr3.current_of(dev)
            mgr3.create_desktop_and_switch(dev)
            extra = mgr3.current_of(dev)
            mgr3.switch_to(dev, cur)
            mgr3.move_window_to_desktop(hmax, dev, extra)  # hmax alone on a desktop not on screen
            check(not N.is_window_visible(hmax) and N.is_zoomed(hmax), "maximized window on a hidden desktop")
            watch = ShowWatch(h5.pid)
            try:
                watch.pump(0.2)
                watch.shown.clear()
                mgr3.move_desktop_to_monitor(dev, extra, other.device)
                watch.pump(0.5)
                check(hmax not in watch.shown, f"moved to {other.device} without ever showing ({watch.shown})")
            finally:
                watch.close()
            check(not N.is_window_visible(hmax) and N.is_zoomed(hmax), "still hidden and still maximized")
            check(N.monitor_device_of_window(hmax) == other.device, "its maximized frame is on the other monitor")
            mgr3.switch_to(other.device, len(mgr3.desktops_of(other.device)) - 1)
            check(N.is_window_visible(hmax) and N.is_zoomed(hmax) and N.monitor_device_of_window(hmax) == other.device,
                  "switching there shows it maximized on that monitor")
            fr, ow = N.get_window_rect(hmax), other.work
            check(abs(fr[0] - ow[0]) <= 40 and abs(fr[2] - ow[2]) <= 40 and abs(fr[1] - ow[1]) <= 40,
                  f"filling that monitor's work area ({fr} vs {ow})")
            N.show_window(hmax, N.SW_RESTORE)
            time.sleep(0.2)
            check(N.monitor_device_of_window(hmax) == other.device, "restored, it stays on that monitor")

            mgr3.sync()
            check(N.is_iconic(hmin), "minimized window")
            mgr3.move_window_to_desktop(hmin, other.device, mgr3.current_of(other.device))
            check(N.is_iconic(hmin), "moved to the other monitor, still minimized")
            N.show_window(hmin, N.SW_RESTORE)
            time.sleep(0.2)
            check(not N.is_iconic(hmin) and N.monitor_device_of_window(hmin) == other.device,
                  f"restored, it opens on {other.device}")
            mgr3.sync()
            check(any(hmin in s for s in mgr3.desktops_of(other.device)), "and stays on that monitor's desktop")
            mgr3.restore_all()

        # --- clear
        mgr3.clear_desktops()
        with open(os.path.join(tmp, "display.desktop.layout.json"), encoding="utf-8") as fh:
            check(json.load(fh) == {"Setups": [], "Format": FORMAT}, "clear desktops erases the saved layout")
    finally:
        for m in managers:
            m.restore_all()
        for h in helpers:
            for x in h.all():
                if N.is_window(x) and not N.is_window_visible(x):
                    N.show_window(x, N.SW_SHOWNA)
            h.quit()
        shutil.rmtree(tmp, ignore_errors=True)

    went_hidden = [h for h in others_before if N.is_window(h) and not N.is_window_visible(h)]
    check(not went_hidden, f"none of the user's own windows was hidden ({len(others_before)} checked)")
    check(not os.path.exists(tmp), "temporary data folder deleted")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    with real_app_stopped():
        code = main()
    sys.exit(code)
