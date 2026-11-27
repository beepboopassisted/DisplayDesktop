"""Where the app keeps its files: %LOCALAPPDATA%\\DisplayDesktop by default, or the folder chosen
in Settings (datafolder.py).

DISPLAYDESKTOP_DATA overrides the folder. It is meant for tests only: a copy started with it never
touches the real files or the Windows startup entry."""
import json
import os
import threading
import time

DATA_ENV = "DISPLAYDESKTOP_DATA"
DIR_NAME = "DisplayDesktop"

# The data files. The prefix keeps them apart from other programs' files when the data folder is
# an ordinary folder (Settings > Settings Folder); older versions used the names without it.
PREFIX = "display.desktop."
SETTINGS_FILE = PREFIX + "settings.json"
LAYOUT_FILE = PREFIX + "layout.json"
PLACES_FILE = PREFIX + "places.json"
SHOWING_FILE = PREFIX + "showing.json"
# The crash journal (windows hidden right now: shown again after a crash). A settings folder belongs
# to one PC (never shared by two), so its name carries no computer name.
JOURNAL_FILE = PREFIX + "crash.json"
LATER_SUFFIX = ".later.json"  # a crashed run's journal not readable at start, moved aside (read later)
OLD_JOURNALS = ("hidden.json",)  # the name older versions used (read once, removed)


def is_journal(name: str) -> bool:
    """A crash journal's file name: ours, one set aside (.later.json), or an older version's."""
    name = name.lower()
    return name.startswith(PREFIX + "crash.") or name.endswith(LATER_SUFFIX) or name in OLD_JOURNALS
DATA_FILES = (SETTINGS_FILE, LAYOUT_FILE, PLACES_FILE, SHOWING_FILE, JOURNAL_FILE)


def is_test_mode() -> bool:
    return bool(os.environ.get(DATA_ENV))


def local_appdata() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")


def data_dir() -> str:
    """The data folder of this run, made if missing (datafolder.settle: one folder per run; a
    chosen folder that cannot be made gives the default one, but never in test mode)."""
    from . import datafolder
    return datafolder.settle()


RETRY_DELAYS_S = (0.05, 0.15)  # waits before a write is tried again: only for a file another program holds
SHARING_ERRORS = (32, 33)      # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION (a sync client, an antivirus)
ERROR_ACCESS_DENIED = 5
PAUSE_GONE_S = 60.0            # a folder that is gone or offline is tried again after this long
PAUSE_REFUSED_S = 30.0         # one that refuses writes (no permission, disk full) after this long
_failed: dict[str, float] = {}  # folder (normcase) -> time.monotonic() when writes there are tried again
_reported: set[str] = set()     # folders whose write failure was reported
on_write_failed = None          # called with the folder when a write there fails for the first time


def _key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(folder))


def failed(folder: str) -> bool:
    """Writes to this folder failed and have not worked since (it may still be gone or offline)."""
    return _key(folder) in _failed


def paused(folder: str) -> bool:
    """Writes (and deletes) in this folder are skipped right now: it failed less than its pause ago."""
    until = _failed.get(_key(folder))  # (one read: a probe thread may drop it meanwhile, _found_again)
    return until is not None and time.monotonic() < until


def note_failure(folder: str, gone: bool) -> None:
    """Another operation (a delete) failed there: the folder is paused like after a failed write."""
    _pause(folder, gone)


def _pause(folder: str, gone: bool, offline_only: bool = False) -> None:
    """Pauses the folder. `offline_only`: only because its share is known offline (no write tried):
    a late answer of the share also takes back its "told" mark, unless it failed for real before."""
    key = _key(folder)
    with _pause_lock:  # (a probe thread's late answer changes these too: _found_again)
        real = key in _failed and key not in _offline_paused
        _failed[key] = time.monotonic() + (PAUSE_GONE_S if gone else PAUSE_REFUSED_S)
        if offline_only and not real:
            _offline_paused.add(key)
        else:
            _offline_paused.discard(key)
        if gone:
            _gone.add(key)
        else:
            _gone.discard(key)


_pause_lock = threading.RLock()  # (re-entrant: write_json holds it around _pause and report)
_gone: set[str] = set()  # folders whose pause is for being gone or offline (not for a refusal)
_offline_paused: set[str] = set()  # folders paused only because their share was known offline (no write tried)


