"""Where the saved settings and desktops live (the data folder): display.desktop.settings.json,
.layout.json, .places.json, .showing.json and .crash.json (paths.DATA_FILES). The "display.desktop."
prefix keeps them apart from other programs' files in an ordinary folder; files with the older names
(settings.json ...) are renamed when a run starts (`settle`).

The default is %LOCALAPPDATA%\\DisplayDesktop. Settings > Settings Folder can move them elsewhere;
the chosen folder is kept in the registry (HKCU\\Software\\DisplayDesktop, value DataFolder), not
in a file, so the old folder can be left empty and removed. Moving copies DisplayDesktop's own
files to the new folder (replacing the DisplayDesktop files already there), switches to it, then
deletes them from the old folder, and removes the old folder only if nothing else is left in it.

A run keeps one folder from start to end (`settle`), so its files never end up split between two.
A chosen folder that can't be reached at start (`unreachable`) or stops working while the app runs
(`paths.failed`) is reported to the user (tray.py), who can choose another one.
A copy in test mode (DISPLAYDESKTOP_DATA set) never reads or changes the registry value, and never
falls back to the real folder.
"""
from __future__ import annotations

import json
import os
import re
import winreg

from . import paths
from .paths import DATA_ENV, DATA_FILES, DIR_NAME, JOURNAL_FILE, PREFIX, is_test_mode, local_appdata

KEY = r"Software\DisplayDesktop"
VALUE_NAME = "DataFolder"
LEFT_JOURNAL = "LeftJournal"  # path of our journal a move kept in the old folder (read by the next start)
FILES = DATA_FILES
JOURNAL = JOURNAL_FILE
MOVED = tuple(n for n in FILES if n != JOURNAL_FILE)  # what a move copies: never a crash journal (the app
# reads the new folder's own journal first, then writes its own there: DesktopManager.relocate)
STAGED = ".moving"   # a file copied by move_to, until every copy is made
settings_not_copied = False  # the last move kept the current data but could not read it (old folder failed)
kept_new = False  # the last move kept the new folder's own data
left_working = True  # the folder the last move left could still be read and written
BACKUP = ".replaced"  # a file of the new folder that move_to replaces, until the move is done
_UNKNOWN_VARIABLE = re.compile(r"%[^%\\/]+%")  # a %NAME% (expanded by `expand` when it is known)

_in_use: str | None = None  # the folder this run uses: fixed at first use, changed only by move_to
unreachable: str | None = None  # the chosen folder this run could not reach at start (default used)


def default_folder() -> str:
    return os.path.join(local_appdata(), DIR_NAME)


def _full_path(folder: str) -> bool:
    """A drive or network path (D:\\x, \\\\server\\share\\x), not one relative to the current folder
    or drive (\\x passes os.path.isabs)."""
    return bool(folder) and bool(os.path.splitdrive(folder)[0]) and os.path.isabs(folder)


def chosen_folder() -> str | None:
    """The folder chosen in Settings (None: the default)."""
    if is_test_mode():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value = winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None
    return value if isinstance(value, str) and _full_path(value) else None


def current() -> str:
    """The data folder in use: the test folder, else the one this run settled on, else the chosen
    one, else the default."""
    return os.environ.get(DATA_ENV) or _in_use or chosen_folder() or default_folder()


def _migrate(folder: str) -> None:
    """Files an older version wrote without the prefix get their new names (never over a newer
    file). Only in the default folder: the only one older versions wrote to (in a chosen folder a
    settings.json may well be another program's). Journals are not renamed: `journals` reads the
    older names too (then they are deleted)."""
    for name in MOVED:
        old, new = os.path.join(folder, name[len(PREFIX):]), os.path.join(folder, name)
        try:
            if os.path.isfile(old) and not os.path.exists(new):
                os.replace(old, new)
        except OSError:
            pass


def settle() -> str:
    """The folder this run uses, made if missing (paths.data_dir). Fixed at first use, so every file
    of a run stays in one folder even if a chosen folder's drive comes and goes. A chosen folder
    that cannot be made (its drive is gone) gives the default folder for this run and is kept in
    `unreachable` (the user is told). A test copy never falls back: it must never touch the real
    files."""
    global _in_use, unreachable
    test = os.environ.get(DATA_ENV)
    if test:
        os.makedirs(test, exist_ok=True)
        return test
    if _in_use is not None:
        return _in_use  # (no disk access: an unreachable network folder would block every call)
    folder = chosen_folder() or default_folder()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        if same(folder, default_folder()):
            raise
        unreachable = folder
        paths.offline(folder)  # (its drive checked once: marked offline only if the drive is not there)
        folder = default_folder()
        os.makedirs(folder, exist_ok=True)
    if same(folder, default_folder()):
        _migrate(folder)
    _in_use = folder
    return folder


