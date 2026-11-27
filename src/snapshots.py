"""Last-seen images of windows, used as previews in the overview for windows on inactive desktops.
Windows cannot draw live previews of hidden windows, so each window is captured (PrintWindow) just
before it is hidden. Images are scaled down (longest side 480 px) and kept in memory only: never
written to disk or sent anywhere.

Captures are taken in the background (`refresh`: one worker thread, one window at a time), so a
switch rarely waits on one: the manager decides which windows and when, and installs the results on
its own thread (`collect`); the store itself is only changed on that thread.
"""
from __future__ import annotations

import ctypes
import threading
import time

from . import gui as G
from . import native as N

MAX_SIDE = 480
REFRESH_GAP_S = 0.05  # pause between two background captures


class Shot:
    __slots__ = ("hbm", "w", "h", "at")

    def __init__(self, hbm: int, w: int, h: int, at: float = 0.0):
        self.hbm, self.w, self.h, self.at = hbm, w, h, at  # at: time.monotonic() of the capture


def _looks_blank(bits: int, stride_px: int, crop) -> bool:
    """Some apps answer PrintWindow with an all-black (or empty) image; treat it as no preview.
    Samples an 8x8 grid. Alpha is ignored: GDI rendering leaves it 0 even for real content."""
    left, top, w, h = crop
    steps = 8
    buf = ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint32))
    for i in range(steps):
        for j in range(steps):
            x = left + w * (2 * i + 1) // (2 * steps)
            y = top + h * (2 * j + 1) // (2 * steps)
            if buf[y * stride_px + x] & 0x00FFFFFF:
                return False
    return True


class WindowSnapshots:
    def __init__(self):
        self._shots: dict[int, Shot] = {}
        self._failed: dict[int, float] = {}  # window -> time.monotonic() its last capture gave nothing
        self._lock = threading.Lock()
        self._wanted: list[int] = []  # background captures still to take
        self._done: list[tuple[int, float, "Shot | None"]] = []  # (window, perf_counter at its start, shot)
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._hold_until = 0.0  # perf_counter: no background capture starts before (a slide runs)

    def get(self, h: int) -> Shot | None:
        return self._shots.get(h)

    def failed_recently(self, h: int, within_s: float) -> bool:
        """The window's last capture, less than `within_s` ago, gave no preview."""
        t = self._failed.get(h)
        return t is not None and time.monotonic() - t < within_s

    def capture(self, h: int) -> None:
        """Captures the window's current content now. Minimized and hung windows are skipped."""
        self._store(h, self.take(h))

    def _store(self, h: int, shot: Shot | None) -> None:
        if shot is None:
            self._failed[h] = time.monotonic()
            return
        self._failed.pop(h, None)
        prev = self._shots.pop(h, None)
        if prev:
            G.DeleteObject(prev.hbm)
        self._shots[h] = shot

    def refresh(self, hs: list[int]) -> None:
        """Background: captures these windows one by one on the worker thread (replaces the list not
        taken yet). The results wait for `collect`."""
        with self._lock:
            self._wanted = list(hs)
        if hs:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="DisplayDesktop previews", daemon=True)
                self._thread.start()
            self._wake.set()

    def hold(self, seconds: float) -> None:
        """No background capture starts for that long (a slide: its frames must not wait on DWM)."""
        self._hold_until = max(self._hold_until, time.perf_counter() + seconds)

    def collect(self, keep) -> None:
        """Installs the background captures finished so far for which keep(h, started) is true (the
        window was not hidden meanwhile: a capture of a window being hidden can be half drawn)."""
        with self._lock:
            done, self._done = self._done, []
        for h, started, shot in done:
            if keep(h, started):
                self._store(h, shot)
            elif shot is not None:
                G.DeleteObject(shot.hbm)

    def _work(self) -> None:
        while True:
            self._wake.wait()
            wait = self._hold_until - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
                continue
            with self._lock:
                if not self._wanted:
                    self._wake.clear()
                    continue
                h = self._wanted.pop(0)
            started = time.perf_counter()
            shot = self.take(h)
            with self._lock:
                self._done.append((h, started, shot))
            time.sleep(REFRESH_GAP_S)

    def take(self, h: int) -> Shot | None:
        """The window's current content as a new preview (None: minimized, hung, blank or failed).
        Any thread."""
        if not N.is_window_visible(h) or N.is_iconic(h) or N.is_hung(h):
            return None
        wr, fr = N.get_window_rect(h), N.get_visible_frame(h)
        if wr is None or fr is None:
            return None
        ww, wh = wr[2] - wr[0], wr[3] - wr[1]
        if ww <= 0 or wh <= 0 or ww > 16384 or wh > 16384:
            return None

        screen = G.GetDC(0)
        mem = G.CreateCompatibleDC(screen)
        small_dc = G.CreateCompatibleDC(screen)
        G.ReleaseDC(0, screen)
        full, bits = G.create_dib(ww, wh)
        small = 0
        try:
            if not full or not bits:
                return None
            old = G.SelectObject(mem, full)
            try:
                if not N.print_window(h, mem):
                    return None
                # Crop the invisible resize borders, keep only the visible frame
                cl, ct = max(0, fr[0] - wr[0]), max(0, fr[1] - wr[1])
                cr, cb = min(ww, fr[2] - wr[0]), min(wh, fr[3] - wr[1])
                cw, chh = cr - cl, cb - ct
                if cw <= 0 or chh <= 0 or _looks_blank(bits, ww, (cl, ct, cw, chh)):
                    return None
                scale = min(1.0, MAX_SIDE / max(cw, chh))
                sw, sh = max(1, int(cw * scale)), max(1, int(chh * scale))
                # (kept while the window is hidden: 24-bit, a quarter less memory than 32-bit; only its
                # colours are ever drawn, so the preview looks the same)
                small, _ = G.create_dib(sw, sh, 24)
                if not small:
                    return None
                old_small = G.SelectObject(small_dc, small)
                G.SetStretchBltMode(small_dc, G.HALFTONE)
                G.SetBrushOrgEx(small_dc, 0, 0, None)
                G.StretchBlt(small_dc, 0, 0, sw, sh, mem, cl, ct, cw, chh, G.SRCCOPY)
                G.SelectObject(small_dc, old_small)
            finally:
                G.SelectObject(mem, old)
            shot = Shot(small, sw, sh, time.monotonic())
            small = 0  # the caller's now
            return shot
        except Exception:  # noqa: BLE001 - a missing preview is fine (icon fallback)
            return None
        finally:
            if small:
                G.DeleteObject(small)
            if full:
                G.DeleteObject(full)
            G.DeleteDC(mem)
            G.DeleteDC(small_dc)

    def purge(self, is_window=N.is_window) -> None:
        """Forgets snapshots of windows that no longer exist."""
        for h in [h for h in self._shots if not is_window(h)]:
            G.DeleteObject(self._shots.pop(h).hbm)
        for h in [h for h in self._failed if not is_window(h)]:
            del self._failed[h]

    def clear(self) -> None:
        self.refresh([])
        self.collect(lambda h, started: False)
        for s in self._shots.values():
            G.DeleteObject(s.hbm)
        self._shots.clear()
        self._failed.clear()