def paused_as_gone(folder: str) -> bool:
    """Paused because it is gone or offline: known without touching the disk."""
    return paused(folder) and _key(folder) in _gone


def forget_failures(folder: str | None = None) -> None:
    """The data folder changed (or works again): writes are tried everywhere again. `folder` (the
    one just written by a move): its drive or share is no longer taken as offline. Other drives
    found offline, and the read retries of journals elsewhere, keep their schedule (forgetting them
    would probe every dead share at once)."""
    with _pause_lock:  # (a probe thread's late answer changes these too)
        _failed.clear()
        _gone.clear()
        _offline_paused.clear()
    if folder is None:
        _offline_roots.clear()
        _offline_sure.clear()
        _offline_count.clear()
        _read_next.clear()
    else:
        root = _root(folder)
        _offline_roots.pop(root, None)
        _offline_sure.discard(root)
        _offline_count.pop(root, None)
    _file_paused.clear()
    _reported.clear()
    _held.clear()
    _write_next.clear()


# The saved files' format number ("Format" in every data file, encrypted ones inside their content).
# Independent of the app's version: it changes only when a file's layout does. A file of an older
# format is brought up to this one when it is read (MIGRATIONS, one step per number) and written
# back that way. A file that can't be read at all should never exist: damaged or junk (not JSON, not
# ours, a bad format), or of a NEWER format (a hacked file, or an older version installed again, e.g.
# from a backup or on a new computer). The user is asked (tray: Quit, or delete the files and start
# them fresh; never converted back or repaired); until answered it reads as damaged (ValueError).
FORMAT = 5
FORMAT_KEY = "Format"


def _from_1(name: str, data: dict) -> dict:
    """Format 2 keeps windows, desktops and shown desktops per display setup (by display name):
    format 1's (one list keyed by monitor path) start empty; settings and the journal are the same."""
    return {} if name.lower() in (LAYOUT_FILE, PLACES_FILE, SHOWING_FILE) else data


def _from_2(name: str, data: dict) -> dict:
    """Format 3 matches windows by count, not title: saved windows lose "TitleParts" and get
    "Order" (their order in the file, per setup)."""
    if name.lower() not in (LAYOUT_FILE, PLACES_FILE):
        return data
    for setup in data.get("Setups") or []:
        if isinstance(setup, dict):
            for i, w in enumerate(setup.get("Windows") or []):
                if isinstance(w, dict):
                    w.pop("TitleParts", None)
                    w["Order"] = i
    return data


def _from_3(name: str, data: dict) -> dict:
    """Format 4: places.json windows carry "Command" (their program's command line, for Restore
    Applications On Restart). Older records have none: their apps are not started again."""
    return data


def _from_4(name: str, data: dict) -> dict:
    """Format 5: places.json windows carry "App" (a packaged app's id: Store apps start again by it).
    Older records have none: their Store apps are not started again."""
    return data


MIGRATIONS: dict[int, object] = {1: _from_1, 2: _from_2, 3: _from_3, 4: _from_4}  # format n -> fn(file name, data) returning that file's data in format n + 1
on_unreadable = None  # called with a data file's path when it can't be read at all (tray: the question);
# returns whether that question covers the file (the error then carries `asked = True`)
kept: set[str] = set()  # normcased paths waiting for the Quit / Recreate Files answer: never written over


def is_kept(path: str) -> bool:
    return bool(kept) and os.path.normcase(path) in kept


class NewerFormat(ValueError):
    """A data file of a newer format than this version's: unreadable, like a damaged one."""

    def __init__(self, fmt: int):
        super().__init__(f"format {fmt} is newer than {FORMAT}")
        self.fmt = fmt


# The settings files the Quit / Recreate Files question covers: not the crash journal (a damaged one
# is told with Restore Hidden Windows instead; Quit must never leave a crashed run's windows hidden)
ASKED_FILES = tuple(n for n in DATA_FILES if n != JOURNAL_FILE)


def junk(path: str) -> bool:
    """A data file that can't be read at all (damaged, junk, a newer format); not one that can't be
    read now (held, offline, encrypted with a key not entered), nor a missing one."""
    try:
        read_json(path, ask=False)
    except (ValueError, RecursionError):
        return True
    except OSError:
        pass
    return False