def journals(folder: str) -> list[str]:
    """The crash journals a start reads: the one in `folder`, and in the default folder too
    (a run that could not reach the chosen folder kept it there); in the default folder also the
    names older versions used (paths.OLD_JOURNALS: only ever written there or in a test folder)."""
    found = folder_journals(folder)
    if is_test_mode():
        return found + [os.path.join(folder, "hidden.json")]
    if not same(folder, default_folder()):
        found += _set_aside(default_folder()) + [os.path.join(default_folder(), JOURNAL)]
    if unreachable:
        # The chosen folder this run could not reach: a crashed run's journal there is read once it
        # is back (offline: read later), and by later starts too (registered until it is gone)
        add_left_journal(os.path.join(unreachable, JOURNAL))
    kept = []
    for left in _left_journals():  # (kept by moves in the folders they left: forgotten once gone)
        # (offline first: it remembers an unreachable share, so it is probed once, not three times)
        if paths.offline(os.path.dirname(left)) or os.path.exists(left):
            kept.append(left)  # (recovery deletes it after reading it; its folder offline: read later)
            found.append(left)
    if kept != _left_journals():
        _store_left(kept)
    return list(dict.fromkeys(found + [os.path.join(default_folder(), n) for n in paths.OLD_JOURNALS]))


def folder_journals(folder: str) -> list[str]:
    """The journals one folder may hold: crashed runs' set aside (oldest first), then its own."""
    return _set_aside(folder) + [os.path.join(folder, JOURNAL)]


def _set_aside(folder: str) -> list[str]:
    """Crashed runs' journals moved aside (not readable when found: DesktopManager), oldest first."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = [n for n in names if n.startswith(JOURNAL) and n.endswith(paths.LATER_SUFFIX)]
    found.sort(key=lambda n: _mtime(os.path.join(folder, n)))
    return [os.path.join(folder, n) for n in found]


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _uptime_ms() -> int:
    from . import native
    return native.tick_count64()


BOOT_ID_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"


def _boot_id() -> str:
    """Windows' boot counter (one more at every boot; "" if it can't be read). No clock involved."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BOOT_ID_KEY) as key:
            value = winreg.QueryValueEx(key, "BootId")[0]
    except OSError:
        return ""
    return str(value) if isinstance(value, int) else ""


def _left_journals() -> list[str]:
    """Our journals kept in folders moves left (the new folder had none of ours yet), of this
    Windows session only: each is stored with Windows' boot counter and the uptime it was registered
    at; one from another boot (another counter, or an uptime greater than now's) can't match any
    window: dropped, so an unplugged drive is not looked for at every start. The clock is never used:
    a clock set in the meantime must not drop one. Test mode never touches the registry."""
    if is_test_mode():
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value = winreg.QueryValueEx(key, LEFT_JOURNAL)[0]
    except OSError:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    now, boot, found, entries, stale = _uptime_ms(), _boot_id(), [], [], False
    for v in value:
        if not isinstance(v, str):
            continue
        path, *rest = v.split("|")
        when = rest[0] if rest else ""
        their_boot = rest[1] if len(rest) > 1 else ""
        if not path or not (when.isascii() and when.isdigit()) or int(when) > now \
                or (boot and their_boot and their_boot != boot):
            stale = True  # (an earlier boot, or no uptime recorded)
            continue
        if _full_path(path):
            found.append(path)
            entries.append(v)
        else:
            stale = True  # (not a full path: dropped)
    if stale:
        _store_entries(entries)
    return found


