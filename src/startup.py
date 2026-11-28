"""Start with Windows: the shortcut "DisplayDesktop.lnk" in the user's Startup folder, written by
shortcut.py. Its target is "<this folder>\\DisplayDesktop.pyw" itself (no arguments, work folder =
this folder, icon src/app.ico): Windows starts it through the .pyw file association (the Python
launcher), and Settings > Apps > Startup lists it as "DisplayDesktop.pyw" (a pythonw.exe target is
listed as "Python"). On a PC whose .pyw files open with no Python: the full path of this Python's
pythonw.exe with the launch flags.

On = the shortcut exists and the Startup apps page has not turned it off (StartupApproved\\StartupFolder
"DisplayDesktop.lnk": an odd first byte = off). A shortcut differing only in the letter case of its
paths is current; any other is outdated and rewritten at launch while on. Older versions used the HKCU
Run value "DisplayDesktop": refresh() moves it to the shortcut (an entry turned off stays off) and
deletes it. The Settings item and `run.cmd enable-startup` / `disable-startup` all use this module.
A copy in test mode (DISPLAYDESKTOP_DATA set) never touches the shortcut or the registry.
"""
from __future__ import annotations

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # (older versions' entry; moved to the shortcut)
APPROVED_RUN = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_FOLDER = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
VALUE_NAME = "DisplayDesktop"
LNK = VALUE_NAME + ".lnk"
CSIDL_STARTUP = 0x0007
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO_DIR, "DisplayDesktop.pyw")
# Python flags of every app launch (Run value, Start menu shortcut, `run start`): isolated (no PYTHON*
# variables, no user site), no `site` import, no .pyc files written. Never -OO with -B: the stdlib has
# no opt-2 .pyc files, so it would compile the stdlib at every start.
FLAGS = ("-I", "-S", "-B")


SLOW_WAIT_S = 0.5  # all checks of folders not on a fixed disk (UNC, network, removable) of one search wait at most this long
_UNANSWERED_MAX = 64
_unanswered = set()  # roots (normcased drive or UNC share) whose check did not answer in time: skipped for the rest of the run


def _is_file(path: str) -> bool:
    """on_path_all's check of one file (tests replace this, never os.path.isfile: probe threads of
    another search may still be running)."""
    return os.path.isfile(path)


def on_path_all(cmd: str, folders: list[str] | None = None):
    """Each `cmd` file in a PATH folder (`folders`, default this process's PATH), in PATH order. The
    exact name, ".exe" added only without an extension (CreateProcess's rule; no PATHEXT). Only
    folders with a drive or UNC root (`\\\\server\\share` and `//server/share` too): never the current
    folder nor a relative or drive-relative entry. Folders not on a fixed disk (UNC, a network,
    removable or optical drive) are checked on threads, started only as the search gets to them (a
    caller taking the first match starts none past it), at most SLOW_WAIT_S for all of one search: one
    that has not answered by then is skipped and its root is never checked again this run (never a
    wait for the network timeout, no new stuck thread per search). No shutil (it loads bz2 and lzma)."""
    if not os.path.splitext(cmd)[1]:
        cmd += ".exe"
    check = _is_file
    fixed = {}  # root -> a fixed disk (one GetDriveTypeW each)
    pending = []  # (file, root, probe) of the folders off the fixed disks since the last match, in PATH order
    deadline = 0.0
    for d in (os.environ.get("PATH") or "").split(os.pathsep) if folders is None else folders:
        drive = os.path.splitdrive(d)[0] if d else ""
        slow = drive[1:2] != ":"  # (a UNC root, either slash)
        if not drive or not (slow or os.path.isabs(d)):
            continue
        f = os.path.join(d, cmd)
        root = os.path.normcase(drive)
        if not slow:
            if root not in fixed:
                from . import native
                fixed[root] = native.is_fixed_drive(drive + "\\")
            slow = not fixed[root]
        if not slow:
            if check(f):
                yield from _answered(pending, deadline)
                yield f
        elif root not in _unanswered:
            if not deadline:
                import time
                deadline = time.monotonic() + SLOW_WAIT_S
            pending.append((f, root, _probe(f, check)))
    yield from _answered(pending, deadline)


