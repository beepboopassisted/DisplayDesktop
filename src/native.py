"""All Win32 calls (ctypes, no external packages).

Every function gets explicit argtypes/restype: without them ctypes passes and returns C ints, which
would truncate 64-bit handles. Handles are plain Python ints (0 = NULL) everywhere in the app.
DLLs are loaded by full path from System32 only, never from the current or script folder.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import (POINTER, Structure, WINFUNCTYPE, byref, c_int, c_long, c_size_t, c_ssize_t,
                    c_uint, c_ulong, c_ulonglong, c_void_p, c_wchar, c_wchar_p,
                    create_unicode_buffer, sizeof)
from collections import namedtuple

def _system_dir() -> str:
    """The System32 folder, wherever Windows is installed. kernel32 is a KnownDLL, so loading it by
    name always gets the System32 copy."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get = k32["GetSystemDirectoryW"]
    get.restype, get.argtypes = ctypes.c_uint, [ctypes.c_wchar_p, ctypes.c_uint]
    buf = ctypes.create_unicode_buffer(520)
    n = get(buf, 520)
    if not 0 < n < 520:
        raise OSError("GetSystemDirectoryW failed")
    return buf.value


_SYS32 = _system_dir()


def _load(name: str) -> ctypes.WinDLL:
    return ctypes.WinDLL(os.path.join(_SYS32, name), use_last_error=True)


user32 = _load("user32.dll")
kernel32 = _load("kernel32.dll")
gdi32 = _load("gdi32.dll")
dwmapi = _load("dwmapi.dll")
shell32 = _load("shell32.dll")
gdiplus = _load("gdiplus.dll")
comctl32 = _load("comctl32.dll")


def preload(*names: str) -> None:
    """Loads System32 DLLs on a thread while the caller goes on (LoadLibrary runs without the GIL),
    so a later _load(name) finds them in memory. Never raises: a DLL that fails here fails in _load
    as before. (Same flags as ctypes: LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | _DLL_LOAD_DIR.)"""
    try:
        import _thread
        load = _f(kernel32, "LoadLibraryExW", c_ssize_t, c_wchar_p, c_ssize_t, c_ulong)
        paths = [os.path.join(_SYS32, n) for n in names]

        def run() -> None:
            try:
                for p in paths:
                    load(p, 0, 0x1100)
            except Exception:  # noqa: BLE001
                pass

        _thread.start_new_thread(run, ())
    except Exception:  # noqa: BLE001
        pass


H = c_ssize_t          # any handle (HWND, HMONITOR, HDC, HICON, HBITMAP, HANDLE, ...)
LRESULT = c_ssize_t
WPARAM = c_size_t
LPARAM = c_ssize_t
BOOL = c_int
UINT = c_uint
DWORD = c_ulong


def _f(dll, name, restype, *argtypes):
    # dll[name] creates a new function pointer on every call. getattr(dll, name) would return one
    # cached object, so two declarations of the same API with different argtypes would overwrite
    # each other.
    fn = dll[name]
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


# ---------------------------------------------------------------- constants

WPF_RESTORETOMAXIMIZED = 0x2

SW_HIDE = 0
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_SHOWMINNOACTIVE = 7
SW_SHOWNA = 8
SW_RESTORE = 9

GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_POPUP = 0x80000000
WS_VSCROLL = 0x00200000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

GA_ROOT = 2
GW_ANCHOR = 4  # GetWindow: the window this one floats above
DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_ESCAPE = 0x1B

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_ASYNCWINDOWPOS = 0x4000
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

WM_GETICON = 0x007F
GCLP_HICONSM = -34
ICON_SMALL2 = 2
SMTO_ABORTIFHUNG = 0x0002

PW_RENDERFULLCONTENT = 0x00000002

DWM_TNP_RECTDESTINATION = 0x01
DWM_TNP_RECTSOURCE = 0x02
DWM_TNP_OPACITY = 0x04
DWM_TNP_VISIBLE = 0x08
DWM_TNP_SOURCECLIENTAREAONLY = 0x10

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# ---------------------------------------------------------------- structures


