"""The tray menu's items, drawn by the app (owner-drawn) so the keyboard shortcuts can sit in a
right-aligned column with clear arrow keys.

A standard menu draws every item in one font, and Segoe UI's arrows (← ↑ →) are thin; ↑ is narrow
enough to look squished. Here the label and "Ctrl+Alt+" use Segoe UI at FONT_PT (Windows' 9 pt menu font) and the arrow
uses Segoe Fluent Icons (else Segoe MDL2 Assets; plain Segoe UI arrows if neither exists),
whose arrows are even and easy to see. The background comes from the Windows menu theme (uxtheme
"MENU"); the hover highlight and separators are drawn in the Windows 11 style (a light rounded
highlight, a thin line), because the theme's own versions are the older flat blue/grey ones. Separators
are owner-drawn too (ID 0): mixed with owner-drawn items the system draws them as grey bands.
Sizes follow the DPI of the monitor the menu opens on (the one under the mouse).
"""
from __future__ import annotations

import ctypes
from collections import namedtuple
from ctypes import POINTER, Structure, byref, c_long, c_size_t, c_uint, c_wchar_p

from . import gui as G
from . import native as N

WM_DRAWITEM, WM_MEASUREITEM = 0x002B, 0x002C
MF_OWNERDRAW, MF_SEPARATOR = 0x0100, 0x0800
SEPARATOR_ID = 0
HOVER_ARGB = 0x0F000000     # black at 6% over the menu background (Windows 11 menu hover)
LINE_COLOR = 0xE0E0E0      # separator line (COLORREF: same in BGR)
ODT_MENU = 1
ODS_SELECTED = 0x0001
MENU_POPUPBACKGROUND, MENU_POPUPITEM = 9, 14
MPI_NORMAL = 1
TMT_TEXTCOLOR = 3803
COLOR_MENU, COLOR_MENUTEXT = 4, 7
TEXT_LEFT, TEXT_RIGHT = 12, 12  # text insets at 96 DPI
FONT_PT = 9        # Windows' default menu font size
ARROW_PT = 9 * 1.25  # (user) the shortcut arrows stay this size: even and easy to see
ITEM_HEIGHT = 21   # at 96 DPI (for the 9 pt font)
SHORTCUT_GAP = 24  # least space between a label and its shortcut at 96 DPI (the menu is as wide as it needs)

ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
ICON_ARROWS = {"left": "", "up": "", "right": ""}  # Back, Up, Forward
TEXT_ARROWS = {"left": "←", "up": "↑", "right": "→"}  # fallback (no icon font)


# keys: e.g. "Ctrl+Alt+" (shown right-aligned, before the arrow); arrow: "left", "up" or "right"
Item = namedtuple("Item", "id label keys arrow", defaults=("", ""))


class MEASUREITEMSTRUCT(Structure):
    _fields_ = [("CtlType", c_uint), ("CtlID", c_uint), ("itemID", c_uint), ("itemWidth", c_uint),
                ("itemHeight", c_uint), ("itemData", c_size_t)]


class DRAWITEMSTRUCT(Structure):
    _fields_ = [("CtlType", c_uint), ("CtlID", c_uint), ("itemID", c_uint), ("itemAction", c_uint),
                ("itemState", c_uint), ("hwndItem", N.H), ("hDC", N.H), ("rcItem", N.RECT),
                ("itemData", c_size_t)]


_ux = N._load("uxtheme.dll")
_OpenThemeDataForDpi = N._f(_ux, "OpenThemeDataForDpi", N.H, N.H, c_wchar_p, c_uint)
_CloseThemeData = N._f(_ux, "CloseThemeData", c_long, N.H)
_DrawThemeBackground = N._f(_ux, "DrawThemeBackground", c_long, N.H, N.H, ctypes.c_int, ctypes.c_int,
                            POINTER(N.RECT), POINTER(N.RECT))
_GetThemeColor = N._f(_ux, "GetThemeColor", c_long, N.H, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      POINTER(N.DWORD))
_GetDpiForMonitor = N._f(N._load("shcore.dll"), "GetDpiForMonitor", c_long, N.H, ctypes.c_int,
                         POINTER(c_uint), POINTER(c_uint))
_GetSysColor = N._f(N.user32, "GetSysColor", N.DWORD, ctypes.c_int)
_GetTextFaceW = N._f(N.gdi32, "GetTextFaceW", ctypes.c_int, N.H, ctypes.c_int, c_wchar_p)
_GetTextExtentPoint32W = N._f(N.gdi32, "GetTextExtentPoint32W", N.BOOL, N.H, c_wchar_p, ctypes.c_int,
                              POINTER(N.POINT))
_MONITOR_DEFAULTTONEAREST = 2


def dpi_at(pos: tuple[int, int]) -> int:
    """Effective DPI of the monitor at a screen point (96 if unknown)."""
    x, y = c_uint(96), c_uint(96)
    hmon = N._MonitorFromPoint(N.POINT(*pos), _MONITOR_DEFAULTTONEAREST)
    return x.value if hmon and _GetDpiForMonitor(hmon, 0, byref(x), byref(y)) == 0 else 96


_icon_face: str | None = None