def _answered(pending: list, deadline: float):
    """The files of `pending` whose probe answered yes by `deadline`, in order; the roots of those that
    did not answer go to _unanswered. Empties `pending`."""
    import time
    for f, root, probe in pending:
        if probe[0].acquire(timeout=max(0.0, deadline - time.monotonic())):
            if probe[1]:
                yield f
        elif len(_unanswered) < _UNANSWERED_MAX:
            _unanswered.add(root)
    pending.clear()


def _probe(f: str, check) -> list:
    """check(f) on a thread: [lock released once answered, answer]. No thread to be had: answered no."""
    import _thread  # (not threading: commands load no threading)
    lock = _thread.allocate_lock()
    lock.acquire()
    box = [lock, False]

    def run():
        try:
            box[1] = check(f)
        finally:
            lock.release()
    try:
        _thread.start_new_thread(run, ())
    except RuntimeError:  # (can't start new thread)
        lock.release()
    return box


def on_path(cmd: str, folders: list[str] | None = None) -> str | None:
    """The first `cmd` of on_path_all, or None."""
    return next(on_path_all(cmd, folders), None)


def same_command(got, want: str) -> bool:
    """`got` (a stored command line or shortcut arguments) runs what `want` does: paths in any letter
    case (abspath(__file__) keeps the case the repo path was typed in), the flags exactly. Not a
    string (a Run value of another registry type): not the same."""
    return got == want or (isinstance(got, str) and os.path.normcase(got) == os.path.normcase(want)
                           and f" {' '.join(FLAGS)} " in f" {got} ")


def pythonw_command() -> str:
    """The full path of the running Python's pythonw.exe (no pythonw.exe there: sys.executable), never a
    bare "pythonw": no PATH decides which Python runs."""
    own = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "pythonw.exe")
    return own if os.path.exists(own) else sys.executable


def windowless() -> bool:
    """This Python has a pythonw.exe (or is one): what Start With Windows needs where .pyw files open
    with no Python."""
    here = os.path.dirname(os.path.abspath(sys.executable))
    return os.path.exists(os.path.join(here, "pythonw.exe")) or \
        os.path.basename(sys.executable).lower() == "pythonw.exe"


def app_command() -> list[str]:
    """The command that runs the app (no console window)."""
    return [pythonw_command(), *FLAGS, LAUNCHER]


def launcher_args() -> str:
    """The arguments after the Python executable, as one string (shortcuts with a Python target)."""
    return f'{" ".join(FLAGS)} "{LAUNCHER}"'


def command_line() -> str:
    """app_command() as one command line (what `run start` runs; `run status` prints it)."""
    exe = pythonw_command()
    if " " in exe:
        exe = f'"{exe}"'
    return f"{exe} {launcher_args()}"


def can_manage() -> bool:
    from .paths import is_test_mode  # (here: `run status` never loads paths, json and re)
    return not is_test_mode()


# ---------------------------------------------------------------- the Startup-folder shortcut

PYW_PROGRAMS = ("pyw.exe", "pythonw.exe")  # what a working .pyw association opens the file with
ASSOCF_INIT_IGNOREUNKNOWN, ASSOCSTR_EXECUTABLE = 0x400, 2
_assoc = None  # AssocQueryStringW, declared on first use


def pyw_program() -> str:
    """The program the .pyw file association runs ("" when there is none)."""
    global _assoc
    import ctypes
    from . import native as N
    if _assoc is None:
        _assoc = N._f(N._load("shlwapi.dll"), "AssocQueryStringW", ctypes.c_long, N.DWORD, N.DWORD,
                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(N.DWORD))
    n = N.DWORD(32768)
    buf = ctypes.create_unicode_buffer(n.value)
    if _assoc(ASSOCF_INIT_IGNOREUNKNOWN, ASSOCSTR_EXECUTABLE, ".pyw", None, buf, ctypes.byref(n)) != 0:
        return ""
    return buf.value


def pyw_opens() -> bool:
    """.pyw files open with a Python here: the association's program is the Python launcher pyw.exe
    or a pythonw.exe, and it exists."""
    exe = pyw_program()
    return os.path.basename(exe).lower() in PYW_PROGRAMS and os.path.isfile(exe)


