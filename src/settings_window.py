"""The Settings dialog (tray menu "Settings"): the version as a link to the GitHub page (SysLink
control; opens in the browser), Start With Windows, Add To Start Menu, Use GPU Acceleration (each
monitor's animations on the GPU that drives it; greyed, unchecked, without any GPU), then Show
Advanced (setting ShowAdvanced, off by default), which shows or hides the Advanced box: Settings
Folder, Encrypt Settings, Show Console, and on its row the Recovery button (the tray opens the
Recovery dialog, restore_window.py).

A plain Win32 window with standard checkboxes and a Close button. Every change applies at once.
The controls are created inside an activation context for Common Controls 6 (the manifest built
into shell32.dll), so they get the current Windows look even though python.exe has no such
manifest. Keyboard: Tab moves, Space toggles, Enter/Esc close (gui.run_message_loop passes keys
through IsDialogMessage). One instance: opening it again brings it to the front.
A copy in test mode greys Start With Windows and Add To Start Menu (it never touches the registry
or the Start menu). Show Console is refreshed whenever the dialog is activated (the console window
can be closed on its own).
"""
from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, Structure, byref, c_size_t, c_ulong, c_ushort, c_void_p, c_wchar_p
from collections.abc import Callable

from . import __version__, console, datafolder, gpu, settings, shortcut, startup
from . import gui as G
from . import native as N
from . import strings as L
from .tray_menu import icon_face  # the installed icon font (Segoe Fluent Icons / MDL2 Assets), or ""

WS_CHILD, WS_VISIBLE, WS_TABSTOP, WS_DISABLED = 0x40000000, 0x10000000, 0x00010000, 0x08000000
WS_SYSMENU, WS_EX_DLGMODALFRAME = 0x00080000, 0x00000001
BS_DEFPUSHBUTTON, BS_AUTOCHECKBOX = 0x1, 0x3
BM_GETCHECK, BM_SETCHECK = 0xF0, 0xF1
WM_SETFONT, WM_SETICON, WM_CTLCOLORBTN, WM_CTLCOLORSTATIC = 0x30, 0x80, 0x135, 0x138
IDOK, IDCANCEL = 1, 2
ID_STARTUP, ID_START_MENU, ID_CONSOLE, ID_LINK, ID_GPU = 101, 102, 104, 105, 106
REPO_URL = "https://github.com/beepboopassisted/DisplayDesktop"
TEXT_SCALE = 1.0  # Windows' default 9 pt dialog text (the layout scales with it)
WM_NOTIFY, NM_CLICK, NM_RETURN = 0x004E, 0xFFFFFFFE, 0xFFFFFFFC  # NM_* = -2, -4 as UINT
ICC_LINK_CLASS = 0x00008000
LM_GETIDEALSIZE, SS_CENTER = 0x0701, 0x1  # WM_USER + 0x301
WM_ACTIVATE, WA_INACTIVE = 0x0006, 0
MB_OK, MB_ICONWARNING = 0x0, 0x30
ID_FOLDER_LABEL, ID_FOLDER, ID_BROWSE, ID_APPLY, ID_DEFAULT = 108, 109, 110, 111, 112
ID_RECOVERY = 122  # the Recovery button (Show Console's row, right): opens the Recovery dialog
RECOVERY_W = 88
ID_ADVANCED = 114  # the Show Advanced checkbox (shows or hides the Advanced box)
ID_ENCRYPT, ID_KEY, ID_EYE, ID_REMEMBER = 115, 116, 117, 118  # Encrypt Settings, its key, show key, Remember Key
ID_KEY_APPLY, ID_CRYPT_STATUS = 119, 120  # the key's Apply, and "Encrypted" / "Not encrypted" (right)
ID_KEY_REVERT = 121  # the key's Revert (before Apply): back to the saved key
SS_RIGHT = 0x2
BOX_GAP = 32  # from the top of the Show Advanced row to the box, in layout units
OPTIONS_GAP = 8  # from the last option row to Show Advanced, in layout units
# The Advanced box, in layout units: margin, folder (label, box, Default/Apply), Encrypt Settings, key,
# Remember Key, Show Console with Recovery, margin
GROUP_HEIGHT = 14 + 54 + 28 + 10 + 24 + 6 + 28 + 6 + 28 + 10 + 28 + 14
KEY_BTN_W = 72  # the key's Revert and Apply (narrower than 88: Remember Key shares their row)
IN_BOX = (ID_FOLDER_LABEL, ID_FOLDER, ID_BROWSE, ID_DEFAULT, ID_APPLY, ID_ENCRYPT, ID_CRYPT_STATUS, ID_KEY, ID_EYE,
          ID_KEY_REVERT, ID_KEY_APPLY, ID_REMEMBER, ID_CONSOLE, ID_RECOVERY)
ES_PASSWORD, EM_SETPASSWORDCHAR, PASSWORD_CHAR = 0x0020, 0x00CC, 0x25CF  # ● like every login box
EYE_SHOWN, EYE_HIDDEN = "", ""  # Segoe Fluent Icons / MDL2: View (eye), Hide (eye with a slash)
OPTION_ROWS = 4  # checkboxes above the box: Start With Windows, Add To Start Menu, Use GPU Acceleration,
# Restore Applications On Restart
ID_RESTORE_APPS = 123
ES_AUTOHSCROLL, WS_EX_CLIENTEDGE, EN_CHANGE = 0x0080, 0x0200, 0x0300
MB_YESNO, MB_ICONQUESTION, MB_DEFBUTTON2, IDYES = 0x4, 0x20, 0x100, 6
MB_YESNOCANCEL, MB_DEFBUTTON3, IDNO = 0x3, 0x200, 7
BIF_RETURNONLYFSDIRS, BIF_NEWDIALOGSTYLE = 0x0001, 0x0040
SWP_NOSIZE = 0x0001