class POINT(Structure):
    _fields_ = [("x", c_long), ("y", c_long)]


class RECT(Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]

    def tup(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


class SIZE(Structure):
    _fields_ = [("cx", c_long), ("cy", c_long)]


class MONITORINFOEXW(Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD),
                ("szDevice", c_wchar * 32)]


class WINDOWPLACEMENT(Structure):
    _fields_ = [("length", UINT), ("flags", UINT), ("showCmd", UINT), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


class DISPLAY_DEVICEW(Structure):
    _fields_ = [("cb", DWORD), ("DeviceName", c_wchar * 32), ("DeviceString", c_wchar * 128),
                ("StateFlags", DWORD), ("DeviceID", c_wchar * 128), ("DeviceKey", c_wchar * 128)]


class DWM_THUMBNAIL_PROPERTIES(Structure):
    _fields_ = [("dwFlags", DWORD), ("rcDestination", RECT), ("rcSource", RECT),
                ("opacity", ctypes.c_ubyte), ("fVisible", BOOL), ("fSourceClientAreaOnly", BOOL)]


# ---------------------------------------------------------------- window functions

WNDENUMPROC = WINFUNCTYPE(BOOL, H, LPARAM)
MONITORENUMPROC = WINFUNCTYPE(BOOL, H, H, POINTER(RECT), LPARAM)

# Pure reads (window state, the Z-order, monitors, the tick) called for every window on every sync:
# they never send a message nor wait on another thread, so they keep the GIL (PyDLL: no release and
# re-take per call, nor per EnumWindows callback), skip the last-error bookkeeping, and take handles as
# c_void_p (its int conversion is the fastest): about twice as fast per call. Anything that can send a
# message (GetWindowTextLength sends WM_GETTEXTLENGTH to our own windows) stays on `user32`.
# (64-bit only: PyDLL calls as cdecl, which on x64 is the one calling convention; 32-bit Windows APIs
# are stdcall, so a 32-bit Python uses plain WinDLL objects for these)
_DLL = ctypes.PyDLL if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.WinDLL
_qu = _DLL(os.path.join(_SYS32, "user32.dll"))
_qk = _DLL(os.path.join(_SYS32, "kernel32.dll"))
HP = c_void_p  # a handle argument of those

_EnumWindows = _f(_qu, "EnumWindows", BOOL, WNDENUMPROC, LPARAM)
_IsWindow = _f(_qu, "IsWindow", BOOL, HP)
_IsWindowVisible = _f(_qu, "IsWindowVisible", BOOL, HP)
_IsIconic = _f(_qu, "IsIconic", BOOL, HP)
_IsZoomed = _f(_qu, "IsZoomed", BOOL, HP)
_ShowWindow = _f(user32, "ShowWindow", BOOL, H, c_int)
_ShowWindowAsync = _f(user32, "ShowWindowAsync", BOOL, H, c_int)
_IsHungAppWindow = _f(_qu, "IsHungAppWindow", BOOL, HP)
_GetAncestor = _f(_qu, "GetAncestor", H, HP, UINT)
_GetWindow = _f(_qu, "GetWindow", H, HP, UINT)
_GetWindowLongPtrW = _f(_qu, "GetWindowLongPtrW", c_ssize_t, HP, c_int)
_GetWindowTextLengthW = _f(user32, "GetWindowTextLengthW", c_int, H)
_GetWindowTextW = _f(user32, "GetWindowTextW", c_int, H, c_wchar_p, c_int)
_GetClassNameW = _f(_qu, "GetClassNameW", c_int, HP, c_wchar_p, c_int)
_GetWindowThreadProcessId = _f(_qu, "GetWindowThreadProcessId", DWORD, HP, POINTER(DWORD))
_MonitorFromWindow = _f(_qu, "MonitorFromWindow", H, HP, DWORD)
_MonitorFromPoint = _f(user32, "MonitorFromPoint", H, POINT, DWORD)
_GetCursorPos = _f(user32, "GetCursorPos", BOOL, POINTER(POINT))
_GetMonitorInfoW = _f(_qu, "GetMonitorInfoW", BOOL, HP, POINTER(MONITORINFOEXW))
_EnumDisplayMonitors = _f(_qu, "EnumDisplayMonitors", BOOL, HP, c_void_p, MONITORENUMPROC, LPARAM)
_GetForegroundWindow = _f(user32, "GetForegroundWindow", H)
_SetForegroundWindow = _f(user32, "SetForegroundWindow", BOOL, H)
_GetWindowRect = _f(user32, "GetWindowRect", BOOL, H, POINTER(RECT))
_SetWindowPos = _f(user32, "SetWindowPos", BOOL, H, H, c_int, c_int, c_int, c_int, UINT)
_SendMessageTimeoutW = _f(user32, "SendMessageTimeoutW", LRESULT, H, UINT, WPARAM, LPARAM, UINT, UINT,
                          POINTER(c_size_t))
_GetClassLongPtrW = _f(user32, "GetClassLongPtrW", c_size_t, H, c_int)
_GetWindowPlacement = _f(user32, "GetWindowPlacement", BOOL, H, POINTER(WINDOWPLACEMENT))
_SetWindowPlacement = _f(user32, "SetWindowPlacement", BOOL, H, POINTER(WINDOWPLACEMENT))
_GetWindowDpiAwarenessContext = _f(user32, "GetWindowDpiAwarenessContext", H, H)
_SetThreadDpiAwarenessContext = _f(user32, "SetThreadDpiAwarenessContext", H, H)
_SetProcessDpiAwarenessContext = _f(user32, "SetProcessDpiAwarenessContext", BOOL, H)
_GetThreadDpiAwarenessContext = _f(user32, "GetThreadDpiAwarenessContext", H)
_AreDpiAwarenessContextsEqual = _f(user32, "AreDpiAwarenessContextsEqual", BOOL, H, H)
_EnumDisplayDevicesW = _f(user32, "EnumDisplayDevicesW", BOOL, c_wchar_p, DWORD, POINTER(DISPLAY_DEVICEW), DWORD)
_PrintWindow = _f(user32, "PrintWindow", BOOL, H, H, UINT)
_DestroyIcon = _f(user32, "DestroyIcon", BOOL, H)
_CopyIcon = _f(user32, "CopyIcon", H, H)
RegisterHotKey = _f(user32, "RegisterHotKey", BOOL, H, c_int, UINT, UINT)
UnregisterHotKey = _f(user32, "UnregisterHotKey", BOOL, H, c_int)
GetDpiForWindow = _f(user32, "GetDpiForWindow", UINT, H)

_DwmGetWindowAttribute = _f(dwmapi, "DwmGetWindowAttribute", c_long, H, DWORD, c_void_p, DWORD)
_DwmRegisterThumbnail = _f(dwmapi, "DwmRegisterThumbnail", c_long, H, H, POINTER(H))
_DwmUnregisterThumbnail = _f(dwmapi, "DwmUnregisterThumbnail", c_long, H)
_DwmUpdateThumbnailProperties = _f(dwmapi, "DwmUpdateThumbnailProperties", c_long, H,
                                   POINTER(DWM_THUMBNAIL_PROPERTIES))
_DwmQueryThumbnailSourceSize = _f(dwmapi, "DwmQueryThumbnailSourceSize", c_long, H, POINTER(SIZE))

_GetTickCount64 = _f(_qk, "GetTickCount64", c_ulonglong)
_OpenProcess = _f(kernel32, "OpenProcess", H, DWORD, BOOL, DWORD)
CloseHandle = _f(kernel32, "CloseHandle", BOOL, H)
_QueryFullProcessImageNameW = _f(kernel32, "QueryFullProcessImageNameW", BOOL, H, DWORD, c_wchar_p, POINTER(DWORD))
_GetProcessTimes = _f(kernel32, "GetProcessTimes", BOOL, H, POINTER(c_ulonglong), POINTER(c_ulonglong),
                      POINTER(c_ulonglong), POINTER(c_ulonglong))


def enable_per_monitor_dpi() -> bool:
    """Per-Monitor-V2 DPI awareness for the whole process (must run before any window exists)."""
    if _SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        return True
    # Already set (e.g. by the interpreter's manifest): fine if it is the same mode
    return bool(_AreDpiAwarenessContextsEqual(_GetThreadDpiAwarenessContext(),
                                              DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))


def enum_windows() -> list[int]:
    """Top-level windows, top to bottom of the Z-order."""
    # The callback is d.setdefault(h, 1) itself (no Python frame per window): it returns the lParam 1
    # (go on); the dict keeps the order. (The callback object lives until the call returns.)
    d: dict[int, int] = {}
    _EnumWindows(WNDENUMPROC(d.setdefault), 1)
    return list(d)


# Plain BOOL/int answers: the ctypes functions themselves (no wrapper call per window; 0/1, truthy)
is_window = _IsWindow  # (IsWindow(0) is 0)
is_window_visible = _IsWindowVisible
is_iconic = _IsIconic
is_zoomed = _IsZoomed
is_hung = _IsHungAppWindow


_DwmSetTransitions = _f(dwmapi, "DwmSetWindowAttribute", c_long, H, DWORD, POINTER(c_int), DWORD)
DWMWA_TRANSITIONS_FORCEDISABLED = 3
_TRANSITIONS_OFF, _TRANSITIONS_ON = byref(c_int(1)), byref(c_int(0))  # (read-only inputs: shared)


def show_window(h: int, cmd: int) -> bool:
    """ShowWindow without the DWM fade (Windows 11 fades a window in or out over ~180 ms). The fade is
    turned off only for this call and straight back on, so the app's own animations (minimize,
    restore) are unchanged. Switching then shows the new desktop at once, which the slide needs."""
    _DwmSetTransitions(h, DWMWA_TRANSITIONS_FORCEDISABLED, _TRANSITIONS_OFF, 4)
    try:
        return bool(_ShowWindow(h, cmd))
    finally:
        _DwmSetTransitions(h, DWMWA_TRANSITIONS_FORCEDISABLED, _TRANSITIONS_ON, 4)


def show_window_safe(h: int, cmd: int) -> bool:
    """ShowWindow that does not wait on windows whose thread is not responding (those are shown or
    hidden asynchronously, with the normal fade)."""
    return bool(_ShowWindowAsync(h, cmd)) if is_hung(h) else show_window(h, cmd)


def get_root(h: int) -> int:
    return _GetAncestor(h, GA_ROOT)


def get_root_anchor(h: int) -> int:
    """The main window at the end of the chain of windows this one floats above (the window
    itself if it floats above none). Follows GetWindow, not GetAncestor: GetAncestor only
    reports this link for WS_POPUP windows, so floating overlapped windows would be missed."""
    cur = h
    for _ in range(32):  # bounded: never trust such a chain to end
        anchor = _GetWindow(cur, GW_ANCHOR)
        if not anchor:
            break
        cur = anchor
    return cur


def get_style(h: int) -> int:
    return _GetWindowLongPtrW(h, GWL_STYLE) & 0xFFFFFFFF


def get_exstyle(h: int) -> int:
    return _GetWindowLongPtrW(h, GWL_EXSTYLE) & 0xFFFFFFFF


title_length = _GetWindowTextLengthW


def get_title(h: int) -> str:
    n = _GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    buf = (c_wchar * (n + 1))()
    _GetWindowTextW(h, buf, n + 1)
    return buf.value


_CLASS_BUF = c_wchar * 256


def get_class(h: int) -> str:
    buf = _CLASS_BUF()
    _GetClassNameW(h, buf, 256)
    return buf.value


def get_pid(h: int) -> int:
    pid = DWORD()
    _GetWindowThreadProcessId(h, byref(pid))
    return pid.value


def is_cloaked(h: int) -> bool:
    v = c_int()
    return _DwmGetWindowAttribute(h, DWMWA_CLOAKED, byref(v), 4) == 0 and v.value != 0


get_foreground = _GetForegroundWindow


def set_foreground(h: int) -> bool:
    return bool(_SetForegroundWindow(h))


def get_window_rect(h: int) -> tuple[int, int, int, int] | None:
    r = RECT()
    return r.tup() if _GetWindowRect(h, byref(r)) else None


def get_visible_frame(h: int) -> tuple[int, int, int, int] | None:
    """The visible frame of a window, without the invisible resize borders and shadow that
    GetWindowRect includes. Falls back to GetWindowRect."""
    r = RECT()
    if (_DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, byref(r), sizeof(r)) == 0
            and r.right > r.left and r.bottom > r.top):
        return r.tup()
    return get_window_rect(h)