def unreadable_files(folder: str, names=ASKED_FILES) -> list[str]:
    """The folder's files `names` (the settings files) that can't be read at all: damaged, junk or
    of a newer format. Files that can't be read NOW (held, offline, encrypted with a key not entered
    yet) are not counted: they have their own handling; a folder known offline or gone is not read
    (never a wait on the network timeout)."""
    if known_offline(folder) or paused_as_gone(folder):
        return []
    return [p for p in (os.path.join(folder, n) for n in names) if junk(p)]


def read_json(path: str, ask: bool = True):
    """A data file's JSON: plain, or encrypted (Encrypt Settings) and opened with a known key; in the
    current format (an older one is brought up to it and the file rewritten so). "Format" itself is
    not in the result. Raises FileNotFoundError / OSError (crypto.Locked: ours, no known key opens
    it) / ValueError (can't be read at all: damaged, junk, a bad or newer format (NewerFormat);
    `on_unreadable` is told first when `ask`; `asked` on the error: whether its question covers the
    file). Not `ask`: neither question (nor crypto's key popup)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return _parse(path, raw, ask)
    except (ValueError, RecursionError) as exc:
        asked = False
        if ask and on_unreadable is not None:
            try:
                asked = bool(on_unreadable(path))
            except Exception:  # noqa: BLE001 - asking must never break a read
                pass
        exc.asked = asked
        raise


def _parse(path: str, raw: bytes, ask: bool = True):
    from . import crypto
    data = json.loads(crypto.decode(raw, path if ask else ""))  # ("": no key popup)
    if not isinstance(data, dict):
        return data
    fmt = data.pop(FORMAT_KEY, 1)  # (written before the number: format 1, upgraded like any format 1 file)
    if not (type(fmt) is int and fmt >= 1):
        raise ValueError(f"bad format {fmt!r}")
    if fmt > FORMAT:
        raise NewerFormat(fmt)
    if fmt < FORMAT:
        name = os.path.basename(path)
        while fmt < FORMAT:
            step = MIGRATIONS.get(fmt)
            if step is None:
                raise ValueError(f"no way from format {fmt}")  # (damaged: never an error that ends the app)
            try:
                data = step(name, data)
            except Exception as exc:  # noqa: BLE001 - an odd older file: damaged, never an error that ends the app
                raise ValueError(f"format {fmt} upgrade failed: {exc!r}") from exc
            fmt += 1
        if _upgrade_on_disk(path, raw):
            write_json(path, data, quiet=True)  # (a read never pauses a folder nor reports a failure)
    return data


def _upgrade_on_disk(path: str, raw: bytes) -> bool:
    """An older-format file just read is rewritten in the current format only when that is safe and
    quiet: a file of the folder in use (never another folder's or another run's), not a crash
    journal (its own code writes and deletes those), its folder not paused or offline (a read must
    never block or report a failure), and in the form it had (plain while encryption is off, or
    encrypted with the key in use: another key's file is never re-keyed). Otherwise it is upgraded
    by its next normal write."""
    from . import crypto, datafolder
    folder = os.path.dirname(path)
    if is_journal(os.path.basename(path)) or not datafolder.same(folder, datafolder.current()):
        return False
    if paused(folder) or known_offline(folder) or _key(folder) in _gone:
        return False
    ours = crypto.is_ours(raw)
    return ours == bool(crypto.enabled and crypto.key()) and (not ours or crypto.opens(raw, crypto.key()) is not None)


def write_json(path: str, data, quiet: bool = False) -> bool:
    """Writes compact JSON with the format number (encrypted while Encrypt Settings is on:
    crypto.encode); see write_bytes."""
    from . import crypto
    if isinstance(data, dict):
        data = {**data, FORMAT_KEY: FORMAT}
    return write_bytes(path, crypto.encode(json.dumps(data, separators=(",", ":"))), quiet)


def write_bytes(path: str, payload: bytes, quiet: bool = False) -> bool:
    """Writes a data file atomically (temp file, then replace), so a reader never sees half a
    file. A folder that is gone is made again. Returns False if it could not be written; no temp
    file is left behind. A file another program holds for a moment is waited for briefly (only
    then: every write runs on the UI thread); if it is still held, only that write fails (no pause,
    no report). After any other failure the folder is paused (PAUSE_GONE_S
    when it is gone or offline, PAUSE_REFUSED_S when it refuses writes): no write there blocks or
    fails again until then, then it is tried again by itself (a drive back after resume). The first
    failure in a folder is reported (`on_write_failed`); once a write works again, a new failure is
    reported again."""
    if is_kept(path):
        return False  # (waiting for the Quit / Recreate Files answer: never written over; not a failure)
    folder = os.path.dirname(path)
    key = _key(folder)
    if paused(folder) or time.monotonic() < _file_paused.get(path, 0.0):
        return False
    if known_offline(folder) or (key in _gone and offline(folder)):
        # (its drive or share is known offline, or still is by the quick check: no write waits on it;
        # told like any failure, once, but only when that is sure: a share that was only slow to
        # answer once (waking up) is not reported, and its late answer takes the pause back)
        with _pause_lock:  # (one step: a late answer never lands between the pause and the told mark)
            _pause(folder, gone=True, offline_only=True)
            if offline_for_sure(folder):
                report(folder)
        return False
    tmp = path + ".tmp"
    gone = False
    held = False  # every attempt failed because another program held the file
    denied = False  # ... and the last one was "access denied" while replacing it
    for attempt in range(1, 1 + len(RETRY_DELAYS_S) + 1):
        try:
            replacing = False
            with open(tmp, "wb") as fh:
                fh.write(payload)
            replacing = True  # (the folder took a new file: a failure from here on is the target's)
            os.replace(tmp, path)
            held_ended(path)
            _file_paused.pop(path, None)
            _reported.discard("file:" + os.path.normcase(path))  # (works again: a new refusal is told again)
            # Works again: not failed, and a new failure is told again (also after a late answer of
            # its share ended the pause: _found_again keeps the told mark for this write to clear)
            with _pause_lock:
                _failed.pop(key, None)
                _gone.discard(key)
                _offline_paused.discard(key)
                _reported.discard(key)
            return True
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if not os.path.isdir(folder):
                try:
                    os.makedirs(folder, exist_ok=True)  # (deleted while the app runs)
                    continue
                except OSError:
                    gone, held = True, False
                    break  # gone or offline: no use waiting
            code = getattr(exc, "winerror", None)
            if code is None and not replacing and isinstance(exc, PermissionError):
                # (open() errors come from the C runtime, without the Windows code: a sharing
                # violation and "access denied" both arrive as EACCES)
                code = ERROR_ACCESS_DENIED
            # Held by another program: a sharing/lock violation, or "access denied" when replacing a
            # file another program has open (the temp file was just written, so the folder is fine)
            # (also when making the temp file while an earlier one is still there: removed after a
            # failure but kept "delete pending" by a program that has it open, on some drives)
            denied = code == ERROR_ACCESS_DENIED and (replacing or os.path.exists(tmp))
            held = code in SHARING_ERRORS or denied
            if code not in SHARING_ERRORS or attempt > len(RETRY_DELAYS_S):
                break  # no waiting on the UI thread unless the file is locked for a moment
            time.sleep(RETRY_DELAYS_S[attempt - 1])  # held for a moment: tried again
    if held:
        _write_next[path] = time.monotonic() + READ_RETRY_S  # (flushes wait: no 0.2 s sleep every sync)
    if held and not denied:
        held_ended(path)  # (a sharing violation: another program for sure, so not a lasting refusal)
    if held and not (denied and held_again(path)):
        # Only this file is held (a sync client, an antivirus): the folder works. Nothing is paused
        # (the crash journal must not be skipped because places.json is busy) and nothing reported;
        # the caller writes again on its next change or sync. "Access denied" on every write for
        # HELD_REFUSED_S means it can't be replaced at all (read-only, a share without delete
        # rights): refused. A sharing/lock violation never counts.
        return False
    held_ended(path)
    if denied:
        # Only this FILE can't be replaced (read-only ...): the folder takes new files, so only it is
        # paused (never the journal with it), and the user is told. The crash journal itself is never
        # paused: a refused try costs nothing, and the next one may work the moment it is free
        if quiet:
            return False  # (an upgrade while reading: nothing paused nor told; its next normal write tries)
        if not is_journal(os.path.basename(path)):
            _file_paused[path] = time.monotonic() + PAUSE_REFUSED_S
        report(folder, file=path)
        return False
    if not quiet:
        fail(folder, gone)
    return False


_file_paused: dict[str, float] = {}  # file -> time.monotonic() until its writes are skipped (it can't be replaced)


HELD_LIMIT = 3              # "access denied" failures of one file in a row ...
HELD_REFUSED_S = 10.0       # ... over at least this long, before it counts as refused (not held)
_held: dict[str, tuple[int, float]] = {}  # file -> (such failures in a row, time.monotonic() of the first)


def held_again(path: str) -> bool:
    """One more "access denied" failure for this file: True once there have been HELD_LIMIT in a row
    over at least HELD_REFUSED_S (a sync client holding it for a moment never gets there)."""
    now = time.monotonic()
    count, first = _held.get(path, (0, now))
    count += 1
    if count >= HELD_LIMIT and now - first >= HELD_REFUSED_S:
        del _held[path]
        return True
    _held[path] = (count, first)
    return False


READ_RETRY_S = 5.0                # a data file that could not be read is read again at most this often
_read_next: dict[str, float] = {}  # file -> time.monotonic() when its read may be tried again


def retry_due(path: str) -> bool:
    """Whether a file that could not be read may be read again now: never while its folder is
    paused (offline: it would block), and at most every READ_RETRY_S (reads run on the UI thread)."""
    if paused(os.path.dirname(path)) or known_offline(os.path.dirname(path)):
        return False
    now = time.monotonic()
    if now < _read_next.get(path, 0.0):
        return False
    _read_next[path] = now + READ_RETRY_S
    return True


_offline_roots: dict[str, float] = {}  # drive or share (normcase) -> time.monotonic() until it is assumed offline


def offline(folder: str) -> bool:
    """The folder can't be reached (its drive or share is not there), as opposed to a folder that
    simply does not exist (deleted, never made) on a drive that is. A drive or share found offline
    is taken as offline for PAUSE_GONE_S without checking again (each check of an unreachable share
    waits for the network timeout, on the UI thread)."""
    drive = os.path.splitdrive(os.path.abspath(folder))[0]
    root = os.path.normcase(drive)
    if root and time.monotonic() < _offline_roots.get(root, 0.0):
        return True
    if drive and not drive.startswith("\\\\"):
        from . import native
        if native.is_fixed_drive(drive + os.sep):
            # (a local disk is never slow to be taken as offline; one not ready (locked, spun down
            # for good) has no root folder: offline, so nothing there is taken as "nothing saved")
            return not os.path.isdir(drive + os.sep)
    # The folder and its drive or share checked together, waited for at most PROBE_WAIT_S: one that
    # doesn't answer by then counts as offline (the checks finish in the background, the UI thread
    # never waits for the network timeout); a later "it is there" takes the offline mark back
    if _any_dir_quick([folder] + ([drive + os.sep] if drive else []), root):
        return False
    return True


def _root(folder: str) -> str:
    return os.path.normcase(os.path.splitdrive(os.path.abspath(folder))[0])


def offline_for_sure(folder: str) -> bool:
    """Its drive or share is known offline and that is no guess: every check answered "not there",
    or it did not answer twice in a row (a share waking up answers late only once)."""
    root = _root(folder)
    return known_offline(folder) and (root in _offline_sure or _offline_count.get(root, 0) >= 2)


_offline_sure: set[str] = set()      # roots whose checks all answered "not there" (not just slow)
_offline_count: dict[str, int] = {}  # root -> times in a row it was found offline


def _found_again(root: str) -> None:
    """The drive or share answered: not offline, and folders on it paused as gone work again (tried
    at the next write). Only those paused just for being known offline (no write tried:
    `_offline_paused`) lose their "told" mark: a folder that failed for a real reason is never told
    again after every late answer (the next write that works takes the mark back, and a failure
    after that is told again)."""
    _offline_roots.pop(root, None)
    _offline_sure.discard(root)
    _offline_count.pop(root, None)
    # (runs on a probe thread; every change of these sets on the UI thread holds the lock too)
    with _pause_lock:
        for key in [k for k in _gone if _root(k) == root]:
            _gone.discard(key)
            _failed.pop(key, None)
            if key in _offline_paused:
                _offline_paused.discard(key)
                _reported.discard(key)


def known_offline(folder: str) -> bool:
    """Its drive or share was found offline less than PAUSE_GONE_S ago (memory only: no disk access)."""
    root = _root(folder)
    return bool(root) and time.monotonic() < _offline_roots.get(root, 0.0)


PROBE_WAIT_S = 2.0  # how long a reachability check may hold up the caller (a slow share answers by then)
OFFLINE_ROW_S = 3 * PAUSE_GONE_S  # two misses further apart than this are not "in a row"


def _any_dir_quick(paths_: list[str], root: str) -> bool:
    """Whether any of these is a folder, by os.path.isdir on background threads, waited for at most
    PROBE_WAIT_S in all. False (offline): `root` is marked offline, and a later "it is there" from a
    check still running clears that mark."""
    lock = threading.Lock()
    state = {"found": False, "decided": False, "answers": 0}
    done = threading.Event()

    def probe(path: str) -> None:
        found = os.path.isdir(path)
        with lock:
            state["answers"] += 1
            if found:
                state["found"] = True
                if state["decided"] and root:
                    _found_again(root)  # (it was only slow)
            if found or state["answers"] == len(paths_):
                done.set()  # (a "yes", or every answer in: no need to wait longer)
    for p in paths_:
        threading.Thread(target=probe, args=(p,), name="probe", daemon=True).start()
    done.wait(PROBE_WAIT_S)
    with lock:  # (decided under the lock: an answer is either seen here or clears the mark after)
        state["decided"] = True
        if state["found"]:
            if root:
                _offline_sure.discard(root)
                _offline_count.pop(root, None)
            return True
        if root:
            now = time.monotonic()
            # "In a row" = no answer in between (an answer resets the count). Only a miss long after
            # the last one (OFFLINE_ROW_S: no write wanted meanwhile) starts again: a share that
            # sleeps and is slow to wake once each time is never taken as dead, while a dead one is
            # probed by the regular writes (the places heartbeat, every HEARTBEAT_S) and is told
            last = _offline_roots.get(root)  # (the previous miss's deadline: that miss + PAUSE_GONE_S)
            if last is None or now > last + OFFLINE_ROW_S:
                _offline_count.pop(root, None)  # (not in a row: an earlier miss long ago is forgotten)
            _offline_roots[root] = now + PAUSE_GONE_S
            _offline_count[root] = _offline_count.get(root, 0) + 1
            if state["answers"] == len(paths_):
                _offline_sure.add(root)  # (every check answered: not there, not just slow)
            else:
                _offline_sure.discard(root)
        return False


_write_next: dict[str, float] = {}  # file -> time.monotonic() before which a flush does not write it again


def write_due(path: str) -> bool:
    """A background re-write (a flush from the sync timer) of this file may be tried now: not within
    READ_RETRY_S of finding it held by another program (each try may wait RETRY_DELAYS_S on the UI
    thread). Writes the user asks for, and the crash journal, never wait for this."""
    return time.monotonic() >= _write_next.get(path, 0.0)


def retry_after(path: str, seconds: float) -> None:
    """The next read of this file is due no sooner than `seconds` from now (retry_due)."""
    _read_next[path] = max(_read_next.get(path, 0.0), time.monotonic() + seconds)


def unreadable(path: str, exc: OSError) -> bool:
    """A read of this data file failed with `exc`: whether it is "can't read now" (never written
    over) rather than "nothing saved". Not found counts as nothing saved unless the folder is gone
    because its drive or share is offline (a folder gone from a drive that answers holds nothing)."""
    if isinstance(exc, FileNotFoundError):
        folder = os.path.dirname(path)
        return not os.path.isdir(folder) and offline(folder)
    return not truly_missing(path)


def truly_missing(path: str) -> bool:
    """The file is not there while its folder is (an offline or unplugged folder also reports the
    file as missing: that is "can't read now", never "nothing saved")."""
    return not os.path.exists(path) and os.path.isdir(os.path.dirname(path))


def held_ended(path: str) -> None:
    """The file was written or deleted: its count of failures in a row starts again."""
    _held.pop(path, None)


def fail(folder: str, gone: bool) -> None:
    """A write or delete in this folder failed for good: it is paused, and the first such failure is
    reported (`on_write_failed`)."""
    _pause(folder, gone)
    report(folder)


def report(folder: str, file: str | None = None) -> None:
    """The first failure in a folder is told to the user (once, until a write there works again).
    `file`: only that file is refused (read-only ...): told once for it, kept apart from the folder's
    own failures (which are still told)."""
    key = "file:" + os.path.normcase(file) if file else _key(folder)
    if key in _reported or on_write_failed is None:
        return  # told already / nobody to tell yet (start-up: told at the next failure)
    _reported.add(key)
    try:
        on_write_failed(folder)
    except Exception:  # noqa: BLE001 - reporting must never break the caller
        pass


def is_int(v) -> bool:
    """A plain int from a JSON file (JSON true/false load as bool, which is an int in Python)."""
    return isinstance(v, int) and not isinstance(v, bool)
