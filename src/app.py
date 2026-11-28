"""Entry point: single instance, `--quit`, `--recovery`, crash safety, message loop."""
from __future__ import annotations

import atexit
import ctypes
import sys

from . import native as N
# (gui, strings and threading are imported by main() for the app run only: the commands (cli.py)
# start faster without them)

MUTEX_NAME = "DisplayDesktop_SingleInstance"
QUIT_EVENT_NAME = "DisplayDesktop_Quit"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0, WAIT_ABANDONED = 0, 0x80
EVENT_MODIFY_STATE = 0x0002

_k = N.kernel32
_CreateMutexW = N._f(_k, "CreateMutexW", N.H, ctypes.c_void_p, N.BOOL, ctypes.c_wchar_p)
_ReleaseMutex = N._f(_k, "ReleaseMutex", N.BOOL, N.H)
_OpenEventW = N._f(_k, "OpenEventW", N.H, N.DWORD, N.BOOL, ctypes.c_wchar_p)
_OpenMutexW = N._f(_k, "OpenMutexW", N.H, N.DWORD, N.BOOL, ctypes.c_wchar_p)
SYNCHRONIZE, MUTANT_MODIFY_STATE = 0x00100000, 0x0001
# Shared with cli.py and tray.py
SetEvent = N._f(_k, "SetEvent", N.BOOL, N.H)
WaitForSingleObject = N._f(_k, "WaitForSingleObject", N.DWORD, N.H, N.DWORD)
_HANDLER = ctypes.WINFUNCTYPE(N.BOOL, N.DWORD)
_SetConsoleCtrlHandler = N._f(_k, "SetConsoleCtrlHandler", N.BOOL, _HANDLER, N.BOOL)


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", N.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", N.BOOL)]


# The mutex and the quit event: an elevated copy's are opened by the same user's normal console too
# (`run stop`, `status`, `--quit`): full access for SYSTEM, administrators and this user only (`{0}`:
# the user's SID, never other accounts in the session; full, so a new copy can open one an ending copy
# still holds); a medium integrity label (a high one's default would refuse a normal process)
_SHARED_SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{0})S:(ML;;NW;;;ME)"
_shared: _SECURITY_ATTRIBUTES | None = None


_advapi = None


def _advapi32():
    """advapi32.dll from System32 (native._load), loaded once."""
    global _advapi
    if _advapi is None:
        _advapi = N._load("advapi32.dll")
    return _advapi


