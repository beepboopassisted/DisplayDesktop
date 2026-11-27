"""macOS-style switch animation: the desktops sit side by side like one long comic strip, and the
strip slides by one desktop. Just before the switch, a screenshot of the monitor is shown on a
fixed layer covering the monitor; the windows are swapped underneath; once they have painted, the
new desktop is captured too (the layer is excluded from screen capture, so it photographs what is
under it without any flicker); then both pictures slide together, the old one out and the new one
in right behind it. When the strip has moved by one desktop the layer goes away, leaving the real
new desktop, which looks exactly like the last frame. The layer never extends past the monitor and
never takes clicks. Where Windows cannot exclude a window from capture (older Windows builds),
the old picture slides off and reveals the new desktop underneath instead.

At the first or last desktop there is nothing to switch to: the screenshot bounces instead (it is
pushed a little in the direction asked for and springs back, with a dark edge in the gap).

Smoothness: frames are drawn in step with the monitor's own refresh (a worker thread waits for its
vertical blank and posts one frame message each time), and each frame's position is computed from
the time of that refresh, so the image moves the same distance every frame. A timer paces the
frames only if no refresh signal can be had. The motion eases in and out (no jump at the start).

GPU: when the layer is excluded from capture, the pictures are handed to the GPU once (gpu.Layer,
DirectComposition) and each frame only moves them, instead of copying two full-monitor pictures per
frame on the CPU. The window's own content is then just black (the bounce gap). Any GPU failure
falls back to the CPU drawing, even in the middle of an animation. `paint()` is the CPU drawing of
the current frame in both cases (tests record frames with it)."""
from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import Structure, byref, c_long, c_uint, c_ulong, c_wchar_p

from . import gpu
from . import gui as G
from . import native as N

DURATION_MS = 280
PAINT_DELAY_MS = 30  # short wait so the new windows can paint underneath (two refreshes at 60 Hz)
LIVE_WAIT_MS = 50  # GPU capture: the strip waits at most this long for a frame from after the switch
_TICK = 1
_FAILSAFE = 2
WM_FRAME = G.WM_APP + 50
_FALLBACK_FRAME_S = 1 / 60


def ease(t: float) -> float:
    """Ease-in-out (sine): starts and ends gently, fastest in the middle."""
    return 0.5 - 0.5 * math.cos(math.pi * min(1.0, max(0.0, t)))


BOUNCE_MS = 380
BOUNCE_FRACTION = 0.035   # how far the bounce pushes, as a share of the monitor width
BOUNCE_MIN_PX = 24


def _spring_raw(t: float) -> float:
    return math.sin(2.5 * math.pi * t) * math.exp(-4.0 * t)


_SPRING_PEAK = max(_spring_raw(i / 1000) for i in range(1001))


def spring(t: float) -> float:
    """Bounce curve: out to 1, back past 0 a little, settles at 0 (damped spring)."""
    if t <= 0.0 or t >= 1.0:
        return 0.0
    return _spring_raw(t) / _SPRING_PEAK


def bounce_offset(t: float, width: int, direction: int) -> int:
    """Where the image is drawn at time t (0..1) of a bounce on a monitor `width` pixels wide.
    Asked to go right (+1): the desktop is pushed left (negative), as if it tried to move on."""
    amplitude = max(BOUNCE_MIN_PX, int(width * BOUNCE_FRACTION))
    sign = 1 if direction > 0 else -1
    return -sign * int(round(spring(t) * amplitude))


# ---------------------------------------------------------------- vertical blank (refresh) signal

class _OPENADAPTERFROMHDC(Structure):
    _fields_ = [("hDc", N.H), ("hAdapter", c_uint), ("LuidLow", c_ulong), ("LuidHigh", c_long),
                ("VidPnSourceId", c_uint)]


class _WAITFORVBLANK(Structure):
    _fields_ = [("hAdapter", c_uint), ("hDevice", c_uint), ("VidPnSourceId", c_uint)]


class _CLOSEADAPTER(Structure):
    _fields_ = [("hAdapter", c_uint)]