def set_window_pos(h: int, x: int, y: int, w: int, hh: int, flags: int) -> bool:
    return bool(_SetWindowPos(h, 0, x, y, w, hh, flags))


# ---------------------------------------------------------------- monitors


# (record types are namedtuples throughout: dataclasses and typing would load ~1 MB of modules)
Screen = namedtuple("Screen", "device bounds work primary hmon")  # bounds, work: (left, top, right, bottom)


_MI_SIZE = sizeof(MONITORINFOEXW)
# device name -> HMONITOR, as the last screens() found them: get_work_area asks that one monitor first
# (its answer names its device, so a stale or reused handle never counts)
_device_hmon: dict[str, int] = {}


def _monitor_info(hmon: int) -> MONITORINFOEXW | None:
    mi = MONITORINFOEXW()
    mi.cbSize = _MI_SIZE
    return mi if _GetMonitorInfoW(hmon, byref(mi)) else None


def screens() -> list[Screen]:
    """All attached displays, in the calling thread's DPI coordinates."""
    global _device_hmon
    result: list[Screen] = []
    add = result.append

    def cb(hmon, _hdc, _rc, _data):
        mi = _monitor_info(hmon)
        if mi is not None:
            add(Screen(mi.szDevice, mi.rcMonitor.tup(), mi.rcWork.tup(), bool(mi.dwFlags & MONITORINFOF_PRIMARY), hmon))
        return 1

    _EnumDisplayMonitors(0, None, MONITORENUMPROC(cb), 0)
    _device_hmon = {s.device: s.hmon for s in result}
    return result


