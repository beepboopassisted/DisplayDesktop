"""End-to-end test of the real app process: pythonw -B -m src, in test mode (temporary data
folder, scoped to the test windows' process). Drives it with the same messages Windows sends it.

    python -B src/tests/live_app.py
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # (src\tests -> the repo)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from src import gui as G  # noqa: E402
from src import native as N  # noqa: E402
from src import startup  # noqa: E402
from src import tray as T  # noqa: E402
from src.app import MUTEX_NAME  # noqa: E402
from src.paths import FORMAT, JOURNAL_FILE, PLACES_FILE  # noqa: E402

from fakes import all_desktops, all_showing, all_windows  # noqa: E402
from live_core import PYTHONW, Helper, check, results, wait_for  # noqa: E402

_OpenMutexW = N._f(N.kernel32, "OpenMutexW", N.H, N.DWORD, N.BOOL, ctypes.c_wchar_p)
_GetDlgItem = N._f(N.user32, "GetDlgItem", N.H, N.H, ctypes.c_int)
_GetWindowTextW = N._f(N.user32, "GetWindowTextW", ctypes.c_int, N.H, ctypes.c_wchar_p, ctypes.c_int)
_RegisterHotKey = N._f(N.user32, "RegisterHotKey", N.BOOL, N.H, ctypes.c_int, N.UINT, N.UINT)
_UnregisterHotKey = N._f(N.user32, "UnregisterHotKey", N.BOOL, N.H, ctypes.c_int)
_SendMessageTimeoutW = N._f(N.user32, "SendMessageTimeoutW", N.LRESULT, N.H, N.UINT, N.WPARAM, N.LPARAM,
                            N.UINT, N.UINT, ctypes.POINTER(ctypes.c_size_t))


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", N.DWORD), ("hWnd", N.H), ("uID", N.UINT), ("guidItem", ctypes.c_byte * 16)]


_Shell_NotifyIconGetRect = N._f(N.shell32, "Shell_NotifyIconGetRect", ctypes.c_long,
                                ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(N.RECT))


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", N.DWORD), ("cntUsage", N.DWORD), ("th32ProcessID", N.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", N.DWORD), ("cntThreads", N.DWORD),
                ("th32ParentProcessID", N.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", N.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_CreateToolhelp32Snapshot = N._f(N.kernel32, "CreateToolhelp32Snapshot", N.H, N.DWORD, N.DWORD)
_Process32FirstW = N._f(N.kernel32, "Process32FirstW", N.BOOL, N.H, ctypes.POINTER(PROCESSENTRY32W))
_Process32NextW = N._f(N.kernel32, "Process32NextW", N.BOOL, N.H, ctypes.POINTER(PROCESSENTRY32W))


def child_pids(pid: int, exe: str) -> list[int]:
    """Running child processes of pid with that executable name."""
    snap = _CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (0, -1):
        return []
    found = []
    try:
        e = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = _Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ParentProcessID == pid and e.szExeFile.lower() == exe:
                found.append(e.th32ProcessID)
            ok = _Process32NextW(snap, ctypes.byref(e))
    finally:
        N.CloseHandle(snap)
    return found


def mutex_exists() -> bool:
    h = _OpenMutexW(0x00100000, False, MUTEX_NAME)
    if h:
        N.CloseHandle(h)
    return bool(h)


def windows_of(pid: int, cls: str | None = None, visible_only=False) -> list[int]:
    return [h for h in N.enum_windows() if N.get_pid(h) == pid and (cls is None or N.get_class(h) == cls)
            and (not visible_only or N.is_window_visible(h))]


def dialog_of(pid: int) -> int:
    d = windows_of(pid, "#32770", visible_only=True)
    return d[0] if d else 0


def answer(pid: int, button: int, timeout=5.0) -> str:
    """Waits for the app's message box, presses a button (IDYES 6, IDNO 7; the OK button of an OK-only box has ID 2) and returns its
    title ("" if no message box appeared)."""
    if not wait_for(lambda: dialog_of(pid) != 0, timeout):
        return ""
    d = dialog_of(pid)
    title = N.get_title(d)
    G.PostMessageW(d, G.WM_COMMAND, button, 0)
    wait_for(lambda: not N.is_window(d), 3)
    return title


WINEVENTPROC = ctypes.WINFUNCTYPE(None, N.H, N.DWORD, N.H, ctypes.c_long, ctypes.c_long, N.DWORD, N.DWORD)
_SetWinEventHook = N._f(N.user32, "SetWinEventHook", N.H, N.DWORD, N.DWORD, N.H, WINEVENTPROC, N.DWORD,
                        N.DWORD, N.DWORD)
_UnhookWinEvent = N._f(N.user32, "UnhookWinEvent", N.BOOL, N.H)
_PeekMessageW = N._f(N.user32, "PeekMessageW", N.BOOL, ctypes.POINTER(G.MSG), N.H, N.UINT, N.UINT, N.UINT)
EVENT_OBJECT_HIDE = 0x8003


def pump_until(pred, timeout: float) -> bool:
    """wait_for that also delivers this thread's messages (WinEvent hooks need them)."""
    end = time.monotonic() + timeout
    msg = G.MSG()
    while time.monotonic() < end:
        while _PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
            G.TranslateMessage(ctypes.byref(msg))
            G.DispatchMessageW(ctypes.byref(msg))
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