def icon_face() -> str:
    """The first installed icon font, or "" (then plain arrows are used)."""
    global _icon_face
    if _icon_face is None:
        _icon_face = ""
        dc = G.CreateCompatibleDC(0)
        try:
            for face in ICON_FONTS:
                f = G.CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, face)
                old = G.SelectObject(dc, f)
                buf = ctypes.create_unicode_buffer(64)
                _GetTextFaceW(dc, 64, buf)
                G.SelectObject(dc, old)
                G.DeleteObject(f)
                if buf.value.lower() == face.lower():  # GDI substitutes a missing font silently
                    _icon_face = face
                    break
        finally:
            G.DeleteDC(dc)
    return _icon_face


def arrow_glyph(arrow: str) -> tuple[str, bool]:
    """(character, uses the icon font)."""
    if icon_face():
        return ICON_ARROWS[arrow], True
    return TEXT_ARROWS[arrow], False


class MenuPainter:
    """Measures and draws the owner-drawn items of one popup menu (one per show_menu call)."""

    def __init__(self, hwnd: int, items: list[Item], dpi: int):
        self.items = {it.id: it for it in items}
        self.dpi = dpi
        self.theme = _OpenThemeDataForDpi(hwnd, "MENU", dpi)
        self.font = G.font(FONT_PT, dpi)
        face = icon_face()
        self.icon_font = G.CreateFontW(-int(round(ARROW_PT * dpi / 72.0)), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                                       face) if face else 0

    def s(self, v: float) -> int:
        return int(round(v * self.dpi / 96.0))

    def close(self) -> None:
        if self.theme:
            _CloseThemeData(self.theme)
            self.theme = 0
        if self.icon_font:
            G.DeleteObject(self.icon_font)
            self.icon_font = 0

    def _width(self, dc: int, text: str, font: int) -> int:
        if not text:
            return 0
        old = G.SelectObject(dc, font)
        size = N.POINT()
        _GetTextExtentPoint32W(dc, text, len(text), byref(size))
        G.SelectObject(dc, old)
        return size.x

    def _shortcut_width(self, dc: int, it: Item) -> int:
        if not it.keys and not it.arrow:
            return 0
        w = self._width(dc, it.keys, self.font)
        if it.arrow:
            glyph, icon = arrow_glyph(it.arrow)
            w += self.s(2) + self._width(dc, glyph, self.icon_font if icon else self.font)
        return w

    # ------------------------------------------------------------ messages

    def measure(self, lp: int) -> bool:
        mis = MEASUREITEMSTRUCT.from_address(lp)
        if mis.CtlType != ODT_MENU:
            return False
        if mis.itemID == SEPARATOR_ID:
            mis.itemWidth, mis.itemHeight = 0, self.s(7)
            return True
        it = self.items.get(mis.itemID)
        if it is None:
            return False
        dc = G.CreateCompatibleDC(0)
        try:
            label = self._width(dc, it.label, self.font)
            keys = self._shortcut_width(dc, it)
        finally:
            G.DeleteDC(dc)
        gap = self.s(SHORTCUT_GAP) if keys else 0
        mis.itemWidth = self.s(TEXT_LEFT) + label + gap + keys + self.s(TEXT_RIGHT)
        mis.itemHeight = self.s(ITEM_HEIGHT)
        return True

    def _background(self, dc: int, rc: N.RECT) -> None:
        if self.theme:
            _DrawThemeBackground(self.theme, dc, MENU_POPUPBACKGROUND, 0, byref(rc), None)
        else:
            G.fill_rect(dc, (rc.left, rc.top, rc.right, rc.bottom), _GetSysColor(COLOR_MENU))

    def draw(self, lp: int) -> bool:
        dis = DRAWITEMSTRUCT.from_address(lp)
        if dis.CtlType != ODT_MENU:
            return False
        dc, rc = dis.hDC, dis.rcItem
        if dis.itemID == SEPARATOR_ID:
            self._background(dc, rc)
            y = (rc.top + rc.bottom) // 2
            G.fill_rect(dc, (rc.left + self.s(4), y, rc.right - self.s(4), y + max(1, self.s(1))), LINE_COLOR)
            return True
        it = self.items.get(dis.itemID)
        if it is None:
            return False
        hot = bool(dis.itemState & ODS_SELECTED)
        color = N.DWORD(_GetSysColor(COLOR_MENUTEXT))
        self._background(dc, rc)
        if self.theme:
            _GetThemeColor(self.theme, MENU_POPUPITEM, MPI_NORMAL, TMT_TEXTCOLOR, byref(color))
        if hot:
            with G.Shapes(dc) as sh:
                sh.fill_round((rc.left + self.s(4), rc.top + self.s(1), rc.right - self.s(4), rc.bottom - self.s(1)),
                              self.s(4), HOVER_ARGB)
        flags = G.DT_SINGLELINE | G.DT_VCENTER
        left, right = rc.left + self.s(TEXT_LEFT), rc.right - self.s(TEXT_RIGHT)
        G.draw_text(dc, it.label, self.font, (left, rc.top, right, rc.bottom), color.value, flags | G.DT_LEFT)
        if it.arrow:
            glyph, icon = arrow_glyph(it.arrow)
            gfont = self.icon_font if icon else self.font
            gw = self._width(dc, glyph, gfont)
            G.draw_text(dc, glyph, gfont, (right - gw, rc.top, right, rc.bottom), color.value, flags | G.DT_LEFT)
            right -= gw + self.s(2)
        if it.keys:
            G.draw_text(dc, it.keys, self.font, (left, rc.top, right, rc.bottom), color.value, flags | G.DT_RIGHT)
        return True
