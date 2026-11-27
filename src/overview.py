"""Overview (Mission Control-like, Ctrl+Alt+Up): every monitor and desktop shown as cards, each with a
grid of window preview tiles. Click a tile to go to that window; drag it onto another desktop card
to move it; drag a card title onto another card of the same monitor to swap the two desktops (their
numbers change), or onto another monitor row to move the whole desktop; right-click a tile
for a move menu; click a card to switch; "+" creates a desktop, the trash icon deletes one (its windows
join the desktop next to it); Esc or clicking elsewhere closes.

Everything is drawn in one window (own layout, hit-testing and scrolling). Visible windows get a live
DWM preview drawn over their tile; windows on inactive desktops show the snapshot taken when they
were hidden; otherwise the window's icon.
"""
from __future__ import annotations

import ctypes
import math
from ctypes import POINTER, Structure, byref, c_size_t, c_void_p, c_wchar_p, sizeof

from . import gui as G
from . import native as N
from . import strings as L
from . import tray_menu as TM
from .desktop_manager import MAX_DESKTOPS_PER_MONITOR, DesktopEntry, DesktopManager, MonitorEntry, WindowEntry

# Colors
BG = (23, 23, 27)
CARD_BG = (38, 38, 45)
CARD_BORDER = (58, 58, 68)
ACCENT = (75, 141, 224)       # active desktop
DROP_ACCENT = (70, 170, 110)  # drop target
DELETE_RED = (232, 72, 85)    # trash icon under the mouse
CHIP_BG = (53, 53, 61)
CHIP_HOVER = (72, 72, 84)
CHIP_BORDER = (80, 80, 94)
TEXT_DIM = (150, 150, 162)
WHITE = (255, 255, 255)
CAPTION_TEXT = (225, 225, 235)

MAX_TILES = 9


def _c(c) -> int:
    return G.rgb(*c)


def _a(c) -> int:
    return G.argb(*c)


def fit(src_w: int, src_h: int, box) -> tuple[int, int, int, int]:
    """Largest rectangle (x, y, w, h) with the source's aspect ratio, centered inside box (x, y, w, h)."""
    bx, by, bw, bh = box
    if src_w <= 0 or src_h <= 0:
        return box
    scale = min(bw / src_w, bh / src_h)
    w, h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
    return bx + (bw - w) // 2, by + (bh - h) // 2, w, h


def intersect(a, b):
    """Intersection of two (l, t, r, b) rectangles, or None."""
    l, t, r, bt = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (l, t, r, bt) if r > l and bt > t else None


def _inside(x: int, y: int, r) -> bool:
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


# ---------------------------------------------------------------- layout items (content coordinates)


# (plain __slots__ classes, compared by identity only: dataclasses would load inspect, ast and dis,
# ~0.65 MB kept for the rest of the run once the Overview has opened)


class Label:
    __slots__ = ("text", "rect", "hfont", "color", "flags")

    def __init__(self, text: str, rect: tuple, hfont: int, color: tuple, flags: int):
        self.text, self.rect, self.hfont, self.color, self.flags = text, rect, hfont, color, flags  # rect: x, y, w, h


class Tile:
    __slots__ = ("win", "rect", "preview", "thumb", "fitted", "source", "last")

    def __init__(self, win: WindowEntry, rect: tuple, preview: tuple, thumb: int = 0, fitted: tuple = (),
                 source: tuple = (), last: object = None):
        self.win = win
        self.rect = rect        # x, y, w, h (content coordinates)
        self.preview = preview  # x, y, w, h (content coordinates)
        self.thumb = thumb      # DWM thumbnail id (live preview) or 0
        self.fitted = fitted    # live preview rect (content coordinates)
        self.source = source    # visible frame in source-window coordinates (l, t, r, b)
        self.last = last        # last (visible, dest) sent to DWM


class MoreTile:
    __slots__ = ("rect", "count", "device", "local")

    def __init__(self, rect: tuple, count: int, device: str, local: int):
        self.rect, self.count, self.device, self.local = rect, count, device, local


class Card:
    __slots__ = ("mon", "desk", "rect", "header", "badge", "area", "grip", "trash", "tiles", "more")

    def __init__(self, mon: MonitorEntry, desk: DesktopEntry, rect: tuple, header: tuple, badge: tuple | None,
                 area: tuple, grip: tuple = (), trash: tuple | None = None, tiles: list | None = None,
                 more: MoreTile | None = None):
        self.mon, self.desk, self.rect = mon, desk, rect
        self.header = header  # the title text
        self.badge, self.area = badge, area
        self.grip = grip      # drag handle: the whole header band above the window tiles
        self.trash = trash    # delete button (None on a monitor's only desktop)
        self.tiles = [] if tiles is None else tiles
        self.more = more


class AddCard:
    __slots__ = ("rect", "device")

    def __init__(self, rect: tuple, device: str):
        self.rect, self.device = rect, device


class Row:
    __slots__ = ("device", "rect")

    def __init__(self, device: str, rect: tuple):
        self.device, self.rect = device, rect


# ---------------------------------------------------------------- tooltips