def get_work_area(device: str) -> tuple[int, int, int, int] | None:
    """Work area of a display (e.g. \\\\.\\DISPLAY3) in the calling thread's DPI coordinates."""
    hmon = _device_hmon.get(device)
    if hmon:
        # (that one monitor asked directly: its answer names its device, so a stale handle never counts)
        mi = _monitor_info(hmon)
        if mi is not None and mi.szDevice == device:
            return mi.rcWork.tup()
    for s in screens():
        if s.device == device:
            return s.work
    return None


def pick_screen(scr_list, pos=None):
    """The screen containing `pos`, else the primary one, else the first; None if there is none
    (Windows briefly reports no displays while it reconfigures them)."""
    if pos is not None:
        for s in scr_list:
            if s.bounds[0] <= pos[0] < s.bounds[2] and s.bounds[1] <= pos[1] < s.bounds[3]:
                return s
    return next((s for s in scr_list if s.primary), scr_list[0] if scr_list else None)


def _monitor_device(hmon: int) -> str | None:
    if not hmon:
        return None
    mi = _monitor_info(hmon)
    return mi.szDevice if mi is not None else None


def cursor_pos() -> tuple[int, int] | None:
    pt = POINT()
    return (pt.x, pt.y) if _GetCursorPos(byref(pt)) else None