def _raw_left() -> list[str]:
    """The stored `path|uptime` entries as they are."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value = winreg.QueryValueEx(key, LEFT_JOURNAL)[0]
    except OSError:
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _store_entries(entries: list[str]) -> None:
    if is_test_mode():
        return
    try:
        if entries:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, LEFT_JOURNAL, 0, winreg.REG_MULTI_SZ, list(dict.fromkeys(entries)))
        else:
            _set_value(LEFT_JOURNAL, None)
    except OSError:
        pass


def _store_left(paths_: list[str]) -> None:
    """Keeps exactly these (with the uptime each was registered at; new ones get now's)."""
    if is_test_mode():
        return
    known = {}
    for v in _raw_left():
        known.setdefault(os.path.normcase(v.split("|")[0]), v)
    now, boot = _uptime_ms(), _boot_id()
    _store_entries([known.get(os.path.normcase(p), f"{p}|{now}|{boot}") for p in dict.fromkeys(paths_)])


def add_left_journal(path: str) -> None:
    """A move kept our journal in the folder it left: the next start reads it there."""
    _store_left(_left_journals() + [path])


def forget_left_journal(path: str) -> None:
    """That journal is gone (deleted once ours was written in the new folder)."""
    left = _left_journals()
    if any(same(path, p) for p in left):
        _store_left([p for p in left if not same(path, p)])


ENDED_RUNS = "EndedRuns"  # runs that ended (this boot): `writer|BootId|handles still hidden then`
ENDED_KEEP = 32           # the most recent ones kept


def _ended_entries() -> list[str]:
    """`writer|BootId[|h1,h2...]` entries of this boot (others dropped on the next store)."""
    if is_test_mode():
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value = winreg.QueryValueEx(key, ENDED_RUNS)[0]
    except OSError:
        return []
    boot = _boot_id()
    return [v for v in value if isinstance(v, str) and v.count("|") in (1, 2) and v.split("|")[1] == boot] \
        if isinstance(value, list) and boot else []


def add_ended_run(writer: str, still_hidden: list[int] = ()) -> bool:
    """A run ended, with these of its windows still hidden (it could not show them; normally none):
    a journal of it left anywhere (a folder offline at exit) only ever shows those (the others may
    have been hidden by their own apps since). Recorded again, the entry is replaced. Returns whether
    it is recorded."""
    boot = _boot_id()
    if is_test_mode() or not boot or not writer:
        return False
    hidden = ",".join(str(int(h)) for h in still_hidden)
    entries = [v for v in _ended_entries() if v.split("|")[0] != writer] + [f"{writer}|{boot}|{hidden}"]
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, ENDED_RUNS, 0, winreg.REG_MULTI_SZ, entries[-ENDED_KEEP:])
        return True
    except OSError:
        return False


def ended_run(writer) -> set[int] | None:
    """That run ended in this boot: the windows it left hidden (a journal of it shows only those;
    usually none). None: not an ended run (a crash: its journal shows all it lists)."""
    if not (isinstance(writer, str) and writer):
        return None
    for v in _ended_entries():
        parts = v.split("|")
        if parts[0] == writer:
            hidden = parts[2] if len(parts) > 2 else ""
            return {int(h) for h in hidden.split(",") if h.isascii() and h.isdigit()}
    return None


SHOWN_VARIABLES = ("LOCALAPPDATA", "APPDATA", "USERPROFILE")  # most specific first


def expand(text: str) -> str:
    """A folder as typed: environment variables (%LOCALAPPDATA% ...) expanded. Only %NAME%: a `$` in
    a folder name stays as it is (os.path.expandvars would also expand $NAME and ${NAME})."""
    return _UNKNOWN_VARIABLE.sub(lambda m: os.environ.get(m.group(0)[1:-1], m.group(0)), text.strip())


def shown(folder: str) -> str:
    """A folder as Settings shows it: %LOCALAPPDATA%\\DisplayDesktop rather than C:\\Users\\..."""
    norm = os.path.normpath(folder)
    for var in SHOWN_VARIABLES:
        base = os.environ.get(var)
        if not base:
            continue
        base = os.path.normpath(base)
        if os.path.normcase(norm) == os.path.normcase(base):
            return f"%{var}%"
        if os.path.normcase(norm).startswith(os.path.normcase(base) + "\\"):
            return f"%{var}%" + norm[len(base):]
    return norm


def same(a: str, b: str) -> bool:
    """The same path, written the same way (text only: no disk access, used while typing)."""
    return os.path.normcase(os.path.normpath(expand(a))) == os.path.normcase(os.path.normpath(expand(b)))


def same_on_disk(a: str, b: str) -> bool:
    """The same folder, also when reached another way (a junction or link, a mapped drive and its
    network path, a short 8.3 name, a trailing dot)."""
    if same(a, b):
        return True
    a, b = os.path.normpath(expand(a)), os.path.normpath(expand(b))
    if any(paths.paused_as_gone(p) or paths.offline(p) for p in (a, b)):
        return False  # (offline: not asked of the disk, it would wait for the network timeout)
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def is_default(folder: str | None = None) -> bool:
    return same(folder or current(), default_folder())