class TOOLINFOW(Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("uFlags", ctypes.c_uint), ("hwnd", N.H), ("uId", c_size_t),
                ("rect", N.RECT), ("hinst", N.H), ("lpszText", c_wchar_p), ("lParam", N.LPARAM),
                ("lpReserved", c_void_p)]


_TOOLINFO_V2_SIZE = TOOLINFOW.lpReserved.offset  # accepted by every comctl32 version
TTF_SUBCLASS = 0x10
TTS_ALWAYSTIP = 0x01
TTS_NOPREFIX = 0x02
TTM_SETDELAYTIME = G.WM_USER + 3
TTM_SETMAXTIPWIDTH = G.WM_USER + 24
TTM_ADDTOOLW = G.WM_USER + 50
TTM_DELTOOLW = G.WM_USER + 51
TTM_GETTOOLCOUNT = G.WM_USER + 13
TTDT_INITIAL = 3


class _INITCOMMONCONTROLSEX(Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("dwICC", ctypes.c_ulong)]


_SetWindowTheme = N._f(TM._ux, "SetWindowTheme", ctypes.c_long, N.H, c_wchar_p, c_wchar_p)  # (one uxtheme DLL object)
_InitCommonControlsEx =N._f(N.comctl32, "InitCommonControlsEx", N.BOOL, POINTER(_INITCOMMONCONTROLSEX))
_SendToolMessage = N._f(N.user32, "SendMessageW", N.LRESULT, N.H, N.UINT, N.WPARAM, POINTER(TOOLINFOW))
_SendMessageL = N._f(N.user32, "SendMessageW", N.LRESULT, N.H, N.UINT, N.WPARAM, N.LPARAM)


class _Tooltips:
    def __init__(self, host: int):
        icc = _INITCOMMONCONTROLSEX(sizeof(_INITCOMMONCONTROLSEX), 0xFF)
        _InitCommonControlsEx(byref(icc))
        self.host = host
        self.hwnd = G.CreateWindowExW(N.WS_EX_TOPMOST, "tooltips_class32", None,
                                      N.WS_POPUP | TTS_ALWAYSTIP | TTS_NOPREFIX,
                                      0, 0, 0, 0, host, 0, G.Window._hinst, None)
        self._count = 0
        self._texts: list[str] = []
        if self.hwnd:
            _SendMessageL(self.hwnd, TTM_SETMAXTIPWIDTH, 0, 600)
            _SendMessageL(self.hwnd, TTM_SETDELAYTIME, TTDT_INITIAL, 400)

    def set(self, tools: list[tuple[tuple, str]]) -> None:
        """Replaces all tools: [(client rect l, t, r, b), text]."""
        if not self.hwnd:
            return
        for i in range(self._count):
            ti = TOOLINFOW(cbSize=_TOOLINFO_V2_SIZE, hwnd=self.host, uId=i + 1)
            _SendToolMessage(self.hwnd, TTM_DELTOOLW, 0, byref(ti))
        self._texts = [t for _, t in tools]
        for i, (r, _) in enumerate(tools):
            ti = TOOLINFOW(cbSize=_TOOLINFO_V2_SIZE, uFlags=TTF_SUBCLASS, hwnd=self.host, uId=i + 1,
                           rect=N.RECT(*r), lpszText=self._texts[i])
            _SendToolMessage(self.hwnd, TTM_ADDTOOLW, 0, byref(ti))
        self._count = len(tools)

    def count(self) -> int:
        return _SendMessageL(self.hwnd, TTM_GETTOOLCOUNT, 0, 0) if self.hwnd else 0

    def destroy(self) -> None:
        if self.hwnd:
            G.DestroyWindow(self.hwnd)
            self.hwnd = 0


# ---------------------------------------------------------------- the overview window