_CreateDCW = N._f(N.gdi32, "CreateDCW", N.H, c_wchar_p, c_wchar_p, c_wchar_p, ctypes.c_void_p)
_OpenAdapter = N._f(N.gdi32, "D3DKMTOpenAdapterFromHdc", c_long, ctypes.POINTER(_OPENADAPTERFROMHDC))
_WaitVBlank = N._f(N.gdi32, "D3DKMTWaitForVerticalBlankEvent", c_long, ctypes.POINTER(_WAITFORVBLANK))
_CloseAdapter = N._f(N.gdi32, "D3DKMTCloseAdapter", c_long, ctypes.POINTER(_CLOSEADAPTER))
_DwmFlush = N._f(N.dwmapi, "DwmFlush", c_long)
_SetWindowDisplayAffinity = N._f(N.user32, "SetWindowDisplayAffinity", N.BOOL, N.H, N.DWORD)
WDA_EXCLUDEFROMCAPTURE = 0x11


def capture_screen(bounds) -> tuple[int, int, int] | None:
    """Copies a screen area into a new memory bitmap: (dc, bitmap, previously selected object)."""
    l, t, r, b = bounds
    w, h = r - l, b - t
    screen = G.GetDC(0)
    dc = G.CreateCompatibleDC(screen)
    bmp = G.CreateCompatibleBitmap(screen, w, h)
    if not dc or not bmp:
        G.ReleaseDC(0, screen)
        if bmp:
            G.DeleteObject(bmp)
        if dc:
            G.DeleteDC(dc)
        return None
    old = G.SelectObject(dc, bmp)
    ok = G.BitBlt(dc, 0, 0, w, h, screen, l, t, G.SRCCOPY)
    G.ReleaseDC(0, screen)
    if not ok:
        free_capture((dc, bmp, old))
        return None
    return dc, bmp, old


def free_capture(cap) -> None:
    dc, bmp, old = cap
    G.SelectObject(dc, old)
    G.DeleteObject(bmp)
    G.DeleteDC(dc)


def strip_positions(offset: int, width: int, direction: int) -> tuple[int, int]:
    """Where the old and the new desktop are drawn when the strip has moved `offset` pixels.
    Forward (+1): the next desktop is on the right, so both move left; backward: both move right.
    The two pictures always touch, like neighbouring panels of a comic strip."""
    if direction > 0:
        return -offset, width - offset
    return offset, offset - width