def monitor_device_under_cursor() -> str | None:
    pt = POINT()
    if not _GetCursorPos(byref(pt)):
        return None
    return _monitor_device(_MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST))


def displays_changed() -> None:
    """The displays changed (WM_DISPLAYCHANGE): the device → monitor mapping is dropped until
    screens() runs again (get_work_area checks it anyway: this only saves a wasted question)."""
    global _device_hmon
    _device_hmon = {}


def monitor_device_of_window(h: int) -> str | None:
    # (always asked of Windows: a monitor handle can name another display right after a change, and
    # a wrong answer puts the window on (or hides it with) the wrong monitor's desktop)
    return _monitor_device(_MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST))


def get_monitor_id(device: str) -> str:
    """Stable identity of the monitor shown on a display output: its device interface path.
    Unlike the \\\\.\\DISPLAYn name, it stays the same across reboots and reconnects. "" if unknown."""
    EDD_GET_DEVICE_INTERFACE_NAME, DISPLAY_DEVICE_ACTIVE = 1, 1
    for i in range(8):
        dd = DISPLAY_DEVICEW()
        dd.cb = sizeof(DISPLAY_DEVICEW)
        if not _EnumDisplayDevicesW(device, i, byref(dd), EDD_GET_DEVICE_INTERFACE_NAME):
            break
        if dd.StateFlags & DISPLAY_DEVICE_ACTIVE and dd.DeviceID:
            return dd.DeviceID
    return ""


