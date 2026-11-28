"""Start menu shortcut "DisplayDesktop". The "Add To Start Menu" setting (Settings dialog, off by
default) decides: while it is on, every launch adds the shortcut if missing and rewrites it if
outdated (`ensure`); turning it off deletes the shortcut (`set_enabled`).

The shortcut targets the running Python's pythonw.exe (as the startup entry; every launch rewrites an
outdated one), or the Python launcher (pyw.exe -3) when this Python has none; it runs
DisplayDesktop.pyw. It must not target the .pyw file itself: the Start menu treats a shortcut
to a document differently from an app shortcut and may not show it. Its icon is src/app.ico.

Written like an installer does: through the shell's ShellLink COM object (ctypes, no packages), with
a fixed AppUserModelID, and the shell is told about the change (SHChangeNotify). Without the ID the
Start menu generates a new one for every new shortcut and keeps the old entry cached.
Nothing is hard-coded: the Start menu folder comes from Windows, the rest from this file's location.
A copy in test mode (DISPLAYDESKTOP_DATA set) never creates or deletes it.

The same functions write the Startup-folder shortcut of Start With Windows (startup.py passes its own
`lnk` path and target). settings is imported where it is used: `run status` reads the Startup
shortcut and must not load paths.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, WINFUNCTYPE, Structure, byref, c_long, c_ubyte, c_ulong, c_ushort, c_void_p

from . import native as N
from . import startup

NAME = "DisplayDesktop"
APP_ID = "DisplayDesktop.App"
DESCRIPTION = "Give every screen its own workspaces"
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
CSIDL_PROGRAMS = 0x0002
CSIDL_STARTUP = 0x0007

_SHGetFolderPathW = N._f(N.shell32, "SHGetFolderPathW", c_long, N.H, ctypes.c_int, N.H, N.DWORD,
                         ctypes.c_wchar_p)
_SHChangeNotify = N._f(N.shell32, "SHChangeNotify", None, c_long, ctypes.c_uint, ctypes.c_wchar_p,
                       c_void_p)
SHCNE_CREATE, SHCNE_DELETE, SHCNE_UPDATEDIR, SHCNE_UPDATEITEM = 0x2, 0x4, 0x1000, 0x2000
SHCNF_PATHW, SHCNF_FLUSH = 0x5, 0x1000

# ---------------------------------------------------------------- COM (ShellLink + property store)

_ole32 = N._load("ole32.dll")


class GUID(Structure):
    _fields_ = [("d1", c_ulong), ("d2", c_ushort), ("d3", c_ushort), ("d4", c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        g = cls()
        _CLSIDFromString(s, byref(g))
        return g


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("ptr", c_void_p), ("pad", c_void_p)]


_CLSIDFromString = N._f(_ole32, "CLSIDFromString", c_long, ctypes.c_wchar_p, POINTER(GUID))
_CoInitializeEx = N._f(_ole32, "CoInitializeEx", c_long, c_void_p, N.DWORD)
_CoUninitialize = N._f(_ole32, "CoUninitialize", None)
_CoCreateInstance = N._f(_ole32, "CoCreateInstance", c_long, POINTER(GUID), c_void_p, N.DWORD,
                         POINTER(GUID), POINTER(c_void_p))
_PropVariantClear = N._f(_ole32, "PropVariantClear", c_long, POINTER(PROPVARIANT))

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: COM already set up differently on this thread; fine
VT_LPWSTR = 31
STGM_READ = 0
CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
IID_IPropertyStore = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
PKEY_AppUserModel_ID = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 5)

# vtable slots
_QI, _RELEASE = 0, 2
_SL_GET_PATH, _SL_SET_DESCRIPTION, _SL_GET_WORKING_DIR, _SL_SET_WORKING_DIR = 3, 7, 8, 9
_SL_GET_ARGUMENTS, _SL_SET_ARGUMENTS, _SL_GET_ICON, _SL_SET_ICON, _SL_SET_PATH = 10, 11, 16, 17, 20
SLGP_RAWPATH = 0x4
_PF_LOAD, _PF_SAVE = 5, 6
_PS_GET_VALUE, _PS_SET_VALUE, _PS_COMMIT = 5, 6, 7


def _call(obj: c_void_p, slot: int, *args: tuple) -> None:
    """Calls COM method `slot` of `obj`; args are (ctype, value) pairs. Raises OSError on failure."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    fn = WINFUNCTYPE(c_long, c_void_p, *(t for t, _ in args))(vtbl[slot])
    hr = fn(obj, *(v for _, v in args))
    if hr < 0:
        raise OSError(f"COM call failed (0x{hr & 0xFFFFFFFF:08X})")