class _FramePacer:
    """Worker thread: once per refresh of the given monitor, records the time and posts WM_FRAME
    to the overlay (at most one frame message waiting at a time). Never raises."""

    def __init__(self, hwnd: int, device: str):
        self._hwnd, self._device = hwnd, device
        self._stop = threading.Event()
        self.pending = threading.Event()   # a WM_FRAME is in the queue and not handled yet
        self.vblank_at = 0.0               # perf_counter() of the latest refresh
        self.source = ""                   # "vblank", "dwm" or "sleep": what paced the last frame
        self._thread = threading.Thread(target=self._run, name="slide-frames", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open(self):
        try:
            hdc = _CreateDCW(self._device, None, None, None)  # a DC for exactly this monitor
            if not hdc:
                return None
            try:
                o = _OPENADAPTERFROMHDC(hDc=hdc)
                if _OpenAdapter(byref(o)) != 0:
                    return None
                return o
            finally:
                G.DeleteDC(hdc)
        except Exception:  # noqa: BLE001 - no refresh signal: DwmFlush or plain pacing
            return None

    def _run(self) -> None:
        adapter = None
        try:
            adapter = self._open()
            wait = (_WAITFORVBLANK(hAdapter=adapter.hAdapter, VidPnSourceId=adapter.VidPnSourceId)
                    if adapter is not None else None)
            last = last_signal = time.perf_counter()
            while not self._stop.is_set():
                source = ""
                try:
                    if wait is not None and _WaitVBlank(byref(wait)) == 0:
                        source = "vblank"
                    elif _DwmFlush() == 0:
                        source = "dwm"
                except Exception:  # noqa: BLE001
                    source = ""
                now = time.perf_counter()
                # Two refresh signals within 2 ms are not real (e.g. DwmFlush returning at once while
                # nothing is drawn): pace ourselves then. A signal soon after our own sleep is fine.
                if not source or now - last_signal < 0.002:
                    time.sleep(max(0.0, _FALLBACK_FRAME_S - (now - last)))
                    now = time.perf_counter()
                    source = "sleep"
                else:
                    last_signal = now
                last = now
                if self._stop.is_set():
                    break
                self.vblank_at, self.source = now, source
                if not self.pending.is_set():
                    self.pending.set()
                    if not G.PostMessageW(self._hwnd, WM_FRAME, 0, 0):
                        self.pending.clear()
        except Exception:  # noqa: BLE001 - a worker thread must never take the app down
            pass
        finally:
            if adapter is not None:
                try:
                    _CloseAdapter(byref(_CLOSEADAPTER(adapter.hAdapter)))
                except Exception:  # noqa: BLE001
                    pass


class _Overlay(G.Window):
    def __init__(self, bounds, device: str, shot_dc: int, shot_bmp: int, old_obj: int, direction: int,
                 on_done, live: "gpu.Capture | None" = None):
        """`live`: the monitor's GPU capture (its latest frame is the old desktop; the new one is taken
        from it while sliding, refreshed every frame). Without it the pictures are GDI screenshots
        (`shot_*`). A live overlay that can't be set up raises OSError (the caller uses screenshots)."""
        super().__init__()
        self._excluded = False
        self._dc, self._bmp, self._old = shot_dc, shot_bmp, old_obj
        self._live = live
        self._old_tex = live.keep_old() if live is not None else None  # (the old desktop, the capture's)
        self._live_strip = False  # the live new desktop is in the strip
        self._capture_lost = False  # the GPU capture was lost mid-slide (it goes on with a screenshot)
        self._committed = 0  # gpu.qpc() of the switch's commit: the new desktop is a frame after it
        self._new_cpu: tuple[int, int, int] | None = None  # the live new desktop read back (CPU drawing)
        self._new_cpu_at = -1  # its frame's present time
        self._device = device
        self._direction = direction  # +1: forward switch -> image slides left; -1: the reverse
        self._on_done = on_done
        self._image_x = 0
        self._new_x = 0
        self._new: tuple[int, int, int] | None = None   # capture of the new desktop (strip mode)
        self._strip_tried = False
        self.was_strip = False  # both desktops slid together (tests, diagnostics)
        self._sliding = False
        self._bounce = 0  # +1/-1 while bouncing (direction asked for), 0 for a switch
        self._started = 0.0
        self._pacer: _FramePacer | None = None
        self._last_message = 0.0  # perf_counter() when the last WM_FRAME arrived
        self.frames: list[float] = []  # time of each frame that moved the image (diagnostics, tests)
        self._gpu: gpu.Layer | None = None  # the pictures on the GPU (None: drawn by the CPU)
        l, t, r, b = bounds
        self._bounds = bounds
        self._w, self._h = r - l, b - t
        self.create("DisplayDesktop.Slide",
                    N.WS_EX_TOPMOST | N.WS_EX_TOOLWINDOW | N.WS_EX_NOACTIVATE | N.WS_EX_TRANSPARENT
                    | N.WS_EX_LAYERED, N.WS_POPUP, l, t, self._w, self._h)
        if live is not None:
            live.use()  # (never closed under this slide; `done` in on_destroyed)
        G.SetLayeredWindowAttributes(self.hwnd, 0, 255, G.LWA_ALPHA)
        # Left out of screen captures, so the new desktop can be captured with the layer on top
        self._excluded = bool(_SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE))
        if live is not None:
            if not self._excluded:
                self.destroy()
                raise OSError("the layer can't be left out of captures")
            try:
                self._put_live_on_gpu()
            except BaseException:
                self.destroy()
                raise
        elif self._excluded:  # (the region fallback below stays on the CPU)
            self._put_on_gpu()
        G.SetWindowPos(self.hwnd, N.HWND_TOPMOST, l, t, self._w, self._h, N.SWP_NOACTIVATE | N.SWP_SHOWWINDOW)
        G.UpdateWindow(self.hwnd)  # paint the screenshot now, before any window is hidden
        self._presented()
        # Safety net: never leave the screenshot covering the monitor if the switch never commits
        self.set_timer(_FAILSAFE, 1500, self.slide_out)

    def _put_on_gpu(self) -> None:
        """The screenshot as a GPU layer on the GPU that drives this monitor, committed before the
        window shows. Only when that GPU is available (none, GPU off or failing: the CPU draws, with
        no new try on every switch). A layer that fails to set up drops that GPU's devices and pauses
        it (gpu.layer_failed)."""
        devices = gpu.devices_for(self._device)
        if devices is None:
            return
        layer = None
        try:
            layer = gpu.Layer(self.hwnd, devices)
            layer.add(self._dc, self._w, self._h)
            layer.commit(wait=True)
        except BaseException as exc:
            if layer is not None:
                layer.close()
            if not isinstance(exc, OSError):
                raise
            gpu.layer_failed(devices)
            return
        self._gpu = layer

    def _put_live_on_gpu(self) -> None:
        """The old desktop (a GPU texture) as the layer's first picture, committed before the window
        shows. Raises OSError (the caller falls back to screenshots)."""
        layer = gpu.Layer(self.hwnd, self._live.devices)
        try:
            layer.add_texture(self._old_tex, self._w, self._h)
            layer.commit(wait=True)
        except BaseException:
            layer.close()
            raise
        self._gpu = layer

    def _start_live_strip(self, now: float) -> bool:
        """The live new desktop joins the strip once the screen shows a frame from after the switch
        (or LIVE_WAIT_MS passed). Returns whether the strip runs."""
        live = self._live
        live.grab(0)
        if live.present <= self._committed and (now - self._started) * 1000.0 < LIVE_WAIT_MS:
            return False
        self._gpu.add_texture(live.texture, self._w, self._h, strip_positions(0, self._w, self._direction)[1])
        self._live_strip = self.was_strip = True
        return True

    def _add_screenshot_strip(self) -> None:
        """The new desktop as a screenshot, into the strip of a slide whose GPU capture was lost (the
        layer is left out of screen captures: the screenshot is the new desktop beneath it)."""
        shot = capture_screen(self._bounds) if self._excluded else None
        if shot is None:
            return  # (the old desktop slides on alone, its edge dark)
        self._new = shot
        try:
            self._gpu.add(shot[0], self._w, self._h, strip_positions(0, self._w, self._direction)[1])
        except OSError:
            self._gpu_failed()
            return
        self.was_strip = True

    @property
    def pacing(self) -> str:
        return self._pacer.source if self._pacer is not None else "timer"

    def bounce(self, direction: int) -> None:
        """No desktop further that way: push the image a little and let it spring back."""
        self._bounce = 1 if direction > 0 else -1
        self.slide_out()

    def slide_out(self) -> None:
        if self._sliding or not self.hwnd:
            return
        self._sliding = True
        self.kill_timer(_FAILSAFE)
        self._started = time.perf_counter() - _FALLBACK_FRAME_S  # (the first frame already moves)
        if self._live is not None:
            self._committed = gpu.qpc()
        try:
            self._pacer = _FramePacer(self.hwnd, self._device)
        except Exception:  # noqa: BLE001 - no thread: timer-paced frames
            self._pacer = None
        # The timer keeps the animation going (and ends it) even if no frame message ever arrives
        self.set_timer(_TICK, 33 if self._pacer else 10, self._on_timer)

    def _on_timer(self) -> None:
        now = time.perf_counter()
        if self._pacer is not None and now - self._last_message < 0.05:
            return  # refresh-paced frames are arriving: the timer only steps in if they stop
        self._frame(now)

    def _frame(self, now: float) -> None:
        if not self.hwnd:
            return
        if self._bounce:
            self._bounce_frame(now)
            return
        if self._live is not None:
            self._live_frame(now)
            return
        elapsed = (now - self._started) * 1000.0 - PAINT_DELAY_MS
        if elapsed < 0:
            return
        if not self._strip_tried:
            # The new windows have had PAINT_DELAY_MS to paint: photograph the new desktop. The
            # slide's clock starts now, so the capture never eats into the motion.
            self._strip_tried = True
            if self._excluded:
                self._new = capture_screen(self._bounds)
                self.was_strip = self._new is not None
                if self._new is not None and self._gpu is not None:
                    try:
                        self._gpu.add(self._new[0], self._w, self._h,
                                      strip_positions(0, self._w, self._direction)[1])
                    except OSError:
                        self._gpu_failed()
            self._started = now - PAINT_DELAY_MS / 1000.0 - _FALLBACK_FRAME_S
            elapsed = _FALLBACK_FRAME_S * 1000.0  # (the first frame already moves)
        t = min(1.0, elapsed / DURATION_MS)
        offset = int(round(ease(t) * self._w))
        if self._new is not None:
            old_x, new_x = strip_positions(offset, self._w, self._direction)
            if old_x != self._image_x or t >= 1.0:
                self._image_x, self._new_x = old_x, new_x
                self._show_frame()
                self.frames.append(now)
        elif offset != abs(self._image_x) or t >= 1.0:
            # Fallback: the image slides inside the layer; the vacated strip is cut out of the
            # region, so the real new desktop underneath shows through. The layer never moves.
            if self._direction > 0:
                self._image_x = -offset
                rgn = G.CreateRectRgn(0, 0, max(0, self._w - offset), self._h)
            else:
                self._image_x = offset
                rgn = G.CreateRectRgn(offset, 0, self._w, self._h)
            G.SetWindowRgn(self.hwnd, rgn, False)  # the system owns the region from now on
            G.InvalidateRect(self.hwnd, None, False)
            G.UpdateWindow(self.hwnd)  # draw now, in this refresh, not whenever WM_PAINT comes
            self._presented()
            self.frames.append(now)
        if t >= 1.0:
            self.destroy()  # also ends its timers

    def _live_frame(self, now: float) -> None:
        """The comic strip with the GPU capture: the old desktop and the live new one side by side.
        It moves from the first frame after the switch (the clock starts at slide_out); the new
        desktop joins once the screen shows it (until then its edge is dark: a few dozen pixels at the
        slow start). It is refreshed every frame, so a window that paints late (shown for the first
        time) is in the strip as soon as it has painted, never popping in at the end. The capture lost
        mid-slide (a display change takes it away: DXGI_ERROR_ACCESS_LOST): the slide goes on, the new
        desktop from a screenshot (or the last frame it had)."""
        if not self._capture_lost:
            try:
                if not self._live_strip:
                    self._start_live_strip(now)
                elif self._live.grab(0):
                    self._gpu.set_texture(1, self._live.texture)
            except OSError:
                gpu.drop_capture(self._device, failed=False)  # (opened again for the next slide)
                self._capture_lost = True
                if not self._live_strip:
                    self._add_screenshot_strip()
                if not self.hwnd:
                    return
        t = min(1.0, (now - self._started) * 1000.0 / DURATION_MS)
        old_x, new_x = strip_positions(int(round(ease(t) * self._w)), self._w, self._direction)
        self._image_x, self._new_x = old_x, new_x
        self._show_frame()
        self.frames.append(now)
        if t >= 1.0 and self.hwnd:
            self.destroy()  # also ends its timers

    def _bounce_frame(self, now: float) -> None:
        t = min(1.0, (now - self._started) * 1000.0 / BOUNCE_MS)
        x = bounce_offset(t, self._w, self._bounce)
        if x != self._image_x or t >= 1.0:
            self._image_x = x
            self._show_frame()
            self.frames.append(now)
        if t >= 1.0:
            self.destroy()  # also ends its timers

    def on_message(self, msg, wp, lp):
        if msg == WM_FRAME:
            pacer = self._pacer
            if pacer is not None:
                pacer.pending.clear()
                self._last_message = time.perf_counter()
                self._frame(pacer.vblank_at or self._last_message)
            return 0
        if msg == G.WM_PAINT:
            ps = G.PAINTSTRUCT()
            hdc = G.BeginPaint(self.hwnd, G.byref(ps))
            if self._gpu is not None:
                G.fill_rect(hdc, (0, 0, self._w, self._h), G.rgb(0, 0, 0))  # under the GPU pictures
            else:
                self.paint(hdc)
            G.EndPaint(self.hwnd, G.byref(ps))
            return 0
        if msg == G.WM_ERASEBKGND:
            return 1
        if msg == G.WM_MOUSEACTIVATE:
            return G.MA_NOACTIVATE
        return None

    @property
    def uses_gpu(self) -> bool:
        return self._gpu is not None

    def _show_frame(self) -> None:
        """Puts the current positions on screen: moves the GPU pictures, or redraws on the CPU."""
        if self._gpu is not None:
            try:
                self._gpu.set_offsets([self._image_x, self._new_x])
                self._gpu.commit()
                self._presented()
                return
            except OSError:
                self._gpu_failed()
                if not self.hwnd:
                    return  # (a live slide ends there: no window left to draw in)
        G.InvalidateRect(self.hwnd, None, False)
        G.UpdateWindow(self.hwnd)  # draw now, in this refresh, not whenever WM_PAINT comes
        self._presented()

    def _gpu_failed(self) -> None:
        """The GPU stopped working: continue on the CPU (the window repaints the pictures). Only this
        layer's own device is dropped (another slide may already use a newer one)."""
        owner = None
        if self._gpu is not None:
            owner = self._gpu.device
            self._gpu.close()
            self._gpu = None
        if owner is not None:
            gpu.reset(owner)
        if self._live is not None:
            self.destroy()  # (its pictures were on the GPU only: the switch is done, the slide just ends)
            return
        if self.hwnd:
            G.InvalidateRect(self.hwnd, None, False)

    def _presented(self) -> None:
        """Called after each frame reaches the screen (tests hook it to record frames)."""

    def paint(self, hdc: int) -> None:
        """Draws the current frame on the CPU: the window's content without the GPU, and what the GPU
        pictures show otherwise (tests record frames with it: the layer is left out of screen
        captures). Live pictures are read back from the GPU for it (slow: only tests draw them)."""
        if self._live is not None and self._gpu is not None:
            try:
                if not self._dc and self._old_tex:
                    self._dc, self._bmp, self._old = self._live.to_bitmap(self._old_tex)
                if self._live_strip and self._new_cpu_at != self._live.present:
                    if self._new_cpu is not None:
                        free_capture(self._new_cpu)
                    self._new_cpu = self._live.to_bitmap(self._live.texture)
                    self._new_cpu_at = self._live.present
            except OSError:
                return
        new = self._new if self._new is not None else self._new_cpu if self._live_strip else None
        if new is not None:
            # Comic strip: the old and the new desktop side by side, sliding together
            G.BitBlt(hdc, self._image_x, 0, self._w, self._h, self._dc, 0, 0, G.SRCCOPY)
            G.BitBlt(hdc, self._new_x, 0, self._w, self._h, new[0], 0, 0, G.SRCCOPY)
            return
        if not self._dc:
            return
        if self._image_x > 0:
            G.fill_rect(hdc, (0, 0, self._image_x, self._h), G.rgb(0, 0, 0))
        elif self._image_x < 0 and (self._bounce or self._live is not None):  # (the gap on the right is dark too)
            G.fill_rect(hdc, (self._w + self._image_x, 0, self._w, self._h), G.rgb(0, 0, 0))
        G.BitBlt(hdc, self._image_x, 0, self._w, self._h, self._dc, 0, 0, G.SRCCOPY)

    def on_destroyed(self) -> None:
        if self._pacer is not None:
            self._pacer.stop()
            self._pacer = None
        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        if self._dc:
            free_capture((self._dc, self._bmp, self._old))
            self._dc = self._bmp = 0
        if self._new is not None:
            free_capture(self._new)
            self._new = None
        if self._new_cpu is not None:
            free_capture(self._new_cpu)
            self._new_cpu = None
        self._old_tex = None  # (the capture's own: never released here)
        if self._live is not None:
            self._live.done()
        self._on_done(self)


