"""Start with Windows: the HKCU Run value "DisplayDesktop" = pythonw -B "<this folder>\\DisplayDesktop.pyw".

Nothing here depends on where Python or the repo is: the repo folder comes from this file's location,
and Python is "pythonw" when the pythonw.exe on PATH is the Python running now (otherwise its full
path). The tray item and `run.cmd enable-startup` / `disable-startup` all use this module. The
registry is the only record of the choice. A copy in test mode (DISPLAYDESKTOP_DATA set) never touches
the registry.
"""
from __future__ import annotations

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DisplayDesktop"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO_DIR, "DisplayDesktop.pyw")


def which(cmd: str) -> str | None:
    """shutil.which(cmd) of Python 3.10 on Windows for a program name (the current folder first, then
    PATH, each with every PATHEXT extension), without importing shutil (it loads bz2 and lzma)."""
    path = os.environ.get("PATH", None)
    if path is None:
        path = os.defpath
    if not path:
        return None
    dirs = path.split(os.pathsep)
    if os.curdir not in dirs:
        dirs.insert(0, os.curdir)
    pathext = [e for e in (os.getenv("PATHEXT") or ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC").split(os.pathsep) if e]
    low = cmd.lower()
    files = [cmd] if any(low.endswith(e.lower()) for e in pathext) else [cmd + e for e in pathext]
    seen = set()
    for d in dirs:
        nd = os.path.normcase(d)
        if nd not in seen:
            seen.add(nd)
            for f in files:
                name = os.path.join(d, f)
                if os.path.exists(name) and os.access(name, os.F_OK | os.X_OK) and not os.path.isdir(name):
                    return name
    return None


def pythonw_command() -> str:
    """How to start pythonw.exe of the Python running now: plain "pythonw" when that is what PATH
    finds, otherwise the full path (so a different Python on PATH is never picked up)."""
    here = os.path.dirname(os.path.abspath(sys.executable))
    own = os.path.join(here, "pythonw.exe")
    found = which("pythonw")
    if found and os.path.normcase(os.path.dirname(os.path.abspath(found))) == os.path.normcase(here):
        return "pythonw"
    return own if os.path.exists(own) else sys.executable


def windowless() -> bool:
    """This Python has a pythonw.exe (or is one): what Start With Windows needs."""
    here = os.path.dirname(os.path.abspath(sys.executable))
    return os.path.exists(os.path.join(here, "pythonw.exe")) or \
        os.path.basename(sys.executable).lower() == "pythonw.exe"


def app_command() -> list[str]:
    """The command that runs the app (no console window)."""
    return [pythonw_command(), "-B", LAUNCHER]


def startup_command() -> str:
    """app_command() as one command line for the Run value."""
    exe = pythonw_command()
    if " " in exe:
        exe = f'"{exe}"'
    return f'{exe} -B "{LAUNCHER}"'


def can_manage() -> bool:
    from .paths import is_test_mode  # (here: `run status` never loads paths, json and re)
    return not is_test_mode()


def _read(name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def is_enabled() -> bool:
    return _read(VALUE_NAME) is not None


def state(command: str | None = None) -> str:
    """"off", "on", or "outdated" (the entry exists but no longer matches this copy). `command`:
    startup_command() when the caller has it already (each one searches PATH: ~2 ms)."""
    value = _read(VALUE_NAME)
    if value is None:
        return "off"
    return "on" if value == (startup_command() if command is None else command) else "outdated"


def set_enabled(enabled: bool, command: str | None = None) -> bool:
    """Adds or removes the Run value. Returns the resulting state. `command`: as for state()."""
    if not can_manage():
        return is_enabled()
    if enabled and not windowless():
        return is_enabled()  # (a console python.exe would open a console window at every sign-in)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                                  startup_command() if command is None else command)
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass
    return is_enabled()


def refresh() -> None:
    """At launch: an enabled entry that is outdated (folder moved, other Python) is rewritten for
    this copy. A disabled one stays disabled."""
    if not can_manage():
        return
    value = _read(VALUE_NAME)
    if value is not None and windowless():  # (off: no PATH search; no pythonw here: left as it is)
        command = startup_command()  # (once: it searches PATH)
        if value != command:  # (outdated)
            set_enabled(True, command)