class App:
    def __init__(self, data: str, pids: set[int], capture_stderr: bool = False, extra_env=None, wait=True,
                 args: tuple[str, ...] = ()):
        """capture_stderr: run under python.exe (without a console window) so that anything the app
        prints to stderr, such as an error swallowed in a callback, can be checked. args: the app's
        own (e.g. --recovery)."""
        env = dict(os.environ)
        env["DISPLAYDESKTOP_DATA"] = data
        env["DISPLAYDESKTOP_TEST_PIDS"] = ",".join(map(str, pids))
        env.update(extra_env or {})
        if capture_stderr:
            python = os.path.join(os.path.dirname(PYTHONW), "python.exe")
            self.proc = subprocess.Popen([python, "-B", "-m", "src", *args], cwd=REPO, env=env,
                                         stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         creationflags=0x08000000)  # CREATE_NO_WINDOW
        else:
            self.proc = subprocess.Popen([PYTHONW, "-B", "-m", "src", *args], cwd=REPO, env=env)
        self.pid = self.proc.pid
        self.main = 0
        if not wait or (extra_env or {}).get(getattr(T, "TEST_FAIL_START_ENV", "")):
            return  # it is meant not to finish starting (or the test waits itself: the key popup)
        if wait_for(mutex_exists, 15) and wait_for(lambda: bool(windows_of(self.pid, "DisplayDesktop.Main")), 5):
            self.main = windows_of(self.pid, "DisplayDesktop.Main")[0]

    def post(self, msg, wp=0, lp=0) -> None:
        G.PostMessageW(self.main, msg, wp, lp)

    def command(self, cmd: int) -> None:
        self.post(T.WM_APP_COMMAND, cmd)

    def hotkey(self, hid: int) -> None:
        self.post(N.WM_HOTKEY, hid)

    def new_desktop(self) -> None:
        """What the overview's "+" does (the only way to add a desktop), via a test-mode message."""
        self.post(T.WM_TEST_NEW_DESKTOP)

    def exited(self, timeout=10.0) -> bool:
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False