def _release(obj: c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        WINFUNCTYPE(c_ulong, c_void_p)(vtbl[_RELEASE])(obj)


def _query(obj: c_void_p, iid: str) -> c_void_p:
    out = c_void_p()
    _call(obj, _QI, (POINTER(GUID), byref(GUID.of(iid))), (POINTER(c_void_p), byref(out)))
    return out


def _app_id_key() -> PROPERTYKEY:
    return PROPERTYKEY(GUID.of(PKEY_AppUserModel_ID[0]), PKEY_AppUserModel_ID[1])


class _Com:
    """COM for the current thread for the duration of a with-block."""

    def __enter__(self):
        hr = _CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        self.owned = hr >= 0
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            raise OSError("COM could not be initialized")
        return self

    def __exit__(self, *exc):
        if self.owned:
            _CoUninitialize()


def _shell_link() -> c_void_p:
    link = c_void_p()
    hr = _CoCreateInstance(byref(GUID.of(CLSID_ShellLink)), None, CLSCTX_INPROC_SERVER,
                           byref(GUID.of(IID_IShellLinkW)), byref(link))
    if hr < 0:
        raise OSError("ShellLink not available")
    return link


def write_link(lnk: str, exe: str, args: str, work_dir: str, icon: str, desc: str, app_id: str) -> None:
    """Writes the .lnk file at `lnk` (overwrites). Raises OSError on failure."""
    w = ctypes.c_wchar_p
    with _Com():
        link = _shell_link()
        store = persist = c_void_p()
        try:
            _call(link, _SL_SET_PATH, (w, exe))
            _call(link, _SL_SET_ARGUMENTS, (w, args))
            _call(link, _SL_SET_WORKING_DIR, (w, work_dir))
            _call(link, _SL_SET_ICON, (w, icon), (ctypes.c_int, 0))
            _call(link, _SL_SET_DESCRIPTION, (w, desc))
            store = _query(link, IID_IPropertyStore)
            text = ctypes.create_unicode_buffer(app_id)   # SetValue copies it
            pv = PROPVARIANT(vt=VT_LPWSTR, ptr=ctypes.cast(text, c_void_p))
            key = _app_id_key()
            _call(store, _PS_SET_VALUE, (POINTER(PROPERTYKEY), byref(key)), (POINTER(PROPVARIANT), byref(pv)))
            _call(store, _PS_COMMIT)
            persist = _query(link, IID_IPersistFile)
            _call(persist, _PF_SAVE, (w, lnk), (ctypes.c_int, 1))
        finally:
            _release(persist)
            _release(store)
            _release(link)


def read_link(lnk: str) -> dict[str, str]:
    """What the .lnk file at `lnk` holds: exe, args, work_dir, icon (environment variables
    expanded) and app_id ("" if not set). Raises OSError if it can't be read."""
    with _Com():
        link = _shell_link()
        store = persist = c_void_p()
        try:
            persist = _query(link, IID_IPersistFile)
            _call(persist, _PF_LOAD, (ctypes.c_wchar_p, lnk), (N.DWORD, STGM_READ))
            n = 32768
            buf, wbuf = ctypes.create_unicode_buffer(n), ctypes.c_wchar_p
            out: dict[str, str] = {}
            _call(link, _SL_GET_PATH, (wbuf, ctypes.cast(buf, wbuf)), (ctypes.c_int, n), (c_void_p, None),
                  (N.DWORD, SLGP_RAWPATH))
            out["exe"] = buf.value
            _call(link, _SL_GET_ARGUMENTS, (wbuf, ctypes.cast(buf, wbuf)), (ctypes.c_int, n))
            out["args"] = buf.value
            _call(link, _SL_GET_WORKING_DIR, (wbuf, ctypes.cast(buf, wbuf)), (ctypes.c_int, n))
            out["work_dir"] = buf.value
            index = ctypes.c_int()
            _call(link, _SL_GET_ICON, (wbuf, ctypes.cast(buf, wbuf)), (ctypes.c_int, n),
                  (POINTER(ctypes.c_int), byref(index)))
            out["icon"] = buf.value
            store = _query(link, IID_IPropertyStore)
            pv, key = PROPVARIANT(), _app_id_key()
            _call(store, _PS_GET_VALUE, (POINTER(PROPERTYKEY), byref(key)), (POINTER(PROPVARIANT), byref(pv)))
            try:
                out["app_id"] = ctypes.wstring_at(pv.ptr) if pv.vt == VT_LPWSTR and pv.ptr else ""
            finally:
                _PropVariantClear(byref(pv))
            for k in ("exe", "work_dir", "icon"):
                out[k] = os.path.expandvars(out[k])
            return out
        finally:
            _release(persist)
            _release(store)
            _release(link)


# ---------------------------------------------------------------- shortcut


def folder(csidl: int) -> str:
    """A shell folder of the current user (wherever it is redirected to): CSIDL_PROGRAMS (Start menu
    "Programs"), CSIDL_STARTUP. OSError if Windows has none."""
    buf = ctypes.create_unicode_buffer(520)
    if _SHGetFolderPathW(0, csidl, 0, 0, buf) != 0 or not buf.value:
        raise OSError("shell folder not found")
    return buf.value


def programs_dir() -> str:
    """The current user's Start menu "Programs" folder."""
    return folder(CSIDL_PROGRAMS)


def path() -> str:
    return os.path.join(programs_dir(), NAME + ".lnk")


def exists(lnk: str | None = None) -> bool:
    """The shortcut `lnk` (None: the Start menu one) is there."""
    try:
        return os.path.isfile(path() if lnk is None else lnk)
    except OSError:
        return False


def target() -> tuple[str, str]:
    """(target executable, arguments) of the shortcut, full paths: the running Python's pythonw.exe
    (the Python it runs on, as the startup entry; every launch rewrites an outdated shortcut), else
    the Python launcher pyw.exe with `-3` when it is on PATH (never the current folder). Neither (a
    console python.exe would open a console window at every launch): OSError, no shortcut written."""
    here = os.path.dirname(os.path.abspath(sys.executable))
    exe = os.path.join(here, "pythonw.exe")
    if os.path.isfile(exe):
        return exe, startup.launcher_args()
    if os.path.basename(sys.executable).lower() == "pythonw.exe":
        return os.path.abspath(sys.executable), startup.launcher_args()
    launcher = _launcher()
    if launcher:
        return launcher, f"-3 {startup.launcher_args()}"
    raise OSError("no pythonw.exe for this Python")


def _launcher() -> str | None:
    """pyw.exe (that name, whatever PATHEXT says) in a PATH folder with a drive or UNC root (never the
    current one nor a drive-relative one), full path (written as is: which PATH finds it does not matter)."""
    p = startup.on_path("pyw.exe")
    return os.path.abspath(p) if p else None


def _notify(event: int, lnk: str) -> None:
    """Tells the shell (and so the Start menu) that the shortcut changed, as installers do."""
    _SHChangeNotify(event, SHCNF_PATHW | SHCNF_FLUSH, lnk, None)
    _SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW | SHCNF_FLUSH, os.path.dirname(lnk), None)


