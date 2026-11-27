"""Minimal Win32 GUI layer: window classes, message loop, timers, double-buffered painting,
GDI text and antialiased GDI+ shapes. Pure ctypes.

Every window procedure runs inside a guard: an exception in a callback calls the fatal handler
(which restores hidden windows) instead of being printed and ignored by ctypes.
"""
from __future__ import annotations

import ctypes
from ctypes import (POINTER, Structure, WINFUNCTYPE, byref, c_float, c_int, c_long, c_size_t,
                    c_uint, c_ushort, c_void_p, c_wchar_p, sizeof)
from collections.abc import Callable

from . import native as N
from .native import BOOL, DWORD, H, LPARAM, LRESULT, UINT, WPARAM, _f, gdi32, gdiplus, kernel32, user32

# ---------------------------------------------------------------- constants

WM_DESTROY = 0x0002
WM_ACTIVATE = 0x0006
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ERASEBKGND = 0x0014
WM_ENDSESSION = 0x0016
WM_SETCURSOR = 0x0020
WM_MOUSEACTIVATE = 0x0021
WM_NCDESTROY = 0x0082
WM_KEYDOWN = 0x0100
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_VSCROLL = 0x0115
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_MOUSELEAVE = 0x02A3
WM_CAPTURECHANGED = 0x0215
WM_DPICHANGED = 0x02E0
WM_NULL = 0x0000
WM_USER = 0x0400
WM_APP = 0x8000

WA_INACTIVE = 0
MA_NOACTIVATE = 3

IDC_ARROW = 32512
IDC_HAND = 32649
IDC_SIZEALL = 32646
IDC_NO = 32648

SRCCOPY = 0x00CC0020
HALFTONE = 4
TRANSPARENT = 1

DT_LEFT = 0x0
DT_CENTER = 0x1
DT_RIGHT = 0x2
DT_VCENTER = 0x4
DT_WORDBREAK = 0x10
DT_SINGLELINE = 0x20
DT_NOPREFIX = 0x800
DT_CALCRECT = 0x400
DT_END_ELLIPSIS = 0x8000

MB_OK = 0x0
MB_YESNO = 0x4
MB_ICONERROR = 0x10
MB_ICONQUESTION = 0x20
MB_ICONINFORMATION = 0x40
MB_DEFBUTTON2 = 0x100
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6

MF_STRING = 0x0
MF_SEPARATOR = 0x800
TPM_RIGHTBUTTON = 0x2
TPM_RETURNCMD = 0x100
TPM_NONOTIFY = 0x80

SB_VERT = 1
SIF_TRACKPOS = 0x10
SIF_ALL = 0x17
SB_LINEUP, SB_LINEDOWN, SB_PAGEUP, SB_PAGEDOWN, SB_THUMBPOSITION, SB_THUMBTRACK, SB_TOP, SB_BOTTOM = 0, 1, 2, 3, 4, 5, 6, 7

SM_CXDRAG = 68
SM_CYDRAG = 69

TME_LEAVE = 0x2
LWA_ALPHA = 0x2

# ---------------------------------------------------------------- structures

WNDPROC = WINFUNCTYPE(LRESULT, H, UINT, WPARAM, LPARAM)


class WNDCLASSEXW(Structure):
    _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c_int),
                ("cbWndExtra", c_int), ("hInstance", H), ("hIcon", H), ("hCursor", H),
                ("hbrBackground", H), ("lpszMenuName", c_wchar_p), ("lpszClassName", c_wchar_p),
                ("hIconSm", H)]


class MSG(Structure):
    _fields_ = [("hwnd", H), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                ("time", DWORD), ("pt", N.POINT), ("lPrivate", DWORD)]


