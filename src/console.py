"""The "Show Console" toggle: a console window with what the app does (switches, errors ...).

The console is NOT attached to this process. Closing an attached console ends its process at once,
which here would leave the windows of other desktops hidden. Instead the console belongs to a small
separate viewer process (python.exe) that only prints what it receives through a pipe:

- closing the console window (or Ctrl+C in it) ends only the viewer; the app keeps running and the
  toggle turns itself off;
- the app never waits for the viewer: lines go to a queue that a background thread writes to the
  pipe, so a console that stops reading (text selected in it) cannot stall the app;
- the last lines are kept in memory, so a console opened later starts with recent history;
- when the app exits, the viewer says so and stays open until Enter is pressed (errors stay
  readable).

The choice is kept in settings.json ("ShowConsole", settings.py) and reapplied at the next start.
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time

from . import settings, startup
from .paths import is_test_mode

HISTORY_LINES = 500
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
TITLE = "DisplayDesktop console"

# Runs in the viewer process: prints the raw UTF-8 it reads from stdin (the pipe) to its own console.
# A trailing NUL = the app closes the console on purpose (toggle off): go away at once. End of the
# pipe without it = the app exited: say so and wait for Enter (argv[2] == "wait"; not in test runs,
# whose viewer has no window to press Enter in).
VIEWER_CODE = r"""
import ctypes, sys
k = ctypes.WinDLL("kernel32", use_last_error=True)
title, codepage = k["SetConsoleTitleW"], k["SetConsoleOutputCP"]
title.argtypes, title.restype = [ctypes.c_wchar_p], ctypes.c_int
codepage.argtypes, codepage.restype = [ctypes.c_uint], ctypes.c_int
title(sys.argv[1])
codepage(65001)
quiet = False
try:
    out = open("CONOUT$", "wb", buffering=0)
    while True:
        b = sys.stdin.buffer.read1(65536)
        if not b:
            break
        if b.endswith(b"\x00"):
            out.write(b[:-1])
            quiet = True
            break
        out.write(b)
    if not quiet and sys.argv[2] == "wait":
        out.write(b"\n[DisplayDesktop has exited. Press Enter to close this window.]\n")
        open("CONIN$").readline()
except (KeyboardInterrupt, OSError):
    pass