class SlideAnimator:
    def __init__(self):
        self._active: dict[str, _Overlay] = {}

    def begin(self, device: str, direction: int) -> None:
        """Before the switch starts: captures the monitor and shows the layer."""
        self._show(device, direction)

    def bounce(self, device: str, direction: int) -> None:
        """No desktop further in that direction: a short bounce on that monitor."""
        o = self._show(device, direction)
        if o is not None:
            o.bounce(direction)

    def _show(self, device: str, direction: int) -> "_Overlay | None":
        self.cancel(device)
        scr = next((s for s in N.screens() if s.device == device), None)
        if scr is None:
            return None

        def done(o: _Overlay) -> None:
            if self._active.get(device) is o:
                del self._active[device]

        # On the GPU that drives the monitor: its latest frame, no screenshot through memory
        live = gpu.capture_for(device, open_now=False)  # (never an opening in the key path)
        if live is not None:
            try:
                live.grab(0)
            except OSError:  # (lost: a display change took it away; opened again when idle)
                gpu.drop_capture(device, failed=False)
                live = None
        if live is not None and (live.w, live.h) != (scr.bounds[2] - scr.bounds[0], scr.bounds[3] - scr.bounds[1]):
            if live.w:  # (no frame yet: size 0, kept; another size: the monitor changed, opened again when idle)
                gpu.drop_capture(device, failed=False)
            live = None
        if live is not None:
            try:
                o = _Overlay(scr.bounds, device, 0, 0, 0, direction, done, live=live)
                self._active[device] = o
                return o
            except OSError:
                gpu.drop_capture(device)  # (screenshots this time and for a while)
        cap = capture_screen(scr.bounds)
        if cap is None:  # capture failed: switch without animation
            return None
        dc, bmp, old = cap
        try:
            o = _Overlay(scr.bounds, device, dc, bmp, old, direction, done)
        except OSError:
            free_capture(cap)
            return None
        self._active[device] = o
        return o

    def commit(self, device: str) -> None:
        """The switch completed: start the slide."""
        o = self._active.get(device)
        if o is not None:
            o.slide_out()

    def cancel(self, device: str) -> None:
        o = self._active.pop(device, None)
        if o is not None:
            o.destroy()

    def overlay(self, device: str) -> "_Overlay | None":
        return self._active.get(device)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def dispose(self) -> None:
        for o in list(self._active.values()):
            o.destroy()
        self._active.clear()