class PAINTSTRUCT(Structure):
    _fields_ = [("hdc", H), ("fErase", BOOL), ("rcPaint", N.RECT), ("fRestore", BOOL),
                ("fIncUpdate", BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class TRACKMOUSEEVENT(Structure):
    _fields_ = [("cbSize", DWORD), ("dwFlags", DWORD), ("hwndTrack", H), ("dwHoverTime", DWORD)]


class SCROLLINFO(Structure):
    _fields_ = [("cbSize", UINT), ("fMask", UINT), ("nMin", c_int), ("nMax", c_int), ("nPage", UINT),
                ("nPos", c_int), ("nTrackPos", c_int)]


class BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", c_long), ("biHeight", c_long), ("biPlanes", c_ushort),
                ("biBitCount", c_ushort), ("biCompression", DWORD), ("biSizeImage", DWORD),
                ("biXPelsPerMeter", c_long), ("biYPelsPerMeter", c_long), ("biClrUsed", DWORD),
                ("biClrImportant", DWORD)]


class BITMAPINFO(Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


class ICONINFO(Structure):
    _fields_ = [("fIcon", BOOL), ("xHotspot", DWORD), ("yHotspot", DWORD), ("hbmMask", H), ("hbmColor", H)]


# ---------------------------------------------------------------- functions

GetModuleHandleW = _f(kernel32, "GetModuleHandleW", H, c_wchar_p)
RegisterClassExW = _f(user32, "RegisterClassExW", c_ushort, POINTER(WNDCLASSEXW))
CreateWindowExW = _f(user32, "CreateWindowExW", H, DWORD, c_wchar_p, c_wchar_p, DWORD, c_int, c_int,
                     c_int, c_int, H, H, H, c_void_p)
DestroyWindow = _f(user32, "DestroyWindow", BOOL, H)
DefWindowProcW = _f(user32, "DefWindowProcW", LRESULT, H, UINT, WPARAM, LPARAM)
GetMessageW = _f(user32, "GetMessageW", BOOL, POINTER(MSG), H, UINT, UINT)
TranslateMessage = _f(user32, "TranslateMessage", BOOL, POINTER(MSG))
DispatchMessageW = _f(user32, "DispatchMessageW", LRESULT, POINTER(MSG))
PostMessageW = _f(user32, "PostMessageW", BOOL, H, UINT, WPARAM, LPARAM)
SendMessageW = _f(user32, "SendMessageW", LRESULT, H, UINT, WPARAM, c_void_p)
PostQuitMessage = _f(user32, "PostQuitMessage", None, c_int)
SetTimer = _f(user32, "SetTimer", c_size_t, H, c_size_t, UINT, c_void_p)
KillTimer = _f(user32, "KillTimer", BOOL, H, c_size_t)
BeginPaint = _f(user32, "BeginPaint", H, H, POINTER(PAINTSTRUCT))
EndPaint = _f(user32, "EndPaint", BOOL, H, POINTER(PAINTSTRUCT))
InvalidateRect = _f(user32, "InvalidateRect", BOOL, H, c_void_p, BOOL)
UpdateWindow = _f(user32, "UpdateWindow", BOOL, H)
GetClientRect = _f(user32, "GetClientRect", BOOL, H, POINTER(N.RECT))
LoadCursorW = _f(user32, "LoadCursorW", H, H, c_void_p)
SetCursor = _f(user32, "SetCursor", H, H)
SetWindowRgn = _f(user32, "SetWindowRgn", c_int, H, H, BOOL)
MessageBoxW = _f(user32, "MessageBoxW", c_int, H, c_wchar_p, c_wchar_p, UINT)
CreatePopupMenu = _f(user32, "CreatePopupMenu", H)
AppendMenuW = _f(user32, "AppendMenuW", BOOL, H, UINT, c_size_t, c_wchar_p)
TrackPopupMenu = _f(user32, "TrackPopupMenu", c_int, H, UINT, c_int, c_int, c_int, H, c_void_p)
DestroyMenu = _f(user32, "DestroyMenu", BOOL, H)
SetLayeredWindowAttributes = _f(user32, "SetLayeredWindowAttributes", BOOL, H, DWORD, ctypes.c_ubyte, DWORD)
ShowWindow = _f(user32, "ShowWindow", BOOL, H, c_int)
SetWindowPos = _f(user32, "SetWindowPos", BOOL, H, H, c_int, c_int, c_int, c_int, UINT)
SetCapture = _f(user32, "SetCapture", H, H)
ReleaseCapture = _f(user32, "ReleaseCapture", BOOL)
GetCapture = _f(user32, "GetCapture", H)
ClientToScreen = _f(user32, "ClientToScreen", BOOL, H, POINTER(N.POINT))
ScreenToClient = _f(user32, "ScreenToClient", BOOL, H, POINTER(N.POINT))
TrackMouseEvent = _f(user32, "TrackMouseEvent", BOOL, POINTER(TRACKMOUSEEVENT))
GetSystemMetricsForDpi = _f(user32, "GetSystemMetricsForDpi", c_int, c_int, UINT)
SetScrollInfo = _f(user32, "SetScrollInfo", c_int, H, c_int, POINTER(SCROLLINFO), BOOL)
GetScrollInfo = _f(user32, "GetScrollInfo", BOOL, H, c_int, POINTER(SCROLLINFO))
RegisterWindowMessageW = _f(user32, "RegisterWindowMessageW", UINT, c_wchar_p)
CreateIconIndirect = _f(user32, "CreateIconIndirect", H, POINTER(ICONINFO))
GetDC = _f(user32, "GetDC", H, H)
ReleaseDC = _f(user32, "ReleaseDC", c_int, H, H)
FillRect = _f(user32, "FillRect", c_int, H, POINTER(N.RECT), H)
DrawTextW = _f(user32, "DrawTextW", c_int, H, c_wchar_p, c_int, POINTER(N.RECT), UINT)
DrawIconEx = _f(user32, "DrawIconEx", BOOL, H, c_int, c_int, H, c_int, c_int, UINT, H, UINT)
SetForegroundWindow = _f(user32, "SetForegroundWindow", BOOL, H)
SetFocus = _f(user32, "SetFocus", H, H)

CreateCompatibleDC = _f(gdi32, "CreateCompatibleDC", H, H)
CreateCompatibleBitmap = _f(gdi32, "CreateCompatibleBitmap", H, H, c_int, c_int)
CreateDIBSection = _f(gdi32, "CreateDIBSection", H, H, POINTER(BITMAPINFO), UINT, POINTER(c_void_p), H, DWORD)
CreateBitmap = _f(gdi32, "CreateBitmap", H, c_int, c_int, UINT, UINT, c_void_p)
SelectObject = _f(gdi32, "SelectObject", H, H, H)
DeleteObject = _f(gdi32, "DeleteObject", BOOL, H)
DeleteDC = _f(gdi32, "DeleteDC", BOOL, H)
BitBlt = _f(gdi32, "BitBlt", BOOL, H, c_int, c_int, c_int, c_int, H, c_int, c_int, DWORD)
StretchBlt = _f(gdi32, "StretchBlt", BOOL, H, c_int, c_int, c_int, c_int, H, c_int, c_int, c_int, c_int, DWORD)
SetStretchBltMode = _f(gdi32, "SetStretchBltMode", c_int, H, c_int)
SetBrushOrgEx = _f(gdi32, "SetBrushOrgEx", BOOL, H, c_int, c_int, c_void_p)
CreateSolidBrush = _f(gdi32, "CreateSolidBrush", H, DWORD)
CreateFontW = _f(gdi32, "CreateFontW", H, c_int, c_int, c_int, c_int, c_int, DWORD, DWORD, DWORD, DWORD,
                 DWORD, DWORD, DWORD, DWORD, c_wchar_p)
SetTextColor = _f(gdi32, "SetTextColor", DWORD, H, DWORD)
SetBkMode = _f(gdi32, "SetBkMode", c_int, H, c_int)
CreateRectRgn = _f(gdi32, "CreateRectRgn", H, c_int, c_int, c_int, c_int)

# ---------------------------------------------------------------- helpers


def rgb(r: int, g: int, b: int) -> int:
    """COLORREF for GDI."""
    return r | (g << 8) | (b << 16)


def argb(r: int, g: int, b: int) -> int:
    """Opaque ARGB for GDI+."""
    return (255 << 24) | (r << 16) | (g << 8) | b


def loword(v: int) -> int:
    return v & 0xFFFF


def hiword(v: int) -> int:
    return (v >> 16) & 0xFFFF


def signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def mouse_xy(lp: int) -> tuple[int, int]:
    return signed16(lp), signed16(lp >> 16)


def message_box(text: str, flags: int) -> int:
    return MessageBoxW(0, text, "DisplayDesktop", flags | MB_SETFOREGROUND | MB_TOPMOST)


_cursors: dict[int, int] = {}


def set_cursor(idc: int) -> None:
    if idc not in _cursors:
        _cursors[idc] = LoadCursorW(0, idc)
    SetCursor(_cursors[idc])


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = N.RECT()
    GetClientRect(hwnd, byref(r))
    return r.tup()


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    p = N.POINT(x, y)
    ClientToScreen(hwnd, byref(p))
    return p.x, p.y


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    p = N.POINT(x, y)
    ScreenToClient(hwnd, byref(p))
    return p.x, p.y


def create_dib(w: int, h: int, bpp: int = 32) -> tuple[int, int]:
    """Top-down DIB section: (HBITMAP, pointer to the pixels: BGRA, or BGR rows padded to 4 bytes
    with bpp=24)."""
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = bpp
    bits = c_void_p()
    hbm = CreateDIBSection(0, byref(bi), 0, byref(bits), 0, 0)
    return hbm, (bits.value or 0)


# ---------------------------------------------------------------- fonts and text

_fonts: dict[tuple, int] = {}


def font(pt: float, dpi: int, bold: bool = False, italic: bool = False) -> int:
    """Cached GDI font (lives for the whole process; there are only a handful)."""
    key = (pt, dpi, bold, italic)
    if key not in _fonts:
        height = -int(round(pt * dpi / 72.0))
        _fonts[key] = CreateFontW(height, 0, 0, 0, 700 if bold else 400, 1 if italic else 0, 0, 0,
                                  1, 0, 0, 5, 0, "Segoe UI")  # charset 1 = DEFAULT, quality 5 = CLEARTYPE
    return _fonts[key]


def draw_text(hdc: int, text: str, hfont: int, rect, color: int, flags: int) -> None:
    old = SelectObject(hdc, hfont)
    SetBkMode(hdc, TRANSPARENT)
    SetTextColor(hdc, color)
    r = N.RECT(*rect)
    DrawTextW(hdc, text, -1, byref(r), flags | DT_NOPREFIX)
    SelectObject(hdc, old)


def measure_text(hdc: int, text: str, hfont: int, max_width: int = 0, flags: int = 0) -> tuple[int, int]:
    old = SelectObject(hdc, hfont)
    r = N.RECT(0, 0, max_width or 100000, 0)
    DrawTextW(hdc, text, -1, byref(r), flags | DT_CALCRECT | DT_NOPREFIX)
    SelectObject(hdc, old)
    return r.right - r.left, r.bottom - r.top


def fill_rect(hdc: int, rect, color: int) -> None:
    b = CreateSolidBrush(color)
    r = N.RECT(*rect)
    FillRect(hdc, byref(r), b)
    DeleteObject(b)


# ---------------------------------------------------------------- GDI+ (antialiased shapes)


class _GdiplusStartupInput(Structure):
    _fields_ = [("GdiplusVersion", c_uint), ("DebugEventCallback", c_void_p),
                ("SuppressBackgroundThread", BOOL), ("SuppressExternalCodecs", BOOL)]


_GdiplusStartup = _f(gdiplus, "GdiplusStartup", c_int, POINTER(c_size_t), POINTER(_GdiplusStartupInput), c_void_p)
_GdipCreateFromHDC = _f(gdiplus, "GdipCreateFromHDC", c_int, H, POINTER(c_void_p))
_GdipDeleteGraphics = _f(gdiplus, "GdipDeleteGraphics", c_int, c_void_p)
_GdipSetSmoothingMode = _f(gdiplus, "GdipSetSmoothingMode", c_int, c_void_p, c_int)
_GdipCreatePath = _f(gdiplus, "GdipCreatePath", c_int, c_int, POINTER(c_void_p))
_GdipDeletePath = _f(gdiplus, "GdipDeletePath", c_int, c_void_p)
_GdipAddPathArc = _f(gdiplus, "GdipAddPathArc", c_int, c_void_p, c_float, c_float, c_float, c_float, c_float, c_float)
_GdipClosePathFigure = _f(gdiplus, "GdipClosePathFigure", c_int, c_void_p)
_GdipCreatePen1 = _f(gdiplus, "GdipCreatePen1", c_int, c_uint, c_float, c_int, POINTER(c_void_p))
_GdipSetPenDashStyle = _f(gdiplus, "GdipSetPenDashStyle", c_int, c_void_p, c_int)
_GdipDeletePen = _f(gdiplus, "GdipDeletePen", c_int, c_void_p)
_GdipCreateSolidFill = _f(gdiplus, "GdipCreateSolidFill", c_int, c_uint, POINTER(c_void_p))
_GdipDeleteBrush = _f(gdiplus, "GdipDeleteBrush", c_int, c_void_p)
_GdipFillPath = _f(gdiplus, "GdipFillPath", c_int, c_void_p, c_void_p, c_void_p)
_GdipDrawPath = _f(gdiplus, "GdipDrawPath", c_int, c_void_p, c_void_p, c_void_p)
_GdipDrawRectangleI = _f(gdiplus, "GdipDrawRectangleI", c_int, c_void_p, c_void_p, c_int, c_int, c_int, c_int)

_gdiplus_token = c_size_t(0)


def gdiplus_startup() -> None:
    if not _gdiplus_token.value:
        inp = _GdiplusStartupInput(1, None, False, False)
        _GdiplusStartup(byref(_gdiplus_token), byref(inp), None)


class Shapes:
    """Antialiased shapes on an HDC (GDI+). Use as a context manager; draw GDI text after it closes."""

    def __init__(self, hdc: int):
        gdiplus_startup()
        self.g = c_void_p()
        _GdipCreateFromHDC(hdc, byref(self.g))
        _GdipSetSmoothingMode(self.g, 4)  # AntiAlias

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.g:
            _GdipDeleteGraphics(self.g)
            self.g = c_void_p()

    @staticmethod
    def _round_path(rect, radius: float):
        x, y, r, b = rect
        d = radius * 2
        path = c_void_p()
        _GdipCreatePath(0, byref(path))
        _GdipAddPathArc(path, x, y, d, d, 180, 90)
        _GdipAddPathArc(path, r - d, y, d, d, 270, 90)
        _GdipAddPathArc(path, r - d, b - d, d, d, 0, 90)
        _GdipAddPathArc(path, x, b - d, d, d, 90, 90)
        _GdipClosePathFigure(path)
        return path

    def fill_round(self, rect, radius: float, color: int) -> None:
        path = self._round_path(rect, radius)
        brush = c_void_p()
        _GdipCreateSolidFill(color, byref(brush))
        _GdipFillPath(self.g, brush, path)
        _GdipDeleteBrush(brush)
        _GdipDeletePath(path)

    def draw_round(self, rect, radius: float, color: int, width: float, dashed: bool = False) -> None:
        path = self._round_path(rect, radius)
        pen = self._pen(color, width, dashed)
        _GdipDrawPath(self.g, pen, path)
        _GdipDeletePen(pen)
        _GdipDeletePath(path)

    def draw_rect(self, x: int, y: int, w: int, h: int, color: int, width: float, dashed: bool = False) -> None:
        pen = self._pen(color, width, dashed)
        _GdipDrawRectangleI(self.g, pen, x, y, w, h)
        _GdipDeletePen(pen)

    @staticmethod
    def _pen(color: int, width: float, dashed: bool):
        pen = c_void_p()
        _GdipCreatePen1(color, width, 2, byref(pen))  # UnitPixel
        if dashed:
            _GdipSetPenDashStyle(pen, 1)
        return pen


# ---------------------------------------------------------------- windows

_on_fatal: Callable[[BaseException], None] | None = None


def set_fatal_handler(handler: Callable[[BaseException], None]) -> None:
    global _on_fatal
    _on_fatal = handler


def report_fatal(exc: BaseException) -> None:
    if _on_fatal is not None:
        _on_fatal(exc)
    else:
        raise exc


class Window:
    """Base for a Win32 window driven from Python. Subclasses override on_message, returning an
    int to handle a message or None for the default processing."""

    _registry: dict[int, "Window"] = {}
    _creating: "Window | None" = None
    _classes: set[str] = set()
    _hinst = GetModuleHandleW(None)

    def __init__(self):
        self.hwnd = 0
        self._timers: dict[int, Callable[[], None]] = {}

    @staticmethod
    def _register(name: str) -> None:
        if name in Window._classes:
            return
        wc = WNDCLASSEXW()
        wc.cbSize = sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _WNDPROC
        wc.hInstance = Window._hinst
        wc.hCursor = LoadCursorW(0, IDC_ARROW)
        wc.lpszClassName = name
        if not RegisterClassExW(byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        Window._classes.add(name)

    def create(self, class_name: str, ex_style: int, style: int, x: int, y: int, w: int, h: int,
               title: str = "", owner: int = 0) -> int:
        Window._register(class_name)
        Window._creating = self
        try:
            hwnd = CreateWindowExW(ex_style, class_name, title, style, x, y, w, h, owner, 0, Window._hinst, None)
        finally:
            Window._creating = None
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd
        Window._registry[hwnd] = self
        return hwnd

    def destroy(self) -> None:
        if self.hwnd:
            DestroyWindow(self.hwnd)

    def set_timer(self, tid: int, ms: int, callback: Callable[[], None]) -> None:
        self._timers[tid] = callback
        SetTimer(self.hwnd, tid, ms, None)

    def kill_timer(self, tid: int) -> None:
        if self._timers.pop(tid, None) is not None and self.hwnd:
            KillTimer(self.hwnd, tid)

    def invalidate(self) -> None:
        if self.hwnd:
            InvalidateRect(self.hwnd, None, False)

    def on_message(self, msg: int, wp: int, lp: int) -> int | None:
        return None

    def on_destroyed(self) -> None:
        """WM_NCDESTROY: the handle is gone."""


def _dispatch(hwnd: int, msg: int, wp: int, lp: int) -> int:
    obj = Window._registry.get(hwnd)
    if obj is None and Window._creating is not None:
        obj = Window._creating
        obj.hwnd = hwnd
        Window._registry[hwnd] = obj
    if obj is None:
        return DefWindowProcW(hwnd, msg, wp, lp)
    try:
        if msg == WM_TIMER and wp in obj._timers:
            obj._timers[wp]()
            return 0
        result = obj.on_message(msg, wp, lp)
    except BaseException as exc:  # noqa: BLE001 - every error must restore hidden windows
        result = None
        report_fatal(exc)
    if msg == WM_NCDESTROY:
        Window._registry.pop(hwnd, None)
        obj.hwnd = 0
        obj._timers.clear()
        try:
            obj.on_destroyed()
        except BaseException as exc:  # noqa: BLE001
            report_fatal(exc)
    if result is not None:
        return result
    return DefWindowProcW(hwnd, msg, wp, lp)


_WNDPROC = WNDPROC(_dispatch)  # module-level: must outlive every window


class BufferedPaint:
    """WM_PAINT into an off-screen bitmap, then one blit (no flicker)."""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.ps = PAINTSTRUCT()

    def __enter__(self) -> tuple[int, int, int]:
        self.hdc = BeginPaint(self.hwnd, byref(self.ps))
        _, _, self.w, self.h = client_rect(self.hwnd)
        self.mem = CreateCompatibleDC(self.hdc)
        self.bmp = CreateCompatibleBitmap(self.hdc, max(1, self.w), max(1, self.h))
        self.old = SelectObject(self.mem, self.bmp)
        return self.mem, self.w, self.h

    def __exit__(self, *exc):
        BitBlt(self.hdc, 0, 0, self.w, self.h, self.mem, 0, 0, SRCCOPY)
        SelectObject(self.mem, self.old)
        DeleteObject(self.bmp)
        DeleteDC(self.mem)
        EndPaint(self.hwnd, byref(self.ps))


dialogs: set[int] = set()  # top-level windows with child controls: get dialog keys (Tab, Enter, Esc)
IsDialogMessageW = _f(user32, "IsDialogMessageW", BOOL, H, POINTER(MSG))


def run_until(done) -> None:
    """A message loop that returns once `done()` is true (a modal dialog before the app's own loop)."""
    msg = MSG()
    pm = byref(msg)
    while not done():
        r = GetMessageW(pm, 0, 0, 0)
        if r == 0 or r == -1:
            if r == 0:
                PostQuitMessage(msg.wParam)  # (a quit is for the main loop too)
            return
        if dialogs and any(IsDialogMessageW(h, pm) for h in tuple(dialogs)):
            continue
        TranslateMessage(pm)
        DispatchMessageW(pm)


def run_message_loop() -> None:
    msg = MSG()
    pm = byref(msg)  # (one reference to the one MSG, for every call)
    while True:
        r = GetMessageW(pm, 0, 0, 0)
        if r == 0 or r == -1:
            return
        if dialogs and any(IsDialogMessageW(h, pm) for h in tuple(dialogs)):
            continue
        TranslateMessage(pm)
        DispatchMessageW(pm)