"""

_history: collections.deque[str] = collections.deque(maxlen=HISTORY_LINES)
_lock = threading.Lock()
_viewer: "_Viewer | None" = None
_partial = {"out": "", "err": ""}


# ---------------------------------------------------------------- settings

def load_enabled() -> bool:
    return settings.get("ShowConsole")


def save_enabled(enabled: bool) -> None:
    settings.set("ShowConsole", enabled)


def set_enabled(enabled: bool) -> bool:
    """The "Show Console" setting: opens or closes the console and remembers the choice. Returns
    whether it is open now (False when it was asked to open but python.exe was not found)."""
    if enabled:
        if not open_console():
            return False
        save_enabled(True)
        return True
    close_console()
    save_enabled(False)
    return False


# ---------------------------------------------------------------- log

def log(message: str) -> None:
    """One timestamped line. Never raises, never blocks."""
    try:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with _lock:
            _history.append(line)
            v = _viewer
        if v is not None:
            v.send(line + "\n")
    except Exception:  # noqa: BLE001
        pass


class _Tee:
    """Replaces sys.stdout/sys.stderr: complete lines go to the log, and everything still goes to
    the original stream when there is one (python.exe runs)."""

    def __init__(self, original, key: str):
        self._original = original
        self._key = key

    def write(self, s: str) -> int:
        if self._original is not None:
            try:
                self._original.write(s)
            except Exception:  # noqa: BLE001
                pass
        try:
            text = _partial[self._key] + s
            *lines, _partial[self._key] = text.split("\n")
            for line in lines:
                log(line.rstrip("\r"))
        except Exception:  # noqa: BLE001
            pass
        return len(s)

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False


def capture_output() -> None:
    """At app start: route print() and error tracebacks into the log."""
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout, "out")
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr, "err")


# ---------------------------------------------------------------- viewer process

def _python_exe() -> str | None:
    """python.exe (console) of the Python running now; else the first python.exe in a PATH folder that
    runs a Python (startup.on_path_all's folders: a drive or UNC root; a WindowsApps alias is tried: a
    Store Python's works, the install stub doesn't)."""
    own = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "python.exe")
    if os.path.isfile(own):
        return own
    for found in startup.on_path_all("python.exe"):
        if "\\windowsapps\\" not in os.path.normcase(found) or _alias_runs(found):
            return found
    return None


def _alias_runs(path: str) -> bool:
    """Whether a WindowsApps alias runs a Python (tried once a run, at most 0.5 s)."""
    key = os.path.normcase(path)
    if key not in _alias_works:
        import subprocess
        try:
            _alias_works[key] = subprocess.run(
                [path, "-c", ""], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=0.5, creationflags=0x08000000).returncode == 0
        except subprocess.TimeoutExpired:
            # (still starting: a real Python, cold; the install stub answers at once)
            _alias_works[key] = True
        except (OSError, subprocess.SubprocessError):
            _alias_works[key] = False
    return _alias_works[key]


_alias_works: dict[str, bool] = {}  # (a WindowsApps python alias: runs a Python or not, once a run)


class _Viewer:
    def __init__(self):
        import queue  # (with subprocess: loaded only once a console opens)
        import subprocess
        exe = _python_exe()
        if exe is None:
            raise OSError("python.exe not found")
        # Test runs must never pop a window: the viewer then has a console without one
        test = is_test_mode()
        self.proc = subprocess.Popen([exe, *startup.FLAGS, "-c", VIEWER_CODE, TITLE, "nowait" if test else "wait"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True,
                                     creationflags=CREATE_NO_WINDOW if test else CREATE_NEW_CONSOLE)
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._pump, name="console-writer", daemon=True)
        self._thread.start()

    def send(self, text: str) -> None:
        self._queue.put(text.replace("\x00", "").encode("utf-8", "replace"))

    def _pump(self) -> None:
        pipe = self.proc.stdin
        try:
            while True:
                data = self._queue.get()
                if data is None:
                    break
                pipe.write(data)
                pipe.flush()
        except (OSError, ValueError):
            pass  # the viewer is gone (console closed): nothing to do, the app keeps running
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self, app_exiting: bool) -> None:
        """app_exiting: the viewer shows "DisplayDesktop has exited" and waits for Enter.
        Otherwise (toggle off) it closes right away."""
        if not app_exiting:
            self._queue.put(b"\x00")  # the viewer reads a trailing NUL as "close quietly"
        self._queue.put(None)
        self._thread.join(1.0)  # let the last lines out (bounded: never holds up the app)
        if not app_exiting and self.alive():
            import subprocess
            try:
                self.proc.wait(1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()  # the viewer, never the app


def is_open() -> bool:
    with _lock:
        v = _viewer
    return v is not None and v.alive()


def open_console() -> bool:
    """Opens the console with recent history. Returns True if it is open."""
    global _viewer
    if is_open():
        return True
    try:
        v = _Viewer()
    except OSError:
        return False
    with _lock:
        if _history:
            v.send("".join(line + "\n" for line in _history))  # one write, not one per line
        _viewer = v
    return True


def close_console(app_exiting: bool = False) -> None:
    global _viewer
    with _lock:
        v, _viewer = _viewer, None
    if v is not None:
        v.close(app_exiting)


def closed_by_user() -> bool:
    """True once if the console was closed from its own window (the toggle then turns off)."""
    global _viewer
    with _lock:
        v = _viewer
        if v is None or v.alive():
            return False
        _viewer = None
    v.close(app_exiting=True)  # ends its writer thread; the process is already gone
    return True