def create(want: tuple[str, str] | None = None, lnk: str | None = None) -> bool:
    """Writes the shortcut `lnk` (None: the Start menu one). `want`: (target, arguments), target()
    when None (it may search PATH)."""
    if not startup.can_manage():
        return False
    try:
        lnk = path() if lnk is None else lnk
        existed = os.path.isfile(lnk)
        tgt, args = target() if want is None else want
        write_link(lnk, tgt, args, startup.REPO_DIR, ICON, DESCRIPTION, APP_ID)
    except OSError:
        return False
    _notify(SHCNE_UPDATEITEM if existed else SHCNE_CREATE, lnk)
    return exists(lnk)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_current(want: tuple[str, str] | None = None, lnk: str | None = None) -> bool:
    """True if the shortcut `lnk` (None: the Start menu one) exists and opens this copy the way
    create() would write it (target, arguments, folder, icon, AppUserModelID; paths in any letter
    case). An unreadable one counts as outdated. `want`: as for create()."""
    try:
        got = read_link(path() if lnk is None else lnk)
        exe, args = target() if want is None else want
    except OSError:
        return False
    return (_same_path(got["exe"], exe) and startup.same_command(got["args"], args)
            and _same_path(got["work_dir"], startup.REPO_DIR) and _same_path(got["icon"], ICON)
            and got["app_id"] == APP_ID)


def remove(lnk: str | None = None) -> bool:
    """Deletes the shortcut `lnk` (None: the Start menu one). Returns True if none is left."""
    if not startup.can_manage():
        return not exists(lnk)
    try:
        lnk = path() if lnk is None else lnk
        os.remove(lnk)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    _notify(SHCNE_DELETE, lnk)
    return not exists(lnk)


def set_enabled(enabled: bool) -> bool:
    """The "Add To Start Menu" setting: remembers the choice, then adds (or rewrites) or removes
    the shortcut. Returns whether a shortcut exists now. Never raises."""
    from . import settings
    if not startup.can_manage():
        return exists()
    want = None
    if enabled:
        try:
            want = target()  # (once: it may search PATH)
        except OSError:
            return exists()  # (no windowless Python to start: never turned on; one already on is left as it is)
    settings.set("StartMenu", enabled)
    try:
        if enabled:
            return is_current(want) or create(want)
        remove()
    except Exception:  # noqa: BLE001
        pass
    return exists()


def ensure() -> str:
    """At launch, if "Add To Start Menu" is on: adds the shortcut if it is missing, rewrites it if
    it is outdated (folder moved, other Python). Never raises. Returns "added", "updated" or ""
    (nothing done, setting off, or it failed)."""
    try:
        from . import settings
        if not startup.can_manage() or not settings.get("StartMenu"):
            return ""
        want = target()  # (once: it may search PATH; none: nothing to write)
        if not exists():
            return "added" if create(want) else ""
        if not is_current(want):
            return "updated" if create(want) else ""
    except Exception:  # noqa: BLE001 - a Start menu entry must never stop the app
        pass
    return ""
