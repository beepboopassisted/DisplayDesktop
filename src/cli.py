"""Commands for running DisplayDesktop from source (what run.cmd passes on):

    start            start it in the tray (pythonw: no console window) and confirm it is running
    stop             restore every hidden window and exit (never kills it)
    restart          stop, then start
    status           running? from where? Start with Windows on/off?
    recovery         Recovery: what is wrong with each settings file, Restore Hidden Windows,
                     Recreate Files, Quit (opened in the running copy, or started with it first)
    enable-startup   start at every Windows sign-in
    disable-startup  stop doing that

Command names are not case-sensitive and dashes are optional (EnableStartup works too).
"""
from __future__ import annotations

import ctypes
import sys
import time

from . import native as N
from . import startup
from .app import MUTEX_NAME, RECOVERY_ARG, WaitForSingleObject, signal_quit

MAIN_CLASS = "DisplayDesktop.Main"
RECOVERY_MESSAGE = 0x8000 + 7  # tray.WM_APP_RECOVERY (WM_APP + 7; not imported: commands never load the GUI)
RECOVERY_CLASS, RECOVERY_QUIT = "DisplayDesktop.Recovery", 212  # restore_window.RecoveryWindow, its ID_QUIT
KEY_CLASS, KEY_CANCEL = "DisplayDesktop.Key", 2  # restore_window.KeyWindow, its Cancel (IDCANCEL)
_OpenMutexW = N._f(N.kernel32, "OpenMutexW", N.H, N.DWORD, N.BOOL, ctypes.c_wchar_p)
_FindWindowW = N._f(N.user32, "FindWindowW", N.H, ctypes.c_wchar_p, ctypes.c_wchar_p)
_PostMessageW = N._f(N.user32, "PostMessageW", N.BOOL, N.H, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
SYNCHRONIZE = 0x00100000
ACCESS_DENIED = 5  # a post refused (UIPI: the window's process runs as administrator)
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200


def normalize(arg: str) -> str:
    return arg.lower().lstrip("-").replace("-", "").replace("_", "")


def is_running() -> bool:
    """The single-instance mutex is the only proof that DisplayDesktop runs."""
    h = _OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if h:
        N.CloseHandle(h)
    return bool(h)


def _main_window() -> int:
    return _FindWindowW(MAIN_CLASS, None) or 0


def running_pid() -> int:
    hwnd = _main_window()
    return N.get_pid(hwnd) if hwnd else 0


def _wait(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return bool(pred())


def start(recovery: bool = False) -> int:
    if is_running():
        print(f"DisplayDesktop is already running (PID {running_pid()}). Use 'restart' to restart it.")
        return 0
    cmd = startup.app_command() + ([RECOVERY_ARG] if recovery else [])
    import subprocess  # (only `start` needs it)
    try:
        proc = subprocess.Popen(cmd, cwd=startup.REPO_DIR, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except OSError as exc:
        print(f"Could not start {cmd[0]}: {exc}")
        return 1
    # Running = the mutex exists and the process is still alive a moment later (the mutex is
    # created first, so a copy that fails right after start-up would release it again).
    if _wait(lambda: is_running() or proc.poll() is not None, 15) and proc.poll() is None:
        time.sleep(1.5)
        if is_running() and proc.poll() is None:
            if recovery:
                print(f"DisplayDesktop started in Recovery (PID {proc.pid}): its window shows what is "
                      "wrong. Continue starts it as usual; Quit ends it.")
            else:
                print(f"DisplayDesktop is running in the tray (PID {proc.pid}). Stop it with 'run stop' "
                      "or Exit in the tray menu.")
            return 0
    if proc.poll() is not None:
        print(f"DisplayDesktop exited right away (exit code {proc.returncode}); "
              "it shows a message explaining why, if it got that far.")
    else:
        print("DisplayDesktop did not finish starting within 15 seconds "
              "(if it shows an error message, that is why).")
    return 1


def stop() -> int:
    if not is_running():
        print("DisplayDesktop is not running.")
        return 0
    pid = running_pid()
    # (still asking at start, before the tray runs: Recovery's Quit or the key popup's Cancel ends
    # it; nothing is hidden yet)
    asking, button = 0, 0
    if not pid:
        for cls, cid in ((RECOVERY_CLASS, RECOVERY_QUIT), (KEY_CLASS, KEY_CANCEL)):
            asking = _FindWindowW(cls, None) or 0
            if asking:
                button = cid
                break
    if asking:
        pid = N.get_pid(asking)
    process = N._OpenProcess(SYNCHRONIZE, False, pid) if pid else 0
    print("Stopping DisplayDesktop (restoring hidden windows)...")
    if asking and not _PostMessageW(asking, 0x0111, button, 0) and ctypes.get_last_error() == ACCESS_DENIED:
        # (refused: a copy running as administrator can't be told from a normal console; a window
        # closed meanwhile is no refusal: the quit event and the wait below go on)
        if process:
            N.CloseHandle(process)
        print("DisplayDesktop is still starting (asking for a key or in Recovery) and can't be told "
              "from here (it may run as administrator): answer its window.")
        return 1
    signal_quit()
    try:
        # Done when its process has exited (or, if it was not found, when the mutex is gone)
        if process:
            stopped = WaitForSingleObject(process, 15_000) == 0
        else:
            stopped = _wait(lambda: not is_running(), 15)
    finally:
        if process:
            N.CloseHandle(process)
    if not stopped:
        print(f"DisplayDesktop did not exit (PID {pid}). Use Exit in the tray menu. "
              "It is not killed on purpose, so hidden windows are not lost.")
        return 1
    print("Stopped. All hidden windows were restored.")
    return 0


def recovery() -> int:
    """Recovery: opened in the running copy, or the app is started with it first (before
    anything is read or hidden)."""
    if not is_running():
        return start(recovery=True)
    hwnd = _main_window()
    asking = 0 if hwnd else (_FindWindowW(RECOVERY_CLASS, None) or 0)
    if asking:  # (still in Recovery at start: it is already open)
        print(f"Recovery is already open in DisplayDesktop (PID {N.get_pid(asking)}).")
        return 0
    key = 0 if hwnd else (_FindWindowW(KEY_CLASS, None) or 0)
    if key:
        print("DisplayDesktop is waiting for its encryption key at start: answer it first.")
        return 1
    if hwnd and not _PostMessageW(hwnd, RECOVERY_MESSAGE, 0, 0):
        if ctypes.get_last_error() == ACCESS_DENIED:
            print("DisplayDesktop can't be told from here (it may run as administrator): open Recovery "
                  "from a console run as administrator.")
            return 1
        hwnd = 0  # (closed meanwhile)
    if not hwnd:
        print("DisplayDesktop is running but its window was not found (still starting?). Try again shortly.")
        return 1
    print(f"Recovery opened in the running DisplayDesktop (PID {N.get_pid(hwnd)}).")
    return 0


def status() -> int:
    if is_running():
        hwnd = _main_window()
        pid = N.get_pid(hwnd) if hwnd else 0
        exe = N.get_exe_path(hwnd) if hwnd else ""
        print(f"DisplayDesktop is running (PID {pid}{', ' + exe if exe else ''}).")
    else:
        print("DisplayDesktop is not running.")
    v = sys.version_info
    print(f"Python {v.major}.{v.minor}.{v.micro}: {sys.executable}")
    print(f"App command: {startup.command_line()}")
    s = startup.state()
    if s == "outdated":
        print("Start with Windows: on, but the shortcut is outdated (it is updated the next time "
              "DisplayDesktop starts, or now with 'enable-startup').")
    elif s == "off" and startup.exists():
        print("Start with Windows: off (turned off in Settings > Apps > Startup)")
    else:
        print(f"Start with Windows: {s}")
    got = startup.link()
    if got is not None:
        print(f"Startup shortcut: {_opens(got['exe'], got['args'])}")
    if startup.has_run_value():
        print("An older startup entry (Run value) is there: it is moved to the shortcut the next time "
              "DisplayDesktop starts.")
    return 0


def _opens(exe: str, args: str) -> str:
    return f"{exe} {args}" if args else exe


def enable_startup() -> int:
    if not startup.can_manage():
        print("Not available in test mode.")
        return 1
    try:
        want = startup.target()
    except OSError:
        print("Could not add the startup shortcut: no Python opens .pyw files here and this Python has no "
              "pythonw.exe (it would open a console window at every sign-in).")
        return 1
    try:
        ok = startup.set_enabled(True, want) and startup.state(want) == "on"
    except OSError:
        ok = False
    print(f"Start with Windows: on ({_opens(*want)})" if ok else "Could not add the startup shortcut.")
    return 0 if ok else 1


def disable_startup() -> int:
    if not startup.can_manage():
        print("Not available in test mode.")
        return 1
    if not startup.exists() and not startup.has_run_value():
        print("Start with Windows was already off.")
        return 0
    try:
        ok = not startup.set_enabled(False)
    except OSError:
        ok = False
    print("Start with Windows: off" if ok else "Could not remove the startup shortcut.")
    return 0 if ok else 1


def run(command: str) -> int:
    # (-I ignores PYTHONIOENCODING/PYTHONUTF8: a redirected stdout uses the ANSI code page with
    # "strict" or "surrogateescape", so a path character outside it is printed as "?", never an error)
    for stream in (sys.stdout, sys.stderr):
        if getattr(stream, "errors", "replace") not in ("replace", "backslashreplace"):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError, OSError):  # (not a TextIOWrapper, closed)
                pass
    cmd = normalize(command)
    if cmd == "start":
        return start()
    if cmd == "stop":
        return stop()
    if cmd == "restart":
        return stop() or start()
    if cmd == "status":
        return status()
    if cmd == "recovery":
        return recovery()
    if cmd == "enablestartup":
        return enable_startup()
    if cmd == "disablestartup":
        return disable_startup()
    print(f"Unknown command: {command}\n\n{__doc__.strip()}")
    return 2
