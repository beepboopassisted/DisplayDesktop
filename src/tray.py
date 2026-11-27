"""Tray icon and menu, global shortcuts, the 600 ms sync timer and the slide animation.

All hidden windows are restored on Exit, on sign-out/shutdown (WM_ENDSESSION), on the quit event
(`--quit`, run.cmd Stop) and on any unhandled error.
"""
from __future__ import annotations

import ctypes
import importlib
import os
import sys
import threading
from ctypes import Structure, byref, c_byte, c_wchar, sizeof

from . import __version__
from . import gui as G
from . import native as N
from . import console, crypto, datafolder, gpu, paths, settings, shortcut, startup
from . import strings as L
from .app import QUIT_EVENT_NAME, SetEvent
from .desktop_manager import DesktopManager
from .layout import LOCKED, NOT_READABLE, NOT_READ_YET
from .paths import is_test_mode
from . import tray_menu as TM
from .slide import SlideAnimator
from .testing import window_api

# The Overview, Settings and the dialogs are imported on first use (their code and memory only once
# opened): `_ui(name)` imports, `_loaded(name)` never does (None: never opened, so nothing to close)
_UI = {"Overview": "overview", "SettingsWindow": "settings_window", "RestoreWindow": "restore_window",
       "RecoveryWindow": "restore_window", "KeyWindow": "restore_window"}


def _ui(name: str):
    return getattr(importlib.import_module(f"{__package__}.{_UI[name]}"), name)


def _loaded(name: str):
    module = sys.modules.get(f"{__package__}.{_UI[name]}")
    return getattr(module, name, None) if module is not None else None