_display_names: dict[str, str] = {}  # device interface path -> display name (a path's EDID never changes)


def edid_name(edid: bytes) -> str:
    """The monitor name an EDID holds (its 0xFC descriptor), "" if none."""
    for o in range(54, min(len(edid), 126), 18):
        d = edid[o:o + 18]
        if len(d) == 18 and d[0:3] == b"\0\0\0" and d[3] == 0xFC:
            return d[5:18].split(b"\n")[0].decode("cp437").strip()
    return ""


def display_name(monitor_id: str) -> str:
    """The display's name, from the monitor's own EDID (as Windows shows it in Settings); its
    hardware id ("ABC1234") when the EDID has no name. Unlike \\\\.\\DISPLAYn, the same monitor
    has the same name on any port. "" for an unknown monitor."""
    if not monitor_id:
        return ""
    name = _display_names.get(monitor_id)
    if name is None:
        # \\?\DISPLAY#<hardware id>#<instance>#{interface class}
        parts = monitor_id.split("#")
        name = parts[1] if len(parts) >= 3 else monitor_id
        if len(parts) >= 3:
            import winreg
            key = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters"
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    edid = winreg.QueryValueEx(k, "EDID")[0]
                if isinstance(edid, bytes):
                    name = edid_name(edid) or name
            except OSError:
                pass
        _display_names[monitor_id] = name
    return name


# ---------------------------------------------------------------- placement (saved layout)


_WP_SIZE = sizeof(WINDOWPLACEMENT)


def get_placement(h: int) -> WINDOWPLACEMENT | None:
    wp = WINDOWPLACEMENT()
    wp.length = _WP_SIZE
    return wp if _GetWindowPlacement(h, byref(wp)) else None


def set_placement(h: int, wp: WINDOWPLACEMENT) -> bool:
    wp.length = _WP_SIZE
    return bool(_SetWindowPlacement(h, byref(wp)))


def show_by_placement(h: int, wp: WINDOWPLACEMENT) -> bool:
    """SetWindowPlacement with a show command, without the DWM fade (like show_window): a window shown
    in its saved state appears at once, with the rest of its desktop."""
    _DwmSetTransitions(h, DWMWA_TRANSITIONS_FORCEDISABLED, _TRANSITIONS_OFF, 4)
    try:
        return set_placement(h, wp)
    finally:
        _DwmSetTransitions(h, DWMWA_TRANSITIONS_FORCEDISABLED, _TRANSITIONS_ON, 4)