class _BROWSEINFOW(Structure):
    _fields_ = [("hwndOwner", N.H), ("pidlRoot", c_void_p), ("pszDisplayName", c_wchar_p),
                ("lpszTitle", c_wchar_p), ("ulFlags", ctypes.c_uint), ("lpfn", c_void_p),
                ("lParam", ctypes.c_ssize_t), ("iImage", ctypes.c_int)]


_SHBrowseForFolderW = N._f(N.shell32, "SHBrowseForFolderW", c_void_p, POINTER(_BROWSEINFOW))
_SHGetPathFromIDListW = N._f(N.shell32, "SHGetPathFromIDListW", N.BOOL, c_void_p, c_wchar_p)
_CoTaskMemFree = N._f(shortcut._ole32, "CoTaskMemFree", None, c_void_p)  # (one ole32 DLL object)
_GetWindowTextW = N._f(N.user32, "GetWindowTextW", ctypes.c_int, N.H, c_wchar_p, ctypes.c_int)
_SetWindowTextW = N._f(N.user32, "SetWindowTextW", N.BOOL, N.H, c_wchar_p)


def move_folder(target: str, on_folder=None, owner: int = 0, icon: int = 0) -> bool:
    """Settings Folder's Apply / Default (in Settings and in Recovery): a folder with any data
    file that can't be read at all is never switched to (a dialog lists each with why: Close only,
    nothing is repaired nor read by force; another folder must be chosen); otherwise asks which data
    to keep (choose_data), then moves (datafolder.move_to; `on_folder`: the tray's folder_changed).
    Returns whether it moved."""
    reason = datafolder.problem(target)
    if reason:
        G.message_box(L.f("msg.moveFail", reason), MB_OK | MB_ICONWARNING)
        return False
    bad = datafolder.unreadable_in(target)
    if bad:
        console.log("Settings folder: not switched; its files can't be read: " + ", ".join(bad))
        from .restore_window import show_bad_folder
        show_bad_folder(datafolder.expand(target), owner, icon)
        return False
    keep_new = SettingsWindow.choose_data(target)
    if keep_new is None:
        return False
    old = datafolder.current()
    try:
        new = datafolder.move_to(target, on_folder, keep_new=keep_new)
    except OSError as exc:
        G.message_box(L.f("msg.moveFail", exc.strerror or str(exc)), MB_OK | MB_ICONWARNING)
        return False
    console.log(f"Settings folder: {old} -> {new} ({'its own data' if keep_new else 'data moved there'}).")
    return True


def pick_folder(owner: int, title: str) -> str | None:
    """Windows' folder picker; the chosen folder, or None (cancelled)."""
    name = ctypes.create_unicode_buffer(260)
    info = _BROWSEINFOW(owner, None, ctypes.cast(name, c_wchar_p), title,
                        BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE, None, 0, 0)
    with shortcut._Com():
        pidl = _SHBrowseForFolderW(byref(info))
        if not pidl:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            return path.value if _SHGetPathFromIDListW(pidl, path) and path.value else None
        finally:
            _CoTaskMemFree(pidl)
COLOR_BTNFACE = 15

_GetSysColor = N._f(N.user32, "GetSysColor", N.DWORD, ctypes.c_int)
_GetSysColorBrush = N._f(N.user32, "GetSysColorBrush", N.H, ctypes.c_int)
_SetBkColor = N._f(N.gdi32, "SetBkColor", N.DWORD, N.H, N.DWORD)
_EnableWindow = N._f(N.user32, "EnableWindow", N.BOOL, N.H, N.BOOL)
_IsWindowEnabled = N._f(N.user32, "IsWindowEnabled", N.BOOL, N.H)
_GetFocus = N._f(N.user32, "GetFocus", N.H)
_MessageBeep = N._f(N.user32, "MessageBeep", N.BOOL, ctypes.c_uint)
_AdjustWindowRectExForDpi = N._f(N.user32, "AdjustWindowRectExForDpi", N.BOOL, POINTER(N.RECT), N.DWORD,
                                 N.BOOL, N.DWORD, N.UINT)


class _ACTCTXW(Structure):
    _fields_ = [("cbSize", c_ulong), ("dwFlags", N.DWORD), ("lpSource", c_wchar_p),
                ("wProcessorArchitecture", c_ushort), ("wLangId", c_ushort),
                ("lpAssemblyDirectory", c_wchar_p), ("lpResourceName", c_void_p),
                ("lpApplicationName", c_wchar_p), ("hModule", N.H)]


class _NMHDR(Structure):
    _fields_ = [("hwndFrom", N.H), ("idFrom", c_size_t), ("code", N.UINT)]