class Overview(G.Window):
    _open: "Overview | None" = None

    @classmethod
    def is_open(cls) -> bool:
        return cls._open is not None

    @classmethod
    def current(cls) -> "Overview | None":
        return cls._open

    @classmethod
    def toggle(cls, mgr: DesktopManager) -> None:
        if cls._open is not None:
            cls._open.close()
            return
        scr = N.pick_screen(N.screens(), N.cursor_pos())
        if scr is None:
            return  # no display reported right now (Windows is reconfiguring them): nothing to open on
        Overview(mgr, scr)

    def __init__(self, mgr: DesktopManager, scr: N.Screen):
        super().__init__()
        # Open from the start: building it syncs, and a sync can switch desktops (a desktop brought
        # back after start or "Load"); with the overview counted as open no slide starts
        Overview._open = self
        try:
            self._setup(mgr, scr)
        except BaseException:
            if Overview._open is self:
                Overview._open = None
            if self.hwnd:
                self.destroy()
            raise

    def _setup(self, mgr: DesktopManager, scr: N.Screen) -> None:
        self._mgr = mgr
        self._icons: dict[int, int] = {}
        self._acting = False
        self._closing = False
        self._scroll = 0
        self._content_h = 0
        self.labels: list[Label] = []
        self.rows: list[Row] = []
        self.cards: list[Card] = []
        self.adds: list[AddCard] = []
        self._hover = None        # Tile / AddCard under the mouse
        self._press = None        # what the left button went down on
        self._down_at = None      # (x, y) for the drag threshold
        self._drag = None         # ("window", hwnd) or ("desktop", device, local)
        self._drop = None         # Card / Row highlighted as drop target
        self._tracking = False
        self._tips = None

        l, t, r, b = scr.work
        w, h = r - l, b - t
        self.create("DisplayDesktop.Overview", N.WS_EX_TOPMOST | N.WS_EX_TOOLWINDOW,
                    N.WS_POPUP | N.WS_VSCROLL, l + w // 12, t + h // 12, w * 10 // 12, h * 10 // 12,
                    title=L.t("ov.title"))
        self._dpi = N.GetDpiForWindow(self.hwnd) or 96
        _SetWindowTheme(self.hwnd, "DarkMode_Explorer", None)  # dark scrollbar (ignored where unsupported)
        self._tips = _Tooltips(self.hwnd)
        self._build()
        G.ShowWindow(self.hwnd, N.SW_SHOW)
        G.SetForegroundWindow(self.hwnd)
        G.SetFocus(self.hwnd)

    # ------------------------------------------------------------ helpers

    def s(self, v: float) -> int:
        """Scales a 96-DPI length to this window's DPI."""
        return int(round(v * self._dpi / 96.0))

    def font(self, pt: float, bold: bool = False, italic: bool = False) -> int:
        return G.font(pt, self._dpi, bold, italic)

    def close(self) -> None:
        if self.hwnd and not self._closing:
            self._closing = True
            self.destroy()

    def _icon(self, h: int) -> int:
        """Window icons, fetched once per overview session (fetching can wait up to 120 ms)."""
        if h not in self._icons:
            self._icons[h] = N.get_window_small_icon(h)
        return self._icons[h]

    def act(self, change) -> None:
        """Runs a change made from inside the overview, then shows the new layout. A change can briefly
        take activation away (a switch focuses another window); that must not close the overview."""
        self._acting = True
        try:
            change()
        finally:
            self._acting = False
        if not self.hwnd:
            return
        self._build()
        G.SetForegroundWindow(self.hwnd)

    # ------------------------------------------------------------ layout

    def _card_size(self, client_w: int) -> tuple[int, int]:
        usable = client_w - self.s(44) - self.s(8)
        w = min(max((usable - self.s(86)) // 3 - self.s(14), self.s(300)), self.s(520))
        return w, int(w * 0.62) + self.s(48)

    def _unregister_thumbs(self) -> None:
        for c in self.cards:
            for t in c.tiles:
                if t.thumb:
                    N.dwm_unregister_thumbnail(t.thumb)
                    t.thumb = 0

    def _build(self) -> None:
        if not self.hwnd:
            return
        self._unregister_thumbs()
        self.labels, self.rows, self.cards, self.adds = [], [], [], []
        self._hover = self._press = self._drop = self._drag = None
        s = self.s
        cw = G.client_rect(self.hwnd)[2]
        hdc = G.GetDC(self.hwnd)
        try:
            x0 = s(22)
            y = s(16)
            max_w = max(s(100), cw - s(44))
            f_title = self.font(15, bold=True)
            tw, th = G.measure_text(hdc, L.t("ov.title"), f_title)
            self.labels.append(Label(L.t("ov.title"), (x0 + s(4), y, tw, th), f_title, WHITE, G.DT_LEFT | G.DT_SINGLELINE))
            y += th + s(2)
            f_legend = self.font(9.5)
            _, lh = G.measure_text(hdc, L.t("ov.legend"), f_legend, max_w - s(4), G.DT_WORDBREAK)
            self.labels.append(Label(L.t("ov.legend"), (x0 + s(4), y, max_w - s(4), lh), f_legend, TEXT_DIM, G.DT_LEFT | G.DT_WORDBREAK))
            y += lh + s(14)

            card_w, card_h = self._card_size(cw)
            add_w, add_h = s(72), s(216)
            margin = s(7)
            f_mon = self.font(12.5, bold=True)
            f_header = self.font(11, bold=True)
            f_badge = self.font(8, bold=True)
            for mon in self._mgr.get_layout():
                y += s(10)
                text = "\U0001F5A5  " + L.f("ov.monitor", mon.ordinal)
                mw, mh = G.measure_text(hdc, text, f_mon)
                self.labels.append(Label(text, (x0 + s(4), y, mw, mh), f_mon, WHITE, G.DT_LEFT | G.DT_SINGLELINE))
                y += mh + s(4)

                # Row: cards flow left to right and wrap
                row_x, row_y = x0, y
                cx, cy = row_x + s(4), row_y + s(4)
                line_h = 0
                right = cx
                items = [("card", d) for d in mon.desktops]
                if len(mon.desktops) < MAX_DESKTOPS_PER_MONITOR:
                    items.append(("add", None))
                for kind, desk in items:
                    iw, ih = (card_w, card_h) if kind == "card" else (add_w, add_h)
                    ow, oh = iw + 2 * margin, ih + 2 * margin
                    if cx + ow > row_x + max_w - s(4) and cx > row_x + s(4):
                        cx = row_x + s(4)
                        cy += line_h
                        line_h = 0
                    rect = (cx + margin, cy + margin, iw, ih)
                    if kind == "card":
                        self.cards.append(self._layout_card(hdc, mon, desk, rect, f_header, f_badge))
                    else:
                        self.adds.append(AddCard(rect, mon.device))
                    cx += ow
                    right = max(right, cx)
                    line_h = max(line_h, oh)
                row_h = (cy + line_h) - row_y + s(4)
                self.rows.append(Row(mon.device, (row_x, row_y, right - row_x + s(4), row_h)))
                y = row_y + row_h + s(8)
            self._content_h = y + s(16)
        finally:
            G.ReleaseDC(self.hwnd, hdc)

        self._update_scrollbar()
        self._sync_thumbs()
        self._update_tooltips()
        self.invalidate()

    def _layout_card(self, hdc, mon: MonitorEntry, desk: DesktopEntry, rect, f_header, f_badge) -> Card:
        s = self.s
        x, y, w, h = rect
        hw, hh = G.measure_text(hdc, L.f("ov.desktop", desk.global_number), f_header)
        header = (x + s(10), y + s(9), hw, hh)
        right = x + w - s(10)  # header items line up from the right edge
        trash = None
        if len(mon.desktops) > 1:
            ts = s(24)
            trash = (right - ts, y + s(7), ts, ts)
            right -= ts + s(6)
        badge = None
        if desk.is_current:
            bw, bh = G.measure_text(hdc, L.t("ov.active"), f_badge)
            bw, bh = bw + s(10), bh + s(4)
            badge = (right - bw, y + s(10), bw, bh)
        area = (x + s(10), y + s(40), w - s(20), h - s(50))
        grip = (x, y, w, area[1] - y)
        card = Card(mon, desk, rect, header, badge, area, grip, trash)

        count = len(desk.windows)
        shown = MAX_TILES - 1 if count > MAX_TILES else count  # last slot becomes "+N more"
        slots = MAX_TILES if count > MAX_TILES else count
        if slots > 0:
            cols = math.ceil(math.sqrt(slots))
            rows = math.ceil(slots / cols)
            gap = s(8)
            tw = (area[2] - (cols - 1) * gap) // cols
            th = (area[3] - (rows - 1) * gap) // rows
            for i in range(slots):
                b = (area[0] + (i % cols) * (tw + gap), area[1] + (i // cols) * (th + gap), tw, th)
                if i < shown:
                    card.tiles.append(self._layout_tile(desk.windows[i], b))
                else:
                    card.more = MoreTile(b, count - shown, mon.device, desk.local_index)
        return card

    def _layout_tile(self, win: WindowEntry, b) -> Tile:
        s = self.s
        preview = (b[0] + s(4), b[1] + s(4), b[2] - s(8), max(1, b[3] - s(8) - s(22)))
        tile = Tile(win, b, preview)
        if N.is_window_visible(win.handle):
            tid = N.dwm_register_thumbnail(self.hwnd, win.handle)
            size = N.dwm_thumbnail_source_size(tid) if tid else None
            if tid and (size is None or size[0] <= 0 or size[1] <= 0):
                N.dwm_unregister_thumbnail(tid)
                tid = 0
            if tid:
                source = (0, 0, size[0], size[1])
                wr, fr = N.get_window_rect(win.handle), N.get_visible_frame(win.handle)
                if wr and fr:
                    frame = (fr[0] - wr[0], fr[1] - wr[1], fr[2] - wr[0], fr[3] - wr[1])
                    clipped = intersect(frame, source)
                    if clipped:
                        source = clipped
                tile.thumb = tid
                tile.source = source
                tile.fitted = fit(source[2] - source[0], source[3] - source[1], preview)
        return tile

    # ------------------------------------------------------------ scrolling and live previews

    def _client_h(self) -> int:
        return G.client_rect(self.hwnd)[3]

    def _max_scroll(self) -> int:
        return max(0, self._content_h - self._client_h())

    def _update_scrollbar(self) -> None:
        self._scroll = min(max(0, self._scroll), self._max_scroll())
        si = G.SCROLLINFO(cbSize=sizeof(G.SCROLLINFO), fMask=G.SIF_ALL | 0x8,  # 0x8 = DISABLENOSCROLL
                          nMin=0, nMax=max(0, self._content_h - 1), nPage=self._client_h(), nPos=self._scroll)
        G.SetScrollInfo(self.hwnd, G.SB_VERT, byref(si), True)

    def scroll_to(self, pos: int) -> None:
        pos = min(max(0, pos), self._max_scroll())
        if pos == self._scroll:
            return
        self._scroll = pos
        self._update_scrollbar()
        self._sync_thumbs()
        self._update_tooltips()
        self.invalidate()

    def _sync_thumbs(self) -> None:
        """Positions every live preview over its tile, clipped to the window."""
        _, _, cw, ch = G.client_rect(self.hwnd)
        viewport = (0, 0, cw, ch)
        for c in self.cards:
            for t in c.tiles:
                if not t.thumb:
                    continue
                fx, fy, fw, fh = t.fitted
                dest = (fx, fy - self._scroll, fx + fw, fy - self._scroll + fh)
                inter = intersect(dest, viewport)
                state = (inter is not None, inter)
                if state == t.last:
                    continue
                t.last = state
                if inter is None:
                    N.dwm_update_thumbnail(t.thumb, False)
                    continue
                sl, st_, sr, sb = t.source
                sx = (sr - sl) / max(1, dest[2] - dest[0])
                sy = (sb - st_) / max(1, dest[3] - dest[1])
                src = (sl + int((inter[0] - dest[0]) * sx), st_ + int((inter[1] - dest[1]) * sy),
                       sl + int((inter[2] - dest[0]) * sx), st_ + int((inter[3] - dest[1]) * sy))
                N.dwm_update_thumbnail(t.thumb, True, inter, src)

    def _update_tooltips(self) -> None:
        tools = []
        sc = self._scroll

        def r(rect):
            x, y, w, h = rect
            return (x, y - sc, x + w, y - sc + h)

        for c in self.cards:
            if c.trash:  # first, so it wins over the header band it sits in
                tools.append((r(c.trash), L.t("ov.tip.trash")))
            tools.append((r(c.grip), L.t("ov.tip.header")))
            for t in c.tiles:
                tools.append((r(t.rect), t.win.title + "\n" + L.t("ov.tip.window")))
        for a in self.adds:
            tools.append((r(a.rect), L.t("ov.tip.add")))
        self._tips.set(tools)

    # ------------------------------------------------------------ hit testing

    def hit(self, x: int, y: int):
        """What is at a client point: ("trash", card), ("header", card), ("tile", tile, card),
        ("more", more), ("card", card), ("add", add) or None."""
        y += self._scroll
        for c in self.cards:
            if not _inside(x, y, c.rect):
                continue
            if c.trash and _inside(x, y, c.trash):
                return ("trash", c)
            if _inside(x, y, c.grip):
                return ("header", c)
            for t in c.tiles:
                if _inside(x, y, t.rect):
                    return ("tile", t, c)
            if c.more and _inside(x, y, c.more.rect):
                return ("more", c.more)
            return ("card", c)
        for a in self.adds:
            if _inside(x, y, a.rect):
                return ("add", a)
        return None

    def row_at(self, x: int, y: int) -> Row | None:
        y += self._scroll
        return next((r for r in self.rows if _inside(x, y, r.rect)), None)

    # ------------------------------------------------------------ messages

    def on_message(self, msg, wp, lp):
        if msg == G.WM_PAINT:
            self._paint()
            return 0
        if msg == G.WM_ERASEBKGND:
            return 1
        if msg == G.WM_ACTIVATE:
            if G.loword(wp) == G.WA_INACTIVE and not self._acting and not self._drag:
                G.PostMessageW(self.hwnd, G.WM_CLOSE, 0, 0)  # clicking elsewhere closes the overview
            return None
        if msg == G.WM_CLOSE:
            if not self._acting:
                self.close()
            return 0
        if msg == G.WM_KEYDOWN:
            if wp == N.VK_ESCAPE:
                if self._drag or self._press:
                    self._cancel_mouse()
                else:
                    self.close()
            return 0
        if msg == G.WM_MOUSEWHEEL:
            delta = G.signed16(wp >> 16)
            self.scroll_to(self._scroll - delta * self.s(120) // 120)
            return 0
        if msg == G.WM_VSCROLL:
            self._on_vscroll(G.loword(wp))
            return 0
        if msg == G.WM_SETCURSOR:
            if G.loword(lp) == 1:  # HTCLIENT
                self._set_cursor()
                return 1
            return None
        if msg == G.WM_MOUSEMOVE:
            self._on_mouse_move(*G.mouse_xy(lp))
            return 0
        if msg == G.WM_MOUSELEAVE:
            self._tracking = False
            if self._hover is not None and not self._press:
                self._hover = None
                self.invalidate()
            return 0
        if msg == G.WM_LBUTTONDOWN:
            self._on_left_down(*G.mouse_xy(lp))
            return 0
        if msg == G.WM_LBUTTONUP:
            self._on_left_up(*G.mouse_xy(lp))
            return 0
        if msg == G.WM_RBUTTONUP:
            h = self.hit(*G.mouse_xy(lp))
            if h and h[0] == "tile":
                self._show_window_menu(h[1])
            return 0
        if msg == G.WM_CAPTURECHANGED:
            if lp != self.hwnd and (self._drag or self._press):
                self._cancel_mouse()
            return 0
        if msg == G.WM_DPICHANGED:
            self._dpi = G.hiword(wp) or 96
            r = ctypes.cast(lp, POINTER(N.RECT)).contents
            G.SetWindowPos(self.hwnd, 0, r.left, r.top, r.right - r.left, r.bottom - r.top,
                           N.SWP_NOZORDER | N.SWP_NOACTIVATE)
            self._build()
            return 0
        if msg == 0x0005:  # WM_SIZE
            if self.cards or self.adds:
                self._build()
            return 0
        if msg == G.WM_DESTROY:
            self._unregister_thumbs()
            if self._tips is not None:  # (None if it failed while opening)
                self._tips.destroy()
            for icon in self._icons.values():
                N.destroy_icon(icon)
            self._icons.clear()
            if Overview._open is self:
                Overview._open = None
            return 0
        return None

    def _on_vscroll(self, code: int) -> None:
        page = self._client_h()
        line = self.s(40)
        pos = self._scroll
        if code == G.SB_LINEUP:
            pos -= line
        elif code == G.SB_LINEDOWN:
            pos += line
        elif code == G.SB_PAGEUP:
            pos -= page
        elif code == G.SB_PAGEDOWN:
            pos += page
        elif code == G.SB_TOP:
            pos = 0
        elif code == G.SB_BOTTOM:
            pos = self._max_scroll()
        elif code in (G.SB_THUMBTRACK, G.SB_THUMBPOSITION):
            si = G.SCROLLINFO(cbSize=sizeof(G.SCROLLINFO), fMask=G.SIF_TRACKPOS)
            G.GetScrollInfo(self.hwnd, G.SB_VERT, byref(si))
            pos = si.nTrackPos
        self.scroll_to(pos)

    def _set_cursor(self) -> None:
        if self._drag:
            G.set_cursor(G.IDC_SIZEALL if self._drop is not None else G.IDC_NO)
            return
        pt = N.cursor_pos()
        if pt is None:
            G.set_cursor(G.IDC_ARROW)
            return
        h = self.hit(*G.screen_to_client(self.hwnd, *pt))
        if h is None:
            G.set_cursor(G.IDC_ARROW)
        elif h[0] == "header":
            G.set_cursor(G.IDC_SIZEALL)
        else:
            G.set_cursor(G.IDC_HAND)

    def _cancel_mouse(self) -> None:
        self._press = self._down_at = self._drag = self._drop = None
        if G.GetCapture() == self.hwnd:
            G.ReleaseCapture()
        self.invalidate()

    def _on_left_down(self, x: int, y: int) -> None:
        h = self.hit(x, y)
        self._press = h
        self._down_at = (x, y)
        if h is None:
            return
        G.SetCapture(self.hwnd)
        if h[0] == "header":  # the card's header band is a drag handle (swap with another card, or move the desktop)
            c = h[1]
            self._drag = ("desktop", c.mon.device, c.desk.local_index)
            self._update_drop(x, y)

    def _on_mouse_move(self, x: int, y: int) -> None:
        if not self._tracking:
            tme = G.TRACKMOUSEEVENT(sizeof(G.TRACKMOUSEEVENT), G.TME_LEAVE, self.hwnd, 0)
            self._tracking = bool(G.TrackMouseEvent(byref(tme)))
        if self._press and self._press[0] == "tile" and not self._drag and self._down_at:
            dx = G.GetSystemMetricsForDpi(G.SM_CXDRAG, self._dpi)
            dy = G.GetSystemMetricsForDpi(G.SM_CYDRAG, self._dpi)
            if abs(x - self._down_at[0]) >= dx or abs(y - self._down_at[1]) >= dy:
                self._drag = ("window", self._press[1].win.handle)
        if self._drag:
            self._update_drop(x, y)
            self._set_cursor()
            return
        h = self.hit(x, y)
        hover = h[1] if h and h[0] in ("tile", "add", "trash") else None
        if hover is not self._hover:
            self._hover = hover
            self.invalidate()

    def _update_drop(self, x: int, y: int) -> None:
        target = None
        if self._drag[0] == "window":
            h = self.hit(x, y)
            if h is not None and h[0] in ("card", "tile", "header", "more"):
                target = h[2] if h[0] == "tile" else (h[1] if h[0] in ("card", "header") else
                                                      next(c for c in self.cards if c.more is h[1]))
        else:
            h = self.hit(x, y)
            card = h[-1] if h is not None and h[0] in ("card", "header", "tile") else None
            if h is not None and h[0] == "more":
                card = next(c for c in self.cards if c.more is h[1])
            if card is not None and card.mon.device == self._drag[1]:
                if card.desk.local_index != self._drag[2]:
                    target = card  # same monitor: swap the two desktops
            else:
                row = self.row_at(x, y)
                if row is not None and row.device != self._drag[1]:
                    target = row  # other monitor: move the desktop there
        if target is not self._drop:
            self._drop = target
            self.invalidate()

    def _on_left_up(self, x: int, y: int) -> None:
        press, drag, drop = self._press, self._drag, self._drop
        self._press = self._down_at = self._drag = self._drop = None
        if G.GetCapture() == self.hwnd:
            G.ReleaseCapture()
        self.invalidate()
        if drag:
            if drag[0] == "window" and isinstance(drop, Card):
                card = drop
                self.act(lambda: self._mgr.move_window_to_desktop(drag[1], card.mon.device, card.desk.local_index))
            elif drag[0] == "desktop" and isinstance(drop, Card):
                card = drop
                self.act(lambda: self._mgr.swap_desktops(drag[1], drag[2], card.desk.local_index))
            elif drag[0] == "desktop" and isinstance(drop, Row):
                row = drop
                self.act(lambda: self._mgr.move_desktop_to_monitor(drag[1], drag[2], row.device))
            return
        if press is None:
            return
        now = self.hit(x, y)
        if now is None or now[0] != press[0] or now[1] is not press[1]:
            return  # released somewhere else: not a click
        kind = press[0]
        if kind == "tile":
            h = press[1].win.handle
            self.close()
            self._mgr.focus_window(h)
        elif kind == "card":
            c = press[1]
            self._mgr.switch_to(c.mon.device, c.desk.local_index)
            self.close()
        elif kind == "more":
            m = press[1]
            self._mgr.switch_to(m.device, m.local)
            self.close()
        elif kind == "add":
            device = press[1].device
            self.act(lambda: self._mgr.create_desktop_and_switch(device))
        elif kind == "trash":
            c = press[1]
            self.act(lambda: self._mgr.delete_desktop(c.mon.device, c.desk.local_index))

    def _show_window_menu(self, tile: Tile) -> None:
        """Right-click on a window: move it to any desktop on any monitor."""
        menu = G.CreatePopupMenu()
        targets = {}
        cmd = 1
        for mon in self._mgr.get_layout():
            for desk in mon.desktops:
                label = L.f("ov.menu.move", mon.ordinal, desk.global_number) + \
                    (L.t("ov.menu.activeSuffix") if desk.is_current else "")
                G.AppendMenuW(menu, G.MF_STRING, cmd, label)
                targets[cmd] = (mon.device, desk.local_index)
                cmd += 1
        G.AppendMenuW(menu, G.MF_SEPARATOR, 0, None)
        goto = 1000
        G.AppendMenuW(menu, G.MF_STRING, goto, L.t("ov.menu.goto"))
        x, y, _, h = tile.rect
        sx, sy = G.client_to_screen(self.hwnd, x, y + h - self._scroll)
        self._acting = True  # the menu must not close the overview
        try:
            chosen = G.TrackPopupMenu(menu, G.TPM_RETURNCMD | G.TPM_RIGHTBUTTON, sx, sy, 0, self.hwnd, None)
        finally:
            self._acting = False
            G.DestroyMenu(menu)
        handle = tile.win.handle
        if chosen in targets:
            device, local = targets[chosen]
            self.act(lambda: self._mgr.move_window_to_desktop(handle, device, local))
        elif chosen == goto:
            self.close()
            self._mgr.focus_window(handle)

    # ------------------------------------------------------------ painting

    def _paint(self) -> None:
        with G.BufferedPaint(self.hwnd) as (dc, w, h):
            G.fill_rect(dc, (0, 0, w, h), _c(BG))
            sc = self._scroll

            def vis(rect) -> bool:
                return rect[1] - sc < h and rect[1] + rect[3] - sc > 0

            for lb in self.labels:
                if vis(lb.rect):
                    x, y, lw, lh = lb.rect
                    G.draw_text(dc, lb.text, lb.hfont, (x, y - sc, x + lw, y - sc + lh), _c(lb.color), lb.flags)
            for row in self.rows:
                if row is self._drop and vis(row.rect):
                    x, y, rw, rh = row.rect
                    with G.Shapes(dc) as sh:
                        sh.draw_rect(x + 1, y - sc + 1, rw - 3, rh - 3, _a(DROP_ACCENT), self.s(2), dashed=True)
            for c in self.cards:
                if vis(c.rect):
                    self._paint_card(dc, c, sc)
            for a in self.adds:
                if vis(a.rect):
                    self._paint_add(dc, a, sc)

    def _paint_card(self, dc: int, c: Card, sc: int) -> None:
        s = self.s
        x, y, w, h = c.rect
        y -= sc
        drop = c is self._drop
        G.fill_rect(dc, (x, y, x + w, y + h), _c(CARD_BG))
        border = DROP_ACCENT if drop else ACCENT if c.desk.is_current else CARD_BORDER
        with G.Shapes(dc) as sh:
            sh.draw_round((x + 1, y + 1, x + w - 2, y + h - 2), s(10), _a(border),
                          s(2.5) if drop or c.desk.is_current else s(1.5))
            if c.badge:
                bx, by, bw, bh = c.badge
                sh.fill_round((bx, by - sc, bx + bw, by - sc + bh), 0.5, _a(ACCENT))
            for t in c.tiles:
                tx, ty, tw, th = t.rect
                ty -= sc
                hover = t is self._hover
                sh.fill_round((tx, ty, tx + tw - 1, ty + th - 1), s(7), _a(CHIP_HOVER if hover else CHIP_BG))
                sh.draw_round((tx, ty, tx + tw - 1, ty + th - 1), s(7), _a(ACCENT if hover else CHIP_BORDER),
                              s(2) if hover else s(1))
            if c.more:
                mx, my, mw, mh = c.more.rect
                sh.draw_round((mx, my - sc, mx + mw - 1, my - sc + mh - 1), s(7), _a(CHIP_BORDER), s(1), dashed=True)

        if c.trash:
            self._paint_trash(dc, c.trash, sc, c is self._hover)
        hx, hy, hw, hh = c.header
        G.draw_text(dc, L.f("ov.desktop", c.desk.global_number), self.font(11, bold=True),
                    (hx, hy - sc, hx + hw, hy - sc + hh), _c(WHITE), G.DT_LEFT | G.DT_SINGLELINE)
        if c.badge:
            bx, by, bw, bh = c.badge
            G.draw_text(dc, L.t("ov.active"), self.font(8, bold=True), (bx, by - sc, bx + bw, by - sc + bh),
                        _c(WHITE), G.DT_CENTER | G.DT_VCENTER | G.DT_SINGLELINE)
        if drop:
            label = L.t("ov.swap") if self._drag and self._drag[0] == "desktop" else L.t("ov.drop")
            G.draw_text(dc, label, self.font(10, bold=True), (x, y + s(10), x + w - s(14), y + s(34)),
                        _c(DROP_ACCENT), G.DT_RIGHT | G.DT_SINGLELINE)
        if not c.tiles and not c.more:
            ax, ay, aw, ah = c.area
            G.draw_text(dc, L.t("ov.empty"), self.font(9.5, italic=True), (ax, ay - sc, ax + aw, ay - sc + ah),
                        _c(TEXT_DIM), G.DT_CENTER | G.DT_VCENTER | G.DT_SINGLELINE)
        for t in c.tiles:
            self._paint_tile_content(dc, t, sc)
        if c.more:
            mx, my, mw, mh = c.more.rect
            G.draw_text(dc, L.f("ov.more", c.more.count), self.font(10, bold=True),
                        (mx, my - sc, mx + mw, my - sc + mh), _c(TEXT_DIM),
                        G.DT_CENTER | G.DT_VCENTER | G.DT_SINGLELINE)

    def _paint_trash(self, dc: int, r, sc: int, hover: bool) -> None:
        """A trash can drawn with shapes (no icon font needed): lid, handle, body with two ribs."""
        s = self.s
        x, y, w, h = r
        y -= sc
        color = _a(DELETE_RED if hover else TEXT_DIM)
        with G.Shapes(dc) as sh:
            if hover:
                sh.fill_round((x, y, x + w, y + h), s(6), _a(CHIP_HOVER))
            cx = x + w // 2
            lid_y = y + s(7)
            sh.fill_round((x + s(5), lid_y, x + w - s(5), lid_y + s(2)), s(1), color)       # lid
            sh.draw_round((cx - s(3), lid_y - s(3), cx + s(3), lid_y + s(1)), s(1), color, s(1.5))  # handle
            body = (x + s(7), lid_y + s(4), x + w - s(7), y + h - s(4))
            sh.draw_round(body, s(2), color, s(1.5))                                       # body
            for rx in (cx - s(2.5), cx + s(2.5)):                                          # ribs
                sh.fill_round((rx - s(0.6), body[1] + s(3), rx + s(0.6), body[3] - s(3)), 0.5, color)

    def _paint_tile_content(self, dc: int, t: Tile, sc: int) -> None:
        s = self.s
        tx, ty, tw, th = t.rect
        ty -= sc
        px, py, pw, ph = t.preview
        py -= sc
        if not t.thumb:
            shot = self._mgr.snapshots.get(t.win.handle)  # looked up at paint time: may be replaced
            if shot is not None:
                fx, fy, fw, fh = fit(shot.w, shot.h, (px, py, pw, ph))
                src = G.CreateCompatibleDC(dc)
                old = G.SelectObject(src, shot.hbm)
                G.SetStretchBltMode(dc, G.HALFTONE)
                G.SetBrushOrgEx(dc, 0, 0, None)
                G.StretchBlt(dc, fx, fy, fw, fh, src, 0, 0, shot.w, shot.h, G.SRCCOPY)
                G.SelectObject(src, old)
                G.DeleteDC(src)
            else:
                icon = self._icon(t.win.handle)
                if icon:
                    size = min(s(32), pw, ph)
                    G.DrawIconEx(dc, px + (pw - size) // 2, py + (ph - size) // 2, icon, size, size, 0, 0, 3)
        # Caption: small icon + title
        x, y = tx + s(6), ty + th - s(22)
        icon = self._icon(t.win.handle)
        if icon:
            G.DrawIconEx(dc, x, y + s(3), icon, s(16), s(16), 0, 0, 3)
            x += s(20)
        G.draw_text(dc, t.win.title, self.font(8.8), (x, y, tx + tw - s(4), y + s(22)), _c(CAPTION_TEXT),
                    G.DT_LEFT | G.DT_VCENTER | G.DT_SINGLELINE | G.DT_END_ELLIPSIS)

    def _paint_add(self, dc: int, a: AddCard, sc: int) -> None:
        s = self.s
        x, y, w, h = a.rect
        y -= sc
        hover = a is self._hover
        with G.Shapes(dc) as sh:
            sh.draw_round((x + 1, y + 1, x + w - 2, y + h - 2), s(10), _a(ACCENT if hover else CARD_BORDER),
                          s(1.5), dashed=True)
        G.draw_text(dc, "+", self.font(20), (x, y, x + w, y + h), _c(ACCENT if hover else TEXT_DIM),
                    G.DT_CENTER | G.DT_VCENTER | G.DT_SINGLELINE)

    # ------------------------------------------------------------ test helpers

    def live_thumb_count(self) -> int:
        return sum(1 for c in self.cards for t in c.tiles if t.thumb)

    def tooltip_count(self) -> int:
        return self._tips.count()