class in_window_dpi_context:  # noqa: N801 - used like a function: `with in_window_dpi_context(h):`
    """Runs the block with this thread in the window's own DPI awareness. Get/SetWindowPlacement do
    not convert coordinates consistently between a per-monitor-aware caller and a DPI-unaware window,
    so placements are read, translated and written in the window's own coordinate space, together
    with monitor work areas from get_work_area."""
    __slots__ = ("_h", "_old")

    def __init__(self, h: int):
        self._h = h

    def __enter__(self):
        context = _GetWindowDpiAwarenessContext(self._h)
        self._old = _SetThreadDpiAwarenessContext(context) if context else 0

    def __exit__(self, *exc):
        if self._old:
            _SetThreadDpiAwarenessContext(self._old)


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def get_exe_path(h: int) -> str:
    """Full path of the program that owns the window ("" if it cannot be read)."""
    p = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, get_pid(h))
    if not p:
        return ""
    try:
        buf = create_unicode_buffer(1024)
        size = DWORD(1024)
        return buf.value if _QueryFullProcessImageNameW(p, 0, buf, byref(size)) else ""
    finally:
        CloseHandle(p)


def process_started(pid: int) -> int:
    """When a process started (FILETIME as an int; 0 if it cannot be read). With the pid it names one
    process for sure: a pid can be used again, its start time can't, and setting the clock later
    does not change it."""
    p = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else 0
    if not p:
        return 0
    try:
        created, ended, kernel, user = c_ulonglong(), c_ulonglong(), c_ulonglong(), c_ulonglong()
        ok = _GetProcessTimes(p, byref(created), byref(ended), byref(kernel), byref(user))
        return created.value if ok else 0
    finally:
        CloseHandle(p)


_cmdline_api: list = []  # NtQueryInformationProcess, declared on first use (Restore Applications On Restart)


