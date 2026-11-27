"""Small dialogs in the style of Settings (settings_window.py: same look, fonts and keyboard):

- RestoreWindow ("Restore Hidden Windows", from Settings and from Recovery): every hidden
  window (DesktopManager.hidden_windows) in a list with checkboxes; OK shows the ticked ones on
  desktop 1 (DesktopManager.restore_windows). Hidden windows have no taskbar button, so after a
  crash whose journal is lost this is the only way back for them. The user chooses: apps also keep
  windows hidden on purpose, and those look the same.
- RecoveryWindow (Recovery): every error, crash and file that can't be read (recovery.py's
  report), and on request. Settings Folder, Restore Hidden Windows, Try Key, Recreate Files, Quit,
  Continue.
- ApplyKeyWindow, KeyWindow: the encryption key questions.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import Structure, byref, c_int, c_size_t, c_ssize_t, c_uint, c_wchar_p
from collections.abc import Callable

from . import gui as G
from . import native as N
from . import strings as L
from .settings_window import (BROWSE_W, BS_DEFPUSHBUTTON, COLOR_BTNFACE, ES_AUTOHSCROLL, ES_PASSWORD, IDCANCEL,
                              IDOK, TEXT_SCALE, WM_CTLCOLORBTN, WM_CTLCOLORSTATIC, WM_SETFONT, WM_SETICON,
                              WS_CHILD, WS_DISABLED, WS_EX_CLIENTEDGE, WS_EX_DLGMODALFRAME, WS_SYSMENU, WS_TABSTOP,
                              WS_VISIBLE, _ActivateActCtx, _AdjustWindowRectExForDpi, _DeactivateActCtx,
                              _EnableWindow, _GetFocus, _MessageBeep,
                              _GetSysColor, _GetSysColorBrush, _ICC, _InitCommonControlsEx, _SetBkColor,
                              _SetWindowTextW, _visual_styles, eye_text, icon_font, show_key, text_of,
                              FOLDER_GLYPH, ICC_LINK_CLASS, LM_GETIDEALSIZE, NM_CLICK, NM_RETURN, REPO_URL, SS_CENTER,
                              WM_NOTIFY, _IsWindowEnabled, _NMHDR, _SIZE, open_repo_page)
from .tray_menu import icon_face

ICC_LISTVIEW_CLASSES = 0x1
LVS_REPORT, LVS_SINGLESEL, LVS_SHOWSELALWAYS, LVS_NOCOLUMNHEADER = 0x1, 0x4, 0x8, 0x4000
LVS_EX_CHECKBOXES, LVS_EX_FULLROWSELECT = 0x4, 0x20
LVM_GETITEMCOUNT, LVM_SETITEMSTATE, LVM_GETITEMSTATE = 0x1004, 0x102B, 0x102C
LVM_SETEXTENDEDLISTVIEWSTYLE, LVM_INSERTITEMW, LVM_INSERTCOLUMNW = 0x1036, 0x104D, 0x1061
LVCF_WIDTH, LVIF_TEXT, LVIF_PARAM, LVIS_STATEIMAGEMASK = 0x2, 0x1, 0x4, 0xF000
SS_ICON, SS_NOPREFIX, STM_SETICON = 0x3, 0x80, 0x170
IDI_ERROR, IDI_WARNING, IDI_INFORMATION = 32513, 32515, 32516
WM_SETREDRAW = 0x000B
ID_LIST, ID_TEXT, ID_ICON, ID_RESTORE = 201, 202, 203, 205
ID_KEY, ID_EYE, ID_TRY, ID_STATUS = 206, 207, 208, 209  # the key box (KeyWindow, RecoveryWindow)

_LoadIconW = N._f(N.user32, "LoadIconW", N.H, N.H, ctypes.c_void_p)


class _LVCOLUMNW(Structure):
    _fields_ = [("mask", c_uint), ("fmt", c_int), ("cx", c_int), ("pszText", c_wchar_p),
                ("cchTextMax", c_int), ("iSubItem", c_int), ("iImage", c_int), ("iOrder", c_int)]


class _LVITEMW(Structure):
    _fields_ = [("mask", c_uint), ("iItem", c_int), ("iSubItem", c_int), ("state", c_uint),
                ("stateMask", c_uint), ("pszText", c_wchar_p), ("cchTextMax", c_int), ("iImage", c_int),
                ("lParam", c_ssize_t), ("iIndent", c_int)]


def label(h: int, exes: dict[int, str] | None = None) -> str:
    """A hidden window as the list shows it: its title and program. `exes`: program per process,
    filled as it goes (a whole list asks each process once, not each window)."""
    pid = N.get_pid(h) if exes is not None else 0
    if not pid:  # (no list cache, or a window closed meanwhile: pid 0 is no process, never shared)
        exe = os.path.basename(N.get_exe_path(h))
    else:
        exe = exes.get(pid)
        if exe is None:
            exe = exes[pid] = os.path.basename(N.get_exe_path(h))
    title = N.get_title(h)
    return f"{title}  ({exe})" if exe else title


class _Dialog(G.Window):
    """What both dialogs share: a fixed-size frame centered on the monitor under the mouse, child
    controls in the Common Controls 6 look, the Settings fonts and background, keyboard support."""
    WINDOW_CLASS = ""

    def _open(self, title: str, icon: int, width: float, height: float, owner: int = 0) -> None:
        scr = N.pick_screen(N.screens(), N.cursor_pos())
        if scr is None:
            raise OSError("no display")
        self._controls: dict[int, int] = {}
        l, t, r, b = scr.work
        # (`owner`: kept above that window, and brought up with it when it is clicked)
        self.create(self.WINDOW_CLASS, WS_EX_DLGMODALFRAME, N.WS_CAPTION | WS_SYSMENU, (l + r) // 2, (t + b) // 2,
                    0, 0, title=title, owner=owner)
        self._own = self.hwnd
        G.dialogs.add(self._own)
        if icon:
            G.SendMessageW(self.hwnd, WM_SETICON, 0, icon)
            G.SendMessageW(self.hwnd, WM_SETICON, 1, icon)
        self.dpi = N.GetDpiForWindow(self.hwnd) or 96
        self.cw, self.ch = self.s(width), self.s(height)
        rc = N.RECT(0, 0, self.cw, self.ch)
        _AdjustWindowRectExForDpi(byref(rc), N.WS_CAPTION | WS_SYSMENU, False, WS_EX_DLGMODALFRAME, self.dpi)
        w, h = rc.right - rc.left, rc.bottom - rc.top
        G.SetWindowPos(self.hwnd, 0, (l + r - w) // 2, (t + b - h) // 2, w, h, N.SWP_NOZORDER | N.SWP_NOACTIVATE)
        self.font = G.font(9 * TEXT_SCALE, self.dpi)

    def s(self, v: float) -> int:
        return int(round(v * TEXT_SCALE * self.dpi / 96.0))

    def _fit_height(self, need: int) -> None:
        """The client area grown or shrunk to `need` (its text), the top left kept."""
        if need != self.ch:
            self.ch = need
            rc = N.RECT(0, 0, self.cw, self.ch)
            _AdjustWindowRectExForDpi(byref(rc), N.WS_CAPTION | WS_SYSMENU, False, WS_EX_DLGMODALFRAME, self.dpi)
            l, t, _, _ = N.get_window_rect(self.hwnd)
            G.SetWindowPos(self.hwnd, 0, l, t, rc.right - rc.left, rc.bottom - rc.top,
                           N.SWP_NOZORDER | N.SWP_NOACTIVATE)

    def _add_icon(self, idi: int) -> int:
        """A system icon (IDI_*) at the top left, 32 px (scaled)."""
        s = self.s
        ico = self._add("STATIC", "", SS_ICON, ID_ICON, s(16), s(18), s(32), s(32))
        G.SendMessageW(ico, STM_SETICON, _LoadIconW(0, ctypes.c_void_p(idi)), None)
        return ico

    def _add(self, cls: str, text: str, style: int, cid: int, x: int, y: int, w: int, h: int, ex: int = 0) -> int:
        hwnd = G.CreateWindowExW(ex, cls, text, WS_CHILD | WS_VISIBLE | style, x, y, w, h, self.hwnd, cid,
                                 G.Window._hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        G.SendMessageW(hwnd, WM_SETFONT, self.font, 1)
        self._controls[cid] = hwnd
        return hwnd

    def _show(self, focus: int) -> None:
        G.ShowWindow(self.hwnd, N.SW_SHOW)
        G.SetForegroundWindow(self.hwnd)
        G.SetFocus(self._controls[focus])

    def _paint_message(self, msg, wp) -> int | None:
        if msg == G.WM_ERASEBKGND:
            G.fill_rect(wp, G.client_rect(self.hwnd), _GetSysColor(COLOR_BTNFACE))
            return 1
        if msg in (WM_CTLCOLORBTN, WM_CTLCOLORSTATIC):
            _SetBkColor(wp, _GetSysColor(COLOR_BTNFACE))
            return _GetSysColorBrush(COLOR_BTNFACE)
        return None

    def _add_key_row(self, x: int, y: int, w: int) -> None:
        """A key box (dots; the eye button next to it shows the key) `w` wide, eye included."""
        self._face = icon_face()
        self._eye_font = icon_font(self.dpi, self._face)
        self._key_shown = False
        s = self.s
        self._add("EDIT", "", WS_TABSTOP | ES_AUTOHSCROLL | ES_PASSWORD, ID_KEY, x, y + s(2),
                  w - s(BROWSE_W + 8), s(24), ex=WS_EX_CLIENTEDGE)
        eye = self._add("BUTTON", eye_text(False, self._face), WS_TABSTOP, ID_EYE, x + w - s(BROWSE_W), y,
                        s(BROWSE_W), s(28))
        if self._eye_font:
            G.SendMessageW(eye, WM_SETFONT, self._eye_font, 1)
        show_key(self._controls[ID_KEY], eye, False, self._face)

    def key_text(self) -> str:
        return text_of(self._controls[ID_KEY])

    def toggle_key_shown(self) -> None:
        self._key_shown = not self._key_shown
        show_key(self._controls[ID_KEY], self._controls[ID_EYE], self._key_shown, self._face)

    def _wrong_key(self) -> None:
        _SetWindowTextW(self._controls[ID_STATUS], L.t("key.wrong"))
        _MessageBeep(0x30)  # MB_ICONWARNING sound
        G.SetFocus(self._controls[ID_KEY])
        G.SendMessageW(self._controls[ID_KEY], 0x00B1, 0, -1)  # EM_SETSEL: all, ready to retype

    def _free_fonts(self) -> None:
        if getattr(self, "_eye_font", 0):
            G.DeleteObject(self._eye_font)
            self._eye_font = 0

    def _text_height(self, text: str, width: int) -> int:
        """Height of wrapped text in the dialog font."""
        dc = G.GetDC(0)
        old = G.SelectObject(dc, self.font)
        try:
            rc = N.RECT(0, 0, width, 0)
            G.DrawTextW(dc, text, -1, byref(rc), G.DT_WORDBREAK | G.DT_CALCRECT | G.DT_NOPREFIX)
            return rc.bottom - rc.top
        finally:
            G.SelectObject(dc, old)
            G.ReleaseDC(0, dc)


class RestoreWindow(_Dialog):
    WINDOW_CLASS = "DisplayDesktop.Restore"
    current: "RestoreWindow | None" = None

    @classmethod
    def show(cls, mgr, icon: int = 0) -> "RestoreWindow | None":
        """Opens the list (or brings the open one to the front, with a fresh list). `mgr`: the
        DesktopManager (hidden_windows, restore_windows). None while Windows reports no display."""
        cls.close_open()
        if N.pick_screen(N.screens(), N.cursor_pos()) is None:
            return None
        cls.current = cls(mgr, icon)
        return cls.current

    @classmethod
    def close_open(cls) -> None:
        if cls.current is not None:
            cls.current.destroy()

    def __init__(self, mgr, icon: int = 0):
        super().__init__()
        self._mgr = mgr
        self.windows: list[int] = mgr.hidden_windows()
        self._open(L.t("restore.title"), icon, 400, 340)
        cw, ch, s = self.cw, self.ch, self.s
        cookie = c_size_t()
        ctx = _visual_styles()
        active = bool(ctx) and _ActivateActCtx(ctx, byref(cookie))
        try:
            _InitCommonControlsEx(byref(_ICC(ctypes.sizeof(_ICC), ICC_LISTVIEW_CLASSES)))
            about = L.t("restore.about")
            top = s(14)
            th = self._text_height(about, cw - s(32))
            self._add("STATIC", about, 0, ID_TEXT, s(16), top, cw - s(32), th)
            ly = top + th + s(10)
            lh = ch - ly - s(14 + 28 + 12)
            if self.windows:
                lv = self._add("SysListView32", "", WS_TABSTOP | LVS_REPORT | LVS_NOCOLUMNHEADER | LVS_SHOWSELALWAYS
                               | LVS_SINGLESEL, ID_LIST, s(16), ly, cw - s(32), lh, ex=WS_EX_CLIENTEDGE)
                G.SendMessageW(lv, LVM_SETEXTENDEDLISTVIEWSTYLE, LVS_EX_CHECKBOXES | LVS_EX_FULLROWSELECT,
                               LVS_EX_CHECKBOXES | LVS_EX_FULLROWSELECT)
                col = _LVCOLUMNW(LVCF_WIDTH, 0, cw - s(32) - s(24))
                G.SendMessageW(lv, LVM_INSERTCOLUMNW, 0, ctypes.addressof(col))
                # (no layout and scroll bar update per item: one when redraw is back on, ~6x faster)
                G.SendMessageW(lv, WM_SETREDRAW, 0, None)
                try:
                    exes: dict[int, str] = {}
                    for i, h in enumerate(self.windows):
                        item = _LVITEMW(LVIF_TEXT | LVIF_PARAM, i, 0, 0, 0, label(h, exes), 0, 0, h)
                        G.SendMessageW(lv, LVM_INSERTITEMW, 0, ctypes.addressof(item))
                finally:
                    G.SendMessageW(lv, WM_SETREDRAW, 1, None)
            else:
                self._add("STATIC", L.t("restore.none"), 0, ID_LIST, s(16), ly, cw - s(32), s(24))
            by = ch - s(14 + 28)
            self._add("BUTTON", L.t("restore.ok"), WS_TABSTOP | BS_DEFPUSHBUTTON | (0 if self.windows else WS_DISABLED),
                      IDOK, cw - s(16 + 88 + 8 + 88), by, s(88), s(28))
            self._add("BUTTON", L.t("restore.cancel"), WS_TABSTOP, IDCANCEL, cw - s(16 + 88), by, s(88), s(28))
        finally:
            if active:
                _DeactivateActCtx(0, cookie.value)
        self._show(ID_LIST if self.windows else IDCANCEL)

    # ---- the list (also used by tests)

    def set_checked(self, i: int, on: bool) -> None:
        state = _LVITEMW(0, 0, 0, (2 if on else 1) << 12, LVIS_STATEIMAGEMASK)
        G.SendMessageW(self._controls[ID_LIST], LVM_SETITEMSTATE, i, ctypes.addressof(state))

    def checked(self) -> list[int]:
        """The ticked windows, in list order."""
        if not self.windows:
            return []
        lv = self._controls[ID_LIST]
        return [h for i, h in enumerate(self.windows)
                if (G.SendMessageW(lv, LVM_GETITEMSTATE, i, LVIS_STATEIMAGEMASK) >> 12) == 2]

    def ok(self) -> list[int]:
        """OK: the ticked windows are shown on desktop 1; returns them."""
        chosen = self.checked()
        if chosen:
            shown = self._mgr.restore_windows(chosen)
            from . import console
            console.log(f"Restored {shown} hidden window(s) to desktop 1"
                        + (f" ({len(chosen) - shown} closed or not responding)." if shown < len(chosen) else "."))
        self.destroy()
        return chosen

    def on_message(self, msg, wp, lp):
        if msg == G.WM_COMMAND:
            cid = G.loword(wp)
            if cid == IDOK:
                if self.windows:
                    self.ok()
            elif cid == IDCANCEL:
                self.destroy()
            return 0
        if msg == G.WM_CLOSE:
            self.destroy()
            return 0
        return self._paint_message(msg, wp)

    def on_destroyed(self) -> None:
        G.dialogs.discard(self._own)
        if RestoreWindow.current is self:
            RestoreWindow.current = None


ID_RECREATE, ID_QUIT, ID_KEY_LABEL, ID_VERSION = 211, 212, 214, 215  # RecoveryWindow (Continue: IDOK)
ID_CLEAR = 216  # RecoveryWindow's Clear Desktops
ID_FOLDER_LABEL, ID_FOLDER, ID_BROWSE, ID_DEFAULT, ID_APPLY = 220, 221, 222, 223, 224  # its Settings Folder row
ID_ROW = 300  # the file rows: name ID_ROW + 2i, state ID_ROW + 2i + 1 (a link when it holds one)
EN_CHANGE = 0x0300


def _plain(text: str) -> str:
    """A state as plain text (its link tags dropped)."""
    import re
    return re.sub(r"</?a\b[^>]*>", "", text)


class _ReportDialog(_Dialog):
    """What Recovery and the folder check share: the version as a link to the project page at the
    top (as in Settings), and the file rows (name, then its state: a link when it holds one, e.g.
    "check for updates")."""

    def _add_version(self, y: int) -> int:
        """The bold, centered "DisplayDesktop <version>" link; returns the y below it."""
        from . import __version__
        s, cw = self.s, self.cw
        version = f"DisplayDesktop {__version__}"
        self._bold = G.font(9 * TEXT_SCALE, self.dpi, bold=True)
        _InitCommonControlsEx(byref(_ICC(ctypes.sizeof(_ICC), ICC_LINK_CLASS)))
        try:
            link = self._add("SysLink", f'<a href="{REPO_URL}">{version}</a>', WS_TABSTOP, ID_VERSION,
                             s(16), y, cw - s(32), s(20))
            G.SendMessageW(link, WM_SETFONT, self._bold, 1)
            size = _SIZE()  # (centered: sized to its text, then moved)
            G.SendMessageW(link, LM_GETIDEALSIZE, cw - s(32), ctypes.addressof(size))
            lw = min(cw - s(32), size.cx) if size.cx > 0 else cw - s(32)
            G.SetWindowPos(link, 0, (cw - lw) // 2, y, lw, s(20), N.SWP_NOZORDER | N.SWP_NOACTIVATE)
        except OSError:
            G.SendMessageW(self._add("STATIC", version, SS_CENTER, ID_VERSION, s(16), y, cw - s(32), s(20)),
                           WM_SETFONT, self._bold, 1)
        return y + s(20)

    def _add_rows(self, rows, x: int, y: int, w: int) -> int:
        """Each file: its name, then its state indented under it; returns the y below them."""
        s = self.s
        self._links = {ID_VERSION}
        for i, r in enumerate(rows):
            self._add("STATIC", r.shown, SS_NOPREFIX, ID_ROW + 2 * i, x, y, w, s(18))
            y += s(18)
            sw = w - s(20)
            sh = max(self._text_height(_plain(r.state), sw), s(18))
            cid = ID_ROW + 2 * i + 1
            if "<a " in r.state:
                try:
                    self._add("SysLink", r.state, WS_TABSTOP, cid, x + s(20), y, sw, sh)
                    self._links.add(cid)
                except OSError:
                    self._add("STATIC", _plain(r.state), SS_NOPREFIX, cid, x + s(20), y, sw, sh)
            else:
                self._add("STATIC", r.state, SS_NOPREFIX, cid, x + s(20), y, sw, sh)
            y += sh + s(6)
        return y

    def _link_clicked(self, lp) -> bool:
        hdr = _NMHDR.from_address(lp)
        if hdr.idFrom in getattr(self, "_links", ()) and hdr.code in (NM_CLICK, NM_RETURN):
            open_repo_page()  # (every link here is the project page)
            return True
        return False

    def _free_fonts(self) -> None:
        super()._free_fonts()
        for name in ("_bold", "_folder_font"):
            if getattr(self, name, 0):
                G.DeleteObject(getattr(self, name))
                setattr(self, name, 0)


class RecoveryWindow(_ReportDialog):
    """Recovery: the one dialog every error, crash and file that can't be read leads to (and
    `run recovery` on request). At the top the version as a link to the project page (as in
    Settings); the report (recovery.build): what is wrong in words, then every data file with its
    state, so the user can decide. Offers the Settings Folder row as in Settings (box, folder picker,
    Default, Apply: settings_window.move_folder, which never switches to a folder with files that
    can't be read), Restore Hidden Windows, Clear Desktops (greyed while a settings file is bad, and
    without the running app's desktops), Try Key (files encrypted with a key not entered),
    Recreate Files (the files that can't be read at all: deleted, started fresh; never converted back
    nor repaired), Quit and Continue. Files that can't be read at all must be decided: no Continue
    then, and Enter, Esc and closing it = Quit (the default). Opened from Settings (`can_quit`
    False): never quits the app: Close in Quit's place (the default; no Continue), Enter, Esc and
    closing it = "continue". `result`: "continue", "quit", "recreate", "key" (Try Key opened the
    files) or "folder" (moved to another folder)."""
    WINDOW_CLASS = "DisplayDesktop.Recovery"
    current: "RecoveryWindow | None" = None

    @classmethod
    def show(cls, report, icon: int = 0, on_folder: Callable[[str], None] | None = None,
             on_restore: Callable[[], None] | None = None, on_key: Callable[[str], bool] | None = None,
             on_done: Callable[[str], None] | None = None, can_quit: bool = True,
             on_clear: Callable[[], None] | None = None) -> "RecoveryWindow | None":
        """Opens it, not modal (one at a time: a newer one replaces it). on_folder: the tray's
        folder_changed (datafolder.move_to's switch); on_restore: Restore Hidden Windows (it stays
        open); on_key(key): Try Key, True when the key opened the files; on_done(result) once it is
        closed; on_clear: Clear Desktops (asks first; greyed without it or while a settings file is
        bad). None while Windows reports no display."""
        cls.close_open()
        if N.pick_screen(N.screens(), N.cursor_pos()) is None:
            return None
        cls.current = cls(report, icon, on_folder, on_restore, on_key, on_done, can_quit, on_clear)
        return cls.current

    @classmethod
    def close_open(cls) -> None:
        if cls.current is not None:
            cls.current.destroy()

    def __init__(self, report, icon: int = 0, on_folder=None, on_restore=None, on_key=None, on_done=None,
                 can_quit: bool = True, on_clear=None):
        super().__init__()
        self.report = report
        self.must_decide = bool(report.unreadable)
        self.can_quit = can_quit
        self.result = self._closed_result()  # (also when closed any other way)
        self._icon = icon
        self._on_folder, self._on_restore, self._on_key, self._on_done = on_folder, on_restore, on_key, on_done
        self._on_clear = on_clear
        try:
            self._open(L.t("recovery.title"), icon, 16 + 128 + 8 + 168 + 8 + 128 + 16, 300)  # (the action row's
            # three buttons 8 apart, like normal dialog buttons: the dialog is as wide as that row)
            self._build()
        except BaseException:
            self._on_done = None  # (never opened: no answer)
            if self.hwnd:
                self.destroy()
            raise

    def _build(self) -> None:
        report, s, cw = self.report, self.s, self.cw
        cookie = c_size_t()
        ctx = _visual_styles()
        active = bool(ctx) and _ActivateActCtx(ctx, byref(cookie))
        try:
            tx = s(16) + s(32) + s(14)
            tw = cw - tx - s(16)
            th = max(self._text_height(report.summary, tw), s(32))
            fw = cw - s(32)
            key = bool(report.locked and self._on_key)  # (Try Key: the files may just need another key)
            y = self._add_version(s(14)) + s(12)
            ico = self._add_icon(IDI_WARNING if self.must_decide else IDI_ERROR if report.problem else IDI_INFORMATION)
            G.SetWindowPos(ico, 0, s(16), y, s(32), s(32), N.SWP_NOZORDER | N.SWP_NOACTIVATE)
            self._add("STATIC", report.summary, SS_NOPREFIX, ID_TEXT, tx, y, tw, th)  # ("&" in a path)
            y += th + s(14)
            self._add("STATIC", L.t("recovery.files"), 0, ID_LIST, s(16), y, fw, s(18))
            y = self._add_rows(report.rows, s(16), y + s(22), fw) + s(6)
            y = self._add_folder_row(s(16), y, fw)
            if key:
                y += s(12)
                self._add("STATIC", L.t("key.label"), 0, ID_KEY_LABEL, s(16), y, fw, s(18))
                y += s(18 + 4)
                self._add_key_row(s(16), y, fw - s(88 + 8))
                self._add("BUTTON", L.t("key.try"), WS_TABSTOP, ID_TRY, s(16) + fw - s(88), y, s(88), s(28))
                self._add("STATIC", "", SS_NOPREFIX, ID_STATUS, s(16), y + s(28 + 4), fw, s(18))
                y += s(28 + 4 + 18)
            self._fit_height(y + s(22) + s(28) + s(10) + s(28) + s(14))  # (grown or shrunk to the report)
            by = self.ch - s(14 + 28)
            # The actions, on their own row above Quit / Continue: Clear Desktops (left), Restore Hidden
            # Windows (centered), Recreate Files (right)
            ay = by - s(10 + 28)
            # Clear Desktops: greyed while a settings file is bad (can't be read, or locked: Clear writes
            # the saved layout) and before the app runs (no desktops yet)
            clear_ok = self._on_clear is not None and not report.unreadable and not report.locked
            self._add("BUTTON", L.t("recovery.clear"), WS_TABSTOP | (0 if clear_ok else WS_DISABLED), ID_CLEAR,
                      s(16), ay, s(128), s(28))
            self._add("BUTTON", L.t("settings.restore"), WS_TABSTOP | (0 if self._on_restore else WS_DISABLED),
                      ID_RESTORE, (cw - s(168)) // 2, ay, s(168), s(28))
            self._add("BUTTON", L.t("recovery.recreate"), WS_TABSTOP, ID_RECREATE, cw - s(16 + 128), ay, s(128),
                      s(28))  # (never greyed)
            right = cw - s(16)
            quit_default = self.must_decide or not self.can_quit
            default = ID_QUIT if quit_default else IDOK
            if not quit_default:
                right -= s(88)
                self._add("BUTTON", L.t("recovery.continue"), WS_TABSTOP | BS_DEFPUSHBUTTON, IDOK, right, by,
                          s(88), s(28))
                right -= s(8)
            right -= s(88)  # (opened from Settings: Close in Quit's place, never quits the app)
            self._add("BUTTON", L.t("recovery.quit" if self.can_quit else "settings.close"),
                      WS_TABSTOP | (BS_DEFPUSHBUTTON if quit_default else 0), ID_QUIT, right, by, s(88), s(28))
        finally:
            if active:
                _DeactivateActCtx(0, cookie.value)
        _MessageBeep(0x30 if self.must_decide else 0x10 if report.problem else 0x40)  # (warning, error, info)
        self._show(default)

    def _add_folder_row(self, x: int, y: int, w: int) -> int:
        """Settings Folder as in Settings: the folder in use (typed or picked), Default and Apply (the
        check for files that can't be read comes with Apply). Off in test mode, like Settings."""
        from . import datafolder, startup
        s = self.s
        off = 0 if startup.can_manage() else WS_DISABLED
        self._add("STATIC", L.t("settings.folder"), 0, ID_FOLDER_LABEL, x, y, w, s(18))
        self._add("EDIT", datafolder.shown(datafolder.current()), WS_TABSTOP | ES_AUTOHSCROLL | off, ID_FOLDER,
                  x, y + s(20), w - s(BROWSE_W + 8), s(24), ex=WS_EX_CLIENTEDGE)
        face = self._face = icon_face()
        self._folder_font = icon_font(self.dpi, face)
        picker = self._add("BUTTON", FOLDER_GLYPH if face else "...", WS_TABSTOP | off, ID_BROWSE,
                           x + w - s(BROWSE_W), y + s(18), s(BROWSE_W), s(28))
        if self._folder_font:
            G.SendMessageW(picker, WM_SETFONT, self._folder_font, 1)
        self._add("BUTTON", L.t("settings.default"), WS_TABSTOP, ID_DEFAULT, x + w - s(88 + 8 + 88), y + s(54),
                  s(88), s(28))
        self._add("BUTTON", L.t("settings.apply"), WS_TABSTOP, ID_APPLY, x + w - s(88), y + s(54), s(88), s(28))
        self.update_folder_buttons()
        return y + s(54 + 28)

    def folder_text(self) -> str:
        return text_of(self._controls[ID_FOLDER]).strip()

    def update_folder_buttons(self) -> None:
        """Apply: only for a folder other than the one in use; Default: only away from the default."""
        from . import datafolder, startup
        manage = startup.can_manage()
        typed = self.folder_text()
        _EnableWindow(self._controls[ID_APPLY], manage and bool(typed) and not datafolder.same(typed, datafolder.current()))
        _EnableWindow(self._controls[ID_DEFAULT], manage and not datafolder.default_in_use())

    def move_folder(self, target: str) -> bool:
        """Apply / Default: settings_window.move_folder (a folder with files that can't be read is never
        switched to: the dialog listing them); moved → closes ("folder"), the caller reports afresh."""
        from .settings_window import move_folder
        if move_folder(target, self._on_folder, self.hwnd, self._icon):
            self.finish("folder")
            return True
        if self.hwnd:
            self.update_folder_buttons()
        return False

    def finish(self, result: str) -> None:
        if not self.hwnd:
            return  # (already answered, e.g. Quit by `run stop` while a box of its own was open: kept)
        self.result = result
        self.destroy()

    def on_message(self, msg, wp, lp):
        if msg == WM_NOTIFY:
            return 0 if self._link_clicked(lp) else None
        if msg == G.WM_COMMAND:
            cid = G.loword(wp)
            if (cid == ID_TRY or (cid == IDOK and _GetFocus() == self._controls.get(ID_KEY))) and self._on_key:
                self.try_key()  # (Enter in the key box tries it: never closes)
            elif cid == IDOK and _GetFocus() == self._controls.get(ID_FOLDER):
                if _IsWindowEnabled(self._controls[ID_APPLY]):  # (Enter in the folder box: Apply, never close)
                    self.move_folder(self.folder_text())
            elif cid == ID_FOLDER:
                if (wp >> 16) & 0xFFFF == EN_CHANGE and ID_APPLY in self._controls:
                    self.update_folder_buttons()
            elif cid == ID_BROWSE:
                from .settings_window import pick_folder
                from . import datafolder
                picked = pick_folder(self.hwnd, L.t("settings.folder"))
                if picked:
                    _SetWindowTextW(self._controls[ID_FOLDER], datafolder.shown(picked))
            elif cid == ID_APPLY:
                self.move_folder(self.folder_text())
            elif cid == ID_DEFAULT:
                from . import datafolder
                self.move_folder(datafolder.default_folder())
            elif cid in (IDOK, IDCANCEL):  # (Enter: the default; Esc: Quit when files must be decided)
                self.finish(self._closed_result())
            elif cid == ID_QUIT:
                self.finish("quit" if self.can_quit else "continue")
            elif cid == ID_RECREATE:
                self.finish("recreate")  # (every settings file removed and made new: tray.recreate_files)
            elif cid == ID_EYE:
                self.toggle_key_shown()
            elif cid == ID_RESTORE and self._on_restore is not None:
                # (asks first, No the default; this stays open: there may be more to decide)
                if G.message_box(L.t("msg.restoreConfirm"), G.MB_YESNO | G.MB_ICONQUESTION
                                 | G.MB_DEFBUTTON2) == G.IDYES and self.hwnd:
                    self._on_restore()
            elif cid == ID_CLEAR and self._on_clear is not None and _IsWindowEnabled(self._controls[ID_CLEAR]):
                self._on_clear()  # (asks first; this stays open)
            return 0
        if msg == G.WM_CLOSE:
            self.finish(self._closed_result())
            return 0
        return self._paint_message(msg, wp)

    def _closed_result(self) -> str:
        """Closed (Enter, Esc, the title bar's X): Quit when files must be decided, unless it was
        opened from Settings (it never quits the app then)."""
        return "quit" if self.must_decide and self.can_quit else "continue"

    def try_key(self) -> bool:
        """Try Key: the key typed opens the files (closes: "key"), or "wrong key"."""
        key = self.key_text()
        if key and self._on_key(key):
            self.finish("key")
            return True
        self._wrong_key()
        return False

    def on_destroyed(self) -> None:
        self._free_fonts()
        G.dialogs.discard(self._own)
        if RecoveryWindow.current is self:
            RecoveryWindow.current = None
        done, self._on_done = self._on_done, None
        if done is not None:
            done(self.result)


def ask_recovery(report, icon: int = 0, on_restore=None, on_key=None, on_folder=None,
                 can_quit: bool = True) -> str | None:
    """Recovery, modal (at start, and for files that can't be read at all): its `result`, or None
    when it can't be shown (no screen, the window fails): the caller goes on, never Quits unasked.
    `can_quit` False (opened from Settings): Close instead of Quit."""
    if N.pick_screen(N.screens(), N.cursor_pos()) is None:
        return None
    try:
        win = RecoveryWindow(report, icon, on_folder=on_folder, on_restore=on_restore, on_key=on_key,
                             can_quit=can_quit)
    except Exception:  # noqa: BLE001 - can't be asked: never Quit for it, nothing deleted unasked
        return None
    try:
        G.run_until(lambda: not win.hwnd)
    finally:
        if win.hwnd:
            win.destroy()
    return win.result


ID_APPLY_KEY = 210  # ApplyKeyWindow's Apply (its Revert is IDCANCEL)


class ApplyKeyWindow(_Dialog):
    """Settings closed with a key typed but not applied: Apply it, or Revert (the default: Enter,
    Esc and closing it any other way all revert, and reverting saves and re-encrypts nothing)."""
    WINDOW_CLASS = "DisplayDesktop.ApplyKey"

    def __init__(self, owner: int = 0):
        super().__init__()
        self.applied = False
        self._owner = owner
        try:
            self._open(L.t("key.title"), 0, 360, 120, owner=owner)  # (owned by Settings: never behind it)
            self._build()
        except BaseException:
            if self.hwnd:
                self.destroy()  # (half built: never left behind, invisible, in G.dialogs)
            raise

    def _build(self) -> None:
        s = self.s
        cookie = c_size_t()
        ctx = _visual_styles()
        active = bool(ctx) and _ActivateActCtx(ctx, byref(cookie))
        try:
            from . import crypto  # (no saved key yet: the question says what Apply and Revert mean then)
            text = L.t("key.unapplied" if crypto.enabled else "key.unappliedNew")
            tw = self.cw - s(32)
            th = self._text_height(text, tw)
            self._fit_height(s(16) + th + s(16) + s(28) + s(14))
            self._add("STATIC", text, SS_NOPREFIX, ID_TEXT, s(16), s(16), tw, th)
            by = self.ch - s(14 + 28)
            self._add("BUTTON", L.t("settings.apply"), WS_TABSTOP, ID_APPLY_KEY,
                      self.cw - s(16 + 88 + 8 + 88), by, s(88), s(28))
            self._add("BUTTON", L.t("settings.revert"), WS_TABSTOP | BS_DEFPUSHBUTTON, IDCANCEL,
                      self.cw - s(16 + 88), by, s(88), s(28))
        finally:
            if active:
                _DeactivateActCtx(0, cookie.value)
        self._show(IDCANCEL)

    def _finish(self, applied: bool) -> None:
        """Answered: Settings is enabled again BEFORE this goes (else Windows activates another app's
        window and Settings drops behind it), then this closes."""
        self.applied = applied
        if self._owner:
            _EnableWindow(self._owner, True)
        self.destroy()

    def on_message(self, msg, wp, lp):
        if msg == G.WM_COMMAND:
            cid = G.loword(wp)
            if cid == ID_APPLY_KEY:
                self._finish(True)
            elif cid in (IDOK, IDCANCEL):  # (Enter on Revert, Esc)
                self._finish(False)
            return 0
        if msg == G.WM_CLOSE:
            self._finish(False)
            return 0
        return self._paint_message(msg, wp)

    def on_destroyed(self) -> None:
        G.dialogs.discard(self._own)


def ask_apply_key(owner: int = 0) -> bool:
    """Asks whether to apply the key typed (True) or revert (False: the default, also when the
    question is closed any other way). Modal: returns once answered."""
    if N.pick_screen(N.screens(), N.cursor_pos()) is None:
        return False
    try:
        win = ApplyKeyWindow(owner)
    except Exception:  # noqa: BLE001 - the question can't open (no window): revert, never an error
        return False
    if owner:
        _EnableWindow(owner, False)  # (modal over Settings)
    try:
        G.run_until(lambda: not win.hwnd)
    finally:
        if owner:
            _EnableWindow(owner, True)
        if win.hwnd:
            win.destroy()  # (the loop ended without an answer: the app is quitting. Revert)
    return win.applied


class BadFolderWindow(_ReportDialog):
    """Settings Folder's Apply / Default chose a folder with data files that can't be read at all:
    each with why (as in Recovery), and Close only (nothing is repaired nor read by force: another
    folder must be chosen). Owned by Settings or Recovery, which waits for it."""
    WINDOW_CLASS = "DisplayDesktop.BadFolder"

    def __init__(self, folder: str, rows, owner: int = 0, icon: int = 0):
        super().__init__()
        self._owner = owner
        try:
            self._open(L.t("badFolder.title"), icon, 560, 200, owner=owner)
            self._build(folder, rows)
        except BaseException:
            if self.hwnd:
                self.destroy()
            raise

    def _build(self, folder: str, rows) -> None:
        from . import datafolder
        s, cw = self.s, self.cw
        cookie = c_size_t()
        ctx = _visual_styles()
        active = bool(ctx) and _ActivateActCtx(ctx, byref(cookie))
        try:
            text = L.f("badFolder.text", datafolder.shown(folder))
            tx = s(16) + s(32) + s(14)
            tw = cw - tx - s(16)
            th = max(self._text_height(text, tw), s(32))
            y = self._add_version(s(14)) + s(12)
            ico = self._add_icon(IDI_WARNING)
            G.SetWindowPos(ico, 0, s(16), y, s(32), s(32), N.SWP_NOZORDER | N.SWP_NOACTIVATE)
            self._add("STATIC", text, SS_NOPREFIX, ID_TEXT, tx, y, tw, th)
            y = self._add_rows(rows, s(16), y + th + s(14), cw - s(32))
            self._fit_height(y + s(16) + s(28) + s(14))
            self._add("BUTTON", L.t("settings.close"), WS_TABSTOP | BS_DEFPUSHBUTTON, IDOK,
                      cw - s(16 + 88), self.ch - s(14 + 28), s(88), s(28))
        finally:
            if active:
                _DeactivateActCtx(0, cookie.value)
        _MessageBeep(0x30)
        self._show(IDOK)

    def on_message(self, msg, wp, lp):
        if msg == WM_NOTIFY:
            return 0 if self._link_clicked(lp) else None
        if msg == G.WM_COMMAND:
            if G.loword(wp) in (IDOK, IDCANCEL):
                self._close()
            return 0
        if msg == G.WM_CLOSE:
            self._close()
            return 0
        return self._paint_message(msg, wp)

    def _close(self) -> None:
        if self._owner:
            _EnableWindow(self._owner, True)  # (before it goes: the owner stays in front)
        self.destroy()

    def on_destroyed(self) -> None:
        self._free_fonts()
        G.dialogs.discard(self._own)


def show_bad_folder(folder: str, owner: int = 0, icon: int = 0) -> None:
    """The folder chosen has data files that can't be read at all: lists each with why; modal (over
    `owner`), Close only. Nothing when it can't be shown (the folder is still not switched to)."""
    from . import recovery
    rows = [r for r in recovery.build(folder).rows if r.kind == "unreadable"]  # (what made it refused)
    if N.pick_screen(N.screens(), N.cursor_pos()) is None:
        return
    try:
        win = BadFolderWindow(folder, rows, owner, icon)
    except Exception:  # noqa: BLE001 - can't open: the folder is still not used
        return
    if owner:
        _EnableWindow(owner, False)
    try:
        G.run_until(lambda: not win.hwnd)
    finally:
        if owner:
            _EnableWindow(owner, True)
        if win.hwnd:
            win.destroy()


class KeyWindow(_Dialog):
    """Encrypted settings found (our marker) that no known key opens: asks for the key. At start
    (`modal`: the app can't read its files without it) and while running (another folder's files,
    files replaced by hand). OK tries the key (`on_ok(key)` → whether it opened them): right closes,
    wrong says so and stays; Cancel closes (the files are left as they are, unread)."""
    WINDOW_CLASS = "DisplayDesktop.Key"
    current: "KeyWindow | None" = None

    @classmethod
    def show(cls, folder: str, on_ok: Callable[[str], bool], icon: int = 0,
             on_closed: Callable[[bool], None] | None = None) -> "KeyWindow | None":
        if cls.current is not None and cls.current.hwnd:
            G.SetForegroundWindow(cls.current.hwnd)
            return cls.current
        if N.pick_screen(N.screens(), N.cursor_pos()) is None:
            return None
        cls.current = cls(folder, on_ok, icon, on_closed)
        return cls.current

    @classmethod
    def close_open(cls) -> None:
        if cls.current is not None:
            cls.current.destroy()

    def __init__(self, folder: str, on_ok, icon: int = 0, on_closed=None):
        super().__init__()
        self._on_ok, self._on_closed = on_ok, on_closed
        self.accepted = False
        self._open(L.t("key.title"), icon, 380, 160)
        s = self.s
        cookie = c_size_t()
        ctx = _visual_styles()
        active = bool(ctx) and _ActivateActCtx(ctx, byref(cookie))
        try:
            text = L.f("key.ask", folder)
            tw = self.cw - s(32)
            th = self._text_height(text, tw)
            self._fit_height(s(16) + th + s(10) + s(28) + s(4) + s(18) + s(12) + s(28) + s(14))
            self._add("STATIC", text, SS_NOPREFIX, ID_TEXT, s(16), s(16), tw, th)
            ky = s(16) + th + s(10)
            self._add_key_row(s(16), ky, tw)
            self._add("STATIC", "", SS_NOPREFIX, ID_STATUS, s(16), ky + s(28 + 4), tw, s(18))
            by = self.ch - s(14 + 28)
            self._add("BUTTON", L.t("restore.ok"), WS_TABSTOP | BS_DEFPUSHBUTTON, IDOK,
                      self.cw - s(16 + 88 + 8 + 88), by, s(88), s(28))
            self._add("BUTTON", L.t("restore.cancel"), WS_TABSTOP, IDCANCEL, self.cw - s(16 + 88), by, s(88), s(28))
        finally:
            if active:
                _DeactivateActCtx(0, cookie.value)
        self._show(ID_KEY)

    def ok(self) -> bool:
        key = self.key_text()
        if key and self._on_ok(key):
            self.accepted = True
            self.destroy()
            return True
        self._wrong_key()
        return False

    def on_message(self, msg, wp, lp):
        if msg == G.WM_COMMAND:
            cid = G.loword(wp)
            if cid == IDOK:
                self.ok()
            elif cid == IDCANCEL:
                self.destroy()
            elif cid == ID_EYE:
                self.toggle_key_shown()
            return 0
        if msg == G.WM_CLOSE:
            self.destroy()
            return 0
        return self._paint_message(msg, wp)

    def on_destroyed(self) -> None:
        self._free_fonts()
        G.dialogs.discard(self._own)
        if KeyWindow.current is self:
            KeyWindow.current = None
        if self._on_closed is not None:
            try:
                self._on_closed(self.accepted)
            except Exception:  # noqa: BLE001
                pass