def default_in_use() -> bool:
    """The default folder is used and no other folder is chosen (Settings greys Default then). A run
    that could not reach the chosen folder uses the default one but still has a choice to forget."""
    return is_default() and (is_test_mode() or chosen_folder() is None)


def has_data(folder: str) -> bool:
    """`folder` holds DisplayDesktop settings or desktops worth keeping: one of its files (the crash
    journal aside: it only matters to the run that wrote it) reads as a JSON object, or is there but
    can't be read now (held, a cloud file not available offline: never taken as "nothing saved",
    so a move never replaces it unasked). A folder that failed this run counts as having none."""
    folder = os.path.normpath(expand(folder))
    if paths.failed(folder) and same(folder, current()):
        return False  # (the folder in use failed: what it holds can't be read; another one is checked)
    if paths.paused_as_gone(folder) or paths.known_offline(folder) or \
            (paths.failed(folder) and paths.offline(folder)):
        # (offline, or so it seemed a moment ago, or failed earlier and still not answering the quick
        # check: not asked of the disk, it would wait for the network timeout; taken as "may have
        # data", so a move there asks and never replaces it unasked)
        return True
    for name in FILES:
        if name == JOURNAL:
            continue
        from . import crypto
        try:
            with open(os.path.join(folder, name), "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            continue
        except OSError:
            # there but not readable now (held, a cloud file not available ...): data. The error
            # code can't tell (open() errors map most Windows errors to EINVAL), the file's presence
            # can: a name that can't exist has no file
            if os.path.lexists(os.path.join(folder, name)):
                return True
            continue
        if crypto.is_ours(raw):
            return True  # (encrypted (Encrypt Settings): data, whatever key it needs)
        try:
            if isinstance(json.loads(raw.decode("utf-8-sig")), dict):
                return True
        except ValueError:
            continue
    return False


def problem(folder: str) -> str | None:
    """Why `folder` cannot hold the data (None: it can). Environment variables may be used."""
    folder = expand(folder)
    if _UNKNOWN_VARIABLE.search(folder):
        return "Unknown variable in the folder."
    if not _full_path(folder):
        return "Enter a full folder path (for example D:\\DisplayDesktop)."
    if os.path.exists(folder) and not os.path.isdir(folder):
        return "That is a file, not a folder."
    return None


def _store(folder: str | None) -> None:
    """Remembers the chosen folder (None: back to the default, the value is removed, and the key
    too when nothing else is stored in it)."""
    _set_value(VALUE_NAME, folder)


def _set_value(name: str, text: str | None) -> None:
    """A string value under KEY; None removes it (and the key when nothing else is stored in it)."""
    if text is None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
                subkeys, values, _ = winreg.QueryInfoKey(key)
        except OSError:
            return
        if subkeys == 0 and values == 0:  # (DeleteKey would also delete any values left in it)
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)
            except OSError:
                pass
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, text)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _made_levels(folder: str) -> list[str]:
    """The folders os.makedirs(folder) will create, deepest first (to remove them again)."""
    levels, cur = [], os.path.normpath(folder)
    while cur and not os.path.exists(cur):
        levels.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return levels


def _switch(switch, folder: str) -> bool:
    """Runs the app's switch to `folder` (it returns False when it could not write its journal
    there). An error in it must not leave the move half told: the folder has changed by then."""
    if switch is None:
        return True
    try:
        return switch(folder) is not False
    except Exception:  # noqa: BLE001 - logged by the caller's console where it can; the move stands
        import traceback
        traceback.print_exc()
        return False


def unreadable_in(folder: str) -> list[str]:
    """`folder`'s settings files that can't be read at all (Settings Folder's Apply / Default checks the
    folder chosen first: one with any is never switched to). Not the crash journal: a damaged one is
    told and removed when read (Recovery, Restore Hidden Windows), never a reason to refuse."""
    return paths.unreadable_files(os.path.normpath(expand(folder)))