def _user_sid() -> str | None:
    """This process's user SID as a string (S-1-5-21-...), or None."""
    advapi = _advapi32()
    open_token = N._f(advapi, "OpenProcessToken", N.BOOL, N.H, N.DWORD, ctypes.POINTER(N.H))
    token_info = N._f(advapi, "GetTokenInformation", N.BOOL, N.H, ctypes.c_int, ctypes.c_void_p, N.DWORD,
                      ctypes.POINTER(N.DWORD))
    to_string = N._f(advapi, "ConvertSidToStringSidW", N.BOOL, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
    local_free = N._f(_k, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
    token = N.H()
    if not open_token(N._f(_k, "GetCurrentProcess", N.H)(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        return None
    try:
        buf = (ctypes.c_byte * 256)()
        size = N.DWORD()
        if not token_info(token, 1, buf, ctypes.sizeof(buf), ctypes.byref(size)):  # TokenUser: its SID first
            return None
        text = ctypes.c_wchar_p()
        if not to_string(ctypes.c_void_p.from_buffer(buf).value, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            local_free(ctypes.cast(text, ctypes.c_void_p))
    finally:
        N.CloseHandle(token)


def shared_attributes():
    """SECURITY_ATTRIBUTES for the mutex and the quit event (None if Windows refuses: the default)."""
    global _shared
    if _shared is None:
        sid = _user_sid()
        if not sid:
            return None
        sd = ctypes.c_void_p()
        convert = N._f(_advapi32(),
                       "ConvertStringSecurityDescriptorToSecurityDescriptorW", N.BOOL,
                       ctypes.c_wchar_p, N.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        if not convert(_SHARED_SDDL.format(sid), 1, ctypes.byref(sd), None):
            return None
        _shared = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd, False)  # (kept for the run)
    return ctypes.byref(_shared)


def signal_quit() -> bool:
    """Sets the quit event of a running copy (it then restores its windows and exits).
    Returns False if no copy was running."""
    ev = _OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT_NAME)
    if not ev:
        return False
    try:
        SetEvent(ev)
    finally:
        N.CloseHandle(ev)
    return True


def quit_running_instance() -> bool:
    """Asks a running copy to restore its hidden windows and exit, and waits (up to 10 s) for it.
    Returns False if no copy was running."""
    if not signal_quit():
        return False
    # (opened, never created: SYNCHRONIZE + MUTANT_MODIFY_STATE, what an elevated copy's grants a normal one)
    mutex = _OpenMutexW(SYNCHRONIZE | MUTANT_MODIFY_STATE, False, MUTEX_NAME)
    if mutex:
        try:
            if WaitForSingleObject(mutex, 10_000) in (WAIT_OBJECT_0, WAIT_ABANDONED):
                _ReleaseMutex(mutex)  # abandoned = the other process exited owning it: expected
        finally:
            N.CloseHandle(mutex)
    return True


def make_thread_excepthook(get_app, quit_message: int):
    """threading.excepthook for the app: an error in a background thread is logged (traceback in
    the console) and the app quits through its UI thread, which restores the windows. Before the
    app exists, Python's own hook prints it."""
    import threading
    from . import gui as G

    def hook(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return  # a thread ending itself is not an error (Python's own hook ignores it too)
        try:
            import traceback
            from . import console
            console.log(f"Error in background thread {getattr(args.thread, 'name', '?')}:\n"
                        + "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                             args.exc_traceback)).rstrip())
        except BaseException:  # noqa: BLE001 - quitting (restoring the windows) comes first
            pass
        app = get_app()
        if app is not None and app.hwnd:
            if getattr(app, "thread_error", False) is None:
                app.thread_error = args.exc_value  # (the UI thread tells the user, with where it happened)
            G.PostMessageW(app.hwnd, quit_message, 0, 0)
        else:
            threading.__excepthook__(args)
    return hook


RECOVERY_ARG = "--recovery"


def main(argv: list[str] | None = None) -> int:
    """No arguments: run the app. `--recovery`: run it, Recovery first (cli `recovery` starts it
    so). `--quit`: ask a running copy to exit (silently). Anything else is a command for running from
    source (see cli.py), e.g. `start`, `stop`, `status`, `recovery`."""
    argv = sys.argv[1:] if argv is None else argv
    N.enable_per_monitor_dpi()  # before any window exists

    recovery = [a.lower() for a in argv] == [RECOVERY_ARG]
    if argv and not recovery:
        if any(a.lower() == "--quit" for a in argv):
            quit_running_instance()
            return 0
        from . import cli
        return cli.run(argv[0])

    N.preload("d3d11.dll", "dcomp.dll", "dxgi.dll")  # (the slide's GPU DLLs, beside the imports below)
    import threading
    from . import gui as G
    from . import strings as L
    mutex = _CreateMutexW(shared_attributes(), True, MUTEX_NAME)
    if not mutex or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if mutex:
            N.CloseHandle(mutex)
        G.message_box(L.t("msg.alreadyRunning"), G.MB_OK | G.MB_ICONINFORMATION)
        return 1

    from .tray import WM_APP_QUIT, TrayApp

    app = None
    done = threading.Event()

    def last_resort() -> None:
        if app is not None:
            try:
                app.mgr.end()  # (again after the tray's: windows still hidden get another try)
            except BaseException:  # noqa: BLE001
                pass

    atexit.register(last_resort)

    def excepthook(exc_type, exc, tb):
        if app is not None:
            app.on_fatal(exc)
        else:
            sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook
    threading.excepthook = make_thread_excepthook(lambda: app, WM_APP_QUIT)

    # Console runs (python.exe): Ctrl+C or closing the console exits cleanly, restoring windows
    def on_console(ctrl_type: int) -> bool:
        signal_quit()
        done.wait(4.5)
        return True

    handler = _HANDLER(on_console)
    _SetConsoleCtrlHandler(handler, True)

    startup_error: BaseException | None = None
    try:
        app = TrayApp(recovery=recovery)
        G.run_message_loop()
    except BaseException as exc:  # noqa: BLE001
        if app is not None:
            app.on_fatal(exc)
        else:
            # Failed while starting: TrayApp has already shown any window it hid (_abort_start)
            startup_error = exc
    finally:
        last_resort()
        done.set()
        _ReleaseMutex(mutex)
        N.CloseHandle(mutex)
    from .tray import StartCancelled
    if isinstance(startup_error, StartCancelled):
        return 0  # (the key popup or Recovery at start: Cancel / Quit, nothing was read or hidden)
    if startup_error is not None:
        # After releasing the mutex, so nothing mistakes this copy for a running one
        G.message_box(L.f("msg.startFailed", L.error_text(startup_error)),
                      G.MB_OK | G.MB_ICONERROR)
        return 1
    return 0