def __getattr__(name: str):  # tray.Overview, tray.SettingsWindow ... (as if imported here)
    if name in _UI:
        return _ui(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _overview_open() -> bool:
    ov = _loaded("Overview")
    return ov is not None and ov.is_open()


def _close_open(name: str) -> None:
    cls = _loaded(name)
    if cls is not None:
        cls.close_open()

HK_PREV, HK_NEXT, HK_OVERVIEW = 1, 2, 5
HOTKEY_IDS = (HK_PREV, HK_NEXT, HK_OVERVIEW)

WM_TRAY = G.WM_APP + 1
WM_APP_QUIT = G.WM_APP + 2
WM_APP_COMMAND = G.WM_APP + 3
WM_APP_PROBLEM = G.WM_APP + 4  # wp: PROBLEM_* (posted: the dialog opens from the message loop)
PROBLEM_UNREACHABLE, PROBLEM_SAVE_FAILED, PROBLEM_DAMAGED = 1, 2, 3
WM_DISPLAYCHANGE = 0x007E
WM_APP_KEY = G.WM_APP + 5  # an encrypted file no known key opens: the key popup (posted)
WM_APP_UNREADABLE = G.WM_APP + 6  # a data file can't be read at all: Recovery, Quit or Recreate (posted)
WM_APP_RECOVERY = G.WM_APP + 7  # Recovery on request (`run recovery` posts it: cli.RECOVERY_MESSAGE)


class StartCancelled(Exception):
    """The key popup at start was cancelled: the app ends before anything is hidden."""


def _with_default(folder: str) -> list[str]:
    """`folder`, then the default folder (a fallback journal may be there) unless it is the same or
    in test mode."""
    default = datafolder.default_folder()
    return [folder] if is_test_mode() or datafolder.same(default, folder) else [folder, default]


def unlock_at_start() -> bool:
    """Encrypt Settings: the folder in use (or the default folder, where a fallback journal may be)
    holds encrypted files: the key remembered on this PC opens them, or the key popup asks (modal:
    before anything is read or hidden). False: cancelled. No encrypted files: nothing to do."""
    folder = paths.data_dir()
    locked = [f for f in _with_default(folder) if crypto.folder_encrypted(f)]
    if not locked:
        return True
    stored = crypto.stored_key()
    if stored and crypto.unlocks(locked[0], stored):
        crypto.use(stored, crypto.folder_encrypted(folder))
        return True

    def tried(key: str) -> bool:
        if not crypto.unlocks(locked[0], key):
            return False
        crypto.use(key, crypto.folder_encrypted(folder))
        if stored:
            crypto.remember(key)  # (Remember Key was on: the right key is remembered now)
        return True
    win = _ui("KeyWindow").show(datafolder.shown(locked[0]), tried)
    if win is None:
        return False
    G.run_until(lambda: not win.hwnd)
    return win.accepted


def recovery_report(folder: str, problem: str = "", skip=(), journals=None):
    """Recovery's report (recovery.build) for `folder`, the folder in use."""
    from . import recovery
    return recovery.build(folder, problem, skip, journals)


def resolve_unreadable_files(folder: str, icon: int = 0, skip: set | None = None, forced: bool = False,
                             restorer=None, problem: str = "", journals=None, on_key=None, on_folder=None,
                             can_quit: bool = True) -> bool:
    """Recovery, modal: for settings files in `folder` (and, for the app's own folder, the default
    folder's) that can't be read at all (damaged, junk, or of a newer format; never expected: a hacked
    file, or an older version installed again, from a backup or on a new computer), or on request
    (`forced`: `--recovery`, before anything is read or hidden). The report says what is wrong with
    each file (a newer version's: "check for updates", a link); the user chooses: Quit (returns
    False), Recreate Files (they are deleted and start fresh: True), another settings folder (its
    Apply checks it first; then Recovery again, for that folder), or, with no such files,
    Continue (True). Never converted back nor repaired. `restorer`: what Restore Hidden Windows uses
    (a DesktopManager, or a StartRestorer before one exists); `problem`, `journals`, `on_key`,
    `on_folder` (the tray's folder_changed; None before the tray runs): see recovery.build and
    RecoveryWindow. No such files and not `forced`: True, nothing asked. `skip` (normcased paths)
    holds the files never asked about again this run (added here): one Recreate could not delete
    (read as damaged and written fresh by its own writer), and those of a question that could not be
    shown (no screen: the app goes on, never Quits unasked). `can_quit` False (opened from Settings):
    Close in Quit's place, so the answer is never Quit."""
    skip = set() if skip is None else skip
    found = hold_unreadable_files(folder, skip)
    if not found and not forced:
        _release_kept(folder)  # (replaced or gone since it was found: nothing waits)
        return True
    if found:
        console.log("Settings files that can't be read: " + ", ".join(found))
    report = recovery_report(folder, problem, skip, journals)
    from .restore_window import ask_recovery
    on_restore = (lambda: _ui("RestoreWindow").show(restorer, icon)) if restorer is not None else None
    answer = ask_recovery(report, icon, on_restore=on_restore, on_key=on_key, on_folder=on_folder,
                          can_quit=can_quit)
    if answer == "folder":  # (moved to another folder, checked first: its files can be read)
        _release_kept(folder)  # (the old folder's files that can't be read are left as they are)
        console.log("Recovery: another settings folder chosen.")
        return resolve_unreadable_files(datafolder.current(), icon, skip, True, restorer, "", journals, on_key,
                                        on_folder, can_quit)
    if answer is None and found:
        console.log("Files that can't be read: Recovery could not be shown; read as damaged.")
        skip.update(os.path.normcase(p) for p in found)
    if answer == "quit":
        console.log("Recovery: Quit chosen.")
        return False  # (still kept: the app ends without writing over them)
    if answer != "recreate":
        _release_kept(folder)  # (Continue, Try Key, or not shown: written fresh by their writers)
        return True
    _release_kept(folder)  # (before rewriting: kept files are never written; also one found meanwhile)
    recreate_files(folder, found, skip)
    console.log("Recovery: settings files recreated.")
    return True


def recreate_files(folder: str, bad=(), skip: set | None = None) -> None:
    """Recovery's Recreate Files: every settings file of the folder is removed and made new. One
    that can be read is written again at once with what it holds (nothing is lost); one that can't
    (`bad`, or failing now) is removed, and its writer makes it new from what the app knows. The crash
    record is never touched. A file that can't be removed (held, read-only) goes in `skip` (read as
    damaged, never asked about again)."""
    bad_set = {os.path.normcase(p) for p in bad}
    for name in paths.ASKED_FILES:
        p = os.path.join(folder, name)
        if not os.path.exists(p):
            continue  # (none yet: made by its writer when needed)
        data = None
        if os.path.normcase(p) not in bad_set:
            try:
                data = paths.read_json(p, ask=False)
            except (OSError, ValueError, RecursionError):
                data = None  # (missing, or can't be read: removed, made new by its writer)
        try:
            os.remove(p)
        except FileNotFoundError:
            continue
        except OSError:
            if skip is not None:
                skip.add(os.path.normcase(p))
            continue
        if data is not None:
            paths.write_json(p, data)


def _release_kept(folder: str) -> None:
    """Answered: no file of the folders asked about is kept any longer."""
    folders = _with_default(folder)
    paths.kept.difference_update([k for k in paths.kept
                                  if any(datafolder.same(os.path.dirname(k), f) for f in folders)])


def hold_unreadable_files(folder: str, skip: set) -> list[str]:
    """The files resolve_unreadable_files asks about (not in `skip`), kept from now on
    (`paths.kept`: never written over) until the answer. Only the folder in use: another folder's
    settings files (the default folder's, left behind by a move) are never read, so never asked
    about (a move away from them would otherwise bring the same question back)."""
    found = [p for p in paths.unreadable_files(folder) if os.path.normcase(p) not in skip]
    paths.kept.update(os.path.normcase(p) for p in found)
    return found


WM_TEST_CRASH = G.WM_APP + 99  # test mode only: raise an error to prove the crash path restores windows
WM_TEST_NEW_DESKTOP = G.WM_APP + 98  # test mode only: the overview's "+" on the monitor under the cursor
TEST_FAIL_START_ENV = "DISPLAYDESKTOP_TEST_FAIL_START"  # test mode only: fail right after the first sync
SYNC_TIMER = 1
SYNC_INTERVAL_MS = 600

ID_OVERVIEW, ID_LOAD, ID_SAVE, ID_CLEAR, ID_SETTINGS, ID_EXIT, ID_CONSOLE, \
    ID_PREV, ID_NEXT = range(1, 10)

# ---------------------------------------------------------------- tray icon API


class NOTIFYICONDATAW(Structure):
    _fields_ = [("cbSize", N.DWORD), ("hWnd", N.H), ("uID", N.UINT), ("uFlags", N.UINT),
                ("uCallbackMessage", N.UINT), ("hIcon", N.H), ("szTip", c_wchar * 128),
                ("dwState", N.DWORD), ("dwStateMask", N.DWORD), ("szInfo", c_wchar * 256),
                ("uVersion", N.UINT), ("szInfoTitle", c_wchar * 64), ("dwInfoFlags", N.DWORD),
                ("guidItem", c_byte * 16), ("hBalloonIcon", N.H)]


_Shell_NotifyIconW = N._f(N.shell32, "Shell_NotifyIconW", N.BOOL, N.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING = 1, 2

_CreateEventW = N._f(N.kernel32, "CreateEventW", N.H, ctypes.c_void_p, N.BOOL, N.BOOL, ctypes.c_wchar_p)
_WaitForMultipleObjects = N._f(N.kernel32, "WaitForMultipleObjects", N.DWORD, N.DWORD,
                               ctypes.POINTER(N.H), N.BOOL, N.DWORD)
INFINITE = 0xFFFFFFFF


def create_app_icon() -> int:
    """32x32 icon: two blue rectangles (two desktops side by side)."""
    w = h = 32
    hbm, bits = G.create_dib(w, h)
    px = (ctypes.c_uint32 * (w * h)).from_address(bits)
    for y in range(h):
        for x in range(w):
            if 6 <= y < 26 and 2 <= x < 15:
                px[y * w + x] = 0xFF000000 | (120 << 8) | 215
            elif 6 <= y < 26 and 17 <= x < 30:
                px[y * w + x] = 0xFF000000 | (90 << 16) | (200 << 8) | 250
    mask = G.CreateBitmap(w, h, 1, 1, None)
    ii = G.ICONINFO(True, 0, 0, mask, hbm)
    hicon = G.CreateIconIndirect(byref(ii))
    G.DeleteObject(hbm)
    G.DeleteObject(mask)
    return hicon


class TrayApp(G.Window):
    def __init__(self, recovery: bool = False):
        """`recovery` (`--recovery`, `run recovery`, or Exit's "Start in Recovery next time" the last
        time: one start only): Recovery first, before anything is hidden."""
        super().__init__()
        self.exiting = False   # set first thing when the app ends: no more syncs, switches or commands
        self._options_owed = False  # a folder move's options wait until its settings can be read
        self._ended = False    # exit() ran
        self._icon_added = False
        self._menu_painter: TM.MenuPainter | None = None  # set while the tray menu is open
        self._fatal = False
        self.thread_error: BaseException | None = None  # a background thread's error (told at its quit)
        self._asking_key = False  # the key popup is open (or posted)
        self._asking_unreadable = False  # the can't-be-read question is open (or posted)
        self._pending_problem = 0  # a PROBLEM_* that came while it was: told in it, or right after it
        self._unreadable_skip: set[str] = set()  # files never asked about again (resolve_unreadable_files)
        console.capture_output()
        # Encrypted settings (Encrypt Settings): the key first. Nothing can be read without it (the
        # settings, the crash journal): Cancel ends the app before anything is hidden
        if not unlock_at_start():
            raise StartCancelled()
        # Settings files that can't be read at all (damaged, junk, a newer version's; never expected), or
        # asked for: Recovery (Quit, or deleted and started fresh) before anything reads them
        from .desktop_manager import StartRestorer
        go_on = resolve_unreadable_files(paths.data_dir(), skip=self._unreadable_skip, forced=recovery,
                                         restorer=StartRestorer(window_api()))
        _close_open("RestoreWindow")  # (one opened from it works without the manager: never left open)
        if not go_on:
            self._recover_crashed_run()
            raise StartCancelled()
        startup.refresh()  # an enabled but outdated startup entry is rewritten for this copy
        done = shortcut.ensure()  # Start menu entry: added if missing, rewritten if outdated
        if done:
            console.log(f"Start menu entry {done}.")
        self.mgr = DesktopManager(win=window_api())
        # From here on windows can be hidden (the first sync puts windows back on their desktops), and
        # main() only knows this app once __init__ returns: a failure must show them again itself
        try:
            self._start()
        except BaseException:
            self._abort_start()
            raise

    def _recover_crashed_run(self) -> None:
        """Quit in Recovery at start, before anything runs: a crashed run's windows (its journals)
        are still shown first (the manager's start reads them), then it ends at once, hiding nothing."""
        try:
            DesktopManager(win=window_api()).end()
        except Exception as exc:  # noqa: BLE001 - the quit goes on; the next start reads the journals
            console.log(f"Recovery Quit: crashed run's windows not shown ({type(exc).__name__}: {exc}).")

    def _abort_start(self) -> None:
        """Startup failed after windows may have been hidden: show them all again, stop the quit
        watcher (it would otherwise keep waiting on the shared quit event while the error box is
        open, and could take a `run stop` meant for a new copy) and any slide the first sync started,
        remove the tray icon and the window (main() then reports the error)."""
        for step in (self.mgr.end,
                     lambda: setattr(paths, "on_write_failed", None),
                     lambda: setattr(crypto, "on_locked", None),
                     lambda: setattr(paths, "on_unreadable", None),
                     lambda: getattr(self, "_stop_watch", None) and SetEvent(self._stop_watch),
                     lambda: getattr(self, "animator", None) and self.animator.dispose(),
                     lambda: self.kill_timer(SYNC_TIMER),
                     lambda: self._icon_added and self._remove_tray_icon(), self.destroy):
            try:
                step()
            except BaseException:  # noqa: BLE001 - showing the windows again comes first
                pass

    def _start(self) -> None:
        # Settings held at start (a sync client, an antivirus): the defaults now, the stored options
        # once they can be read (_on_sync_timer)
        self._options_owed = not settings.readable()
        # The GPU setting first: the first sync can already switch desktops (and slide)
        gpu.set_enabled(settings.get("UseGpu"))  # Settings > Use GPU Acceleration (on by default)
        # Everything on_message uses must exist before the window does: CreateWindowEx already
        # delivers messages to it.
        self._taskbar_created = G.RegisterWindowMessageW("TaskbarCreated")
        G.set_fatal_handler(self.on_fatal)
        self.create("DisplayDesktop.Main", 0, N.WS_POPUP, 0, 0, 0, 0, title="DisplayDesktop")
        # A failed save in the settings folder is told to the user (posted: the dialog opens from
        # the message loop). Set before the first sync, whose places record is the first write.
        paths.on_write_failed = self._write_failed
        crypto.on_locked = self._key_needed  # (an encrypted file no known key opens: the key popup)
        paths.on_unreadable = self._unreadable_found  # (a file found unreadable later: Quit or Recreate Files)
        self.animator = SlideAnimator()
        self._icon = create_app_icon()
        self._add_tray_icon()

        self.mgr.switch_starting.append(self._on_switch_starting)
        self.mgr.desktop_switched.append(self._on_switched)
        self.mgr.edge_reached.append(self._on_edge)

        # `--quit` and run.cmd Stop set this named event to close us cleanly
        from .app import shared_attributes  # (an elevated copy's is set by a normal console too)
        self._quit_event = _CreateEventW(shared_attributes(), False, False, QUIT_EVENT_NAME)
        self._stop_watch = _CreateEventW(None, True, False, None)
        self._watcher = threading.Thread(target=self._watch_quit, name="quit-watcher", daemon=True)
        self._watcher.start()

        self._register_hotkeys()
        self.mgr.sync()
        if self.exiting:
            return  # an error during the first sync (e.g. in a slide) already ended the app
        if is_test_mode() and os.environ.get(TEST_FAIL_START_ENV):
            # test mode only: proves a failed start shows the windows the first sync hid again
            raise RuntimeError("test: start failed after the first sync")
        self.set_timer(SYNC_TIMER, SYNC_INTERVAL_MS, self._on_sync_timer)
        v = sys.version_info
        console.log(f"DisplayDesktop {__version__} started (PID {os.getpid()}, Python {v.major}.{v.minor}.{v.micro}).")
        console.log(f"Animations run on {gpu.describe()}.")
        if console.load_enabled():
            console.open_console()
        # The settings folder: one that can't be reached, or a journal that can't be read, is told to
        # the user (Recovery)
        if datafolder.unreachable:
            G.PostMessageW(self.hwnd, WM_APP_PROBLEM, PROBLEM_UNREACHABLE, 0)
        elif self.mgr.recovery_problem:
            G.PostMessageW(self.hwnd, WM_APP_PROBLEM, PROBLEM_DAMAGED, 0)

    # ------------------------------------------------------------ files that can't be read at all

    def _unreadable_found(self, path: str) -> bool:
        """paths.on_unreadable: a data file that can't be read at all was read (replaced by hand, another
        folder's): the question, once at a time (posted: reads happen anywhere). Returns whether a
        question covers the file (then kept, never written over, until the answer); not covered (a
        journal of another name, a skipped file, exiting, another folder while one is open): the
        reader treats it as damaged itself."""
        if self.exiting or not self.hwnd or \
                os.path.normcase(path) in self._unreadable_skip or os.path.basename(path) not in paths.ASKED_FILES:
            return False
        folder = os.path.dirname(path)
        if self._asking_unreadable:
            if not any(datafolder.same(folder, f) for f in _with_default(self._unreadable_folder)):
                return False
        else:
            self._asking_unreadable = True
            self._unreadable_folder = folder
            G.PostMessageW(self.hwnd, WM_APP_UNREADABLE, 0, 0)
        paths.kept.add(os.path.normcase(path))
        return True

    def ask_unreadable(self, problem: str = "", forced: bool = False, can_quit: bool = True) -> None:
        """Recovery, modal, for files that can't be read at all (posted by _unreadable_found, or
        from open_recovery): Recreate Files → read afresh; Quit → the app ends (`can_quit` False,
        from Settings: Close instead, the app goes on)."""
        self._asking_unreadable = True
        try:
            if self.exiting:
                return  # (Quit was chosen already, e.g. by a folder move's own question)
            if not problem and self._pending_problem:  # (a folder problem that came first: told here)
                problem, self._pending_problem = self.problem_text(self._pending_problem), 0
            _close_open("RecoveryWindow")  # (one Recovery at a time: this one says it all)
            go_on = resolve_unreadable_files(getattr(self, "_unreadable_folder", None) or datafolder.current(),
                                             self._icon, self._unreadable_skip, forced, self.mgr, problem,
                                             self.mgr.journal_problems,
                                             on_key=lambda key: self.key_entered(datafolder.current(), key),
                                             on_folder=self.folder_changed, can_quit=can_quit)
            if self.exiting:
                return  # (the app ended while it was open: nothing more to do)
            if go_on:
                self.reread()  # (recreated: what could not be read starts fresh now)
            else:
                G.PostMessageW(self.hwnd, WM_APP_QUIT, 0, 0)  # Quit (every window shown first)
        finally:
            self._asking_unreadable = False
            pending, self._pending_problem = self._pending_problem, 0
            if pending and not self.exiting:  # (came while it was open: told now)
                G.PostMessageW(self.hwnd, WM_APP_PROBLEM, pending, 0)

    def open_recovery(self, kind: int = 0, forced: bool = False, can_quit: bool = True) -> None:
        """Recovery for a folder problem (PROBLEM_*), a crash record that can't be read, or on
        request (`forced`: `run recovery`, Settings' Recovery button). Files that can't be read at all
        make it the modal choice (ask_unreadable); otherwise it stays open beside the app (Continue
        closes it). `can_quit` False (from Settings): Close instead of Quit, it never ends the app."""
        if self.exiting:
            return
        if self._asking_unreadable:  # (one is open or posted: a folder problem is told in it or after it)
            self._pending_problem = kind or self._pending_problem
            return
        problem = self.problem_text(kind) if kind else ""
        if problem:
            console.log(problem.replace("\n\n", " ").replace("\n", " "))
        folder = datafolder.current()
        report = recovery_report(folder, problem, self._unreadable_skip, self.mgr.journal_problems)
        if report.unreadable:
            self._unreadable_folder = folder
            self.ask_unreadable(problem, forced, can_quit)
            return
        _ui("RecoveryWindow").show(report, self._icon, on_folder=self.folder_changed,
                                   on_restore=self.open_restore,
                                   on_key=lambda key: self.key_entered(folder, key),
                                   on_done=lambda result: self._recovery_done(result, can_quit),
                                   can_quit=can_quit, on_clear=lambda: self.run_command(ID_CLEAR))

    def _recovery_done(self, result: str, can_quit: bool = True) -> None:
        if self.exiting:
            return
        if result == "quit" and can_quit:
            G.PostMessageW(self.hwnd, WM_APP_QUIT, 0, 0)  # Quit (every window shown first)
        elif result == "recreate":
            self.recreate_all()
        elif result == "folder":  # (Recovery again, for the folder moved to; wParam 1: from Settings)
            G.PostMessageW(self.hwnd, WM_APP_RECOVERY, 0 if can_quit else 1, 0)

    # ------------------------------------------------------------ settings folder problems

    def _write_failed(self, folder: str) -> None:
        if not self.exiting and datafolder.same(folder, datafolder.current()):
            G.PostMessageW(self.hwnd, WM_APP_PROBLEM, PROBLEM_SAVE_FAILED, 0)

    def problem_text(self, kind: int) -> str:
        if kind == PROBLEM_UNREACHABLE:
            return L.f("problem.unreachable", datafolder.shown(datafolder.unreachable or datafolder.current()))
        if kind == PROBLEM_SAVE_FAILED:
            return L.f("problem.saveFailed", datafolder.shown(datafolder.current()))
        return L.t("problem.damaged")

    # ------------------------------------------------------------ Encrypt Settings

    def _key_needed(self, path: str) -> None:
        """crypto.on_locked: a file is ours (encrypted) and no known key opens it: the key popup, once
        at a time (posted: reads happen anywhere, even inside a message box's loop)."""
        if self.exiting or self._asking_key or not self.hwnd:
            return
        self._asking_key = True
        self._locked_folder = os.path.dirname(path)
        G.PostMessageW(self.hwnd, WM_APP_KEY, 0, 0)

    def ask_key(self, folder: str | None = None) -> None:
        if self.exiting:
            return
        folder = folder or getattr(self, "_locked_folder", None) or datafolder.current()
        self._asking_key = True
        shown = _ui("KeyWindow").show(datafolder.shown(folder), lambda key: self.key_entered(folder, key), self._icon,
                                      on_closed=lambda accepted: setattr(self, "_asking_key", False))
        if shown is None:
            self._asking_key = False

    def key_entered(self, folder: str, key: str) -> bool:
        """A key typed (the popup, Try Key): whether it opens the folder's encrypted files. If it
        does, it is tried on every file from now on; for the folder in use it becomes the key (its
        files are encrypted with it: new ones are too), and what could not be read is read now."""
        if not crypto.unlocks(folder, key):
            return False
        crypto.add_key(key)
        if datafolder.same(folder, datafolder.current()):
            crypto.use(key, True)
            if crypto.remembered():
                crypto.remember(key)
        console.log("Encryption key accepted.")
        self.reread()
        return True

    def recreate_all(self) -> None:
        """Recovery's Recreate Files with nothing wrong: every settings file of the folder in use is
        removed and made new with what it holds (recreate_files)."""
        if self.exiting or self._asking_unreadable:
            return
        recreate_files(datafolder.current(), skip=self._unreadable_skip)
        console.log("Recovery: settings files recreated.")
        self.reread()

    def reread(self) -> None:
        """Files that could not be read (encrypted, the key typed only now) are read again at once."""
        paths._read_next.clear()  # (no throttle: read at once)
        settings.retry()
        self._options_owed = True  # (the options follow the settings just read, at the next sync)
        try:
            self.mgr.reread()
        except Exception as exc:  # noqa: BLE001 - read again on its own schedule otherwise
            console.log(f"Reading again failed ({type(exc).__name__}: {exc}).")
        window = getattr(_loaded("SettingsWindow"), "current", None)
        if window is not None and window.hwnd:
            window.refresh_options()

    def set_encryption(self, key: str | None, on: bool, remember: bool) -> bool:
        """Settings > Encrypt Settings: every data file rewritten encrypted with `key` (on; also a new
        key) or plain (off). The key is remembered on this PC or not (Remember Key)."""
        folder = datafolder.current()
        before = (crypto.key(), crypto.enabled)
        crypto.use(key if on else crypto.key(), on)
        failed = crypto.recode_folder(folder)
        if failed and len(failed) == len([p for p in crypto._data_files(folder) if os.path.exists(p)]):
            crypto.use(*before)  # (nothing could be rewritten: as it was)
            G.message_box(L.f("msg.encryptFail", "\n".join(os.path.basename(p) for p in failed)),
                          G.MB_OK | G.MB_ICONWARNING)
            return False
        if failed:
            G.message_box(L.f("msg.encryptFail", "\n".join(os.path.basename(p) for p in failed)),
                          G.MB_OK | G.MB_ICONWARNING)
        crypto.remember(key if on and remember else None)
        console.log(f"Encrypt Settings: {'on' if on else 'off'}.")
        return True

    def set_remember(self, on: bool) -> bool:
        """Settings > Remember Key: the key in use stored on this PC (protected by Windows) or not."""
        done = crypto.remember(crypto.key() if on else None)
        console.log(f"Remember Key: {'on' if on and done else 'off'}.")
        return on if done else not on

    def folder_changed(self, folder: str) -> bool:
        """Settings > Settings Folder moved the data (datafolder.move_to): the manager's files point
        there, the settings still known are written there if they could not be copied, and the
        settings that act at once follow the folder's (its own data may have been chosen). Returns
        whether the manager's journal is written there (move_to keeps the old one otherwise)."""
        # Encrypt Settings follows the folder's files: encrypted there (ours copied, or its own) or not
        crypto.use(crypto.key(), crypto.folder_encrypted(folder))
        # Its files that can't be read at all: kept (never written over) before the manager moves there,
        # asked about after (on Quit the manager's files are the new folder's); one question only (the
        # manager's reads are covered by it, none is posted)
        was_asking, self._asking_unreadable = self._asking_unreadable, True
        was_folder, self._unreadable_folder = getattr(self, "_unreadable_folder", None), folder
        go_on = True
        try:
            try:
                hold_unreadable_files(folder, self._unreadable_skip)
            except Exception as exc:  # noqa: BLE001 - the move is done: the rest must still follow it
                console.log(f"Settings folder: its files could not be checked ({type(exc).__name__}: {exc}).")
            try:
                written = self.mgr.relocate(folder)
            except Exception as exc:  # noqa: BLE001 - the move is done; the manager keeps going
                console.log(f"Settings folder: switching files failed ({type(exc).__name__}: {exc}).")
                written = False
            try:
                go_on = resolve_unreadable_files(folder, self._icon, self._unreadable_skip, restorer=self.mgr,
                                                 journals=self.mgr.journal_problems)
            except Exception as exc:  # noqa: BLE001 - never Quit for an error: the app goes on
                console.log(f"Settings folder: Recovery failed ({type(exc).__name__}: {exc}).")
        finally:
            self._asking_unreadable = was_asking  # (a question posted before for another folder: still its)
            self._unreadable_folder = was_folder if was_asking else folder
        if not go_on:
            G.PostMessageW(self.hwnd, WM_APP_QUIT, 0, 0)  # (Quit: the move is done; the app ends, windows shown)
            return written
        if datafolder.kept_new:
            settings.forget()  # that folder's own settings: read afresh (an unwritten old change must not win)
        elif datafolder.settings_not_copied:
            settings.write_if_missing()  # (the old folder could not be read: what is known is kept)
        if self.mgr.recovery_problem:  # (a damaged journal a crashed run left in that folder)
            G.PostMessageW(self.hwnd, WM_APP_PROBLEM, PROBLEM_DAMAGED, 0)
        if not settings.readable():
            # Its settings can't be read now (held a moment): the options are applied once they can
            # be (_sync_tick), never from defaults (the setters store what they set: it would erase
            # the folder's)
            console.log("Settings folder: its settings could not be read yet; options applied once they can be.")
            self._options_owed = True
            return written
        self._options_owed = False
        self._apply_options()
        return written

    def _apply_options(self) -> None:
        """The settings that act at once follow the stored ones (after a folder move)."""
        for step in (lambda: gpu.set_enabled(settings.get("UseGpu")),
                     lambda: shortcut.set_enabled(settings.get("StartMenu")),
                     lambda: console.is_open() != settings.get("ShowConsole")
                     and console.set_enabled(settings.get("ShowConsole"))):
            try:
                step()
            except Exception:  # noqa: BLE001 - the move itself is done
                pass
        window = getattr(_loaded("SettingsWindow"), "current", None)
        if window is not None and window.hwnd:
            window.refresh_options()

    def open_restore(self) -> None:
        if not self.exiting:
            _ui("RestoreWindow").show(self.mgr, self._icon)

    # ------------------------------------------------------------ quit event

    def _watch_quit(self) -> None:
        handles = (N.H * 2)(self._quit_event, self._stop_watch)
        if _WaitForMultipleObjects(2, handles, False, INFINITE) == 0:
            G.PostMessageW(self.hwnd, WM_APP_QUIT, 0, 0)

    # ------------------------------------------------------------ tray icon

    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        return nid

    def _add_tray_icon(self) -> None:
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        nid.szTip = L.t("tray.tooltip")[:127]
        _Shell_NotifyIconW(NIM_ADD, byref(nid))
        self._icon_added = True

    def _remove_tray_icon(self) -> None:
        _Shell_NotifyIconW(NIM_DELETE, byref(self._nid()))

    def balloon(self, text: str, warning: bool = False) -> None:
        nid = self._nid()
        nid.uFlags = NIF_INFO
        nid.szInfo = text[:255]
        nid.szInfoTitle = "DisplayDesktop"
        nid.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        _Shell_NotifyIconW(NIM_MODIFY, byref(nid))

    # ------------------------------------------------------------ menu

    @staticmethod
    def menu_items() -> list[TM.Item | None]:
        """The tray menu, top to bottom (None = separator)."""
        keys = L.t("menu.keys")
        return [
            TM.Item(ID_OVERVIEW, L.t("menu.overview"), keys, "up"),
            TM.Item(ID_PREV, L.t("menu.prev"), keys, "left"),
            TM.Item(ID_NEXT, L.t("menu.next"), keys, "right"),
            None,
            TM.Item(ID_LOAD, L.t("menu.load")),
            TM.Item(ID_SAVE, L.t("menu.save")),
            None,
            TM.Item(ID_SETTINGS, L.t("menu.settings")),
            None,
            TM.Item(ID_EXIT, L.t("menu.exit")),
        ]

    def show_menu(self) -> None:
        items = TrayApp.menu_items()
        pos = N.cursor_pos() or (0, 0)
        # Items are owner-drawn (tray_menu.py): WM_MEASUREITEM/WM_DRAWITEM go to self.on_message
        self._menu_painter = TM.MenuPainter(self.hwnd, [it for it in items if it], TM.dpi_at(pos))
        menu = G.CreatePopupMenu()
        try:
            for it in items:
                if it is None:
                    G.AppendMenuW(menu, TM.MF_OWNERDRAW | TM.MF_SEPARATOR, TM.SEPARATOR_ID, None)
                else:
                    G.AppendMenuW(menu, TM.MF_OWNERDRAW, it.id, None)
            G.SetForegroundWindow(self.hwnd)  # so the menu closes when clicking elsewhere
            cmd = G.TrackPopupMenu(menu, G.TPM_RETURNCMD | G.TPM_RIGHTBUTTON | G.TPM_NONOTIFY,
                                   pos[0], pos[1], 0, self.hwnd, None)
            G.PostMessageW(self.hwnd, G.WM_NULL, 0, 0)
        finally:
            G.DestroyMenu(menu)
            self._menu_painter.close()
            self._menu_painter = None
        if cmd:
            self.run_command(cmd)

    def _confirm(self, key: str) -> bool:
        """Yes/No question; "No" is the default."""
        answer = G.message_box(L.t(key), G.MB_YESNO | G.MB_ICONQUESTION | G.MB_DEFBUTTON2)
        return answer == G.IDYES and not self.exiting

    def run_command(self, cmd: int) -> None:
        if self.exiting:
            return
        if cmd in (ID_LOAD, ID_SAVE, ID_CLEAR) and getattr(self, "_asking_unreadable", False):
            # (Recovery waits for its answer: files it is about are never written meanwhile)
            self.balloon(L.t("msg.recoveryOpen"), warning=True)
            return
        if cmd == ID_OVERVIEW:
            _ui("Overview").toggle(self.mgr)
        elif cmd == ID_PREV:
            self.mgr.switch_relative(-1)
        elif cmd == ID_NEXT:
            self.mgr.switch_relative(+1)
        elif cmd == ID_LOAD:
            if self._confirm("msg.loadConfirm"):  # rearranges every window
                result = self.mgr.load_desktops()
                if result == NOT_READ_YET:  # layout.json there but held
                    console.log("Load not done: the saved desktops could not be read yet.")
                    self.balloon(L.t("msg.loadBusy"), warning=True)
                elif result == NOT_READABLE:
                    console.log("Load not done: the saved desktops can't be read.")
                    self.balloon(L.t("msg.loadUnreadable"), warning=True)
                elif result == LOCKED:
                    console.log("Load not done: the saved desktops are encrypted with another key.")
                    self.balloon(L.t("msg.loadLocked"), warning=True)
                    self.ask_key(datafolder.current())
                elif isinstance(result, int) and result < 0:  # its folder offline
                    console.log("Load not done: the settings folder can't be reached.")
                    self.balloon(L.t("msg.loadFailed"), warning=True)
                else:
                    console.log("Loaded saved desktops.")
        elif cmd == ID_SAVE:
            if self._confirm("msg.saveConfirm"):  # replaces what was saved before
                count = self.mgr.save_desktops()
                if count == NOT_READ_YET:  # layout.json there but held: writing would drop part of it
                    console.log("Save not done: the saved desktops could not be read yet.")
                    self.balloon(L.t("msg.saveBusy"), warning=True)
                elif count == NOT_READABLE:
                    console.log("Save not done: the saved desktops can't be read.")
                    self.balloon(L.t("msg.saveUnreadable"), warning=True)
                elif count == LOCKED:
                    console.log("Save not done: the saved desktops are encrypted with another key.")
                    self.balloon(L.t("msg.saveLocked"), warning=True)
                    self.ask_key(datafolder.current())
                elif count < 0:  # layout.json could not be written
                    console.log("Save failed: the settings folder could not be written.")
                    self.balloon(L.t("msg.saveFailed"), warning=True)
                else:
                    console.log(f"Saved desktops ({count} windows).")
                    self.balloon(L.f("msg.saved", count))
        elif cmd == ID_CLEAR:
            if self._confirm("msg.clearConfirm"):  # can't be undone: the saved layout is erased
                self.mgr.clear_desktops()
                console.log("Cleared desktops: one desktop per monitor, saved layout deleted.")
        elif cmd == ID_SETTINGS:
            _ui("SettingsWindow").show(self._icon, on_recovery=lambda: self.open_recovery(forced=True, can_quit=False),
                                       on_folder=self.folder_changed,
                                       on_encrypt=self.set_encryption, on_remember=self.set_remember)
        elif cmd == ID_CONSOLE:  # no menu item (it is in Settings): a test-mode command for live tests
            want = not console.is_open()
            if console.set_enabled(want) != want:
                self.balloon(L.t("msg.consoleFail"), warning=True)
        elif cmd == ID_EXIT:
            if self._confirm("msg.exitConfirm"):
                self.exit()

    # ------------------------------------------------------------ hotkeys

    def _register_hotkeys(self) -> None:
        failed = []

        def reg(hid: int, mods: int, vk: int, label: str) -> None:
            if not N.RegisterHotKey(self.hwnd, hid, mods | N.MOD_NOREPEAT, vk):
                failed.append(label)

        ca = N.MOD_CONTROL | N.MOD_ALT
        reg(HK_PREV, ca, N.VK_LEFT, "Ctrl+Alt+←")
        reg(HK_NEXT, ca, N.VK_RIGHT, "Ctrl+Alt+→")
        reg(HK_OVERVIEW, ca, N.VK_UP, "Ctrl+Alt+↑")
        if failed:
            console.log("Shortcuts not available (another app may be using them): " + ", ".join(failed))
            self.balloon(L.t("msg.hotkeyFail") + ", ".join(failed), warning=True)

    def on_hotkey(self, hid: int) -> None:
        if hid == HK_PREV:
            self.mgr.switch_relative(-1)
        elif hid == HK_NEXT:
            self.mgr.switch_relative(+1)
        elif hid == HK_OVERVIEW:
            _ui("Overview").toggle(self.mgr)

    # ------------------------------------------------------------ manager events

    def _on_switch_starting(self, device: str, old: int, new: int) -> None:
        if not _overview_open():  # no animation while the overview is open
            self.animator.begin(device, 1 if new > old else -1)

    def _on_edge(self, device: str, direction: int) -> None:
        if not _overview_open():
            self.animator.bounce(device, direction)
        console.log(f"No desktop further {'right' if direction > 0 else 'left'} (add one in the overview).")

    def _on_switched(self, info) -> None:
        self.animator.commit(info.device)
        console.log(f"Monitor {info.ordinal}: desktop {info.local_index + 1} of {info.local_count} "
                    f"(Desktop {info.global_number}).")

    def _on_sync_timer(self) -> None:
        if console.closed_by_user():
            console.save_enabled(False)  # closed from its own window: the toggle turns off
        if not self.exiting and not _overview_open():
            self.mgr.sync()
            settings.flush()  # (a settings change the folder could not take yet)
            gpu.warm_due(idle=self.animator.active_count == 0)  # (a lost GPU capture opened again)
            if self.mgr.late_problem:  # (a crash journal read late was damaged, or stays unreadable)
                self.mgr.late_problem = False
                G.PostMessageW(self.hwnd, WM_APP_PROBLEM, PROBLEM_DAMAGED, 0)
            if self._options_owed and settings.readable():  # (a moved-to folder's, unreadable at the move)
                self._options_owed = False
                self._apply_options()

    # ------------------------------------------------------------ messages

    def on_message(self, msg, wp, lp):
        if msg == WM_DISPLAYCHANGE:
            N.displays_changed()  # (a monitor handle may now name another display: forget the mapping)
            gpu.displays_changed()  # (each monitor's GPU asked again; lost captures reopened when idle)
            return None  # (the ONLY WM_DISPLAYCHANGE handling: add any other here, not below)
        if msg in (TM.WM_MEASUREITEM, TM.WM_DRAWITEM):
            painter = getattr(self, "_menu_painter", None)
            if painter is not None:
                done = painter.measure(lp) if msg == TM.WM_MEASUREITEM else painter.draw(lp)
                return 1 if done else None
            return None
        if msg == N.WM_HOTKEY:
            if not self.exiting:
                self.on_hotkey(wp)
            return 0
        if msg == WM_TRAY:
            event = G.loword(lp)
            if event == G.WM_LBUTTONDBLCLK and not self.exiting:
                _ui("Overview").toggle(self.mgr)
            elif event == G.WM_RBUTTONUP and not self.exiting:
                self.show_menu()
            return 0
        if msg == WM_APP_QUIT:
            if self.thread_error is not None:
                self.on_fatal(self.thread_error)  # (a background thread's error: told like any other)
            else:
                self.exit()
            return 0
        if msg == WM_APP_PROBLEM:
            self.open_recovery(wp)
            return 0
        if msg == WM_APP_KEY:
            self.ask_key()
            return 0
        if msg == WM_APP_UNREADABLE:
            self.ask_unreadable()
            return 0
        if msg == WM_APP_RECOVERY:
            self.open_recovery(forced=True, can_quit=wp != 1)  # (wParam 1: reopened for Settings)
            return 0
        if msg == WM_APP_COMMAND and is_test_mode():
            # A tray menu command by ID, posted by tests to drive the menu without clicking. Test mode
            # only: otherwise any program could open dialogs or toggle the console.
            self.run_command(wp)
            return 0
        if msg == WM_TEST_NEW_DESKTOP and is_test_mode():
            dev = N.monitor_device_under_cursor()
            if dev is not None and not self.exiting:
                self.mgr.create_desktop_and_switch(dev)
            return 0
        if msg == WM_TEST_CRASH and is_test_mode():
            raise RuntimeError("test crash")
        if msg == G.WM_QUERYENDSESSION:
            return 1
        if msg == G.WM_ENDSESSION:
            # Sign-out, shutdown or Restart Manager: one last snapshot, then bring hidden windows back
            # before the process ends
            if wp:
                self.exit()
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            self._add_tray_icon()  # Explorer restarted
            return 0
        if msg == G.WM_CLOSE:
            return 0  # only Exit / --quit / sign-out close the app
        return None

    # ------------------------------------------------------------ exit and errors

    def on_fatal(self, exc: BaseException) -> None:
        """Any unhandled error: stop everything, record and restore the windows, tell the user, exit.
        Nothing runs while the error message is open (its message loop would otherwise still deliver
        the sync timer and the shortcut keys: a sync would merge the restored windows into the shown
        desktops and record that, and a shortcut could hide windows again)."""
        if self._fatal:
            return
        self._fatal = True
        self._stop_and_restore()
        if not self.mgr.ended:
            # The error came from inside an ending already under way (e.g. a window's WM_DESTROY while
            # exit() closes it), before it took the snapshot and showed the windows: do that now,
            # before telling the user they are back
            # (the snapshot is written once the windows are shown: a dead share can't hold them up)
            for step in (lambda: self.mgr.snapshot(defer=True), self.mgr.end, self.mgr.write_snapshot):
                try:
                    step()
                except BaseException:  # noqa: BLE001 - restoring the windows comes first
                    pass
        try:
            import traceback
            console.log("Unexpected error (all hidden windows restored):\n"
                        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        except BaseException:  # noqa: BLE001
            pass
        try:
            G.message_box(L.f("msg.crash", L.error_text(exc)), G.MB_OK | G.MB_ICONERROR)
        except BaseException:  # noqa: BLE001
            pass
        self.exit()

    def _stop_and_restore(self) -> None:
        """The first part of every ending: stop the timer, the quit watcher, the shortcut keys, the
        overview, Settings and the animations; take the last snapshot (the next start puts it all
        back); then show every hidden window. The manager refuses any change after that."""
        if self.exiting:
            return
        self.exiting = True
        for step in (lambda: self.kill_timer(SYNC_TIMER),
                     lambda: SetEvent(self._stop_watch),
                     lambda: [N.UnregisterHotKey(self.hwnd, i) for i in HOTKEY_IDS],
                     # (only what was ever opened: a module never imported has nothing open)
                     lambda: (ov := _loaded("Overview")) is not None and ov.current() and ov.current().close(),
                     lambda: _close_open("SettingsWindow"), lambda: _close_open("RestoreWindow"),
                     lambda: _close_open("RecoveryWindow"), lambda: _close_open("KeyWindow"),
                     lambda: setattr(paths, "on_write_failed", None),
                     lambda: setattr(crypto, "on_locked", None),
                     lambda: setattr(paths, "on_unreadable", None),
                     self.animator.dispose,
                     lambda: self.mgr.snapshot(defer=True),  # (taken now, written once the windows are shown)
                     self.mgr.end,
                     self.mgr.write_snapshot):
            try:
                step()
            except BaseException:  # noqa: BLE001 - restoring the windows comes first
                pass

    def exit(self) -> None:
        """Ends the app: one last snapshot of where every window and desktop is (the next start
        puts it all back), then every hidden window is shown."""
        if self._ended:
            return
        self._ended = True
        self._stop_and_restore()
        steps = [
            lambda: console.log("Exiting: all hidden windows restored; windows and desktops are put "
                                "back at the next start."),
            lambda: console.close_console(app_exiting=True),
            self._remove_tray_icon,
            lambda: N.destroy_icon(self._icon),
            self.mgr.snapshots.clear,
            self.destroy,
        ]
        for step in steps:
            try:
                step()
            except BaseException:  # noqa: BLE001 - finish shutting down whatever happens
                pass
        G.PostQuitMessage(0)