def main() -> int:
    N.enable_per_monitor_dpi()
    if mutex_exists():
        print("DisplayDesktop is running: stop it first (run.cmd Stop).")
        return 2
    pos = N.cursor_pos()
    screens = N.screens()
    scr = next(s for s in screens if s.bounds[0] <= pos[0] < s.bounds[2] and s.bounds[1] <= pos[1] < s.bounds[3])
    tmp = tempfile.mkdtemp(prefix="ws-app-")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    layout = os.path.join(data, "display.desktop.layout.json")
    journal = os.path.join(data, JOURNAL_FILE)
    others_before = {h for h in N.enum_windows() if N.is_window_visible(h)}
    reg_before = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            reg_before = winreg.QueryValueEx(k, startup.VALUE_NAME)[0]
    except OSError:
        pass

    def startup_lnk():
        try:
            st = os.stat(startup.path())
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None
    lnk_before = startup_lnk()

    helper = Helper(tmp, "app", scr.work, ["WS App One", "WS App Two"])
    wins = list(helper.windows.values())
    apps: list[App] = []

    def visible_all():
        return all(N.is_window_visible(h) for h in wins)

    def hidden_all():
        return not any(N.is_window_visible(h) for h in wins)

    try:
        # ---------------- start
        app = App(data, {helper.pid}, capture_stderr=True)
        apps.append(app)
        check(app.main != 0, "app starts: single-instance mutex and its hidden main window exist")
        nii = NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(NOTIFYICONIDENTIFIER), hWnd=app.main, uID=1)
        r = N.RECT()
        check(wait_for(lambda: _Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(r)) == 0, 5),
              "tray icon is in the notification area")
        combos = [(N.MOD_CONTROL | N.MOD_ALT, N.VK_LEFT), (N.MOD_CONTROL | N.MOD_ALT, N.VK_RIGHT),
                  (N.MOD_CONTROL | N.MOD_ALT, N.VK_UP)]
        free = []
        for i, (mods, vk) in enumerate(combos):
            if _RegisterHotKey(0, 0xB000 + i, mods, vk):
                free.append(i)
                _UnregisterHotKey(0, 0xB000 + i)
        check(not free, f"all 3 global shortcuts are registered by the app (free: {free})")
        check(not os.path.exists(layout) and not os.path.exists(journal),
              "no saved desktops or journal written at start (only the last known places)")

        # ---------------- hotkeys: switch, overview
        app.new_desktop()
        check(wait_for(hidden_all, 5), "a new desktop (overview '+'): test windows hidden")
        check(wait_for(lambda: os.path.exists(journal), 3), "display.desktop.crash.json written while windows are hidden")
        app.hotkey(T.HK_PREV)
        check(wait_for(visible_all, 5), "Ctrl+Alt+Left: test windows back")
        check(wait_for(lambda: not os.path.exists(journal), 3), "display.desktop.crash.json removed again")
        time.sleep(1.5)
        check(not windows_of(app.pid, "DisplayDesktop.Slide", True), "no animation layer left on screen")

        app.hotkey(T.HK_OVERVIEW)
        check(wait_for(lambda: bool(windows_of(app.pid, "DisplayDesktop.Overview", True)), 5),
              "Ctrl+Alt+Up opens the overview")
        app.hotkey(T.HK_OVERVIEW)
        check(wait_for(lambda: not windows_of(app.pid, "DisplayDesktop.Overview", True), 5),
              "Ctrl+Alt+Up again closes it")

        # ---------------- tray: Save (No / Yes), Clear (No / Yes), startup item
        app.command(T.ID_SAVE)
        check(answer(app.pid, 7) != "", "Save desktops asks first")
        time.sleep(0.5)
        check(not os.path.exists(layout), "answer No: nothing saved")
        app.command(T.ID_SAVE)
        check(answer(app.pid, 6) != "", "Save desktops asks again")
        check(wait_for(lambda: os.path.exists(layout), 3), "answer Yes: layout.json saved")
        with open(layout, encoding="utf-8") as fh:
            saved = json.load(fh)
        check(len(all_windows(saved)) == 2 and all_showing(saved), "it holds the 2 test windows and the shown desktops")
        mtime = os.path.getmtime(layout)
        app.command(T.ID_LOAD)
        check(answer(app.pid, 7) != "", "Load desktops asks first")
        app.command(T.ID_LOAD)
        check(answer(app.pid, 6) != "", "Load desktops asks again (Yes)")
        time.sleep(0.5)
        check(app.proc.poll() is None and visible_all(), "answer Yes: loaded (the saved desktop is shown)")
        app.new_desktop()
        wait_for(hidden_all, 5)
        app.hotkey(T.HK_PREV)
        wait_for(visible_all, 5)
        time.sleep(1.3)  # two sync ticks
        check(os.path.getmtime(layout) == mtime, "switching and syncing never rewrite layout.json")
        app.command(T.ID_CLEAR)
        check(answer(app.pid, 7) != "", "Clear desktops asks first")
        time.sleep(0.4)
        with open(layout, encoding="utf-8") as fh:
            check(json.load(fh) == saved, "answer No: saved layout unchanged")
        app.command(T.ID_CLEAR)
        answer(app.pid, 6)
        time.sleep(0.5)
        with open(layout, encoding="utf-8") as fh:
            check(json.load(fh) == {"Setups": [], "Format": FORMAT}, "answer Yes: saved layout erased")
        app.command(T.ID_SETTINGS)
        check(wait_for(lambda: bool(windows_of(app.pid, "DisplayDesktop.Settings", True)), 5),
              "Settings opens its dialog")
        for h in windows_of(app.pid, "DisplayDesktop.Settings"):
            G.PostMessageW(h, G.WM_CLOSE, 0, 0)
        check(wait_for(lambda: not windows_of(app.pid, "DisplayDesktop.Settings", True), 5),
              "and it closes")
        time.sleep(0.4)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                now = winreg.QueryValueEx(k, startup.VALUE_NAME)[0]
        except OSError:
            now = None
        check(now == reg_before and startup_lnk() == lnk_before,
              "a test-mode copy never touches the startup entry (Run value, Startup shortcut)")
        app.post(G.RegisterWindowMessageW("TaskbarCreated"))
        time.sleep(0.3)
        check(app.proc.poll() is None, "survives an Explorer restart notification")

        # ---------------- Show console: a separate viewer process, never attached to the app
        settings = os.path.join(data, "display.desktop.settings.json")
        app.command(T.ID_CONSOLE)
        check(wait_for(lambda: len(child_pids(app.pid, "python.exe")) == 1, 5),
              "Show console starts one viewer process")
        check(wait_for(lambda: os.path.exists(settings), 2), "Show console: setting saved")
        viewer = (child_pids(app.pid, "python.exe") or [0])[0]
        app.new_desktop()
        check(wait_for(hidden_all, 5), "console open: windows hidden by a switch")
        # Closing the console window ends the viewer process: simulate it the hard way
        subprocess.run(["taskkill", "/f", "/pid", str(viewer)], capture_output=True,
                       creationflags=0x08000000)
        check(wait_for(lambda: not child_pids(app.pid, "python.exe"), 3), "console closed (viewer ended)")
        time.sleep(1.0)
        check(app.proc.poll() is None and mutex_exists(), "closing the console does not end the app")
        check(hidden_all(), "windows of the other desktop stay managed (still hidden, not lost)")
        check(wait_for(lambda: not os.path.exists(settings), 3), "the toggle turned itself off")
        app.hotkey(T.HK_PREV)
        check(wait_for(visible_all, 5), "switching back still works: windows back")
        app.command(T.ID_CONSOLE)
        check(wait_for(lambda: len(child_pids(app.pid, "python.exe")) == 1, 5), "Show console again")
        app.command(T.ID_CONSOLE)
        check(wait_for(lambda: not child_pids(app.pid, "python.exe"), 5) and
              wait_for(lambda: not os.path.exists(settings), 2), "toggle off closes the viewer, setting cleared")
        check(app.proc.poll() is None, "app still running")

        # ---------------- second instance
        second = subprocess.Popen([PYTHONW, "-B", "-m", "src"], cwd=REPO,
                                  env=dict(os.environ, DISPLAYDESKTOP_DATA=data))
        try:  # (cleaned up whatever happens: it only shows its "already running" box, answered here)
            check(answer(second.pid, 2) != "", "a second copy says it is already running")
            try:
                code = second.wait(5)
            except subprocess.TimeoutExpired:
                code = None
            check(code == 1 and mutex_exists(), "and exits (code 1) while the first keeps running")
        finally:
            for _ in range(3):
                if second.poll() is not None:
                    break
                answer(second.pid, 2)  # its box (OK = IDCANCEL 2): it then exits by itself
                try:
                    second.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            if second.poll() is None:
                print("WARNING: a second test copy is still open (its message box); close it by hand")

        # ---------------- exit paths: each must restore every hidden window, and leave a last
        # snapshot so that the next start puts every window and desktop back
        def hide_then(app_: App) -> bool:
            app_.new_desktop()
            return wait_for(hidden_all, 5)

        places = os.path.join(data, PLACES_FILE)

        def next_start_restores() -> bool:
            with open(places, encoding="utf-8") as fh:
                data = json.load(fh)
            # the test windows are in the last snapshot, and so is the second desktop (it was shown)
            return (len([e for e in all_windows(data) if not e.get("Closed")]) >= 2
                    and max((all_desktops(data) or {"": 0}).values()) >= 2)

        # 1) --quit (same quit event that run.cmd Stop sets)
        check(hide_then(app), "[--quit] windows hidden first")
        q = subprocess.run([PYTHONW, "-B", "-m", "src", "--quit"], cwd=REPO, timeout=20)
        check(q.returncode == 0 and app.exited(5), "[--quit] the app exits and --quit returns after it")
        check(visible_all(), "[--quit] every hidden window restored")
        check(not os.path.exists(journal) and not mutex_exists(), "[--quit] journal deleted, mutex released")
        check(next_start_restores(), "[--quit] the last snapshot keeps the windows' desktops for the next start")
        err = app.proc.stderr.read().decode(errors="replace")
        check(err == "", "the app wrote nothing to stderr during the whole session" + (f": {err[-600:]}" if err else ""))

        # 2) sign-out / shutdown (WM_QUERYENDSESSION + WM_ENDSESSION)
        app = App(data, {helper.pid})
        apps.append(app)
        check(not windows_of(app.pid, "ConsoleWindowClass"), "run with pythonw: no console window")
        check(hide_then(app), "[sign-out] windows hidden first")
        res = ctypes.c_size_t()
        _SendMessageTimeoutW(app.main, G.WM_QUERYENDSESSION, 0, 0, 0, 3000, ctypes.byref(res))
        check(res.value == 1, "[sign-out] allows the session to end")
        _SendMessageTimeoutW(app.main, G.WM_ENDSESSION, 1, 0, 0, 5000, ctypes.byref(res))
        check(visible_all(), "[sign-out] every hidden window restored before it returns")
        check(app.exited(5) and not mutex_exists(), "[sign-out] the app exits")
        check(next_start_restores(), "[sign-out] the next start puts the windows back")

        # 3) tray Exit
        app = App(data, {helper.pid})
        apps.append(app)
        check(hide_then(app), "[Exit] windows hidden first")
        app.command(T.ID_EXIT)
        check(answer(app.pid, 7) == "DisplayDesktop", "[Exit] asks first")
        time.sleep(0.5)
        check(app.proc.poll() is None and hidden_all(), "[Exit] answer No: keeps running, nothing changes")
        app.command(T.ID_EXIT)
        check(answer(app.pid, 6) == "DisplayDesktop", "[Exit] asks again")
        check(app.exited(10) and visible_all(), "[Exit] answer Yes: the app exits and every hidden window is restored")
        check(next_start_restores(), "[Exit] the last snapshot keeps the windows' desktops for the next start")

        # 4) an unexpected error inside the app. One window sits on a later desktop (hidden), the
        # other on the first one; while the error message is open the sync timer fires and a
        # shortcut key arrives: neither may hide a window again or merge that desktop into the first
        # in the snapshot (each run above added a desktop, so "later" is not always desktop 2).
        from src.layout import title_hash
        app = App(data, {helper.pid})
        apps.append(app)
        on_two, on_one = wins[0], wins[1]
        check(hide_then(app), "[crash] windows hidden first")
        N.show_window(on_two, N.SW_SHOWNA)  # its app shows it again: adopted on the shown (new) desktop
        time.sleep(1.0)                     # at least one sync
        app.hotkey(T.HK_PREV)               # one desktop back
        check(wait_for(lambda: not N.is_window_visible(on_two), 5),
              "[crash] one window on a later desktop (hidden), the other on the first (hidden)")
        app.post(T.WM_TEST_CRASH)
        check(wait_for(visible_all, 5), "[crash] every hidden window restored before the error message")
        check(wait_for(lambda: dialog_of(app.pid) != 0, 5), "[crash] the error message is open")
        time.sleep(1.5)                     # the 600 ms sync timer would fire twice here
        app.hotkey(T.HK_NEXT)               # Ctrl+Alt+Right while the message is open
        time.sleep(0.5)
        check(visible_all(), "[crash] nothing is hidden again while the error message is open")
        check(answer(app.pid, 2) != "", "[crash] the error is reported")
        check(app.exited(10) and not mutex_exists(), "[crash] the app exits")
        check(not os.path.exists(journal), "[crash] journal deleted")
        check(next_start_restores(), "[crash] the next start puts the windows back")
        with open(places, encoding="utf-8") as fh:
            entries = {e.get("TitleHash"): e for e in all_windows(json.load(fh)) if not e.get("Closed")}
        two = entries.get(title_hash(N.get_title(on_two)), {})
        one = entries.get(title_hash(N.get_title(on_one)), {})
        check((two.get("Desktop") or 0) >= 1 and one.get("Desktop") == 0,
              "[crash] the last snapshot keeps each window on its own desktop, nothing merged into the first"
              f" (later: {two.get('Desktop')}, first: {one.get('Desktop')})")

        # 5) the start fails after the first sync (which puts the window back on desktop 2, hidden):
        # every window must be shown again before the error is reported
        hides: list[int] = []
        proc = WINEVENTPROC(lambda hk, ev, hwnd, obj, child, tid, t: hides.append(hwnd) if obj == 0 else None)
        hook = _SetWinEventHook(EVENT_OBJECT_HIDE, EVENT_OBJECT_HIDE, 0, proc, helper.pid, 0, 0)
        try:
            app = App(data, {helper.pid}, extra_env={T.TEST_FAIL_START_ENV: "1"})
            apps.append(app)
            pump_until(lambda: dialog_of(app.pid) != 0, 15)
            check(on_two in hides, "[failed start] the first sync hid the desktop-2 window")
        finally:
            _UnhookWinEvent(hook)
        check(visible_all(), "[failed start] every window is shown again before the error message")
        title = answer(app.pid, 2)
        check(title != "", "[failed start] the error is reported")
        check(app.exited(10) and app.proc.returncode == 1 and not mutex_exists(), "[failed start] exit code 1")
        check(visible_all() and not os.path.exists(journal), "[failed start] nothing left hidden, no journal")

        # 6) Encrypt Settings: the folder's files encrypted; the start asks for the key first (a wrong
        # one is refused), then runs; Cancel ends it before anything is read or hidden
        from src import crypto
        crypto.use("k1", True)
        crypto.recode_folder(data)
        crypto.reset()
        check(crypto.folder_encrypted(data), "[encrypted] the test folder's files are encrypted")

        def key_popup(pid: int) -> int:
            found = windows_of(pid, "DisplayDesktop.Key", visible_only=True)
            return found[0] if found else 0

        def type_key(win: int, text: str) -> None:
            buf = ctypes.create_unicode_buffer(text)
            G.SendMessageW(_GetDlgItem(win, 206), 0x000C, 0, ctypes.addressof(buf))  # WM_SETTEXT the key box
            G.PostMessageW(win, G.WM_COMMAND, 1, 0)  # OK

        app = App(data, {helper.pid}, wait=False)
        apps.append(app)
        check(wait_for(lambda: key_popup(app.pid) != 0, 15) and not windows_of(app.pid, "DisplayDesktop.Main"),
              "[encrypted] the start asks for the key before anything else")
        popup = key_popup(app.pid)
        type_key(popup, "wrong")
        check(answer(app.pid, G.IDRETRY, 10) != "", "[encrypted] a wrong key asks to try again (Retry)")
        check(wait_for(lambda: dialog_of(app.pid) == 0, 10), "[encrypted] Retry: back to the key")
        status = ctypes.create_unicode_buffer(256)
        _GetWindowTextW(_GetDlgItem(popup, 209), status, 256)
        check(N.is_window(popup) and status.value != "", f"[encrypted] a wrong key is refused ({status.value!r})")
        type_key(popup, "k1")
        check(wait_for(lambda: bool(windows_of(app.pid, "DisplayDesktop.Main")), 15),
              "[encrypted] the right key: the app runs")
        app.main = windows_of(app.pid, "DisplayDesktop.Main")[0]
        app.new_desktop()
        time.sleep(1.0)
        app.post(T.WM_APP_QUIT)
        check(app.exited(10) and visible_all(), "[encrypted] exits, every window shown")
        with open(layout, "rb") as fh:
            check(crypto.is_ours(fh.read()), "[encrypted] its files are still encrypted")
        app = App(data, {helper.pid}, wait=False)
        apps.append(app)
        check(wait_for(lambda: key_popup(app.pid) != 0, 15), "[encrypted] asked again at the next start")
        G.PostMessageW(key_popup(app.pid), G.WM_COMMAND, 2, 0)  # Cancel
        check(app.exited(10) and app.proc.returncode == 0 and not mutex_exists() and visible_all(),
              "[encrypted] Cancel: the app ends quietly, nothing hidden")

        # 7) A file of a newer version (never expected): the start opens Recovery first (Quit / Recreate)
        from src import paths as P
        for name in os.listdir(data):
            os.remove(os.path.join(data, name))  # (the encrypted files above: a plain folder again)
        settings_file = os.path.join(data, "display.desktop.settings.json")
        with open(settings_file, "w", encoding="utf-8") as fh:
            fh.write('{"ShowConsole":false,"Format":%d}' % (P.FORMAT + 1))

        def newer_question(pid: int) -> int:
            found = windows_of(pid, "DisplayDesktop.Recovery", visible_only=True)
            return found[0] if found else 0
        app = App(data, {helper.pid}, wait=False)
        apps.append(app)
        check(wait_for(lambda: newer_question(app.pid) != 0, 15) and not windows_of(app.pid, "DisplayDesktop.Main"),
              "[newer] the start asks about the newer version's file before anything else")
        G.PostMessageW(newer_question(app.pid), G.WM_COMMAND, 2, 0)  # Quit (the default)
        check(app.exited(10) and app.proc.returncode == 0 and os.path.exists(settings_file) and visible_all(),
              "[newer] Quit: the app ends quietly, the file untouched, nothing hidden")
        app = App(data, {helper.pid}, wait=False)
        apps.append(app)
        check(wait_for(lambda: newer_question(app.pid) != 0, 15), "[newer] asked again at the next start")
        G.PostMessageW(newer_question(app.pid), G.WM_COMMAND, 211, 0)  # Recreate Files
        check(answer(app.pid, 7, 10) != "", "[newer] Recreate Files asks first (No)")
        check(newer_question(app.pid) != 0 and os.path.exists(settings_file)
              and not windows_of(app.pid, "DisplayDesktop.Main"), "[newer] No: nothing recreated, Recovery stays")
        G.PostMessageW(newer_question(app.pid), G.WM_COMMAND, 211, 0)
        check(answer(app.pid, G.IDYES, 10) != "", "[newer] asked again (Yes)")
        check(wait_for(lambda: bool(windows_of(app.pid, "DisplayDesktop.Main")), 15) and not os.path.exists(settings_file),
              "[newer] Recreate Files, Yes: the file is deleted and the app runs")
        app.main = windows_of(app.pid, "DisplayDesktop.Main")[0]
        app.post(T.WM_APP_QUIT)
        check(app.exited(10) and visible_all(), "[newer] exits, every window shown")

        # 8) Recovery on request: `--recovery` opens it before anything is hidden; `run recovery`
        # (WM_APP_RECOVERY) opens it in the running app, whose Quit ends it with every window shown
        app = App(data, {helper.pid}, wait=False, args=("--recovery",))
        apps.append(app)
        check(wait_for(lambda: newer_question(app.pid) != 0, 15) and not windows_of(app.pid, "DisplayDesktop.Main"),
              "[recovery] --recovery: Recovery first, before the app runs")
        G.PostMessageW(newer_question(app.pid), G.WM_COMMAND, 1, 0)  # Continue (IDOK, the default)
        check(wait_for(lambda: bool(windows_of(app.pid, "DisplayDesktop.Main")), 15),
              "[recovery] Continue: the app starts as usual")
        app.main = windows_of(app.pid, "DisplayDesktop.Main")[0]
        app.new_desktop()
        check(wait_for(hidden_all, 5), "[recovery] it runs (a new desktop hides the windows)")
        app.post(T.WM_APP_RECOVERY)
        check(wait_for(lambda: newer_question(app.pid) != 0, 10), "[recovery] run recovery: opened in the running app")
        G.PostMessageW(newer_question(app.pid), G.WM_COMMAND, 212, 0)  # Quit
        check(app.exited(10) and visible_all(), "[recovery] its Quit ends the app, every window shown")
        app = App(data, {helper.pid}, wait=False, args=("--recovery",))
        apps.append(app)
        wait_for(lambda: newer_question(app.pid) != 0, 15)
        env = dict(os.environ, DISPLAYDESKTOP_DATA=data)
        stop = subprocess.run([PYTHONW, "-B", "-m", "src", "stop"], cwd=REPO, env=env, timeout=30)
        check(stop.returncode == 0 and app.exited(10), "[recovery] run stop ends a start still in Recovery")

        # 9) Ctrl+Alt+Left/Right in the running app: the desktop slides left and right, and bounces at the
        # first and at the last desktop. Its own settings folder: the desktops it adds change no test above
        keys_data = os.path.join(tmp, "keys")
        os.makedirs(keys_data)
        app = App(keys_data, {helper.pid})
        apps.append(app)

        def slide_shown(timeout: float = 2.0) -> bool:
            return wait_for(lambda: bool(windows_of(app.pid, "DisplayDesktop.Slide", visible_only=True)), timeout)

        def slide_gone() -> bool:
            return wait_for(lambda: not windows_of(app.pid, "DisplayDesktop.Slide", visible_only=True), 3)
        check(app.main != 0 and wait_for(visible_all, 5), "[keys] the app runs, every window on desktop 1")
        app.hotkey(T.HK_PREV)  # (the keys act on the monitor under the mouse: the test windows' one)
        check(slide_shown() and slide_gone() and visible_all(),
              "[keys] Ctrl+Alt+Left at the first desktop: a bounce, nothing changes")
        app.hotkey(T.HK_NEXT)
        check(slide_shown() and slide_gone() and visible_all(),
              "[keys] Ctrl+Alt+Right at the last desktop: a bounce, nothing changes")
        app.new_desktop()
        check(wait_for(hidden_all, 5) and slide_gone(), "[keys] a second desktop shown (desktop 1's windows hidden)")
        app.hotkey(T.HK_PREV)
        check(slide_shown() and wait_for(visible_all, 5) and slide_gone(),
              "[keys] Ctrl+Alt+Left: slides back to desktop 1, its windows shown")
        app.hotkey(T.HK_PREV)
        check(slide_shown() and slide_gone() and visible_all(), "[keys] Ctrl+Alt+Left at desktop 1 again: a bounce")
        app.hotkey(T.HK_NEXT)
        check(slide_shown() and wait_for(hidden_all, 5) and slide_gone(),
              "[keys] Ctrl+Alt+Right: slides to desktop 2, desktop 1's windows hidden")
        app.hotkey(T.HK_NEXT)
        check(slide_shown() and slide_gone() and hidden_all(), "[keys] Ctrl+Alt+Right at the last desktop: a bounce")
        app.post(T.WM_APP_QUIT)
        check(app.exited(10) and visible_all(), "[keys] exits, every window shown")
    finally:
        for a in apps:
            if a.proc.poll() is None:
                main = a.main or next(iter(windows_of(a.pid, "DisplayDesktop.Main")), 0)
                if main:
                    G.PostMessageW(main, T.WM_APP_QUIT, 0, 0)  # like run stop (no confirm dialog)
                else:
                    dialog = dialog_of(a.pid)  # e.g. its "could not start" message
                    if dialog:
                        G.PostMessageW(dialog, G.WM_COMMAND, 2, 0)
                a.exited(10)
        for h in wins:
            if N.is_window(h) and not N.is_window_visible(h):
                N.show_window(h, N.SW_SHOWNA)
        helper.quit()
        shutil.rmtree(tmp, ignore_errors=True)

    went_hidden = [h for h in others_before if N.is_window(h) and not N.is_window_visible(h)]
    check(not went_hidden, f"none of the user's own windows was hidden ({len(others_before)} checked)")
    check(not any(a.proc.poll() is None for a in apps), "no test copy left running")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
