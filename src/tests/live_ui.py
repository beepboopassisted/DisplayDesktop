"""Live UI test: Overview (layout, live previews, snapshots, clicks, drag and drop, "+", menu, scroll,
Esc, tooltips), the Settings dialog (version link, checkboxes incl. Show Console, Use GPU Acceleration and Restore Applications On Restart, Clear Desktops, Esc/Enter, test mode), slide animation (refresh-paced) and GDI/USER handle leaks. Runs in-process with real windows
(winhelper.py) and a DesktopManager scoped to them, so none of the user's windows are touched.

    python -B src/tests/live_ui.py [--frames <folder>]   (saves the animation frames as one PNG)
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import tempfile
import threading
import time
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))  # the repo (src\tests -> ..\..)
sys.path.insert(0, HERE)

from src import gui as G  # noqa: E402
from src import native as N  # noqa: E402
from src.desktop_manager import DesktopManager  # noqa: E402
from src.overview import Overview  # noqa: E402
from src import slide as S  # noqa: E402
from src.slide import SlideAnimator  # noqa: E402

from live_core import Helper, TestWin, check, force_foreground, real_app_stopped, results, wait_for  # noqa: E402
from moving import move_window  # noqa: E402

_PeekMessageW = N._f(N.user32, "PeekMessageW", N.BOOL, ctypes.POINTER(G.MSG), N.H, N.UINT, N.UINT, N.UINT)
_FindWindowExW = N._f(N.user32, "FindWindowExW", N.H, N.H, N.H, ctypes.c_wchar_p, ctypes.c_wchar_p)
_GetGuiResources = N._f(N.user32, "GetGuiResources", N.DWORD, N.H, N.DWORD)
_GetCurrentProcess = N._f(N.kernel32, "GetCurrentProcess", N.H)
MK_LBUTTON = 1
PIXEL_TOLERANCE = 4  # levels per channel: text smoothing on screen vs in a bitmap (seen: 1-2)
KEY_TO_MOTION_MS = 60  # GPU slide: switch -> first moving frame (seen 24-32 ms; 63-85 with a capture)

errors: list[BaseException] = []
G.set_fatal_handler(errors.append)


def pump(ms: float = 50) -> None:
    end = time.monotonic() + ms / 1000
    msg = G.MSG()
    while True:
        while _PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
            if any(G.IsDialogMessageW(h, ctypes.byref(msg)) for h in tuple(G.dialogs)):
                continue  # dialog keys, as in gui.run_message_loop
            G.TranslateMessage(ctypes.byref(msg))
            G.DispatchMessageW(ctypes.byref(msg))
        if time.monotonic() >= end:
            return
        time.sleep(0.01)


def pump_until(pred, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(20)
        if pred():
            return True
    return bool(pred())


def lparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def center(ov: Overview, rect) -> tuple[int, int]:
    x, y, w, h = rect
    return x + w // 2, y + h // 2 - ov._scroll


def click(ov: Overview, pt) -> None:
    G.SendMessageW(ov.hwnd, G.WM_LBUTTONDOWN, MK_LBUTTON, lparam(*pt))
    G.SendMessageW(ov.hwnd, G.WM_LBUTTONUP, 0, lparam(*pt))
    pump(50)


def drag(ov: Overview, a, b) -> None:
    hwnd = ov.hwnd
    G.SendMessageW(hwnd, G.WM_LBUTTONDOWN, MK_LBUTTON, lparam(*a))
    steps = 6
    for i in range(1, steps + 1):
        x = a[0] + (b[0] - a[0]) * i // steps
        y = a[1] + (b[1] - a[1]) * i // steps
        G.SendMessageW(hwnd, G.WM_MOUSEMOVE, MK_LBUTTON, lparam(x, y))
    G.SendMessageW(hwnd, G.WM_LBUTTONUP, 0, lparam(*b))
    pump(80)


def cards_of(ov: Overview, device: str):
    return [c for c in ov.cards if c.mon.device == device]


def tile_of(ov: Overview, h: int):
    for c in ov.cards:
        for t in c.tiles:
            if t.win.handle == h:
                return t, c
    return None, None


def find_visible(cls: str) -> int:
    h = _FindWindowExW(0, 0, cls, None)
    while h:
        if N.is_window_visible(h) and N.get_pid(h) == os.getpid():
            return h
        h = _FindWindowExW(0, h, cls, None)
    return 0


THUMB_W = 240


def _thumb(src_dc: int, w: int, h: int) -> tuple[int, int, bytes]:
    """(width, height, BGRA bytes) of a small copy of src_dc's w x h picture."""
    tw, th = THUMB_W, max(1, h * THUMB_W // w)
    dc = G.CreateCompatibleDC(0)
    bmp, bits = G.create_dib(tw, th)
    old = G.SelectObject(dc, bmp)
    G.SetStretchBltMode(dc, G.HALFTONE)
    G.SetBrushOrgEx(dc, 0, 0, None)
    G.StretchBlt(dc, 0, 0, tw, th, src_dc, 0, 0, w, h, G.SRCCOPY)
    data = ctypes.string_at(bits, tw * th * 4)
    G.SelectObject(dc, old)
    G.DeleteObject(bmp)
    G.DeleteDC(dc)
    return tw, th, data


def screen_thumb(bounds) -> tuple[int, int, bytes] | None:
    cap = S.capture_screen(bounds)
    if cap is None:
        return None
    try:
        return _thumb(cap[0], bounds[2] - bounds[0], bounds[3] - bounds[1])
    finally:
        S.free_capture(cap)


def diff(a, b) -> float:
    """Mean difference per colour value (0-255) of two thumbnails."""
    if a is None or b is None or a[:2] != b[:2]:
        return 255.0
    x, y = a[2], b[2]
    n = len(x) // 4
    return sum(abs(x[i] - y[i]) for i in range(len(x)) if i % 4 != 3) / (3 * n)


class FrameRecorder:
    """Records every frame the slide layer puts on screen, as thumbnails. Screen captures can't see
    the layer (it is excluded from capture on purpose), so after each frame (`_presented`, called in
    GPU and CPU mode alike) the layer's own CPU drawing of that frame is made into a bitmap. On the
    GPU the pictures sit at the same positions (checked on screen by the GPU pixel test)."""

    def __init__(self):
        self.frames: list[tuple[int, int, tuple[int, int, bytes]]] = []  # (image_x, new_x, thumb)
        self.gpu: list[bool] = []  # per frame: drawn by the GPU
        self._hook = S._Overlay._presented

    def __enter__(self):
        real, frames, on_gpu = self._hook, self.frames, self.gpu

        def presented(ov):
            real(ov)
            screen = G.GetDC(0)
            dc = G.CreateCompatibleDC(screen)
            bmp = G.CreateCompatibleBitmap(screen, ov._w, ov._h)
            G.ReleaseDC(0, screen)
            old = G.SelectObject(dc, bmp)
            ov.paint(dc)
            frames.append((ov._image_x, ov._new_x, _thumb(dc, ov._w, ov._h)))
            on_gpu.append(ov.uses_gpu)
            G.SelectObject(dc, old)
            G.DeleteObject(bmp)
            G.DeleteDC(dc)

        S._Overlay._presented = presented
        return self

    def __exit__(self, *exc):
        S._Overlay._presented = self._hook


def write_sheet(path: str, rows: list[list[tuple[int, int, bytes]]], cols: int = 8) -> None:
    """Saves thumbnails as one PNG: each animation starts a new line, frames left to right."""
    import struct
    import zlib
    tw, th = rows[0][0][0], rows[0][0][1]
    pad = 4
    lines = [r[i:i + cols] for r in rows for i in range(0, len(r), cols)]
    W, H = cols * (tw + pad) + pad, len(lines) * (th + pad) + pad
    canvas = bytearray(b"\x40" * (W * H * 3))
    for li, line in enumerate(lines):
        for ci, (w, h, data) in enumerate(line):
            ox, oy = pad + ci * (tw + pad), pad + li * (th + pad)
            for y in range(h):
                src = data[y * w * 4:(y + 1) * w * 4]
                o = ((oy + y) * W + ox) * 3
                canvas[o:o + w * 3:3], canvas[o + 1:o + w * 3:3], canvas[o + 2:o + w * 3:3] = \
                    src[2::4], src[1::4], src[0::4]
    raw = b"".join(b"\x00" + bytes(canvas[y * W * 3:(y + 1) * W * 3]) for y in range(H))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def gpu_checks(mgr, anim, dev, primary, record, other) -> None:
    """GPU slide: real pixels on screen, a GPU failure in the middle of a slide, CPU cost per frame,
    another monitor, rapid switching and memory."""
    from src import gpu as GPU
    get_px = ctypes.WinDLL("gdi32")["GetPixel"]
    get_px.restype, get_px.argtypes = ctypes.c_uint32, [ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int]
    dwm_flush = N._f(N.dwmapi, "DwmFlush", ctypes.c_long)

    # 1) On screen: GPU pictures on a visible test window (NOT excluded from capture, so the screen
    #    shows them). Window content black (as the slide layer's), picture A red with a green 10 px
    #    left edge, picture B blue: placed like the strip and the bounce, then real pixels are read.
    class Black(G.Window):
        def on_message(self, msg, wp, lp):
            if msg == G.WM_PAINT:
                ps = G.PAINTSTRUCT()
                hdc = G.BeginPaint(self.hwnd, ctypes.byref(ps))
                G.fill_rect(hdc, (0, 0, 2000, 2000), G.rgb(0, 0, 0))
                G.EndPaint(self.hwnd, ctypes.byref(ps))
                return 0
            return 1 if msg == G.WM_ERASEBKGND else None

    W, H = 400, 160
    l, t = primary.work[0] + 40, primary.work[1] + 40
    win = Black()
    win.create("DisplayDesktop.GpuPixels", N.WS_EX_TOPMOST | N.WS_EX_TOOLWINDOW | N.WS_EX_NOACTIVATE, N.WS_POPUP,
               l, t, W, H)

    def pic(color, edge=None):
        dc = G.CreateCompatibleDC(0)
        bmp, _ = G.create_dib(W, H)
        old = G.SelectObject(dc, bmp)
        G.fill_rect(dc, (0, 0, W, H), color)
        if edge is not None:
            G.fill_rect(dc, (0, 0, 10, H), edge)
        return dc, bmp, old

    a, bpic = pic(G.rgb(255, 0, 0), G.rgb(0, 255, 0)), pic(G.rgb(0, 0, 255))
    names = {0x0000FF: "red", 0xFF0000: "blue", 0x00FF00: "green", 0x000000: "black"}
    layer = None
    try:
        layer = GPU.Layer(win.hwnd)
        layer.add(a[0], W, H)
        layer.add(bpic[0], W, H, W)
        layer.commit(wait=True)
        G.SetWindowPos(win.hwnd, N.HWND_TOPMOST, l, t, W, H, N.SWP_NOACTIVATE | N.SWP_SHOWWINDOW)
        G.UpdateWindow(win.hwnd)

        def screen_at(xs, layer=layer):
            layer.commit(wait=True)
            dwm_flush()
            dwm_flush()
            time.sleep(0.05)
            scr = G.GetDC(0)
            got = [names.get(get_px(scr, l + x, t + H // 2), hex(get_px(scr, l + x, t + H // 2))) for x in xs]
            G.ReleaseDC(0, scr)
            return got

        xs = (5, 50, 150, 250, 350, 395)

        def expected(ax, bx):
            out = []
            for x in xs:
                if bx <= x < bx + W:
                    out.append("blue")
                elif ax <= x < ax + W:
                    out.append("green" if x - ax < 10 else "red")
                else:
                    out.append("black")
            return out

        ok = True
        for off in (0, 100, 200, 300, 400):  # the strip: A slides out left, B follows right behind
            ax, bx = S.strip_positions(off, W, +1)
            layer.set_offsets([ax, bx])
            got = screen_at(xs)
            ok &= got == expected(ax, bx)
            if got != expected(ax, bx):
                print("   strip", off, "got", got, "want", expected(ax, bx))
        check(ok, "GPU strip on screen: both pictures exactly where the frame puts them (5 positions)")
        ok = True
        for bx_off in (60, -40, 0):  # the bounce: one picture pushed, black gap behind it
            layer.set_offsets([bx_off, 10_000])
            got = screen_at(xs)
            ok &= got == expected(bx_off, 10_000)
            if got != expected(bx_off, 10_000):
                print("   bounce", bx_off, "got", got, "want", expected(bx_off, 10_000))
        check(ok, "GPU bounce on screen: picture pushed both ways, black gap behind it")
    except OSError as exc:
        check(False, f"GPU pixel test could not run: {exc}")
    finally:
        if layer is not None:
            layer.close()
        win.destroy()
        for dc, bmp, old in (a, bpic):
            G.SelectObject(dc, old)
            G.DeleteObject(bmp)
            G.DeleteDC(dc)

    # 1b) The monitor's own GPU (no choice): its slides run there, the layer on that GPU's devices
    g = GPU.gpu_of(dev)
    check(g is not None and g in GPU.adapters(), f"the monitor's GPU is found ({g.name if g else None})")
    used = []
    real_layer_init = GPU.Layer.__init__

    def note(self, hwnd, devices=None):
        real_layer_init(self, hwnd, devices)
        used.append(self.device.gpu)

    with mock.patch.object(GPU.Layer, "__init__", note):
        for target, sign in ((1, -1), (0, 1)):
            done, before, after, f, on_gpu = record(lambda: mgr.switch_to(dev, target))
            w_ = primary.bounds[2] - primary.bounds[0]
            check(done and len(f) >= 10 and all(on_gpu) and used and used[-1] == g and f[-1][0] == sign * w_
                  and diff(f[-1][2], after) < 3,
                  f"real slide {'there' if target else 'and back'} on the monitor's GPU ({len(f)} frames)")

    # 2) The GPU fails in the middle of a slide: the slide finishes on the CPU, correctly
    calls = {"n": 0}
    real_set = GPU.Layer.set_offsets

    def failing(self, xs):
        calls["n"] += 1
        if calls["n"] > 3:
            raise OSError("simulated device loss")
        real_set(self, xs)

    # (with screenshots, the GPU capture off: the pictures are on the CPU too, the slide goes on there)
    with mock.patch.object(GPU.Layer, "set_offsets", failing), mock.patch.object(GPU, "capture_for", return_value=None):
        done, before, after, f, on_gpu = record(lambda: mgr.switch_to(dev, 1))
    w = primary.bounds[2] - primary.bounds[0]
    check(done and on_gpu and on_gpu[0] and not on_gpu[-1] and len(f) >= 8,  # partly CPU: may drop a frame
          f"GPU lost mid-slide: it continues on the CPU ({sum(on_gpu)} GPU frames, {len(on_gpu) - sum(on_gpu)} CPU)")
    check(f and f[-1][0] == -w and f[-1][1] == 0 and diff(f[-1][2], after) < 3,
          "and still ends exactly on the new desktop")
    check(GPU.available(), "the GPU is used again for the next animation")
    pump_until(lambda: anim.active_count == 0, 2)
    # (with the GPU capture: its pictures are on the GPU only, so the slide just ends; the switch is done)
    calls["n"] = 0
    GPU.capture_for(dev)  # (open, as the app's sync timer keeps it)
    with mock.patch.object(GPU.Layer, "set_offsets", failing):
        done, before, after, f, on_gpu = record(lambda: mgr.switch_to(dev, 0))
    pump(150)
    ended = not find_visible("DisplayDesktop.Slide") and anim.active_count == 0
    shown = diff(screen_thumb(primary.bounds), after)
    check(done and ended and mgr.current_of(dev) == 0 and 0 < len(f) <= 5 and shown < 3,
          f"GPU lost mid-slide (GPU capture): the slide ends at once on the new desktop ({len(f)} frames, "
          f"ended {ended}, screen diff {shown:.1f})")
    mgr.switch_to(dev, 1)  # (back where the checks below start)
    pump_until(lambda: anim.active_count == 0, 2)
    check(GPU.capture_for(dev) is not None, "the GPU capture is used again for the next slide")
    # (the GPU capture taken away just as the slide starts: a display change, DXGI_ERROR_ACCESS_LOST)
    real_grab = GPU.Capture.grab
    lost = {"slide": False}

    def lost_grab(self, wait_ms=0):
        if lost["slide"]:
            raise OSError("screen capture failed (0x887A0026)")
        return real_grab(self, wait_ms)
    real_out = S._Overlay.slide_out

    def out_then_lose(ov):
        real_out(ov)
        lost["slide"] = True  # (after the switch: every grab of the slide fails)
    with mock.patch.object(GPU.Capture, "grab", lost_grab), mock.patch.object(S._Overlay, "slide_out", out_then_lose):
        done, before, after, f, on_gpu = record(lambda: mgr.switch_to(dev, 0))
    ov_lost = f
    check(done and len(ov_lost) >= 10 and ov_lost[-1][0] == w and diff(ov_lost[-1][2], after) < 3,
          f"GPU capture lost as the slide starts: it still slides, onto the new desktop ({len(ov_lost)} frames)")
    mgr.switch_to(dev, 1)
    pump_until(lambda: anim.active_count == 0, 2)
    check(GPU.capture_for(dev) is not None, "and the GPU capture is opened again afterwards")

    # 3) CPU time per frame (the reason for the GPU): GPU moves pictures, the CPU copies them
    def cost(enabled: bool, target: int) -> list[float]:
        spent = []
        real_show = S._Overlay._show_frame

        def timed(self):
            t0 = time.perf_counter()
            real_show(self)
            spent.append((time.perf_counter() - t0) * 1000)

        with mock.patch.object(GPU, "ENABLED", enabled), mock.patch.object(S._Overlay, "_show_frame", timed):
            pump(250)
            mgr.switch_to(dev, target)
            pump_until(lambda: anim.active_count == 0 and not find_visible("DisplayDesktop.Slide"), 3)
        return sorted(spent)

    gpu_ms = cost(True, 0)
    cpu_ms = cost(False, 1)
    cost(True, 0)  # back to desktop 1
    med = lambda v: v[len(v) // 2] if v else 0.0  # noqa: E731
    check(gpu_ms and cpu_ms and med(gpu_ms) < med(cpu_ms) / 3,
          f"CPU time per frame: GPU {med(gpu_ms):.2f} ms vs CPU {med(cpu_ms):.2f} ms (median)")

    # 4) Another monitor (its own size and DPI): a bounce there runs on the GPU too
    if other is not None:
        on_gpu, xs, covered = [], [], []
        real_hook = S._Overlay._presented

        def watch(self):
            real_hook(self)
            on_gpu.append(self.uses_gpu)
            xs.append(self._image_x)
            covered.append(N.get_window_rect(self.hwnd) == other.bounds)

        with mock.patch.object(S._Overlay, "_presented", watch):
            anim.bounce(other.device, +1)
            pump_until(lambda: anim.active_count == 0, 3)
        check(len(on_gpu) >= 10 and all(on_gpu) and all(covered) and xs[-1] == 0 and min(xs) < 0,
              f"bounce on the other monitor {other.bounds}: GPU, covers that monitor, ends in place")
    else:
        check(True, "one monitor: other-monitor GPU check skipped")

    # 4b) GPU capture: a window that paints only after the slide started (shown for the first time) is
    #     in the sliding strip while it slides: the new desktop is live, nothing pops in at the end
    class Late(G.Window):
        def on_message(self, msg, wp, lp):
            if msg == G.WM_ERASEBKGND:
                G.fill_rect(wp, (0, 0, 4000, 4000), G.rgb(255, 0, 0))
                return 1
            return None

    pl, pt, pr, pb = primary.bounds
    lx, ly = (pl + pr) // 2 - 100, (pt + pb) // 2 - 100
    late = Late()
    late.create("DisplayDesktop.LateWindow", N.WS_EX_TOPMOST | N.WS_EX_TOOLWINDOW | N.WS_EX_NOACTIVATE,
                N.WS_POPUP, lx, ly, 200, 200)
    seen = {"shown_at": None, "red_at": None}
    real_live = S._Overlay._live_frame

    def watch_live(self, now):
        real_live(self, now)
        if not self._live_strip or not self.hwnd:
            return
        n = len(self.frames)
        if seen["shown_at"] is None and n >= 2:
            G.ShowWindow(late.hwnd, N.SW_SHOWNA)  # (paints after the strip already slides)
            seen["shown_at"] = n
        elif seen["shown_at"] is not None and seen["red_at"] is None and n % 2 == 0:
            bm = self._live.to_bitmap(self._live.texture)
            try:
                if get_px(bm[0], lx + 100 - pl, ly + 100 - pt) & 0xFFFFFF == 0x0000FF:
                    seen["red_at"] = n
            finally:
                S.free_capture(bm)

    live_now = GPU.capture_for(dev) is not None
    if live_now:
        target = 1 - mgr.current_of(dev)
        with mock.patch.object(S._Overlay, "_live_frame", watch_live):
            pump(200)
            mgr.switch_to(dev, target)
            pump_until(lambda: anim.active_count == 0, 3)
        check(seen["shown_at"] is not None and seen["red_at"] is not None,
              f"a window painting after the slide started is in the sliding new desktop (shown at frame "
              f"{seen['shown_at']}, in the strip by frame {seen['red_at']})")
        mgr.switch_to(dev, 1 - target)
        pump_until(lambda: anim.active_count == 0, 3)
    else:
        check(True, "no GPU capture on this monitor: the live-strip check is skipped")
    late.destroy()

    # 5) Rapid switching: each switch replaces the running animation (no waiting), 30 times
    class PMC(ctypes.Structure):
        _fields_ = [("cb", N.DWORD), ("faults", N.DWORD)] + [(n, ctypes.c_size_t) for n in (
            "peak_ws", "ws", "qpp", "qp", "qnpp", "qnp", "pagefile", "peak_pagefile", "private")]

    mem_info = N._f(N.kernel32, "K32GetProcessMemoryInfo", N.BOOL, N.H, ctypes.POINTER(PMC), N.DWORD)
    handles = N._f(N.kernel32, "GetProcessHandleCount", N.BOOL, N.H, ctypes.POINTER(N.DWORD))
    me = N._f(N.kernel32, "GetCurrentProcess", N.H)()

    def private_mb():
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        mem_info(me, ctypes.byref(pmc), pmc.cb)
        return pmc.private / 2 ** 20

    def handle_count():
        n = N.DWORD()
        handles(me, ctypes.byref(n))
        return n.value

    made, freed, errors_seen = [], [], []
    real_init, real_close = GPU.Layer.__init__, GPU.Layer.close

    def counting_init(self, hwnd, devices=None):
        real_init(self, hwnd, devices)
        made.append(1)

    def counting_close(self):
        if self._target:
            freed.append(1)
        real_close(self)

    pump(300)
    mem0, h0 = private_mb(), handle_count()
    with mock.patch.multiple(GPU.Layer, __init__=counting_init, close=counting_close):
        for i in range(30):
            try:
                mgr.switch_to(dev, 1 if i % 2 == 0 else 0)
            except Exception as exc:  # noqa: BLE001
                errors_seen.append(exc)
            pump(40 if i % 3 else 120)  # sometimes mid-slide, sometimes after the new desktop capture
        pump_until(lambda: anim.active_count == 0 and not find_visible("DisplayDesktop.Slide"), 3)
    pump(300)
    mem1, h1 = private_mb(), handle_count()
    check(not errors_seen and anim.active_count == 0 and not find_visible("DisplayDesktop.Slide"),
          "30 rapid switches (each cutting the last animation short): no errors, no layer left on screen")
    check(len(made) >= 30 and len(made) == len(freed),
          f"every GPU layer of the cut-short animations was released ({len(made)} made, {len(freed)} released)")
    check(mem1 - mem0 < 64 and h1 - h0 < 40,
          f"memory and handles stay flat (private {mem0:.0f} -> {mem1:.0f} MB, handles {h0} -> {h1})")
    if mgr.current_of(dev) != 0:
        mgr.switch_to(dev, 0)
        pump_until(lambda: anim.active_count == 0, 3)

def gui_objects() -> tuple[int, int]:
    p = _GetCurrentProcess()
    return _GetGuiResources(p, 0), _GetGuiResources(p, 1)  # GDI, USER


def main() -> int:
    frames_dir = sys.argv[sys.argv.index("--frames") + 1] if "--frames" in sys.argv[:-1] else None
    N.enable_per_monitor_dpi()
    screens = N.screens()
    primary = next(s for s in screens if s.primary)
    other = next((s for s in screens if not s.primary), None)
    dev = primary.device
    tmp = tempfile.mkdtemp(prefix="ws-ui-")
    helper = None
    mgr = None
    others_before = {h for h in N.enum_windows() if N.is_window_visible(h)}
    try:
        helper = Helper(tmp, "ui", primary.work, ["WS UI One", "WS UI Two", "WS UI Three"])
        one, two, three = (helper.windows[t] for t in ("WS UI One", "WS UI Two", "WS UI Three"))
        mgr = DesktopManager(win=TestWin({helper.pid}), directory=tmp)
        anim = SlideAnimator()
        mgr.switch_starting.append(lambda d, o, n: None if Overview.is_open() else anim.begin(d, 1 if n > o else -1))
        mgr.desktop_switched.append(lambda i: anim.commit(i.device))

        # --- slide animation through a real switch
        mgr.sync()
        force_foreground(three)
        # The pixel checks below sample window One: it must be on top of the user's own windows even
        # when Windows refuses the focus change above (topmost without activation; undone after)
        for h in (one, two, three):
            G.SetWindowPos(h, N.HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE
        time.sleep(0.1)
        # A point covered by window One only (the test windows cascade): One on the old desktop,
        # whatever is under it on the new one. Only that one pixel of each capture is kept.
        r1, r2 = N.get_window_rect(one), N.get_window_rect(two)
        px, py = (r1[0] + r2[0]) // 2, (r1[1] + r2[1]) // 2
        bl, bt = primary.bounds[0], primary.bounds[1]
        get_pixel = ctypes.WinDLL("gdi32").GetPixel
        get_pixel.restype, get_pixel.argtypes = ctypes.c_uint32, [ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int]
        samples = []  # [the old desktop's picture, the new one's] at that point (GPU pictures read back)
        real_capture = S.capture_screen
        real_presented = S._Overlay._presented

        def picture_px(ov, new: bool):
            if ov._live is not None:
                bm = ov._live.to_bitmap(ov._live.texture if new else ov._old_tex)
                try:
                    return get_pixel(bm[0], px - bl, py - bt)
                finally:
                    S.free_capture(bm)
            src = ov._new[0] if new else ov._dc
            return get_pixel(src, px - bl, py - bt) if src else None

        def spy(ov):
            real_presented(ov)
            if ov._bounds != primary.bounds:
                return
            if not samples:
                samples.append(picture_px(ov, False))
            elif ov.was_strip:
                samples[1:] = [picture_px(ov, True)]  # (the latest: a live picture is refreshed)

        S._Overlay._presented = spy
        move_window(mgr, mgr._win, three, +1)  # Three alone on desktop 2 (shown)
        overlay = find_visible("DisplayDesktop.Slide")
        if not check(mgr.desktops_of(dev)[1:2] == [[three]] and mgr.current_of(dev) == 1,
                     "set-up: Three alone on a second desktop, shown"):
            raise RuntimeError("set-up failed: the checks below need a second desktop")
        check(overlay != 0, "slide overlay shown during the switch")
        if overlay:
            r = N.get_window_rect(overlay)
            check(r == primary.bounds, f"overlay covers exactly the monitor {primary.bounds} (got {r})")
            ex = N.get_exstyle(overlay)
            check(ex & N.WS_EX_TRANSPARENT and ex & N.WS_EX_LAYERED and ex & N.WS_EX_NOACTIVATE,
                  "overlay is click-through and never takes focus")
        ov_slide = anim.overlay(dev)
        pump_until(lambda: ov_slide is not None and ov_slide.pacing != "timer", 0.3)
        pacing = ov_slide.pacing if ov_slide is not None else "?"
        check(pacing in ("vblank", "dwm"), f"frames paced by the monitor's refresh (got {pacing})")
        check(pump_until(lambda: anim.active_count == 0 and not find_visible("DisplayDesktop.Slide"), 2),
              "slide finishes and the overlay is destroyed")
        S._Overlay._presented = real_presented
        pump(100)
        after = real_capture(primary.bounds)
        now_px = get_pixel(after[0], px - bl, py - bt) if after else None
        if after:
            S.free_capture(after)
        check(ov_slide is not None and ov_slide.was_strip,
              "comic strip: both desktops slid together (the new one was captured)")
        check(len(samples) == 2 and samples[0] != samples[1],
              "the second picture shows the new desktop, not the old one")
        check(len(samples) == 2 and samples[1] == now_px,
              "and it matches the real desktop afterwards (the layer was left out of the capture)"
              f" [old {samples[0] if samples else 0:#08x}, new {samples[1] if len(samples) > 1 else 0:#08x},"
              f" real {now_px or 0:#08x}]")
        if ov_slide is not None:
            f = ov_slide.frames
            check(len(f) >= 10, f"enough frames for a smooth slide ({len(f)})")
        mgr.switch_to(dev, 0)
        pump_until(lambda: anim.active_count == 0, 2)
        check(mgr.snapshots.get(three) is not None, "snapshot of the hidden window exists for the overview")
        for h in (one, two, three):
            G.SetWindowPos(h, N.HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

        # failsafe: a begun animation that never commits removes itself
        anim.begin(dev, +1)
        check(anim.active_count == 1, "overlay begun without a commit")
        check(pump_until(lambda: anim.active_count == 0 and not find_visible("DisplayDesktop.Slide"), 3.5),
              "failsafe removes an uncommitted overlay")

        # key -> motion: the slide moves right after the switch. Previews are taken in the background
        # between switches (sync), so no switch waits on a capture; nothing waits for the new desktop
        from src import gpu as GPU
        if GPU.available(dev):
            delays, captured = [], []
            real_capture_now = mgr.snapshots.capture
            mgr.snapshots.capture = lambda h: captured.append(h) or real_capture_now(h)
            real_presented = S._Overlay._presented

            def first_move(ov):
                real_presented(ov)
                if ov._image_x and not getattr(ov, "_moved", False):
                    ov._moved = True
                    delays.append((time.perf_counter() - t0) * 1000)
            S._Overlay._presented = first_move
            try:
                for target in (1, 0, 1, 0, 1, 0):
                    mgr.sync()                  # (the sync timer's background previews)
                    GPU.warm_due(idle=True)     # (and its reopening of a GPU capture)
                    pump(400)
                    t0 = time.perf_counter()
                    mgr.switch_to(dev, target)
                    pump_until(lambda: anim.active_count == 0, 2)
            finally:
                S._Overlay._presented = real_presented
                mgr.snapshots.capture = real_capture_now
            delays.sort()
            check(len(delays) == 6 and delays[2] < KEY_TO_MOTION_MS and not captured,  # (whole refreshes)
                  f"the slide moves {', '.join(f'{d:.0f}' for d in delays)} ms after the switch "
                  f"(median < {KEY_TO_MOTION_MS} ms), no preview captured during a switch ({len(captured)})")

        # --- animation frames: every frame the layer draws, checked against real screenshots
        w = primary.bounds[2] - primary.bounds[0]
        sheet = []

        def record(start):
            pump(250)
            before = screen_thumb(primary.bounds)
            with FrameRecorder() as rec:
                start()
                done = pump_until(lambda: anim.active_count == 0 and not find_visible("DisplayDesktop.Slide"), 3)
            pump(150)
            return done, before, screen_thumb(primary.bounds), rec.frames, rec.gpu

        from src import gpu as GPU
        has_gpu = GPU.available()
        check(True, f"GPU (DirectComposition) available: {has_gpu}")
        for mode, enabled in (("GPU", True), ("CPU", False)):
            if mode == "GPU" and not has_gpu:
                continue
            with mock.patch.object(GPU, "ENABLED", enabled):
                for name, target, sign in (("forward slide", 1, -1), ("back slide", 0, 1)):
                    name = f"{mode} {name}"
                    done, before, after, f, on_gpu = record(lambda: mgr.switch_to(dev, target))
                    xs = [x for x, _, _ in f]
                    least = 10 if enabled else 8  # the CPU drawing may drop a frame on a large monitor
                    check(done and len(f) >= least, f"[{name}] frames recorded ({len(f)})")
                    if len(f) < 3:
                        continue
                    check(all(on_gpu) if enabled else not any(on_gpu),
                          f"[{name}] every frame drawn by the {mode}")
                    sheet.append([t for _, _, t in f])
                    check(diff(f[0][2], before) < 3, f"[{name}] first frame = the screen before ({diff(f[0][2], before):.1f})")
                    check(diff(f[-1][2], after) < 3, f"[{name}] last frame = the screen after ({diff(f[-1][2], after):.1f})")
                    check(diff(before, after) > 3, f"[{name}] the two desktops look different ({diff(before, after):.1f})")
                    check(all(sign * (b - a) >= 0 for a, b in zip(xs, xs[1:])) and xs[-1] == sign * w,
                          f"[{name}] old desktop moves one way only, all the way out ({xs[0]} -> {xs[-1]})")
                    check(f[-1][1] == 0, f"[{name}] new desktop ends exactly in place")
                    mid = f[len(f) // 2][2]
                    check(diff(mid, before) > 1 and diff(mid, after) > 1, f"[{name}] middle frame shows both desktops moving")

                # bounce at the first desktop (going left)
                done, before, after, f, on_gpu = record(lambda: anim.bounce(dev, -1))
                xs = [x for x, _, _ in f]
                amp = max(S.BOUNCE_MIN_PX, int(w * S.BOUNCE_FRACTION))
                check(done and len(f) >= (10 if enabled else 8), f"[{mode} bounce] frames recorded ({len(f)})")
                if len(f) >= 3:
                    check(all(on_gpu) if enabled else not any(on_gpu), f"[{mode} bounce] every frame drawn by the {mode}")
                    sheet.append([t for _, _, t in f])
                    check(0 < max(xs) <= amp and xs[-1] == 0 and min(xs) < 0,
                          f"[{mode} bounce] pushed right {max(xs)} px (max {amp}), springs back past 0 ({min(xs)}), ends at 0")
                    check(diff(f[0][2], before) < 3 and diff(f[-1][2], before) < 3 and diff(after, before) < 3,
                          f"[{mode} bounce] starts and ends on the unchanged desktop")
                    peak = f[xs.index(max(xs))][2]
                    gap = max(peak[2][(y * peak[0]) * 4 + c] for y in range(peak[1]) for c in range(3))
                    check(gap < 16, f"[{mode} bounce] the gap behind the pushed desktop is dark (max {gap})")

                # bounce at the last desktop (going right): the mirror image
                done, before, after, f, on_gpu = record(lambda: anim.bounce(dev, +1))
                xs = [x for x, _, _ in f]
                check(done and len(f) >= (10 if enabled else 8), f"[{mode} right bounce] frames recorded ({len(f)})")
                if len(f) >= 3:
                    check(all(on_gpu) if enabled else not any(on_gpu),
                          f"[{mode} right bounce] every frame drawn by the {mode}")
                    sheet.append([t for _, _, t in f])
                    check(-amp <= min(xs) < 0 and xs[-1] == 0 and max(xs) > 0,
                          f"[{mode} right bounce] pushed left {-min(xs)} px (max {amp}), springs back past 0 "
                          f"({max(xs)}), ends at 0")
                    check(diff(f[0][2], before) < 3 and diff(f[-1][2], before) < 3 and diff(after, before) < 3,
                          f"[{mode} right bounce] starts and ends on the unchanged desktop")
                    peak = f[xs.index(min(xs))][2]
                    gap = max(peak[2][(y * peak[0] + peak[0] - 1) * 4 + c] for y in range(peak[1]) for c in range(3))
                    check(gap < 16, f"[{mode} right bounce] the gap behind the pushed desktop is dark (max {gap})")

                # the shortcut keys' way (Ctrl+Alt+Left/Right = switch_relative): left and right switch with
                # a slide; at the first and the last desktop the edge is reported, the tray bounces there
                edges: list[tuple[str, int]] = []
                mgr.edge_reached.append(lambda d, way: (edges.append((d, way)), anim.bounce(d, way)))
                try:
                    l, t, r, b = primary.bounds  # (the keys act on the monitor under the mouse)
                    ctypes.windll.user32.SetCursorPos((l + r) // 2, (t + b) // 2)
                    count = len(mgr.desktops_of(dev))
                    mgr.switch_to(dev, 0)
                    pump_until(lambda: anim.active_count == 0, 3)
                    done, before, after, f, _ = record(lambda: mgr.switch_relative(+1))
                    check(done and mgr.current_of(dev) == 1 and len(f) >= 3 and f[-1][0] < 0,
                          f"[{mode} right key] desktop 2 slides in from the right ({len(f)} frames)")
                    done, before, after, f, _ = record(lambda: mgr.switch_relative(-1))
                    check(done and mgr.current_of(dev) == 0 and len(f) >= 3 and f[-1][0] > 0,
                          f"[{mode} left key] desktop 1 slides in from the left ({len(f)} frames)")
                    edges.clear()
                    done, before, after, f, _ = record(lambda: mgr.switch_relative(-1))
                    check(done and edges == [(dev, -1)] and mgr.current_of(dev) == 0 and len(f) >= 3
                          and max(x for x, _, _ in f) > 0,
                          f"[{mode} left key at desktop 1] no switch: the edge is reported and it bounces ({edges})")
                    mgr.switch_to(dev, count - 1)
                    pump_until(lambda: anim.active_count == 0, 3)
                    edges.clear()
                    done, before, after, f, _ = record(lambda: mgr.switch_relative(+1))
                    check(done and edges == [(dev, +1)] and mgr.current_of(dev) == count - 1 and len(f) >= 3
                          and min(x for x, _, _ in f) < 0,
                          f"[{mode} right key at the last desktop] no switch: the edge is reported and it bounces "
                          f"({edges})")
                    mgr.switch_to(dev, 0)
                    pump_until(lambda: anim.active_count == 0, 3)
                finally:
                    mgr.edge_reached.pop()

        if has_gpu:
            gpu_checks(mgr, anim, dev, primary, record, other)
        if frames_dir and sheet:
            os.makedirs(frames_dir, exist_ok=True)
            out = os.path.join(frames_dir, "animation-frames.png")
            write_sheet(out, sheet)
            print(f"frames saved: {out}")

        # --- opening the overview while a desktop is still to be shown (after start or "Load
        # Desktops"): building it syncs, which switches there; no slide may start under it
        pump_until(lambda: anim.active_count == 0, 3)
        cur = mgr.current_of(dev)
        target = next((i for i, s in enumerate(mgr.desktops_of(dev)) if i != cur and s), None)
        if check(target is not None, "a desktop with windows that is not on screen"):
            st = mgr._monitors[dev]
            st.pending_show, st.pending_until = target, mgr._win.tick_count64() + 10_000
            began = []
            with mock.patch.object(anim, "begin", side_effect=lambda *a: began.append(a)):
                Overview.toggle(mgr)
                pump(300)
            check(mgr.current_of(dev) == target and not began and anim.active_count == 0
                  and not find_visible("DisplayDesktop.Slide"),
                  "opening the overview switches to the waiting desktop without a slide under it")
            if Overview.is_open():
                Overview.current().close()
            pump(100)
            mgr.switch_to(dev, cur)
            pump_until(lambda: anim.active_count == 0, 3)

        # --- overview
        Overview.toggle(mgr)
        ov = Overview.current()
        pump(400)
        check(ov is not None and Overview.is_open(), "overview opens")
        pc = cards_of(ov, dev)
        check(len(pc) == 2 and pc[0].desk.is_current, "primary row: 2 desktop cards, the first active")
        check(len(ov.rows) == len(screens), f"one row per monitor ({len(screens)})")
        check(len(ov.adds) == len(screens), "a '+' card on every row")
        t1, _ = tile_of(ov, one)
        t3, c3 = tile_of(ov, three)
        check(t1 is not None and t1.thumb != 0, "visible window has a live DWM preview")
        check(t3 is not None and t3.thumb == 0 and c3 is pc[1], "hidden window shows on desktop 2 (snapshot, no live preview)")
        check(ov.live_thumb_count() == 2, "exactly 2 live previews")
        expected_tips = (len(ov.cards) + sum(1 for c in ov.cards if c.trash)
                         + sum(len(c.tiles) for c in ov.cards) + len(ov.adds))
        check(ov.tooltip_count() == expected_tips, f"tooltips registered ({ov.tooltip_count()}/{expected_tips})")

        # drag a tile onto another desktop card
        t2, _ = tile_of(ov, two)
        drag(ov, center(ov, t2.rect), center(ov, pc[1].area))
        check(two in mgr.desktops_of(dev)[1] and not N.is_window_visible(two),
              "drag a window onto desktop 2: moved there and hidden")
        check(Overview.is_open() and tile_of(ov, two)[1] is cards_of(ov, dev)[1],
              "overview stays open and shows the new layout")
        # and back: from the hidden desktop onto the shown one, where it is shown again
        t2, _ = tile_of(ov, two)
        drag(ov, center(ov, t2.rect), center(ov, cards_of(ov, dev)[0].area))
        check(two in mgr.desktops_of(dev)[0] and N.is_window_visible(two),
              "drag it back onto desktop 1 (shown): moved there and shown")
        t2, _ = tile_of(ov, two)
        drag(ov, center(ov, t2.rect), center(ov, cards_of(ov, dev)[1].area))
        check(two in mgr.desktops_of(dev)[1] and not N.is_window_visible(two),
              "and onto desktop 2 again: hidden again")

        # drop on nothing: no change
        t1, _ = tile_of(ov, one)
        drag(ov, center(ov, t1.rect), (2, 2))
        check(one in mgr.desktops_of(dev)[0], "drag dropped on empty space: nothing moves")

        # "+" creates a desktop and switches to it, overview stays open
        add = next(a for a in ov.adds if a.device == dev)
        click(ov, center(ov, add.rect))
        check(mgr.current_of(dev) == 2 and len(mgr.desktops_of(dev)) == 3, "'+' creates desktop 3 and switches to it")
        check(Overview.is_open(), "overview stays open after '+'")
        check(not N.is_window_visible(one), "windows of the previous desktop hidden")

        # Esc closes
        G.SendMessageW(ov.hwnd, G.WM_KEYDOWN, N.VK_ESCAPE, 0)
        pump(50)
        check(not Overview.is_open() and not N.is_window(ov.hwnd or 0), "Esc closes the overview")

        # click a card: switch and close
        Overview.toggle(mgr)
        ov = Overview.current()
        pump(200)
        click(ov, center(ov, cards_of(ov, dev)[0].area))
        check(mgr.current_of(dev) == 0 and N.is_window_visible(one) and not Overview.is_open(),
              "click a card: switches to it and closes")

        # drag a card title onto another card of the same monitor: the two desktops swap numbers
        Overview.toggle(mgr)
        ov = Overview.current()
        pump(200)
        before = mgr.desktops_of(dev)
        c0, c1 = cards_of(ov, dev)[0], cards_of(ov, dev)[1]
        gx, gy, gw, gh = c0.grip  # grab the header band past the title text (left of badge/trash)
        hx, hy, hw, hh = c0.header
        grab_x = hx + hw + 12
        right_items = [r[0] for r in (c0.badge, c0.trash) if r]
        check(grab_x < min(right_items or [gx + gw]), "the drag handle reaches past the title text")
        drag(ov, (grab_x, gy + gh // 2 - ov._scroll), center(ov, c1.area))
        check(mgr.desktops_of(dev) == [before[1], before[0]] + before[2:],
              "drag desktop 1 (by its header, past the title) onto desktop 2: they swap")
        check(mgr.current_of(dev) == 1 and N.is_window_visible(one),
              "the desktop on screen stays on screen, now as desktop 2")
        check(Overview.is_open() and one in [t.win.handle for t in cards_of(ov, dev)[1].tiles],
              "overview stays open and shows the new numbers")
        c1 = cards_of(ov, dev)[1]
        gx, gy, gw, gh = c1.grip
        drag(ov, (gx + gw // 2, gy + 3 - ov._scroll), center(ov, cards_of(ov, dev)[0].area))
        check(mgr.desktops_of(dev) == before and mgr.current_of(dev) == 0, "and swap back")

        # trash icon: the empty desktop 3 made by "+" is still there; clicking its trash deletes it
        cards = cards_of(ov, dev)
        check(len(cards) == 3 and not mgr.desktops_of(dev)[2], "the empty desktop from '+' is kept")
        check(all(c.trash for c in cards), "every desktop of a monitor with several has a trash icon")
        if other is not None:
            check(all(c.trash is None for c in cards_of(ov, other.device)),
                  "a monitor's only desktop has none")
        click(ov, center(ov, cards[2].trash))
        check(len(mgr.desktops_of(dev)) == 2 and mgr.desktops_of(dev) == before[:2],
              "click the trash icon: that desktop is deleted")
        check(Overview.is_open() and len(cards_of(ov, dev)) == 2, "overview stays open and shows it")
        check(all(N.is_window_visible(h) for h in mgr.desktops_of(dev)[0]) and
              not any(N.is_window_visible(h) for h in mgr.desktops_of(dev)[1]),
              "every window is still on a desktop, shown or hidden as before")
        G.SendMessageW(ov.hwnd, G.WM_KEYDOWN, N.VK_ESCAPE, 0)
        pump(50)

        # drag a card title onto another monitor row: the whole desktop moves there
        if other is not None:
            Overview.toggle(mgr)
            ov = Overview.current()
            pump(200)
            src = cards_of(ov, dev)[1]
            row = next(r for r in ov.rows if r.device == other.device)
            hx, hy, hw, hh = src.header
            rx, ry, rw, rh = row.rect
            drag(ov, (hx + 5, hy + hh // 2 - ov._scroll), (rx + rw // 2, ry + rh // 2 - ov._scroll))
            check(len(mgr.desktops_of(dev)) == 1, "desktop 2 left the primary monitor")
            moved = mgr.desktops_of(other.device)[-1]
            check(sorted(moved) == sorted([two, three]), f"and arrived on {other.device} with its windows")
            check(all(N.monitor_device_of_window(h) == other.device for h in (two, three)),
                  "its windows were repositioned onto that monitor")

            # click a tile on an inactive desktop: goes to it
            t3, _ = tile_of(ov, three)
            click(ov, center(ov, t3.rect))
            check(not Overview.is_open(), "click a window: overview closes")
            target = next(i for i, s in enumerate(mgr.desktops_of(other.device)) if three in s)
            check(N.is_window_visible(three) and mgr.current_of(other.device) == target,
                  "and its desktop is shown on that monitor")

        # right-click menu: move a window to global desktop 1 via the keyboard
        Overview.toggle(mgr)
        ov = Overview.current()
        pump(200)
        t1, _ = tile_of(ov, one)
        menu_seen = []

        def drive_menu():
            if wait_for(lambda: _FindWindowExW(0, 0, "#32768", None) != 0, 3):
                menu_seen.append(True)
                time.sleep(0.2)
                G.PostMessageW(ov.hwnd, G.WM_KEYDOWN, 0x28, 0)  # Down: first item
                time.sleep(0.1)
                G.PostMessageW(ov.hwnd, G.WM_KEYDOWN, 0x0D, 0)  # Enter

        th = threading.Thread(target=drive_menu)
        th.start()
        G.SendMessageW(ov.hwnd, G.WM_RBUTTONUP, 0, lparam(*center(ov, t1.rect)))
        th.join()
        pump(100)
        check(menu_seen, "right-click opens the move menu")
        first = mgr.get_layout()[0]
        check(one in [w.handle for w in first.desktops[0].windows],
              f"menu 'Move to: Monitor 1 • Desktop 1' moved it there ({first.device})")
        if Overview.is_open():
            Overview.current().close()

        # scrolling keeps live previews clipped to the window
        Overview.toggle(mgr)
        ov = Overview.current()
        pump(200)
        if ov._max_scroll() > 0:
            G.SendMessageW(ov.hwnd, G.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
            pump(50)
            check(ov._scroll > 0, f"mouse wheel scrolls (scroll {ov._scroll}/{ov._max_scroll()})")
            G.SendMessageW(ov.hwnd, G.WM_VSCROLL, G.SB_BOTTOM, 0)
            check(ov._scroll == ov._max_scroll(), "scrollbar: bottom")
            _, _, cw, ch = G.client_rect(ov.hwnd)
            ok = all(t.last is None or not t.last[0] or (t.last[1][1] >= 0 and t.last[1][3] <= ch)
                     for c in ov.cards for t in c.tiles if t.thumb)
            check(ok, "live previews stay inside the window when scrolled")
            G.SendMessageW(ov.hwnd, G.WM_VSCROLL, G.SB_TOP, 0)
            check(ov._scroll == 0, "scrollbar: top")
        else:
            check(True, "content fits without scrolling (scroll test skipped)")
        ov.close()
        pump(50)

        # --- Settings dialog: real window and controls; the options it changes are stand-ins
        from src import settings_window as SW
        is_enabled = N._f(N.user32, "IsWindowEnabled", N.BOOL, N.H)
        get_focus = N._f(N.user32, "GetFocus", N.H)
        BM_CLICK = 0x00F5
        calls, recovered, answer = [], [], {"startup": True, "menu": True, "console": True}

        def key(vk):
            G.PostMessageW(get_focus() or SW.SettingsWindow.current.hwnd, G.WM_KEYDOWN, vk, 0)
            pump(150)

        with mock.patch.object(SW.startup, "can_manage", return_value=True), \
                mock.patch.object(SW.startup, "is_enabled", return_value=False), \
                mock.patch.object(SW.settings, "get", return_value=True), \
                mock.patch.object(SW.startup, "set_enabled",
                                  side_effect=lambda on: calls.append(("startup", on)) or (on and answer["startup"])), \
                mock.patch.object(SW.shortcut, "set_enabled",
                                  side_effect=lambda on: calls.append(("menu", on)) or (on and answer["menu"])), \
                mock.patch.object(SW.console, "set_enabled",
                                  side_effect=lambda on: calls.append(("console", on)) or (on and answer["console"])), \
                mock.patch.object(SW.console, "is_open", return_value=False), \
                mock.patch.object(SW.G, "message_box") as box, \
                mock.patch.object(SW.settings, "set") as set_setting, \
                mock.patch.object(SW.gpu, "set_enabled",
                                  side_effect=lambda on: calls.append(("gpu", on)) or on), \
                mock.patch.object(SW.console, "log"):
            dlg = SW.SettingsWindow.show(on_recovery=lambda: recovered.append(1))
            pump(150)
            ctl = dlg._controls
            check(N.is_window_visible(dlg.hwnd) and N.get_class(dlg.hwnd) == "DisplayDesktop.Settings",
                  "Settings dialog opens")
            link = ctl[SW.ID_LINK]
            check(N.get_class(link) == "SysLink" and f"DisplayDesktop {SW.__version__}<" in N.get_title(link)
                  and SW.__version__.count(".") == 2 and SW.REPO_URL in N.get_title(link),
                  f"top line: the version as a link to GitHub ({N.get_title(link)})")
            with mock.patch.object(SW, "open_repo_page") as open_page:
                for code in (SW.NM_CLICK, SW.NM_RETURN):
                    hdr = SW._NMHDR(link, SW.ID_LINK, code)
                    G.SendMessageW(dlg.hwnd, SW.WM_NOTIFY, SW.ID_LINK, ctypes.addressof(hdr))
            check(open_page.call_count == 2, "click or Enter on the link opens the GitHub page")
            # Show Advanced (its setting reads True here): the box and its controls show; unchecking
            # hides them and shrinks the dialog, checking again brings them back
            adv = ctl[SW.ID_ADVANCED]
            tall = G.client_rect(dlg.hwnd)[3]
            check(dlg.checked(SW.ID_ADVANCED) and dlg._box is not None and N.is_window_visible(ctl[SW.ID_FOLDER]),
                  "Show Advanced on: the Advanced box is shown")
            G.SendMessageW(adv, BM_CLICK, 0, None)
            pump(50)
            short = G.client_rect(dlg.hwnd)[3]
            close_bottom = N.get_window_rect(ctl[SW.IDOK])[3]
            check(not dlg.checked(SW.ID_ADVANCED) and dlg._box is None and short < tall
                  and not any(N.is_window_visible(ctl[c]) for c in SW.IN_BOX)
                  and close_bottom <= N.get_window_rect(dlg.hwnd)[3]
                  and mock.call("ShowAdvanced", False) in set_setting.call_args_list,
                  "Show Advanced off: the box hides, the dialog shrinks (Close stays in it), remembered")
            G.SendMessageW(adv, BM_CLICK, 0, None)
            pump(50)
            check(dlg.checked(SW.ID_ADVANCED) and dlg._box is not None and G.client_rect(dlg.hwnd)[3] == tall
                  and all(N.is_window_visible(ctl[c]) for c in SW.IN_BOX),
                  "Show Advanced on again: the box is back")
            # Encrypt Settings: unchecked, the key as dots with the slashed eye; the eye shows it
            EM_GETPASSWORDCHAR = 0x00D2
            keybox, eye = ctl[SW.ID_KEY], ctl[SW.ID_EYE]
            face = SW.icon_face()
            from src import crypto
            asked = []

            def fake_encrypt(key, on, remember):  # (what the tray does, minus the files)
                asked.append((key, on))
                crypto.use(key if on else crypto.key(), on)
                return True
            dlg._on_encrypt = fake_encrypt
            dlg.update_key_controls()
            status, apply_key = ctl[SW.ID_CRYPT_STATUS], ctl[SW.ID_KEY_APPLY]
            check(not dlg.checked(SW.ID_ENCRYPT) and G.SendMessageW(keybox, EM_GETPASSWORDCHAR, 0, None) == SW.PASSWORD_CHAR
                  and N.get_title(eye) == SW.eye_text(False, face) and N.get_title(status) == "Status: Not encrypted",
                  "Encrypt Settings: off by default, 'Not encrypted' on its right, dots, the eye with a slash")
            check(not any(is_enabled(ctl[c]) for c in (SW.ID_KEY, SW.ID_EYE, SW.ID_KEY_APPLY, SW.ID_REMEMBER)),
                  "unchecked: the key, its eye, Apply and Remember Key are greyed")
            G.SendMessageW(ctl[SW.ID_ENCRYPT], BM_CLICK, 0, None)
            pump(50)
            check(dlg.checked(SW.ID_ENCRYPT) and is_enabled(keybox) and is_enabled(eye) and not is_enabled(apply_key)
                  and asked == [] and N.get_title(status) == "Status: Not encrypted",
                  "checked: the key can be typed (Apply waits for one); nothing encrypted yet")
            remembered = []
            dlg._on_remember = lambda on: remembered.append(on) or on
            dlg.update_key_controls()
            G.SendMessageW(ctl[SW.ID_REMEMBER], BM_CLICK, 0, None)
            pump(50)
            check(remembered == [False] and not dlg.checked(SW.ID_REMEMBER),
                  "unchecking Remember Key removes the key from the registry at once (even before Apply)")
            G.SendMessageW(ctl[SW.ID_REMEMBER], BM_CLICK, 0, None)
            pump(50)
            check(remembered == [False] and dlg.checked(SW.ID_REMEMBER),
                  "checking it again before Apply only chooses it (stored at Apply)")
            dlg._on_remember = None
            SW._SetWindowTextW(keybox, "my key")
            pump(50)
            G.SendMessageW(eye, BM_CLICK, 0, None)
            pump(50)
            check(G.SendMessageW(keybox, EM_GETPASSWORDCHAR, 0, None) == 0 and N.get_title(eye) == SW.eye_text(True, face)
                  and dlg.key_text() == "my key", "the eye shows the key (no slash)")
            G.SendMessageW(eye, BM_CLICK, 0, None)
            pump(50)
            check(G.SendMessageW(keybox, EM_GETPASSWORDCHAR, 0, None) == SW.PASSWORD_CHAR,
                  "and hides it again (dots, slashed eye)")
            check(is_enabled(apply_key), "a key typed: Apply can be used")
            G.SendMessageW(apply_key, BM_CLICK, 0, None)
            pump(50)
            check(asked == [("my key", True)] and N.get_title(status) == "Status: Encrypted" and not is_enabled(apply_key),
                  "Apply encrypts with the key; the status says Encrypted")
            G.SendMessageW(ctl[SW.ID_ENCRYPT], BM_CLICK, 0, None)
            pump(50)
            check(asked[-1] == (None, False) and not dlg.checked(SW.ID_ENCRYPT) and N.get_title(status) == "Status: Not encrypted"
                  and not is_enabled(keybox), "unchecking unencrypts at once; the status follows, the key is greyed")
            crypto.reset()
            dlg._on_encrypt = None
            dlg.update_key_controls()
            check(not dlg.checked(SW.ID_STARTUP) and dlg.checked(SW.ID_START_MENU),
                  "checkboxes show the current settings (startup off, Start menu on)")
            G.SendMessageW(ctl[SW.ID_STARTUP], BM_CLICK, 0, None)
            pump(50)
            check(calls[-1:] == [("startup", True)] and dlg.checked(SW.ID_STARTUP),
                  "click Start With Windows: turned on")
            answer["startup"] = False
            G.SendMessageW(ctl[SW.ID_STARTUP], BM_CLICK, 0, None)
            G.SendMessageW(ctl[SW.ID_STARTUP], BM_CLICK, 0, None)
            pump(50)
            check(calls[-1:] == [("startup", True)] and not dlg.checked(SW.ID_STARTUP),
                  "a change that fails shows the real state (unchecked)")
            G.SendMessageW(ctl[SW.ID_START_MENU], BM_CLICK, 0, None)
            pump(50)
            check(calls[-1:] == [("menu", False)] and not dlg.checked(SW.ID_START_MENU),
                  "click Add To Start Menu: turned off")
            G.SendMessageW(ctl[SW.ID_CONSOLE], BM_CLICK, 0, None)
            pump(50)
            check(calls[-1:] == [("console", True)] and dlg.checked(SW.ID_CONSOLE) and not box.called,
                  "click Show Console: turned on")
            G.SendMessageW(ctl[SW.ID_CONSOLE], BM_CLICK, 0, None)
            answer["console"] = False
            G.SendMessageW(ctl[SW.ID_CONSOLE], BM_CLICK, 0, None)
            pump(50)
            check(not dlg.checked(SW.ID_CONSOLE) and box.call_count == 1,
                  "Show Console that can't open: unchecked, with a warning")
            check(dlg.checked(SW.ID_GPU), "Use GPU Acceleration shows its setting (on)")
            G.SendMessageW(ctl[SW.ID_GPU], BM_CLICK, 0, None)
            pump(50)
            check(calls[-1:] == [("gpu", False)] and not dlg.checked(SW.ID_GPU)
                  and mock.call("UseGpu", False) in set_setting.call_args_list,
                  "click Use GPU Acceleration: off, remembered, animations switched to the CPU")
            G.SendMessageW(ctl[SW.ID_GPU], BM_CLICK, 0, None)
            pump(50)
            check(calls[-1:] == [("gpu", True)] and dlg.checked(SW.ID_GPU), "and back on")
            rows = [N.get_window_rect(ctl[c]) for c in (SW.ID_STARTUP, SW.ID_START_MENU, SW.ID_GPU, SW.ID_RESTORE_APPS)]
            check(N.get_title(ctl[SW.ID_RESTORE_APPS]) == "Restore Applications On Restart"
                  and all(r[0] == rows[0][0] for r in rows) and all(a[3] <= b[1] for a, b in zip(rows, rows[1:]))
                  and rows[-1][3] <= N.get_window_rect(ctl[SW.ID_ADVANCED])[1],
                  f"Restore Applications On Restart: the 4th option row, under Use GPU Acceleration ({rows})")
            check(dlg.checked(SW.ID_RESTORE_APPS), "Restore Applications On Restart shows its setting (on)")
            set_setting.reset_mock()
            G.SendMessageW(ctl[SW.ID_RESTORE_APPS], BM_CLICK, 0, None)
            pump(50)
            check(not dlg.checked(SW.ID_RESTORE_APPS) and set_setting.call_args_list == [mock.call("RestoreApps", False)],
                  "click Restore Applications On Restart: off, remembered")
            G.SendMessageW(ctl[SW.ID_RESTORE_APPS], BM_CLICK, 0, None)
            pump(50)
            check(dlg.checked(SW.ID_RESTORE_APPS) and set_setting.call_args_list[-1] == mock.call("RestoreApps", True),
                  "and back on")
            check(not hasattr(SW, "ID_GPU_LIST") and not any(
                      N.get_class(h) == "ComboBox" for h in ctl.values() if h),
                  "no GPU selector (each monitor uses its own GPU)")
            check(not any(N.get_title(h) in ("Clear Desktops", "Restore Hidden Windows") for h in ctl.values() if h),
                  "no Clear Desktops nor Restore Hidden Windows buttons in Settings")
            rc, cc = N.get_window_rect(ctl[SW.ID_RECOVERY]), N.get_window_rect(ctl[SW.ID_CONSOLE])
            edge = N.get_window_rect(ctl[SW.ID_BROWSE])[2]  # (the box's right margin)
            check(N.get_title(ctl[SW.ID_RECOVERY]) == "Recovery" and abs((rc[1] + rc[3]) - (cc[1] + cc[3])) <= 2
                  and rc[0] > cc[2] and rc[2] == edge,
                  f"Recovery button on Show Console's row, right-aligned (button {rc}, Show Console {cc})")
            G.SendMessageW(ctl[SW.ID_RECOVERY], BM_CLICK, 0, None)
            pump(50)
            check(recovered == [1] and dlg.hwnd, "Recovery opens the Recovery dialog; Settings stays open")

            # Settings Folder: temp folders and a throw-away registry key (the real data stays put)
            DF = SW.datafolder
            ftmp = tempfile.mkdtemp(prefix="dd-folder-ui-")
            fdefault, fother = os.path.join(ftmp, "Default"), os.path.join(ftmp, "Other")
            os.makedirs(fdefault)
            for name in DF.MOVED:
                with open(os.path.join(fdefault, name), "w", encoding="utf-8") as fh:
                    fh.write("{}")
            moved_to = []
            SW.SettingsWindow.close_open()
            pump(50)
            try:
                with mock.patch.object(DF, "KEY", r"Software\DisplayDesktop (live test)"), \
                        mock.patch.object(DF, "default_folder", return_value=fdefault), \
                        mock.patch.object(DF, "_in_use", None):  # as at the start of a run
                    dlg = SW.SettingsWindow.show(on_folder=moved_to.append)
                    pump(100)
                    ctl = dlg._controls
                    check(DF.same(dlg.folder_text(), fdefault) and not is_enabled(ctl[SW.ID_APPLY])
                          and not is_enabled(ctl[SW.ID_DEFAULT]) and is_enabled(ctl[SW.ID_BROWSE]),
                          "Settings Folder shows the folder in use; Apply and Default greyed (nothing to change)")
                    check(dlg.folder_text().startswith("%LOCALAPPDATA%\\"),
                          f"shown with its variable, not C:\\Users\\... ({dlg.folder_text()})")
                    SW._SetWindowTextW(ctl[SW.ID_FOLDER], fother)
                    pump(50)
                    check(is_enabled(ctl[SW.ID_APPLY]), "typing another folder makes Apply clickable")
                    box.reset_mock()
                    box.return_value = 7  # No
                    G.SendMessageW(ctl[SW.ID_APPLY], BM_CLICK, 0, None)
                    pump(50)
                    check(box.call_count == 1 and "Move saved settings?" in box.call_args[0][0] and not moved_to
                          and sorted(os.listdir(fdefault)) == sorted(DF.MOVED),
                          "Apply asks first; No moves nothing")
                    box.return_value = 6  # Yes
                    G.SendMessageW(ctl[SW.ID_APPLY], BM_CLICK, 0, None)
                    pump(50)
                    check(moved_to and DF.same(moved_to[0], fother) and sorted(os.listdir(fother)) == sorted(DF.MOVED)
                          and not os.path.exists(fdefault),
                          "Yes: files moved, the app switched to the new folder, the empty old folder removed")
                    check(DF.same(dlg.folder_text(), fother) and not is_enabled(ctl[SW.ID_APPLY])
                          and is_enabled(ctl[SW.ID_DEFAULT]), "then Apply is greyed again and Default can be used")
                    box.reset_mock()
                    G.SendMessageW(ctl[SW.ID_DEFAULT], BM_CLICK, 0, None)
                    pump(50)
                    check(box.call_count == 1 and "default folder" in box.call_args[0][0]
                          and sorted(os.listdir(fdefault)) == sorted(DF.MOVED) and not os.path.exists(fother)
                          and DF.same(dlg.folder_text(), fdefault) and not is_enabled(ctl[SW.ID_DEFAULT]),
                          "Default asks, moves everything back, removes the empty folder, greys itself")
                    SW._SetWindowTextW(ctl[SW.ID_FOLDER], "not a full path")
                    pump(50)
                    box.reset_mock()
                    G.SendMessageW(ctl[SW.ID_APPLY], BM_CLICK, 0, None)
                    pump(50)
                    check(box.call_count == 1 and "could not be moved" in box.call_args[0][0]
                          and sorted(os.listdir(fdefault)) == sorted(DF.MOVED), "a bad folder is refused, nothing moves")
                    # A run that could not reach the chosen folder (its drive is missing) uses the default
                    SW.SettingsWindow.close_open()
                    pump(50)
                    gone = next(d for d in reversed("DEFGHIJKLMNOPQRSTUVWXYZ") if not os.path.exists(f"{d}:\\"))
                    DF._store(f"{gone}:\\DisplayDesktop")
                    DF._in_use = None
                    DF.settle()
                    dlg = SW.SettingsWindow.show(on_folder=moved_to.append)
                    pump(100)
                    ctl = dlg._controls
                    check(DF.same(dlg.folder_text(), fdefault) and is_enabled(ctl[SW.ID_DEFAULT]),
                          "a run that can't reach the chosen folder shows the default one in use; Default can forget the choice")
                    box.reset_mock()
                    box.return_value = 6
                    G.SendMessageW(ctl[SW.ID_DEFAULT], BM_CLICK, 0, None)
                    pump(50)
                    check(DF.chosen_folder() is None and not is_enabled(ctl[SW.ID_DEFAULT])
                          and sorted(os.listdir(fdefault)) == sorted(DF.MOVED), "Default forgets it; nothing moves")
                    os.makedirs(fother, exist_ok=True)
                    with open(os.path.join(fother, "display.desktop.settings.json"), "w", encoding="utf-8") as fh:
                        fh.write('{"theirs":1}')  # e.g. another computer's, in a synced folder
                    SW._SetWindowTextW(ctl[SW.ID_FOLDER], fother)
                    pump(50)
                    box.reset_mock()
                    box.return_value = 2  # Cancel
                    G.SendMessageW(ctl[SW.ID_APPLY], BM_CLICK, 0, None)
                    pump(50)
                    check(box.call_count == 1 and "Both folders have settings" in box.call_args[0][0]
                          and DF.same(DF.current(), fdefault),
                          "Apply into a folder that has settings too asks which to keep (Cancel: nothing changes)")
                    box.reset_mock()
                    box.return_value = 6  # Yes: keep that folder's
                    G.SendMessageW(ctl[SW.ID_APPLY], BM_CLICK, 0, None)
                    pump(50)
                    with open(os.path.join(fother, "display.desktop.settings.json"), encoding="utf-8") as fh:
                        kept = fh.read()
                    check(DF.same(DF.current(), fother) and kept == '{"theirs":1}' and not os.path.exists(fdefault),
                          "Yes: that folder's settings are used; the other data is dropped")
                    SW.SettingsWindow.close_open()
                    pump(50)
                    with mock.patch.object(SW.startup, "can_manage", return_value=False):
                        dlg = SW.SettingsWindow.show()
                        pump(50)
                        SW._SetWindowTextW(dlg._controls[SW.ID_FOLDER], fother)
                        check(not is_enabled(dlg._controls[SW.ID_APPLY]) and not is_enabled(dlg._controls[SW.ID_BROWSE])
                              and not is_enabled(dlg._controls[SW.ID_FOLDER]), "test mode: the folder can't be changed")
                        SW.SettingsWindow.close_open()
                        pump(50)
            finally:
                with mock.patch.object(DF, "KEY", r"Software\DisplayDesktop (live test)"):
                    DF._store(None)
                shutil.rmtree(ftmp, ignore_errors=True)
            box.return_value = None
            dlg = SW.SettingsWindow.show(on_recovery=lambda: recovered.append(1))
            pump(100)
            ctl = dlg._controls
            check(SW.SettingsWindow.show() is dlg, "opening it again brings back the same dialog")
            key(0x1B)  # Esc
            check(not dlg.hwnd and SW.SettingsWindow.current is None and not G.dialogs, "Esc closes it")
            dlg = SW.SettingsWindow.show()
            pump(100)
            key(0x0D)  # Enter: the default button, Close
            check(not dlg.hwnd, "Enter closes it (Close is the default button)")
            with mock.patch.object(SW.startup, "can_manage", return_value=False):
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None)
                pump(100)
                check(not is_enabled(dlg._controls[SW.ID_STARTUP]) and not is_enabled(dlg._controls[SW.ID_START_MENU])
                      and is_enabled(dlg._controls[SW.ID_CONSOLE]) and is_enabled(dlg._controls[SW.ID_GPU])
                      and is_enabled(dlg._controls[SW.ID_RESTORE_APPS]) and is_enabled(dlg._controls[SW.ID_RECOVERY]),
                      "test mode: Start With Windows and Add To Start Menu greyed, the rest works")
                SW.SettingsWindow.close_open()
                pump(50)
                check(not dlg.hwnd and not G.dialogs, "close_open (app exit) closes it")
            with mock.patch.object(SW.gpu, "adapters", return_value=[]):
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None)
                pump(100)
                check(not is_enabled(dlg._controls[SW.ID_GPU]) and not dlg.checked(SW.ID_GPU),
                      "no GPUs: Use GPU Acceleration is greyed and unchecked")
                SW.SettingsWindow.close_open()
                pump(50)

            # Settings opened while the files are encrypted: the key box is made with the key in it
            # (an edit box with text sends EN_CHANGE while it is made: no other control exists yet)
            crypto.use("open me", True)
            try:
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None, on_encrypt=lambda *a: True,
                                             on_remember=lambda on: on)
                pump(100)
                check(dlg is not None and dlg.hwnd and dlg.checked(SW.ID_ENCRYPT) and dlg.key_text() == "open me"
                      and N.get_title(dlg._controls[SW.ID_CRYPT_STATUS]) == "Status: Encrypted"
                      and not is_enabled(dlg._controls[SW.ID_KEY_APPLY]),
                      "Settings opens while encrypted: checked, the key in its box, Status: Encrypted, Apply greyed")
                revert = dlg._controls[SW.ID_KEY_REVERT]
                check(not is_enabled(revert), "Revert greyed while the box holds the saved key")
                SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "typed key")
                pump(50)
                check(is_enabled(revert) and is_enabled(dlg._controls[SW.ID_KEY_APPLY]), "a key typed: Revert and Apply")
                G.SendMessageW(revert, BM_CLICK, 0, None)
                pump(50)
                check(dlg.key_text() == "open me" and not is_enabled(revert), "Revert puts the saved key back")
                from src import restore_window as RW2
                for answer_apply in (False, True):
                    applied = []
                    dlg = SW.SettingsWindow.current or SW.SettingsWindow.show(
                        on_recovery=lambda: None, on_encrypt=lambda *a: applied.append(a[:2]) or True,
                        on_remember=lambda on: on)
                    dlg._on_encrypt = lambda *a: applied.append(a[:2]) or True
                    pump(50)
                    SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "typed key")
                    asked = []
                    with mock.patch.object(RW2, "ask_apply_key", side_effect=lambda owner: asked.append(1) or answer_apply):
                        G.SendMessageW(dlg.hwnd, G.WM_CLOSE, 0, None)
                    pump(50)
                    check(asked == [1] and not dlg.hwnd and applied == ([("typed key", True)] if answer_apply else []),
                          "closed with a key not applied: asked; " + ("Apply applies it" if answer_apply
                                                                      else "Revert (default) saves nothing"))
                    crypto.use("open me", True)
                # a key typed while Encrypt is unchecked: Apply could do nothing, so no question
                dlg = SW.SettingsWindow.current or SW.SettingsWindow.show(on_recovery=lambda: None,
                                                                         on_encrypt=lambda *a: True)
                pump(50)
                G.SendMessageW(dlg._controls[SW.ID_ENCRYPT], BM_CLICK, 0, None)  # (off: written plain)
                pump(50)
                SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "typed key")
                asked = []
                with mock.patch.object(RW2, "ask_apply_key", side_effect=lambda owner: asked.append(1) or True):
                    G.SendMessageW(dlg.hwnd, G.WM_CLOSE, 0, None)
                pump(50)
                check(asked == [] and not dlg.hwnd, "Encrypt unchecked: closing never asks about a key")
                # Apply chosen but it fails: Settings stays open with the key typed
                crypto.use("open me", True)
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None, on_encrypt=lambda *a: False)
                pump(50)
                SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "typed key")
                with mock.patch.object(RW2, "ask_apply_key", return_value=True):
                    G.SendMessageW(dlg.hwnd, G.WM_CLOSE, 0, None)
                pump(50)
                check(dlg.hwnd and dlg.key_text() == "typed key", "Apply chosen but it fails: Settings stays open")
                q = RW2.ApplyKeyWindow(dlg.hwnd)
                pump(50)
                owner_of = N._f(N.user32, "GetWindow", N.H, N.H, ctypes.c_uint)(q.hwnd, 4)  # GW_OWNER
                check(owner_of == dlg.hwnd, "the question is owned by Settings (never behind it)")
                SW._EnableWindow(dlg.hwnd, False)  # (as ask_apply_key does)
                at_destroy = []
                real_destroy = q.destroy
                q.destroy = lambda: at_destroy.append(is_enabled(dlg.hwnd)) or real_destroy()
                G.SendMessageW(q.hwnd, G.WM_CLOSE, 0, 0)
                check(at_destroy == [1], "Settings is enabled again before the question goes (stays in front)")
                SW.SettingsWindow.close_open()
                pump(50)
                # encrypted with K, then unchecked (plain), checked again with K still in the box: K is
                # not the key in use any more, so closing asks
                crypto.reset()

                def fake(key, on, remember):
                    crypto.use(key if on else crypto.key(), on)
                    return True
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None, on_encrypt=fake)
                pump(50)
                G.SendMessageW(dlg._controls[SW.ID_ENCRYPT], BM_CLICK, 0, None)
                SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "K")
                G.SendMessageW(dlg._controls[SW.ID_KEY_APPLY], BM_CLICK, 0, None)
                G.SendMessageW(dlg._controls[SW.ID_ENCRYPT], BM_CLICK, 0, None)  # (decrypted)
                pump(50)
                check(not is_enabled(dlg._controls[SW.ID_KEY_REVERT]), "unchecked: Revert greyed with the key box")
                G.SendMessageW(dlg._controls[SW.ID_ENCRYPT], BM_CLICK, 0, None)  # (checked again)
                pump(50)
                check(is_enabled(dlg._controls[SW.ID_KEY_REVERT]) and dlg.key_unapplied(),
                      "after decrypting, the key left in the box counts as not applied (Revert usable)")
                asked = []

                def ask_and_close_again(owner):  # (the taskbar's Close while the question is open)
                    asked.append(1)
                    G.SendMessageW(dlg.hwnd, G.WM_CLOSE, 0, None)
                    return False
                with mock.patch.object(RW2, "ask_apply_key", side_effect=ask_and_close_again):
                    G.SendMessageW(dlg.hwnd, G.WM_CLOSE, 0, None)
                pump(50)
                check(asked == [1] and not dlg.hwnd, "and closing asks (once: a close meanwhile is ignored)")
                SW.SettingsWindow.close_open()
                pump(50)
                # the key in use changing (the key popup, a folder move) never replaces a key typed
                crypto.use("A", True)
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None, on_encrypt=lambda *a: True)
                pump(50)
                crypto.use("C", True)
                dlg.refresh_options()
                check(dlg.key_text() == "C", "the box still showing the old key follows the key in use")
                SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "B")
                crypto.use("D", True)
                dlg.refresh_options()
                check(dlg.key_text() == "B", "a key typed and not applied is never replaced")
                SW.SettingsWindow.close_open()
                pump(50)
                # a key applied in this dialog is the one shown: a later key change follows it
                crypto.reset()

                def fake_apply(key, on, remember):
                    crypto.use(key if on else crypto.key(), on)
                    return True
                dlg = SW.SettingsWindow.show(on_recovery=lambda: None, on_encrypt=fake_apply)
                pump(50)
                G.SendMessageW(dlg._controls[SW.ID_ENCRYPT], BM_CLICK, 0, None)
                SW._SetWindowTextW(dlg._controls[SW.ID_KEY], "A")
                G.SendMessageW(dlg._controls[SW.ID_KEY_APPLY], BM_CLICK, 0, None)
                crypto.use("B", True)  # (the key popup accepted another key)
                dlg.refresh_options()
                check(dlg.key_text() == "B" and not dlg.key_unapplied(),
                      "after Apply, a key change elsewhere updates the box (never counted as not applied)")
                SW.SettingsWindow.close_open()
                pump(50)
                # with nothing encrypted yet, the question says what Apply and Revert mean then
                crypto.reset()
                q = RW2.ApplyKeyWindow()
                pump(50)
                check("not encrypted yet" in N.get_title(q._controls[RW2.ID_TEXT]), "the question when nothing is encrypted")
                G.SendMessageW(q.hwnd, G.WM_CLOSE, 0, 0)
                crypto.use("open me", True)
                # the question itself: Revert is the default; Esc, closing it and Revert all revert
                for how, want in (("revert", False), ("esc", False), ("close", False), ("apply", True)):
                    win = RW2.ApplyKeyWindow()
                    pump(50)
                    if how == "revert":
                        style = N._f(N.user32, "GetWindowLongW", ctypes.c_long, N.H, ctypes.c_int)(
                            win._controls[RW2.IDCANCEL], -16)
                        check(style & 0xF == 1, "the question's default button is Revert")
                        G.SendMessageW(win._controls[RW2.IDCANCEL], BM_CLICK, 0, None)
                    elif how == "esc":
                        G.SendMessageW(win.hwnd, G.WM_COMMAND, RW2.IDCANCEL, 0)
                    elif how == "close":
                        G.SendMessageW(win.hwnd, G.WM_CLOSE, 0, 0)
                    else:
                        G.SendMessageW(win._controls[RW2.ID_APPLY_KEY], BM_CLICK, 0, None)
                    pump(50)
                    check(not win.hwnd and win.applied == want, f"the question, {how}: {'apply' if want else 'revert'}")
                SW.SettingsWindow.close_open()
                pump(50)
            finally:
                crypto.reset()

            # Settings: Enter in the folder box
            dlg = SW.SettingsWindow.show(on_recovery=lambda: None)
            pump(100)
            with mock.patch.object(SW.startup, "can_manage", return_value=True), \
                    mock.patch.object(SW.SettingsWindow, "_move_folder") as move:
                G.SetFocus(dlg._controls[SW.ID_FOLDER])
                SW._SetWindowTextW(dlg._controls[SW.ID_FOLDER], r"C:\Somewhere\Else")
                pump(50)
                key(0x0D)  # Enter
                pump(50)
                check(dlg.hwnd and move.call_count == 1, "Enter in the folder box applies it (Settings stays open)")
            SW.SettingsWindow.close_open()
            pump(50)

        # --- Restore Hidden Windows: a test window its own app hid (so it is on no desktop)
        from src import restore_window as RW
        mgr.switch_to(dev, 0)
        pump_until(lambda: anim.active_count == 0, 2)
        lost = next((x for x in (one, two, three) if N.is_window_visible(x)), 0)
        N.show_window(lost, N.SW_HIDE)
        mgr.sync()
        check(lost and lost in mgr.hidden_windows(), "a window hidden by its app is in the list")
        dlg = RW.RestoreWindow.show(mgr)
        pump(150)
        lv = dlg._controls[RW.ID_LIST]
        check(G.SendMessageW(lv, RW.LVM_GETITEMCOUNT, 0, None) == len(dlg.windows) and dlg.checked() == [],
              f"the list shows every hidden window ({len(dlg.windows)}), none ticked")
        dlg.set_checked(dlg.windows.index(lost), True)
        check(dlg.checked() == [lost], "ticking one picks it")
        G.SendMessageW(dlg._controls[RW.IDOK], BM_CLICK, 0, None)
        pump(150)
        lost_dev = N.monitor_device_of_window(lost)
        check(N.is_window_visible(lost) and lost in mgr.desktops_of(lost_dev)[0] and mgr.current_of(lost_dev) == 0
              and RW.RestoreWindow.current is None,
              f"OK: it is shown on desktop 1; the list closes (on {lost_dev}: {mgr.desktops_of(lost_dev)})")
        dlg = RW.RestoreWindow.show(mgr)
        pump(100)
        G.SendMessageW(dlg._controls[RW.IDCANCEL], BM_CLICK, 0, None)
        pump(50)
        check(RW.RestoreWindow.current is None and not dlg.hwnd, "Cancel closes it")
        # --- Recovery: what is wrong, every file with its state, the ways out
        from src import recovery as REC
        from src import datafolder as DFM
        row = REC.Row
        get_style = N._f(N.user32, "GetWindowLongW", ctypes.c_long, N.H, ctypes.c_int)
        _IsWindowEnabled = SW._IsWindowEnabled
        ok_report = REC.Report("The settings folder can't be found:\nQ:\\Gone",
                               [row("a", "display.desktop.settings.json", "OK", "ok"),
                                row("b", "display.desktop.crash.json", "Crash record of hidden windows. "
                                    "Damaged: not a list of windows. Removed: use Restore Hidden Windows.", "journal")],
                               [], False, True)
        picked, done = [], []

        def texts(win) -> str:
            return " | ".join(N.get_title(h) for cid, h in sorted(win._controls.items()) if cid >= RW.ID_ROW)
        prob = RW.RecoveryWindow.show(ok_report, on_restore=lambda: picked.append("restore"), on_done=done.append)
        pump(150)
        files = texts(prob) if prob else ""
        check(prob.hwnd and N.is_window_visible(prob.hwnd) and N.get_title(prob.hwnd) == RW.L.t("recovery.title")
              and "display.desktop.settings.json" in files and "not a list of windows" in files,
              "Recovery opens: what is wrong, every file with its state")
        link = prob._controls[RW.ID_VERSION]
        top = min(N.get_window_rect(h)[1] for h in prob._controls.values())
        check(N.get_class(link) == "SysLink" and f">DisplayDesktop {__import__('src').__version__}<" in N.get_title(link)
              and SW.REPO_URL in N.get_title(link) and N.get_window_rect(link)[1] == top,
              "its title: the version as a link to the project page, as in Settings")
        opened = []
        with mock.patch.object(RW, "open_repo_page", lambda: opened.append(1)):
            G.SetFocus(link)
            G.SendMessageW(link, G.WM_KEYDOWN, 0x0D, 0)  # (Enter on the link: NM_RETURN)
            pump(50)
        check(opened == [1] and prob.hwnd, "the link opens the project page; Recovery stays")
        check("http" not in N.get_title(prob._controls[RW.ID_TEXT]), "no project text besides the title")
        check(N.get_title(prob._controls[RW.ID_FOLDER_LABEL]) == RW.L.t("settings.folder")
              and N.get_title(prob._controls[RW.ID_FOLDER]) == DFM.shown(DFM.current())
              and all(c in prob._controls for c in (RW.ID_BROWSE, RW.ID_DEFAULT, RW.ID_APPLY)),
              "its Settings Folder row as in Settings (folder, picker, Default, Apply)")
        check(_IsWindowEnabled(prob._controls[RW.ID_RECREATE]) and get_style(prob._controls[RW.IDOK], -16) & 0xF == 1,
              "nothing wrong: Recreate Files still on (never greyed), Continue is the default")
        # The actions' own row above Quit / Continue: Clear Desktops left, Restore centered, Recreate right
        cr, rr, xr = (N.get_window_rect(prob._controls[c]) for c in (RW.ID_CLEAR, RW.ID_RESTORE, RW.ID_RECREATE))
        qr, kr = N.get_window_rect(prob._controls[RW.ID_QUIT]), N.get_window_rect(prob._controls[RW.IDOK])
        pr = N.get_window_rect(prob.hwnd)
        mid = (pr[0] + pr[2]) // 2
        check(cr[1] == rr[1] == xr[1] < qr[1] == kr[1] and cr[0] < rr[0] < xr[0]
              and abs((rr[0] + rr[2]) // 2 - mid) <= 2 and xr[2] == kr[2],
              f"Clear Desktops, Restore Hidden Windows, Recreate Files: one row above Quit/Continue, left / "
              f"center / right (clear {cr}, restore {rr}, recreate {xr}, continue {kr})")
        asked = []
        for answer_ in (7, G.IDYES):  # (IDNO, then IDYES)
            with mock.patch.object(RW.G, "message_box", side_effect=lambda t, f: asked.append(t) or answer_):
                G.SendMessageW(prob._controls[RW.ID_RESTORE], BM_CLICK, 0, None)
                pump(50)
        check(len(asked) == 2 and asked[0] == RW.L.t("msg.restoreConfirm") and picked == ["restore"] and prob.hwnd,
              "its Restore Hidden Windows asks first (No: nothing; Yes: the list); Recovery stays")
        check("opened on request" not in N.get_title(prob._controls[RW.ID_TEXT]), "no \"opened on request\" text")
        with mock.patch("src.settings_window.move_folder", return_value=True) as move:
            prob.move_folder(r"Q:\Elsewhere")
        pump(50)
        check(move.call_args[0][0] == r"Q:\Elsewhere" and not prob.hwnd and done == ["folder"],
              "Apply moved it: closes (\"folder\"), shown again for that folder")
        prob = RW.RecoveryWindow.show(ok_report, on_done=done.append)
        pump(100)
        with mock.patch("src.settings_window.move_folder", return_value=False):
            prob.move_folder(r"Q:\Junk")
        check(prob.hwnd, "a folder not moved to (its files can't be read): Recovery stays")
        prob.finish("quit")  # (e.g. `run stop` while a box of its own was open)
        prob.finish("folder")  # (that box's move finishing afterwards)
        pump(50)
        check(prob.result == "quit" and done[-1] == "quit", "a Quit already given is never overwritten")
        prob = RW.RecoveryWindow.show(ok_report, on_done=done.append)
        pump(100)
        G.SendMessageW(prob._controls[RW.ID_QUIT], BM_CLICK, 0, None)
        pump(50)
        check(not prob.hwnd and done[-1] == "quit", "Quit: the app is told to end")
        prob = RW.RecoveryWindow.show(ok_report, on_done=done.append)
        pump(100)
        G.SendMessageW(prob.hwnd, G.WM_COMMAND, RW.IDCANCEL, 0)
        pump(50)
        check(not prob.hwnd and done[-1] == "continue", "Esc with nothing to decide: Continue")
        # Files that can't be read at all: must be decided (no Continue); Quit is the default
        bad = REC.Report(RW.L.t("recovery.unreadable"),
                         [row(r"Q:\Data\display.desktop.layout.json", "display.desktop.layout.json",
                              REC.describe(REC.paths.NewerFormat(2))[0], "unreadable")],
                         [r"Q:\Data\display.desktop.layout.json"], False, True)
        for how, want in (("quit", "quit"), ("esc", "quit"), ("close", "quit"), ("recreate", "recreate")):
            nw = RW.RecoveryWindow(bad)
            pump(100)
            if how == "quit":
                state = nw._controls[RW.ID_ROW + 1]
                check("display.desktop.layout.json" in texts(nw) and N.get_class(state) == "SysLink"
                      and "revision number is newer" in N.get_title(state)
                      and f'{SW.REPO_URL}">check for updates</a>' in N.get_title(state)
                      and get_style(nw._controls[RW.ID_QUIT], -16) & 0xF == 1 and RW.IDOK not in nw._controls
                      and _IsWindowEnabled(nw._controls[RW.ID_RECREATE]),
                      "a newer version's file: its revision number is newer, \"check for updates\" a link; "
                      "Quit the default, no Continue")
                opened.clear()
                with mock.patch.object(RW, "open_repo_page", lambda: opened.append(1)):
                    G.SetFocus(state)
                    G.SendMessageW(state, G.WM_KEYDOWN, 0x0D, 0)
                    pump(50)
                check(opened == [1] and nw.hwnd, "\"check for updates\" opens the project page")
                G.SendMessageW(nw._controls[RW.ID_QUIT], BM_CLICK, 0, None)
            elif how == "esc":
                G.SendMessageW(nw.hwnd, G.WM_COMMAND, RW.IDCANCEL, 0)
            elif how == "close":
                G.SendMessageW(nw.hwnd, G.WM_CLOSE, 0, 0)
            else:
                asked = []
                for answer_ in (7, G.IDYES):  # (IDNO, then IDYES)
                    with mock.patch.object(RW.G, "message_box",
                                           side_effect=lambda t, f: asked.append((t, f)) or answer_):
                        G.SendMessageW(nw._controls[RW.ID_RECREATE], BM_CLICK, 0, None)
                        pump(50)
                    if answer_ == 7:
                        check(nw.hwnd and nw.result != "recreate", "Recreate Files asks first: No recreates nothing, "
                                                                   "Recovery stays")
                check(len(asked) == 2 and asked[0][0] == RW.L.t("msg.recreateConfirm")
                      and asked[0][1] & G.MB_DEFBUTTON2, "the question: Yes / No, No the default")
            pump(50)
            check(not nw.hwnd and nw.result == want, f"files that can't be read, {how}: {want}")
        # Apply / Default to a folder with files that can't be read: each with why; Close only
        for how in ("close", "esc"):
            bw = RW.BadFolderWindow(r"Q:\Junk", bad.rows)
            pump(100)
            buttons = [cid for cid, h in bw._controls.items() if N.get_class(h) == "Button"]
            if how == "close":
                check(N.get_class(bw._controls[RW.ID_VERSION]) == "SysLink" and "display.desktop.layout.json" in texts(bw)
                      and "check for updates" in texts(bw) and "Q:\\Junk" in N.get_title(bw._controls[RW.ID_TEXT])
                      and buttons == [RW.IDOK],
                      "a folder whose files can't be read: the title link, each file with why, Close only")
                G.SendMessageW(bw._controls[RW.IDOK], BM_CLICK, 0, None)
            else:
                G.SendMessageW(bw.hwnd, G.WM_COMMAND, RW.IDCANCEL, 0)
            pump(50)
            check(not bw.hwnd, f"that folder's dialog, {how}: closed (the folder is not used)")
        # Try Key in Recovery (the files may only need another key), and the key popup. A wrong key asks
        # to try again (Retry / Cancel; answered here without a real box)
        asked, answers = [], []
        real_box = G.message_box
        G.message_box = lambda text, flags: asked.append((text, flags)) or answers.pop(0)
        tried = []
        prob = RW.RecoveryWindow.show(REC.Report("Some files can't be read right now.",
                                                 [row("a", "display.desktop.settings.json",
                                                      "Encrypted with a key not entered", "locked")], [], True, True),
                                      on_key=lambda key: tried.append(key) or key == "right")
        pump(150)
        RW._SetWindowTextW(prob._controls[RW.ID_KEY], "wrong")
        answers.append(G.IDRETRY)
        G.SendMessageW(prob._controls[RW.ID_TRY], BM_CLICK, 0, None)
        pump(50)
        check(tried == ["wrong"] and prob.hwnd and N.get_title(prob._controls[RW.ID_STATUS]) == RW.L.t("key.wrong")
              and asked == [(RW.L.t("key.retry"), G.MB_RETRYCANCEL | G.MB_ICONWARNING)],
              "Recovery's Try Key: a wrong key says so and asks to try again; Retry: it stays")
        answers.append(2)  # IDCANCEL
        G.SendMessageW(prob._controls[RW.ID_TRY], BM_CLICK, 0, None)
        pump(50)
        check(len(asked) == 2 and prob.hwnd, "Recovery's Try Key: Cancel: Recovery stays open")
        RW._SetWindowTextW(prob._controls[RW.ID_KEY], "right")
        G.SendMessageW(prob._controls[RW.ID_TRY], BM_CLICK, 0, None)
        pump(50)
        check(tried == ["wrong", "wrong", "right"] and not prob.hwnd and prob.result == "key",
              "the right key reads the files and closes Recovery")
        closed = []
        kw = RW.KeyWindow.show("Q:\\Encrypted", lambda key: key == "right", on_closed=closed.append)
        pump(150)
        check(kw.hwnd and N.is_window_visible(kw.hwnd) and G.SendMessageW(kw._controls[RW.ID_KEY], 0x00D2, 0, None)
              == SW.PASSWORD_CHAR, "the key popup opens, the key as dots")
        RW._SetWindowTextW(kw._controls[RW.ID_KEY], "wrong")
        answers.append(G.IDRETRY)
        G.SendMessageW(kw._controls[RW.IDOK], BM_CLICK, 0, None)
        pump(50)
        check(kw.hwnd and N.get_title(kw._controls[RW.ID_STATUS]) == RW.L.t("key.wrong") and len(asked) == 3,
              "a wrong key: asked to try again; Retry: asked again")
        RW._SetWindowTextW(kw._controls[RW.ID_KEY], "right")
        G.SendMessageW(kw._controls[RW.IDOK], BM_CLICK, 0, None)
        pump(50)
        check(not kw.hwnd and kw.accepted and closed == [True], "the right key: accepted, closed")
        kw = RW.KeyWindow.show("Q:\\Encrypted", lambda key: True, on_closed=closed.append)
        pump(100)
        G.SendMessageW(kw._controls[RW.IDCANCEL], BM_CLICK, 0, None)
        pump(50)
        check(not kw.hwnd and not kw.accepted and closed == [True, False], "Cancel: closed, nothing accepted")
        kw = RW.KeyWindow.show("Q:\\Encrypted", lambda key: False, on_closed=closed.append)
        pump(100)
        RW._SetWindowTextW(kw._controls[RW.ID_KEY], "wrong")
        answers.append(2)  # IDCANCEL
        G.SendMessageW(kw._controls[RW.IDOK], BM_CLICK, 0, None)
        pump(50)
        check(not kw.hwnd and not kw.accepted and closed == [True, False, False] and len(asked) == 4,
              "a wrong key, not tried again: closed as Cancel")
        G.message_box = real_box
        # Recovery opened from Settings: Close in Quit's place (the default), no Continue; Close, Esc
        # and the title bar's X never quit the app, not even when files must be decided
        for name, report in (("no problems", ok_report), ("files that can't be read", bad)):
            for how in ("close", "esc", "x"):
                done = []
                rw = RW.RecoveryWindow.show(report, on_done=done.append, can_quit=False)
                pump(100)
                quit_btn = rw._controls[RW.ID_QUIT]
                if how == "close":
                    check(N.get_title(quit_btn) == RW.L.t("settings.close") and RW.IDOK not in rw._controls
                          and get_style(quit_btn, -16) & 0xF == 1,
                          f"from Settings ({name}): Close instead of Quit, the default; no Continue")
                    G.SendMessageW(quit_btn, BM_CLICK, 0, None)
                elif how == "esc":
                    G.SendMessageW(rw.hwnd, G.WM_COMMAND, RW.IDCANCEL, 0)
                else:
                    G.SendMessageW(rw.hwnd, G.WM_CLOSE, 0, 0)
                pump(50)
                check(not rw.hwnd and done == ["continue"], f"from Settings ({name}), {how}: closed, never quit ({done})")
        rw = RW.RecoveryWindow.show(bad, on_done=lambda r: None)
        pump(100)
        check(N.get_title(rw._controls[RW.ID_QUIT]) == RW.L.t("recovery.quit"), "opened any other way: Quit as before")
        RW.RecoveryWindow.close_open()
        pump(50)
        # Clear Desktops in Recovery: runs the tray's Clear (it asks first), Recovery stays open; greyed
        # while a settings file is bad (can't be read, or locked) and without the running app
        cleared = []
        rw = RW.RecoveryWindow.show(ok_report, on_done=lambda r: None, on_clear=lambda: cleared.append(1))
        pump(100)
        clear_btn = rw._controls[RW.ID_CLEAR]
        check(N.get_title(clear_btn) == "Clear Desktops" and is_enabled(clear_btn), "Recovery has Clear Desktops")
        G.SendMessageW(clear_btn, BM_CLICK, 0, None)
        pump(50)
        check(cleared == [1] and rw.hwnd, "Clear Desktops runs the tray's Clear; Recovery stays open")
        for name, report, on_clear in (("files that can't be read", bad, lambda: cleared.append(2)),
                                       ("a file locked", ok_report._replace(locked=True), lambda: cleared.append(2)),
                                       ("no running app", ok_report, None)):
            rw = RW.RecoveryWindow.show(report, on_done=lambda r: None, on_clear=on_clear)
            pump(100)
            G.SendMessageW(rw._controls[RW.ID_CLEAR], BM_CLICK, 0, None)
            pump(50)
            check(not is_enabled(rw._controls[RW.ID_CLEAR]) and cleared == [1],
                  f"Clear Desktops greyed with {name}, and does nothing")
        RW.RecoveryWindow.close_open()
        pump(50)

        # --- tray menu on screen: the real show_menu + owner drawing on a stand-in tray window
        from src import tray as T
        from src import tray_menu as TM
        get_item_rect = N._f(N.user32, "GetMenuItemRect", N.BOOL, N.H, N.H, N.UINT, ctypes.POINTER(N.RECT))
        keybd = N._f(N.user32, "keybd_event", None, ctypes.c_ubyte, ctypes.c_ubyte, N.DWORD, ctypes.c_size_t)

        class TrayStandIn(G.Window):
            exiting = False
            _menu_painter = None
            chosen: list[int] = []

            def run_command(self, cmd):
                self.chosen.append(cmd)

            def on_message(self, msg, wp, lp):
                if msg in (TM.WM_MEASUREITEM, TM.WM_DRAWITEM):
                    return T.TrayApp.on_message(self, msg, wp, lp)
                return None

        host = TrayStandIn()
        host.create("DisplayDesktop.TestTray", 0, N.WS_POPUP, 0, 0, 0, 0)
        menu_items = T.TrayApp.menu_items()
        menus, seen = [], {}
        real_create = G.CreatePopupMenu

        def screen_item(index: int, hot_test: bool = False):
            """Screen pixels of menu item `index` (rows of 0xBBGGRR), or None."""
            r = N.RECT()
            if not menus or not get_item_rect(0, menus[0], index, ctypes.byref(r)):
                return None
            cap = S.capture_screen((r.left, r.top, r.right, r.bottom))
            if cap is None:
                return None
            w, h = r.right - r.left, r.bottom - r.top
            get_px = ctypes.WinDLL("gdi32")["GetPixel"]
            get_px.restype, get_px.argtypes = ctypes.c_uint32, [ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int]
            rows = [[get_px(cap[0], x, y) for x in range(w)] for y in range(h)]
            S.free_capture(cap)
            return rows

        def offscreen(p, it, shot):
            """Item `it` drawn by painter `p` into a bitmap the size of its screen capture (0xBBGGRR rows)."""
            h, w = len(shot), len(shot[0])
            dc = G.CreateCompatibleDC(0)
            bmp, bits = G.create_dib(w, h)
            old = G.SelectObject(dc, bmp)
            try:
                dis = TM.DRAWITEMSTRUCT(TM.ODT_MENU, 0, it.id, 0, 0, 0, dc, N.RECT(0, 0, w, h), 0)
                p.draw(ctypes.addressof(dis))
                px = (ctypes.c_uint32 * (w * h)).from_address(bits)
                return [[((v & 0xFF) << 16) | (v & 0xFF00) | ((v >> 16) & 0xFF) for v in px[y * w:(y + 1) * w]]
                        for y in range(h)]  # DIB is BGRA; GetPixel gives 0xBBGGRR
            finally:
                G.SelectObject(dc, old)
                G.DeleteObject(bmp)
                G.DeleteDC(dc)

        def inspect():
            try:
                if not wait_for(lambda: find_visible("#32768") or bool(_FindWindowExW(0, 0, "#32768", None)), 3):
                    return
                time.sleep(0.4)  # menu fade-in
                p = host._menu_painter
                seen["painter"] = p is not None
                seen["items"] = [screen_item(i) for i in range(len(menu_items))]
                seen["s"] = p.s if p else (lambda v: v)
                # the same items drawn off-screen by the app's painter, for a pixel comparison
                seen["offscreen"] = [offscreen(p, it, shot) if p and it and shot else None
                                     for it, shot in zip(menu_items, seen["items"])]
                for _ in range(1):  # keyboard: highlight the first item
                    keybd(0x28, 0, 0, 0)
                    keybd(0x28, 0, 2, 0)
                time.sleep(0.3)
                seen["hot0"] = screen_item(0)
            finally:
                G.PostMessageW(host.hwnd, 0x001F, 0, 0)  # WM_CANCELMODE: closes it, nothing chosen

        with mock.patch.object(G, "CreatePopupMenu", side_effect=lambda: menus.append(real_create()) or menus[-1]):
            force_foreground(host.hwnd)
            threading.Thread(target=inspect, daemon=True).start()
            T.TrayApp.show_menu(host)
        pump(50)
        shots = seen.get("items") or []
        check(seen.get("painter") and len(shots) == len(menu_items) and all(shots),
              f"tray menu opens on screen with all {len(menu_items)} items, drawn by the app")
        if shots and all(shots):
            s = seen["s"]

            def ink(rows):  # x positions of dark (text) pixels
                return [x for r in rows for x, c in enumerate(r) if sum((c >> k) & 0xFF for k in (0, 8, 16)) < 384]

            items_only = [(it, px) for it, px in zip(menu_items, shots) if it]
            heights = {len(px) for _, px in items_only}
            seps = [len(px) for it, px in zip(menu_items, shots) if it is None]
            check(len(heights) == 1 and all(h < min(heights) for h in seps),
                  f"items share one height {sorted(heights)}, dividers are thinner {sorted(set(seps))}")
            ok_arrows = ok_plain = True
            for it, px in items_only:
                w = len(px[0])
                right = w - s(TM.TEXT_RIGHT)
                has_right_ink = any(right - s(12) <= x <= right for x in ink(px))
                if it.arrow:
                    ok_arrows &= has_right_ink and bool(ink(px))
                else:
                    ok_plain &= not has_right_ink and bool(ink(px))
            check(ok_arrows, "Overview / Left / Right: shortcut and arrow at the right edge")
            check(ok_plain, "the other items have only their label (no shortcut column)")
            def close(a: int, b: int) -> bool:
                # (ClearType text on screen and in a bitmap may differ by a level or two per channel: the
                # same drawing; a wrong colour, glyph or position is far more than this)
                return all(abs(((a >> k) & 0xFF) - ((b >> k) & 0xFF)) <= PIXEL_TOLERANCE for k in (0, 8, 16))

            same, worst_px = [], None
            for it, px, ref in zip(menu_items, shots, seen.get("offscreen") or []):
                if it and ref:
                    total = sum(len(r) for r in px)
                    bad = [(x, y) for y, (ra, rb) in enumerate(zip(px, ref)) for x, (a, b) in enumerate(zip(ra, rb))
                           if not close(a, b)]
                    same.append(1 - len(bad) / total)
                    if bad and (worst_px is None or same[-1] < min(same[:-1] or [1])):
                        x, y = bad[0]
                        worst_px = (it.id, (x, y), f"{px[y][x]:06x}", f"{ref[y][x]:06x}")
            check(same and min(same) > 0.99,
                  "on screen = the app's drawing, pixel for pixel (incl. the arrows: off-screen tests show them)"
                  f" [{min(same or [0]):.3f}]" + (f" per item {[round(v, 3) for v in same]}, first different pixel "
                                                  f"{worst_px}" if same and min(same) <= 0.99 else ""))
            hot, before = seen.get("hot0"), shots[0]
            if hot is not None and len(hot) == len(before):
                y, x = len(before) // 2, s(TM.TEXT_LEFT) // 2  # the empty margin left of the label
                check(hot[y][x] != before[y][x], "keyboard highlight shows on the first item")
            else:
                check(False, "keyboard highlight shows on the first item (no capture)")
        check(not host.chosen and host._menu_painter is None, "closing the menu runs nothing and frees the painter")
        host.destroy()

        # --- handle leaks: open/close the overview and animate repeatedly
        gdi0, user0 = gui_objects()
        from src import gpu as GPU
        made, freed = [], []
        real_init, real_close = GPU.Layer.__init__, GPU.Layer.close

        def counting_init(self, hwnd, devices=None):
            real_init(self, hwnd, devices)
            made.append(1)

        def counting_close(self):
            if self._target:
                freed.append(1)
            real_close(self)

        layer_patch = mock.patch.multiple(GPU.Layer, __init__=counting_init, close=counting_close)
        layer_patch.start()
        for i in range(8):
            Overview.toggle(mgr)
            pump(60)
            Overview.toggle(mgr)
            pump(30)
            anim.begin(dev, 1 if i % 2 else -1)
            anim.commit(dev)
            pump_until(lambda: anim.active_count == 0, 2)
        pump(1200)
        layer_patch.stop()
        check(len(made) == len(freed), f"every GPU layer was released ({len(made)} made, {len(freed)} released)")
        gdi1, user1 = gui_objects()
        check(gdi1 - gdi0 <= 10 and user1 - user0 <= 10,
              f"no GDI/USER handle leak (GDI {gdi0}->{gdi1}, USER {user0}->{user1})")
        check(not errors, f"no errors inside window procedures {errors[:1]}")
    finally:
        if Overview.is_open():
            Overview.current().close()
        if mgr is not None:
            mgr.restore_all()
        if helper is not None:
            for x in helper.all():
                if N.is_window(x) and not N.is_window_visible(x):
                    N.show_window(x, N.SW_SHOWNA)
            helper.quit()
        shutil.rmtree(tmp, ignore_errors=True)

    went_hidden = [h for h in others_before if N.is_window(h) and not N.is_window_visible(h)]
    check(not went_hidden, f"none of the user's own windows was hidden ({len(others_before)} checked)")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    with real_app_stopped():
        code = main()
    sys.exit(code)