def target() -> tuple[str, str]:
    """(target, arguments) of the Startup shortcut: DisplayDesktop.pyw by full path with no
    arguments when .pyw files open with a Python here, else this Python's pythonw.exe by full path
    with the launch flags. Neither (a console python.exe would open a console window at every
    sign-in): OSError."""
    if pyw_opens():
        return LAUNCHER, ""
    if windowless():
        return pythonw_command(), launcher_args()
    raise OSError("no Python opens .pyw files and this Python has no pythonw.exe")


def path() -> str:
    """The Startup-folder shortcut (OSError: Windows names no Startup folder)."""
    from . import shortcut
    return os.path.join(shortcut.folder(CSIDL_STARTUP), LNK)


def exists() -> bool:
    try:
        return os.path.isfile(path())
    except OSError:
        return False


def link() -> dict[str, str] | None:
    """What the Startup shortcut holds (shortcut.read_link), None when there is none or it can't be read."""
    from . import shortcut
    try:
        return shortcut.read_link(path())
    except OSError:
        return None


def _value(key: str, name: str):
    """The data of value `name` under HKCU\\`key`, None when there is no such value (no data: b"")."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value = winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None
    return b"" if value is None else value


def _delete(key: str, name: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:  # (no key or no value)
        pass


def _turned_off(key: str, name: str) -> bool:
    """StartupApproved: a first byte that is odd (01 from the Startup apps page, 03/07 from others)
    = off; even, or no value = on."""
    v = _value(key, name)
    return isinstance(v, bytes) and len(v) > 0 and v[0] & 1 == 1


def is_enabled() -> bool:
    return exists() and not _turned_off(APPROVED_FOLDER, LNK)


def has_run_value() -> bool:
    """Older versions' HKCU Run value is there (refresh() moves it to the shortcut)."""
    return _value(RUN_KEY, VALUE_NAME) is not None


def state(want: tuple[str, str] | None = None) -> str:
    """"off", "on", or "outdated" (on, but the shortcut no longer opens this copy as target() would).
    `want`: target() when the caller has it already."""
    if not is_enabled():
        return "off"
    from . import shortcut
    try:
        want = target() if want is None else want
    except OSError:
        return "on"  # (nothing better to write)
    return "on" if shortcut.is_current(want, path()) else "outdated"


def _drop_run() -> None:
    _delete(RUN_KEY, VALUE_NAME)
    _delete(APPROVED_RUN, VALUE_NAME)


def set_enabled(enabled: bool, want: tuple[str, str] | None = None) -> bool:
    """Writes (or rewrites) or deletes the Startup shortcut, then deletes its StartupApproved value
    (on: clears an off mark of the Startup apps page) and older versions' Run value. Returns the
    resulting state. On without a target() (nothing that runs without a console window): refused,
    nothing changed. The shortcut can't be written or deleted: OSError. `want`: as for state()."""
    if not can_manage():
        return is_enabled()
    from . import shortcut
    lnk = path()
    if enabled:
        try:
            want = target() if want is None else want
        except OSError:
            return is_enabled()
        if not (shortcut.is_current(want, lnk) or shortcut.create(want, lnk)):
            raise OSError("The Startup folder shortcut could not be written.")
    elif not shortcut.remove(lnk):
        raise OSError("The Startup folder shortcut could not be deleted.")
    _delete(APPROVED_FOLDER, LNK)
    _drop_run()
    return is_enabled()


def refresh() -> None:
    """At launch. Older versions' Run value: not turned off on the Startup apps page → the shortcut is
    made; the Run value and its StartupApproved value are deleted either way (kept only when the
    shortcut can't be made). Then a shortcut that is on but outdated (folder moved, association
    gone or back) is rewritten. Never raises."""
    if not can_manage():
        return
    try:
        if has_run_value() and not _turned_off(APPROVED_RUN, VALUE_NAME):
            set_enabled(True)  # (no target, or not written: the Run value stays)
            return
        _drop_run()
        if is_enabled() and state() == "outdated":
            from . import shortcut
            shortcut.create(target(), path())
    except OSError:
        pass
