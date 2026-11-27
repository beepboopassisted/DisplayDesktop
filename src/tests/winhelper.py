"""Opens real test windows in its own process (another app, as far as DisplayDesktop is concerned).

    pythonw winhelper.py --out info.json --rect l,t,r,b --titles A B C [--floating] [--hang-after S --hang S]

Writes {"pid":..., "windows":{title: hwnd}, "floating": hwnd} to --out once the windows exist, then
pumps messages until the named event in --quit-event is set or --timeout seconds pass. With --hang,
it stops pumping messages for a while (the windows become "not responding" after ~5 s)."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))  # the repo

from src import gui as G  # noqa: E402
from src import native as N  # noqa: E402

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000

_k = N.kernel32
_CreateEventW = N._f(_k, "CreateEventW", N.H, ctypes.c_void_p, N.BOOL, N.BOOL, ctypes.c_wchar_p)
_MsgWait = N._f(N.user32, "MsgWaitForMultipleObjects", N.DWORD, N.DWORD, ctypes.POINTER(N.H), N.BOOL,
                N.DWORD, N.DWORD)
_PeekMessageW = N._f(N.user32, "PeekMessageW", N.BOOL, ctypes.POINTER(G.MSG), N.H, N.UINT, N.UINT, N.UINT)


class TestWindow(G.Window):
    def __init__(self, title, rect, color, ex_style=0, anchor=0):
        super().__init__()
        self.color = color
        l, t, r, b = rect
        G.Window._register("WSTestWindow")
        G.Window._creating = self
        hwnd = G.CreateWindowExW(ex_style, "WSTestWindow", title, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                 l, t, r - l, b - t, anchor, 0, G.Window._hinst, None)
        G.Window._creating = None
        self.hwnd = hwnd
        G.Window._registry[hwnd] = self

    def on_message(self, msg, wp, lp):
        if msg == G.WM_PAINT:
            with G.BufferedPaint(self.hwnd) as (dc, w, h):
                G.fill_rect(dc, (0, 0, w, h), G.rgb(*self.color))
                G.draw_text(dc, N.get_title(self.hwnd), G.font(14, 96, bold=True), (0, 0, w, h),
                            G.rgb(20, 20, 20), G.DT_CENTER | G.DT_VCENTER | G.DT_SINGLELINE)
            return 0
        if msg == G.WM_ERASEBKGND:
            return 1
        return None


def pump() -> None:
    msg = G.MSG()
    while _PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
        G.TranslateMessage(ctypes.byref(msg))
        G.DispatchMessageW(ctypes.byref(msg))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--rect", required=True)
    ap.add_argument("--titles", nargs="+", required=True)
    ap.add_argument("--floating", action="store_true")
    ap.add_argument("--hang-after", type=float, default=0)
    ap.add_argument("--hang", type=float, default=0)
    ap.add_argument("--quit-event", default="WSTestHelperQuit")
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()
    N.enable_per_monitor_dpi()

    l, t, r, b = map(int, a.rect.split(","))
    colors = [(250, 220, 120), (140, 220, 160), (150, 190, 250), (240, 160, 170), (200, 170, 240)]
    windows = {}
    w, h = (r - l) // 3, (b - t) // 3
    for i, title in enumerate(a.titles):
        x, y = l + 40 + (i % 4) * 60, t + 40 + (i % 4) * 50
        windows[title] = TestWindow(title, (x, y, x + w, y + h), colors[i % len(colors)])
    floating = None
    if a.floating:
        first = next(iter(windows.values()))
        floating = TestWindow("Palette", (l + 60, t + 60, l + 60 + w // 2, t + 60 + h // 2), (230, 230, 230),
                           ex_style=N.WS_EX_TOOLWINDOW, anchor=first.hwnd)
    pump()
    info = {"pid": os.getpid(), "windows": {k: v.hwnd for k, v in windows.items()},
            "floating": floating.hwnd if floating else 0}
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.replace(tmp, a.out)

    quit_ev = _CreateEventW(None, True, False, a.quit_event)
    handles = (N.H * 1)(quit_ev)
    start = time.monotonic()
    hung_done = a.hang <= 0
    while time.monotonic() - start < a.timeout:
        if not hung_done and time.monotonic() - start >= a.hang_after:
            end = time.monotonic() + a.hang
            while time.monotonic() < end:  # not pumping messages: the windows stop responding
                time.sleep(0.05)
            hung_done = True
        r = _MsgWait(1, handles, False, 100, 0x04FF)
        if r == 0:
            break
        pump()
    for v in list(windows.values()) + ([floating] if floating else []):
        v.destroy()
    pump()


if __name__ == "__main__":
    main()