def process_command_line(pid: int) -> str:
    """The command line a process was started with ("" if it can't be read: an elevated process, or
    one already gone). Only for processes of this user."""
    if not _cmdline_api:
        _cmdline_api.append(_f(_load("ntdll.dll"), "NtQueryInformationProcess", c_long, H, c_ulong, c_void_p,
                               c_ulong, POINTER(c_ulong)))
    p = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else 0
    if not p:
        return ""
    try:
        size = c_ulong(0)
        buf = ctypes.create_string_buffer(4096)
        status = _cmdline_api[0](p, 60, buf, len(buf), byref(size))  # ProcessCommandLineInformation
        if status != 0 and size.value > len(buf):  # (STATUS_INFO_LENGTH_MISMATCH: a longer one)
            buf = ctypes.create_string_buffer(size.value)
            status = _cmdline_api[0](p, 60, buf, len(buf), byref(size))
        if status != 0:
            return ""
        # UNICODE_STRING {USHORT Length; USHORT MaximumLength; PWSTR Buffer} at the start of the buffer
        length = int.from_bytes(buf.raw[0:2], "little")
        address = c_void_p.from_buffer(buf, sizeof(c_void_p)).value  # (Buffer: aligned after the two USHORTs)
        return ctypes.wstring_at(address, length // 2) if address and length else ""
    finally:
        CloseHandle(p)


_GetApplicationUserModelId = _f(kernel32, "GetApplicationUserModelId", c_long, H, POINTER(c_uint), c_wchar_p)
_FindWindowExW = _f(user32, "FindWindowExW", H, H, H, c_wchar_p, c_wchar_p)
FRAME_CLASS, CORE_CLASS = "ApplicationFrameWindow", "Windows.UI.Core.CoreWindow"


def _process_app_id(pid: int) -> str:
    """The app id (AppUserModelID) of a packaged app's process ("" for other programs)."""
    p = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else 0
    if not p:
        return ""
    try:
        size = c_uint(256)
        buf = create_unicode_buffer(256)
        return buf.value if _GetApplicationUserModelId(p, byref(size), buf) == 0 else ""
    finally:
        CloseHandle(p)


def window_app_id(h: int) -> str:
    """The app id (AppUserModelID) of a packaged app's window (Store apps, packaged programs): what
    starts it again (shell:AppsFolder\\<id>). A Store app's frame belongs to the shared host: the
    app's own window inside it says which app it is. "" for other programs."""
    if get_class(h) == FRAME_CLASS:
        core = _FindWindowExW(h, 0, CORE_CLASS, None)
        return _process_app_id(get_pid(core)) if core else ""
    return _process_app_id(get_pid(h))


def running_app_ids() -> set[str]:
    """The app ids of the packaged apps running now."""
    out = set()
    for pid in _all_pids():
        app = _process_app_id(pid)
        if app:
            out.add(app)
    return out


def start_app(app_id: str) -> None:
    """Starts a packaged app by its app id, as the Start menu does (raises OSError if it can't)."""
    import subprocess
    explorer = os.path.join(os.path.dirname(_SYS32), "explorer.exe")
    subprocess.Popen([explorer, "shell:AppsFolder\\" + app_id], close_fds=True)


_EnumProcesses = _f(kernel32, "K32EnumProcesses", BOOL, POINTER(DWORD), DWORD, POINTER(DWORD))


def _all_pids() -> list[int]:
    pids = (DWORD * 4096)()
    got = DWORD(0)
    if not _EnumProcesses(pids, sizeof(pids), byref(got)):
        return []
    return [p for p in pids[:got.value // sizeof(DWORD)] if p]


def running_programs() -> set[str]:
    """Lowercased paths of the programs running now (those this user can query)."""
    out = set()
    buf = create_unicode_buffer(1024)
    for pid in _all_pids():
        p = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not p:
            continue
        try:
            size = DWORD(1024)
            if _QueryFullProcessImageNameW(p, 0, buf, byref(size)):
                out.add(buf.value.lower())
        finally:
            CloseHandle(p)
    return out


tick_count64 = _GetTickCount64


_GetDriveTypeW = _f(kernel32, "GetDriveTypeW", UINT, c_wchar_p)
DRIVE_FIXED = 3


def is_fixed_drive(root: str) -> bool:
    """A local fixed disk (C:\\ ...): never "offline", however slowly it answers (any other drive, a
    network one above all, may wait for a timeout)."""
    return _GetDriveTypeW(root) == DRIVE_FIXED


def wall_time() -> float:
    """Seconds since 1970 (survives reboots, unlike tick_count64)."""
    return time.time()


# ---------------------------------------------------------------- previews


def print_window(h: int, hdc: int) -> bool:
    return bool(_PrintWindow(h, hdc, PW_RENDERFULLCONTENT))


def dwm_register_thumbnail(dest: int, src: int) -> int:
    tid = H(0)
    return tid.value if _DwmRegisterThumbnail(dest, src, byref(tid)) == 0 and tid.value else 0


def dwm_unregister_thumbnail(tid: int) -> None:
    _DwmUnregisterThumbnail(tid)


def dwm_thumbnail_source_size(tid: int) -> tuple[int, int] | None:
    s = SIZE()
    return (s.cx, s.cy) if _DwmQueryThumbnailSourceSize(tid, byref(s)) == 0 else None


def dwm_update_thumbnail(tid: int, visible: bool, dest=None, src=None) -> None:
    p = DWM_THUMBNAIL_PROPERTIES()
    p.dwFlags = (DWM_TNP_VISIBLE | DWM_TNP_RECTDESTINATION | DWM_TNP_RECTSOURCE | DWM_TNP_OPACITY
                 | DWM_TNP_SOURCECLIENTAREAONLY)
    p.fVisible = 1 if visible else 0
    p.opacity = 255
    p.fSourceClientAreaOnly = 0
    if visible:
        p.rcDestination = RECT(*dest)
        p.rcSource = RECT(*src)
    _DwmUpdateThumbnailProperties(tid, byref(p))


def get_window_small_icon(h: int) -> int:
    """A copy of the window's small icon (destroy it with destroy_icon), or 0. Uses a timeout so
    unresponsive apps cannot hang us."""
    res = c_size_t(0)
    _SendMessageTimeoutW(h, WM_GETICON, ICON_SMALL2, 0, SMTO_ABORTIFHUNG, 120, byref(res))
    hicon = res.value or _GetClassLongPtrW(h, GCLP_HICONSM)
    return _CopyIcon(hicon) if hicon else 0


def destroy_icon(hicon: int) -> None:
    if hicon:
        _DestroyIcon(hicon)