class _SIZE(Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _ICC(Structure):
    _fields_ = [("dwSize", N.DWORD), ("dwICC", N.DWORD)]


_InitCommonControlsEx = N._f(N.comctl32, "InitCommonControlsEx", N.BOOL, POINTER(_ICC))
_ShellExecuteW = N._f(N.shell32, "ShellExecuteW", N.H, N.H, c_wchar_p, c_wchar_p, c_wchar_p, c_wchar_p,
                      ctypes.c_int)


def open_repo_page() -> None:
    """Opens the GitHub page in the default browser (only when the user clicks the link)."""
    _ShellExecuteW(0, "open", REPO_URL, None, None, 1)  # SW_SHOWNORMAL


_CreateActCtxW = N._f(N.kernel32, "CreateActCtxW", N.H, POINTER(_ACTCTXW))
_ActivateActCtx = N._f(N.kernel32, "ActivateActCtx", N.BOOL, N.H, POINTER(c_size_t))
_DeactivateActCtx = N._f(N.kernel32, "DeactivateActCtx", N.BOOL, N.DWORD, c_size_t)
ACTCTX_FLAG_RESOURCE_NAME_VALID = 0x8
_actctx: int | None = None


def _visual_styles() -> int:
    """Activation context for Common Controls 6 (0 if unavailable: classic-looking controls)."""
    global _actctx
    if _actctx is None:
        ctx = _ACTCTXW(ctypes.sizeof(_ACTCTXW), ACTCTX_FLAG_RESOURCE_NAME_VALID,
                       os.path.join(N._SYS32, "shell32.dll"), 0, 0, None, c_void_p(124), None, 0)
        h = _CreateActCtxW(byref(ctx))
        _actctx = 0 if h in (0, -1) else h
    return _actctx


BOX_BORDER_PX = 1  # the Advanced box's border (at 96 DPI; scaled)
BROWSE_W = 36  # the folder picker button (an icon), in layout units
FOLDER_GLYPH = ""  # Segoe Fluent Icons / MDL2 Assets: FolderOpen
BOX_BORDER = G.rgb(160, 24, 24)  # the Advanced box's border: a deep red (its inside is the dialog's colour)


def eye_text(shown: bool, face: str) -> str:
    """The show-key button: an eye while the key is shown, the eye with a slash while it is hidden
    as dots (like a web login); words without an icon font."""
    if face:
        return EYE_SHOWN if shown else EYE_HIDDEN
    return L.t("key.hide") if shown else L.t("key.show")


def show_key(edit: int, eye: int, shown: bool, face: str) -> None:
    """A key box: its text shown, or dots (the default)."""
    G.SendMessageW(edit, EM_SETPASSWORDCHAR, 0 if shown else PASSWORD_CHAR, None)
    _SetWindowTextW(eye, eye_text(shown, face))
    G.InvalidateRect(edit, None, True)


def text_of(h: int) -> str:
    buf = ctypes.create_unicode_buffer(32768)
    _GetWindowTextW(h, buf, 32768)
    return buf.value


def icon_font(dpi: int, face: str) -> int:
    """The icon font for the folder and eye buttons (0 without one)."""
    return G.CreateFontW(-int(round(10.5 * TEXT_SCALE * dpi / 72.0)), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                         face) if face else 0


def _box_colors() -> tuple[int, int]:
    """The Advanced box: (fill, border). The fill is the dialog's own colour; the border a deep red
    (BOX_BORDER)."""
    return _GetSysColor(COLOR_BTNFACE), BOX_BORDER


class SettingsWindow(G.Window):
    current: "SettingsWindow | None" = None

    @classmethod
    def show(cls, icon: int = 0, on_recovery: Callable[[], None] | None = None,
             on_folder: Callable[[str], None] | None = None,
             on_encrypt: Callable[[str | None, bool, bool], bool] | None = None,
             on_remember: Callable[[bool], bool] | None = None) -> "SettingsWindow | None":
        """Opens the dialog, or brings the open one to the front. on_recovery runs for the Recovery
        button (opens the Recovery dialog); on_encrypt(key, on, remember) turns Encrypt Settings
        on (with that key, also a new key) or off and returns whether it worked; on_remember(on)
        remembers the key on this PC or forgets it and returns the resulting state. None (nothing
        opens) while Windows reports no display."""
        if cls.current is not None and cls.current.hwnd:
            G.ShowWindow(cls.current.hwnd, N.SW_SHOW)
            G.SetForegroundWindow(cls.current.hwnd)
            return cls.current
        scr = N.pick_screen(N.screens(), N.cursor_pos())
        if scr is None:
            return None
        cls.current = cls(icon, on_recovery, scr, on_folder, on_encrypt, on_remember)
        return cls.current

    @classmethod
    def close_open(cls) -> None:
        if cls.current is not None:
            cls.current.destroy()

    def __init__(self, icon: int = 0, on_recovery: Callable[[], None] | None = None, scr: N.Screen | None = None,
                 on_folder: Callable[[str], None] | None = None, on_encrypt=None, on_remember=None):
        super().__init__()
        scr = scr or N.pick_screen(N.screens(), N.cursor_pos())
        if scr is None:
            raise OSError("no display")
        self._on_recovery = on_recovery
        self._icon = icon
        self._on_encrypt, self._on_remember = on_encrypt, on_remember
        self._key_shown = False  # the key box shows its text (the eye button), else dots
        self._closing = False  # close() is asking whether to apply the key typed
        self._box_key = ""  # the key in use the box was last set to show (refresh_options)
        self._face = ""  # the icon font's face ("" without one)
        self._box: tuple[int, int, int, int] | None = None  # the Advanced box (client pixels) while shown
        self._box_at: tuple[int, int, int, int] | None = None  # where it goes when shown
        self._brush = 0  # its background, for the controls in it
        self._icon_font = 0  # the folder picker's icon
        self._on_folder = on_folder  # the tray points the manager's files at the new folder
        self._controls: dict[int, int] = {}
        self._has_gpu = bool(gpu.adapters())  # (no GPU at all: Use GPU Acceleration greyed)
        self._handlers: dict[int, Callable[[bool], bool]] = {
            ID_STARTUP: self._set_startup,
            ID_START_MENU: self._set_start_menu,
            ID_CONSOLE: self._set_console,
            ID_GPU: self._set_gpu,
            ID_RESTORE_APPS: self._set_restore_apps,
            ID_ADVANCED: self._set_advanced,
            ID_ENCRYPT: self._set_encrypt,
            ID_REMEMBER: self._set_remember,
        }
        l, t, r, b = scr.work
        self._work = scr.work
        self.create("DisplayDesktop.Settings", WS_EX_DLGMODALFRAME, N.WS_CAPTION | WS_SYSMENU,
                    (l + r) // 2, (t + b) // 2, 0, 0, title=L.t("settings.title"))
        self._own = self.hwnd  # on_destroyed runs after hwnd is cleared
        G.dialogs.add(self._own)
        if icon:
            G.SendMessageW(self.hwnd, WM_SETICON, 0, icon)
            G.SendMessageW(self.hwnd, WM_SETICON, 1, icon)
        dpi = N.GetDpiForWindow(self.hwnd) or 96
        self._dpi = dpi

        def s(v: float) -> int:  # layout units: 96-DPI pixels, scaled with the fonts (TEXT_SCALE)
            return int(round(v * TEXT_SCALE * dpi / 96.0))
        self._s = s

        # client size (with or without the Advanced box), centered in the work area of the monitor
        # under the mouse
        advanced = bool(settings.get("ShowAdvanced"))
        cw, ch = s(340), self._client_height(advanced)
        w, h = self._outer(cw, ch)
        G.SetWindowPos(self.hwnd, 0, (l + r - w) // 2, (t + b - h) // 2, w, h, N.SWP_NOZORDER | N.SWP_NOACTIVATE)

        manage = startup.can_manage()
        cookie = c_size_t()
        ctx = _visual_styles()
        active = bool(ctx) and _ActivateActCtx(ctx, byref(cookie))
        try:
            font = G.font(9 * TEXT_SCALE, dpi)
            # the version, as a link to the GitHub page (plain text if the link control is unavailable)
            _InitCommonControlsEx(byref(_ICC(ctypes.sizeof(_ICC), ICC_LINK_CLASS)))
            version = f"DisplayDesktop {__version__}"
            bold = G.font(9 * TEXT_SCALE, dpi, bold=True)
            try:
                self._add("SysLink", f'<a href="{REPO_URL}">{version}</a>', WS_TABSTOP, ID_LINK,
                          s(16), s(16), cw - s(32), s(20), bold)
                # centered: SysLink has no centering style, so it is sized to its text and moved
                size = _SIZE()
                G.SendMessageW(self._controls[ID_LINK], LM_GETIDEALSIZE, cw - s(32), ctypes.addressof(size))
                lw = min(cw - s(32), size.cx) if size.cx > 0 else cw - s(32)
                G.SetWindowPos(self._controls[ID_LINK], 0, (cw - lw) // 2, s(16), lw, s(20),
                               N.SWP_NOZORDER | N.SWP_NOACTIVATE)
            except OSError:
                self._add("STATIC", version, SS_CENTER, ID_LINK, s(16), s(16), cw - s(32), s(20), bold)
            disabled = 0 if manage else WS_DISABLED
            # (Add To Start Menu greyed while off without a windowless Python to start; on, it stays
            # clickable so it can be turned off)
            try:
                shortcut.target()
                no_shortcut = 0
            except OSError:
                no_shortcut = WS_DISABLED
            menu_on = settings.get("StartMenu")
            rows = [(ID_STARTUP, "settings.startup", startup.is_enabled(), disabled),
                    (ID_START_MENU, "settings.startMenu", menu_on, disabled | (0 if menu_on else no_shortcut)),
                    (ID_GPU, "settings.gpu", settings.get("UseGpu") and self._has_gpu,
                     0 if self._has_gpu else WS_DISABLED),
                    (ID_RESTORE_APPS, "settings.restoreApps", settings.get("RestoreApps"), 0)]
            for i, (cid, key, on, off) in enumerate(rows):
                self._add("BUTTON", L.t(key), WS_TABSTOP | BS_AUTOCHECKBOX | off, cid,
                          s(16), s(42 + 28 * i), cw - s(32), s(24), font)
                G.SendMessageW(self._controls[cid], BM_SETCHECK, 1 if on else 0, None)
            # Show Advanced, then the Advanced box it shows or hides: Settings Folder (Browse, Default,
            # Apply), Encrypt Settings, then Show Console with the Recovery button on its right. The box
            # itself (the dialog's colour inside a deep red BOX_BORDER_PX border) is painted by the
            # dialog (WM_ERASEBKGND).
            gy = 42 + 28 * len(rows) + OPTIONS_GAP
            self._add("BUTTON", L.t("settings.advanced"), WS_TABSTOP | BS_AUTOCHECKBOX, ID_ADVANCED,
                      s(16), s(gy), cw - s(32), s(24), font)
            G.SendMessageW(self._controls[ID_ADVANCED], BM_SETCHECK, 1 if advanced else 0, None)
            by = gy + BOX_GAP
            self._box_at = (s(12), s(by), cw - s(12), s(by + GROUP_HEIGHT))
            left, right = s(24), cw - s(24)  # inside the box
            fy = by + 14
            self._add("STATIC", L.t("settings.folder"), 0, ID_FOLDER_LABEL, left, s(fy), right - left, s(18), font)
            self._add("EDIT", datafolder.shown(datafolder.current()), WS_TABSTOP | ES_AUTOHSCROLL | disabled, ID_FOLDER,
                      left, s(fy + 20), right - left - s(BROWSE_W + 8), s(24), font, ex=WS_EX_CLIENTEDGE)
            # the folder picker: a folder icon (the icon font; "..." without one)
            face = self._face = icon_face()
            self._icon_font = icon_font(dpi, face)
            self._add("BUTTON", FOLDER_GLYPH if face else "...", WS_TABSTOP | disabled, ID_BROWSE,
                      right - s(BROWSE_W), s(fy + 18), s(BROWSE_W), s(28), self._icon_font or font)
            self._add("BUTTON", L.t("settings.default"), WS_TABSTOP, ID_DEFAULT,
                      right - s(88 + 8 + 88), s(fy + 54), s(88), s(28), font)
            self._add("BUTTON", L.t("settings.apply"), WS_TABSTOP, ID_APPLY,
                      right - s(88), s(fy + 54), s(88), s(28), font)
            self.update_folder_buttons()
            # Encrypt Settings, its key (dots; the eye shows it) and Remember Key, indented under it
            from . import crypto
            ey = fy + 54 + 28 + 10
            half = (right - left) // 2
            self._add("BUTTON", L.t("settings.encrypt"), WS_TABSTOP | BS_AUTOCHECKBOX, ID_ENCRYPT,
                      left, s(ey), half, s(24), font)
            G.SendMessageW(self._controls[ID_ENCRYPT], BM_SETCHECK, 1 if crypto.enabled else 0, None)
            self._add("STATIC", "", SS_RIGHT | 0x200, ID_CRYPT_STATUS, left + half, s(ey), right - left - half, s(24),
                      font)  # (SS_CENTERIMAGE: centered vertically like the checkbox's text)
            ky = ey + 24 + 6
            indent = left + s(20)
            self._box_key = (crypto.key() or "") if crypto.enabled else ""
            self._add("EDIT", self._box_key, WS_TABSTOP | ES_AUTOHSCROLL | ES_PASSWORD, ID_KEY,
                      indent, s(ky + 2), right - indent - s(BROWSE_W + 8), s(24), font, ex=WS_EX_CLIENTEDGE)
            self._add("BUTTON", eye_text(False, face), WS_TABSTOP, ID_EYE,
                      right - s(BROWSE_W), s(ky), s(BROWSE_W), s(28), self._icon_font or font)
            show_key(self._controls[ID_KEY], self._controls[ID_EYE], False, face)
            # under the key, like Settings Folder's buttons: Remember Key on the left, Revert and Apply
            # on the right (the key typed changes nothing until Apply; Revert puts the saved one back)
            # (one row, so the dialog stays short enough for a small screen: the two buttons are narrower)
            ry = ky + 28 + 6
            self._add("BUTTON", L.t("settings.rememberKey"), WS_TABSTOP | BS_AUTOCHECKBOX | disabled, ID_REMEMBER,
                      indent, s(ry + 2), right - indent - s(KEY_BTN_W + 8 + KEY_BTN_W + 8), s(24), font)
            self._add("BUTTON", L.t("settings.revert"), WS_TABSTOP, ID_KEY_REVERT,
                      right - s(KEY_BTN_W + 8 + KEY_BTN_W), s(ry), s(KEY_BTN_W), s(28), font)
            self._add("BUTTON", L.t("settings.apply"), WS_TABSTOP, ID_KEY_APPLY,
                      right - s(KEY_BTN_W), s(ry), s(KEY_BTN_W), s(28), font)
            G.SendMessageW(self._controls[ID_REMEMBER], BM_SETCHECK,
                           1 if (crypto.remembered() or not crypto.enabled) and manage else 0, None)
            self.update_key_controls()
            # Show Console, and on its row (right) the Recovery button: opens the Recovery dialog
            cy = ry + 28 + 10
            self._add("BUTTON", L.t("settings.console"), WS_TABSTOP | BS_AUTOCHECKBOX, ID_CONSOLE,
                      left, s(cy + 2), right - left - s(RECOVERY_W + 8), s(24), font)
            G.SendMessageW(self._controls[ID_CONSOLE], BM_SETCHECK, 1 if console.is_open() else 0, None)
            self._add("BUTTON", L.t("settings.recovery"), WS_TABSTOP | (0 if on_recovery else WS_DISABLED),
                      ID_RECOVERY, right - s(RECOVERY_W), s(cy), s(RECOVERY_W), s(28), font)
            self._add("BUTTON", L.t("settings.close"), WS_TABSTOP | BS_DEFPUSHBUTTON, IDOK,
                      cw - s(16 + 88), ch - s(14 + 28), s(88), s(28), font)
        finally:
            if active:
                _DeactivateActCtx(0, cookie.value)
        self._show_advanced(advanced, resize=False)
        G.ShowWindow(self.hwnd, N.SW_SHOW)
        G.SetForegroundWindow(self.hwnd)
        G.SetFocus(self._controls[IDOK])

    def _add(self, cls: str, text: str, style: int, cid: int, x: int, y: int, w: int, h: int, font: int,
             ex: int = 0) -> None:
        hwnd = G.CreateWindowExW(ex, cls, text, WS_CHILD | WS_VISIBLE | style, x, y, w, h, self.hwnd, cid,
                                 G.Window._hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        G.SendMessageW(hwnd, WM_SETFONT, font, 1)
        self._controls[cid] = hwnd

    def checked(self, cid: int) -> bool:
        return G.SendMessageW(self._controls[cid], BM_GETCHECK, 0, None) == 1

    # ------------------------------------------------------------ Show Advanced

    def _client_height(self, advanced: bool) -> int:
        """Pixels: the option rows, Show Advanced, the Advanced box when shown, then Close."""
        top = 42 + 28 * OPTION_ROWS + OPTIONS_GAP
        return self._s(top + (BOX_GAP + GROUP_HEIGHT if advanced else 24) + 14 + 28 + 14)

    def _outer(self, cw: int, ch: int) -> tuple[int, int]:
        rc = N.RECT(0, 0, cw, ch)
        _AdjustWindowRectExForDpi(byref(rc), N.WS_CAPTION | WS_SYSMENU, False, WS_EX_DLGMODALFRAME, self._dpi)
        return rc.right - rc.left, rc.bottom - rc.top

    def _show_advanced(self, on: bool, resize: bool = True) -> None:
        """Shows or hides the Advanced box (its controls and red border); the dialog grows or
        shrinks below, keeping its top (moved up only as far as needed to stay on the work area)."""
        self._box = self._box_at if on else None
        for cid in IN_BOX:
            if cid in self._controls:
                G.ShowWindow(self._controls[cid], N.SW_SHOWNA if on else N.SW_HIDE)
        if not resize:
            return
        cw = G.client_rect(self.hwnd)[2]
        ch = self._client_height(on)
        w, h = self._outer(cw, ch)
        rect = N.get_window_rect(self.hwnd)
        if rect is not None:
            x, y = rect[0], rect[1]
            y = max(self._work[1], min(y, self._work[3] - h))
            G.SetWindowPos(self.hwnd, 0, x, y, w, h, N.SWP_NOZORDER | N.SWP_NOACTIVATE)
        G.SetWindowPos(self._controls[IDOK], 0, cw - self._s(16 + 88), ch - self._s(14 + 28), 0, 0,
                       N.SWP_NOZORDER | N.SWP_NOACTIVATE | SWP_NOSIZE)
        G.InvalidateRect(self.hwnd, None, True)

    def _set_advanced(self, on: bool) -> bool:
        settings.set("ShowAdvanced", on)
        self._show_advanced(on)
        return on

    # ------------------------------------------------------------ settings folder

    def folder_text(self) -> str:
        buf = ctypes.create_unicode_buffer(32768)
        _GetWindowTextW(self._controls[ID_FOLDER], buf, 32768)
        return buf.value.strip()

    def update_folder_buttons(self) -> None:
        """Apply: only when the folder typed or picked differs from the one in use. Default: only
        when the one in use is not the default. Neither in test mode."""
        manage = startup.can_manage()
        typed = self.folder_text()
        _EnableWindow(self._controls[ID_APPLY], manage and bool(typed) and not datafolder.same(typed, datafolder.current()))
        _EnableWindow(self._controls[ID_DEFAULT], manage and not datafolder.default_in_use())

    # ------------------------------------------------------------ Encrypt Settings

    def key_text(self) -> str:
        return text_of(self._controls[ID_KEY])

    def update_key_controls(self) -> None:
        """Encrypt Settings usable when the app handles it; the key, its eye, its Apply and Remember
        Key (never in test mode: the registry) only while it is checked; Apply only for a key to
        apply (none yet, or another one). The status (right of the checkbox) says whether the files
        are encrypted now."""
        from . import crypto
        if not all(c in self._controls for c in (ID_ENCRYPT, ID_CRYPT_STATUS, ID_KEY, ID_EYE, ID_REMEMBER,
                                                  ID_KEY_REVERT, ID_KEY_APPLY)):
            return  # (still being created: an edit box with text sends EN_CHANGE as it is made; set after)
        on = self.checked(ID_ENCRYPT)
        key = self.key_text()
        _EnableWindow(self._controls[ID_ENCRYPT], self._on_encrypt is not None)
        for cid in (ID_KEY, ID_EYE):
            _EnableWindow(self._controls[cid], on)
        _EnableWindow(self._controls[ID_KEY_REVERT], on and key != self._saved_key and self._on_encrypt is not None)
        _EnableWindow(self._controls[ID_KEY_APPLY],
                      on and bool(key) and (not crypto.enabled or key != crypto.key()) and self._on_encrypt is not None)
        _EnableWindow(self._controls[ID_REMEMBER], on and startup.can_manage() and self._on_remember is not None)
        _SetWindowTextW(self._controls[ID_CRYPT_STATUS],
                        L.t("settings.encrypted" if crypto.enabled else "settings.notEncrypted"))

    def toggle_key_shown(self) -> None:
        """The eye button: the key as text, or as dots again."""
        self._key_shown = not self._key_shown
        show_key(self._controls[ID_KEY], self._controls[ID_EYE], self._key_shown, self._face)

    def _set_encrypt(self, on: bool) -> bool:
        """Checked: the key can be typed and applied (Apply). Unchecked while the files are
        encrypted: they are written plain again at once."""
        from . import crypto
        if self._on_encrypt is None:
            return not on
        now = on
        if on:
            G.SetFocus(self._controls[ID_KEY])
        elif crypto.enabled and not self._on_encrypt(None, False, self.checked(ID_REMEMBER)):
            now = True  # (could not be written plain: still encrypted)
        self._box_key = self._saved_key
        G.SendMessageW(self._controls[ID_ENCRYPT], BM_SETCHECK, 1 if now else 0, None)
        self.update_key_controls()
        return now

    def _apply_key(self) -> bool:
        """Apply (or Enter in the key box): every data file encrypted with the key typed (also a
        new key for files already encrypted). Returns whether the key typed is the one in use now."""
        from . import crypto
        key = self.key_text()
        if not (self.checked(ID_ENCRYPT) and key and self._on_encrypt is not None):
            return False
        if crypto.enabled and key == crypto.key():
            return True  # (nothing to change)
        done = bool(self._on_encrypt(key, True, self.checked(ID_REMEMBER)))  # (the saved key now if done)
        if done:
            self._box_key = self._saved_key  # (the box shows the key in use: a later key change may follow it)
        self.update_key_controls()
        return done

    @property
    def _saved_key(self) -> str:
        """The key the files are encrypted with now ("" while not encrypted): what Revert puts back,
        and what a key typed is compared with. Always read afresh (Apply, unchecking, the key popup)."""
        from . import crypto
        return (crypto.key() or "") if crypto.enabled else ""

    def revert_key(self) -> None:
        """Revert: the key box shows the saved key again. Nothing is saved or re-encrypted."""
        _SetWindowTextW(self._controls[ID_KEY], self._saved_key)  # (EN_CHANGE updates the buttons)
        self.update_key_controls()

    def key_unapplied(self) -> bool:
        """A key typed that Apply would apply (Encrypt Settings checked) and that is not the saved one."""
        return (ID_KEY in self._controls and self._on_encrypt is not None and self.checked(ID_ENCRYPT)
                and bool(self.key_text()) and self.key_text() != self._saved_key)

    def close(self) -> None:
        """Close, Esc, the title bar's X: a key typed but not applied is applied or reverted (asked;
        Revert by default, also when that question is closed any other way). The app closing the
        dialog itself (close_open, at exit) never applies anything. A close while the question is
        open (taskbar Close) is ignored: one question at a time."""
        if self._closing:
            return
        if self.key_unapplied():
            from .restore_window import ask_apply_key
            self._closing = True
            try:
                apply = ask_apply_key(self.hwnd)
            finally:
                self._closing = False
            if not self.hwnd:
                return  # (closed meanwhile by the app itself: nothing applied)
            if apply:
                if not self._apply_key():
                    return  # (could not be applied (told by the tray): Settings stays open, the key typed kept)
            else:
                self.revert_key()  # (nothing saved: just the box)
        if self.hwnd:
            self.destroy()

    def _set_remember(self, on: bool) -> bool:
        """Unchecked: the key is removed from the registry at once (always). Checked: stored at once
        while encryption is on; before that, the choice is used at Apply."""
        from . import crypto
        if self._on_remember is None:
            return not on
        return self._on_remember(on) if crypto.enabled or not on else on

    def _browse(self) -> None:
        picked = pick_folder(self.hwnd, L.t("settings.folder"))
        if picked:
            _SetWindowTextW(self._controls[ID_FOLDER], datafolder.shown(picked))  # (EN_CHANGE updates the buttons)

    @staticmethod
    def choose_data(target: str) -> bool | None:
        """Which data a move to `target` keeps; asks the user. True: the target folder's own data
        (it has some, and the current folder has none it can read, or this run could not reach the
        target at start: its data is the real one); asked when both have data. False: the current
        data (moved there). None: cancelled."""
        old = datafolder.current()
        shown = datafolder.shown(datafolder.expand(target))
        theirs = not datafolder.same_on_disk(target, old) and datafolder.has_data(target)
        returning = bool(datafolder.unreachable) and datafolder.same_on_disk(target, datafolder.unreachable)
        if theirs and (returning or not datafolder.has_data(old)):
            answer = G.message_box(L.f("msg.useFolderData", shown), MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2)
            return True if answer == IDYES else None
        if theirs:
            answer = G.message_box(L.f("msg.bothHaveData", shown), MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON3)
            return {IDYES: True, IDNO: False}.get(answer)
        key = "msg.defaultConfirm" if datafolder.same(target, datafolder.default_folder()) else "msg.moveConfirm"
        return False if G.message_box(L.f(key, shown), MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2) == IDYES else None

    def _move_folder(self, target: str) -> None:
        """Apply / Default: see move_folder."""
        if not move_folder(target, self._on_folder, self.hwnd, self._icon):
            return
        if self.hwnd:
            _SetWindowTextW(self._controls[ID_FOLDER], datafolder.shown(datafolder.current()))
            self.update_folder_buttons()
            self.refresh_options()

    def refresh_options(self) -> None:
        """The checkboxes show the settings now in use (another folder's data may
        have been chosen)."""
        for cid, on in ((ID_START_MENU, settings.get("StartMenu")), (ID_CONSOLE, console.is_open()),
                        (ID_GPU, settings.get("UseGpu") and self._has_gpu),
                        (ID_RESTORE_APPS, settings.get("RestoreApps"))):
            if cid in self._controls:
                G.SendMessageW(self._controls[cid], BM_SETCHECK, 1 if on else 0, None)
        if ID_ENCRYPT in self._controls:  # (a folder's own files, or a key typed in the popup)
            from . import crypto
            G.SendMessageW(self._controls[ID_ENCRYPT], BM_SETCHECK, 1 if crypto.enabled else 0, None)
            # (the box follows the key in use only while it still shows the one shown before: a key typed
            # and not applied is never replaced without a word)
            if crypto.enabled and crypto.key() and self.key_text() in ("", self._box_key):
                _SetWindowTextW(self._controls[ID_KEY], crypto.key())
            self._box_key = self._saved_key
            self.update_key_controls()

    # ------------------------------------------------------------ options (return the new state)

    @staticmethod
    def _set_startup(on: bool) -> bool:
        now = startup.set_enabled(on)
        console.log(f"Start With Windows: {'on' if now else 'off'}.")
        return now

    @staticmethod
    def _set_start_menu(on: bool) -> bool:
        now = shortcut.set_enabled(on)
        console.log(f"Add To Start Menu: {'on' if now else 'off'}.")
        return now

    def _set_gpu(self, on: bool) -> bool:
        """Remembered as asked; each monitor's animations run on the GPU that drives it, where there
        is one (else the CPU)."""
        settings.set("UseGpu", on)
        gpu.set_enabled(on)
        console.log(f"Use GPU Acceleration: {'on' if on else 'off'} (animations run on {gpu.describe()}).")
        return on

    @staticmethod
    def _set_restore_apps(on: bool) -> bool:
        """After a Windows restart, the apps that were open start again (DesktopManager.relaunch_apps)."""
        settings.set("RestoreApps", on)  # (read at the next start: the only time it acts)
        console.log(f"Restore Applications On Restart: {'on' if on else 'off'}.")
        return on

    @staticmethod
    def _set_console(on: bool) -> bool:
        now = console.set_enabled(on)
        if on and not now:
            G.message_box(L.t("msg.consoleFail"), MB_OK | MB_ICONWARNING)
        return now

    def on_message(self, msg, wp, lp):
        if msg == WM_ACTIVATE and G.loword(wp) != WA_INACTIVE and ID_CONSOLE in self._controls:
            # the console may have been closed from its own window meanwhile
            G.SendMessageW(self._controls[ID_CONSOLE], BM_SETCHECK, 1 if console.is_open() else 0, None)
            return None
        if msg == WM_NOTIFY:
            hdr = _NMHDR.from_address(lp)
            if hdr.idFrom == ID_LINK and hdr.code in (NM_CLICK, NM_RETURN):
                open_repo_page()
                return 0
            return None
        if msg == G.WM_COMMAND:
            cid = G.loword(wp)
            if cid == IDOK and _GetFocus() == self._controls.get(ID_FOLDER):
                # Enter in the folder box: Apply (when there is something to apply), never close
                if _IsWindowEnabled(self._controls[ID_APPLY]):
                    self._move_folder(self.folder_text())
            elif cid == IDOK and _GetFocus() == self._controls.get(ID_KEY):
                self._apply_key()  # Enter in the key box: a new key applies, never close
            elif cid == ID_KEY:
                if (wp >> 16) & 0xFFFF == EN_CHANGE:  # (also sent while the box is created with a key in it)
                    self.update_key_controls()
            elif cid == ID_KEY_APPLY:
                self._apply_key()
            elif cid == ID_KEY_REVERT:
                self.revert_key()
            elif cid == ID_EYE:
                self.toggle_key_shown()
            elif cid in (IDOK, IDCANCEL):
                self.close()
            elif cid == ID_RECOVERY and self._on_recovery is not None:
                self._on_recovery()  # the Recovery dialog; this stays open
            elif cid == ID_FOLDER:
                if (wp >> 16) & 0xFFFF == EN_CHANGE and ID_APPLY in self._controls:
                    self.update_folder_buttons()
            elif cid == ID_BROWSE:
                self._browse()
            elif cid == ID_APPLY:
                self._move_folder(self.folder_text())
            elif cid == ID_DEFAULT:
                self._move_folder(datafolder.default_folder())
            elif cid in self._handlers and (wp >> 16) & 0xFFFF == 0:  # BN_CLICKED
                now = self._handlers[cid](self.checked(cid))
                G.SendMessageW(self._controls[cid], BM_SETCHECK, 1 if now else 0, None)
            return 0
        if msg == G.WM_CLOSE:
            self.close()
            return 0
        if msg == G.WM_ERASEBKGND:
            G.fill_rect(wp, G.client_rect(self.hwnd), _GetSysColor(COLOR_BTNFACE))
            if self._box is not None:
                l, t, r, b = self._box
                k = max(1, round(BOX_BORDER_PX * self._dpi / 96))
                G.fill_rect(wp, self._box, _box_colors()[1])                     # the border
                G.fill_rect(wp, (l + k, t + k, r - k, b - k), _box_colors()[0])  # the inside
            return 1
        if msg in (WM_CTLCOLORBTN, WM_CTLCOLORSTATIC):
            if lp in self._in_box():
                _SetBkColor(wp, _box_colors()[0])
                return self._box_brush()
            _SetBkColor(wp, _GetSysColor(COLOR_BTNFACE))
            return _GetSysColorBrush(COLOR_BTNFACE)
        return None

    def _in_box(self) -> set[int]:
        return {self._controls[c] for c in IN_BOX if c in self._controls}

    def _box_brush(self) -> int:
        if not self._brush:
            self._brush = G.CreateSolidBrush(_box_colors()[0])
        return self._brush

    def on_destroyed(self) -> None:
        for name in ("_brush", "_icon_font"):
            if getattr(self, name):
                G.DeleteObject(getattr(self, name))
                setattr(self, name, 0)
        G.dialogs.discard(self._own)
        if SettingsWindow.current is self:
            SettingsWindow.current = None