def move_to(new: str, switch=None, keep_new: bool = False) -> str:
    """Moves the data to `new` (the default folder when it is the default). Other files are never
    touched, and only one folder's DisplayDesktop data is kept:
    - keep_new=False (the current data is kept): the new folder then holds exactly the old folder's
      DisplayDesktop files (the same names there are replaced, and DisplayDesktop files there the
      old folder doesn't have are removed: they would otherwise take effect).
    - keep_new=True (the new folder already has data, and the user chose it): nothing is copied;
      the app switches to that folder's settings and saved desktops (Settings asks first when
      both folders have data).
    If anything fails, the new folder is put back as it was (folders made for the move are removed
    again) and the app keeps the old folder. `switch(new)` is called once the files are in place,
    before the old ones are deleted (the app points its files at the new folder there and writes
    what it knows: the journal, places, and whatever the new folder lacks). An old folder that
    can't be read (it failed while running) is neither read nor cleaned up. Returns the new
    folder. Raises OSError."""
    global _in_use, unreachable, kept_new, settings_not_copied, left_working
    if is_test_mode():
        raise OSError("not in test mode")
    reason = problem(new)
    if reason:
        raise OSError(reason)
    new = os.path.normpath(expand(new))  # stored expanded (the registry value is a plain path)
    old = current()
    # (failed: writes there failed; isdir: it may also have gone offline since the last write)
    old_works = not paths.failed(old) and not paths.offline(old) and os.path.isdir(old)  # (offline: no wait)
    if same_on_disk(old, new):
        # nothing to move (also when the same folder is written another way); the choice is still
        # made (Default while a run had to fall back to the default folder forgets the chosen one)
        _store(None if same(new, default_folder()) else new)
        if same(new, default_folder()):
            if unreachable:  # (Default forgot the folder this run could not reach: not looked for again)
                forget_left_journal(os.path.join(unreachable, JOURNAL))
            unreachable = None
        if paths.failed(old):  # the folder works again (the user chose it once more): write it all again
            kept_new, settings_not_copied = False, False
            paths.forget_failures(old)
            _switch(switch, old)
        return new
    made = _made_levels(new)
    staged, replaced, removed, placed = [], [], [], []
    # (the old folder's files that can't be read at all are junk: never copied, nor deleted (a newer
    # version's stays where it is); Recovery moves away from them)
    junk = {n for n in MOVED if paths.junk(os.path.join(old, n))} if old_works else set()
    probe = os.path.join(new, ".displaydesktop-write-test")
    import shutil  # (only for a move: it loads bz2 and lzma)
    try:
        os.makedirs(new, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        for name in MOVED:
            src = os.path.join(old, name)
            if not keep_new and old_works and name not in junk and os.path.isfile(src):
                staged.append(name)
                shutil.copy2(src, os.path.join(new, name + STAGED))
        for name in MOVED:  # the new folder's own files are kept aside until the move is done
            dst = os.path.join(new, name)
            if not keep_new and os.path.isfile(dst):
                os.replace(dst, dst + BACKUP)
                (replaced if name in staged else removed).append(name)
        for name in staged:
            dst = os.path.join(new, name)
            os.replace(dst + STAGED, dst)
            placed.append(name)
        _store(None if same(new, default_folder()) else new)
    except OSError:
        for name in staged:
            _remove(os.path.join(new, name + STAGED))
        for name in placed:
            if name not in replaced:
                _remove(os.path.join(new, name))  # (copied in where nothing was before)
        for name in replaced + removed:  # put the new folder's own files back
            dst = os.path.join(new, name)
            try:
                os.replace(dst + BACKUP, dst)
            except OSError:
                pass
        _remove(probe)
        for level in made:  # deepest first; a folder that isn't empty stays
            try:
                os.rmdir(level)
            except OSError:
                break
        raise
    for name in replaced + removed:
        _remove(os.path.join(new, name + BACKUP))
    settings_not_copied = not keep_new and paths.SETTINGS_FILE not in staged  # (the known ones are written)
    kept_new = keep_new  # (the app then reads that folder's settings afresh: nothing of the old ones wins)
    left_working = old_works  # (a failed old folder is never touched again, not even its journal)
    _in_use = new
    unreachable = None
    paths.forget_failures(new)
    # (no switch: no manager runs yet, e.g. Recovery at start: nothing has read the old journal, so
    # it is kept and registered for the start that follows, never deleted unread)
    journal_written = _switch(switch, new) if switch is not None else False
    if old_works and not same(old, new):
        for name in FILES:
            if name in junk:
                continue  # (left as it is)
            if name == JOURNAL and not journal_written:
                # the new folder has no journal of ours yet: the old one is the only record, and
                # the next start reads it there (a crash before ours is written in the new folder)
                if os.path.isfile(os.path.join(old, name)):
                    add_left_journal(os.path.join(old, name))
                continue
            for path in (os.path.join(old, name), os.path.join(old, name + ".tmp")):
                _remove(path)  # (one in use is left behind: the old folder then stays)
        try:
            if os.path.isdir(old) and not os.listdir(old):
                os.rmdir(old)
        except OSError:
            pass
    return new
