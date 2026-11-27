"""Preview snapshots (snapshots.py): kept as 24-bit bitmaps (a quarter less memory than 32-bit) that
the Overview draws exactly like 32-bit ones (off-screen; the window capture is a stand-in)."""
from __future__ import annotations

import ctypes
import random
import unittest
from unittest import mock

from src import gui as G
from src import native as N
from src import snapshots as S


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


_GetObjectW = N._f(N.gdi32, "GetObjectW", ctypes.c_int, N.H, ctypes.c_int, ctypes.c_void_p)
_GetDIBits = N._f(N.gdi32, "GetDIBits", ctypes.c_int, N.H, N.H, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                  ctypes.POINTER(G.BITMAPINFO), ctypes.c_uint)
W, H = 1000, 640  # the captured window (the visible frame is inset by its borders)
FRAME = (7, 0, W - 7, H - 7)


def colours(hbm: int, w: int, h: int) -> bytes:
    """The bitmap's pixels as BGRA bytes with alpha zeroed (colours only; no Python loop per pixel)."""
    bi = G.BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(G.BITMAPINFOHEADER)
    bi.bmiHeader.biWidth, bi.bmiHeader.biHeight = w, -h
    bi.bmiHeader.biPlanes, bi.bmiHeader.biBitCount = 1, 32
    buf = (ctypes.c_uint32 * (w * h))()
    dc = G.CreateCompatibleDC(0)
    try:
        assert _GetDIBits(dc, hbm, 0, h, buf, ctypes.byref(bi), 0) == h
    finally:
        G.DeleteDC(dc)
    raw = bytearray(buf)
    raw[3::4] = bytes(w * h)  # (alpha left out)
    return bytes(raw)


def drawn(hbm: int, w: int, h: int, size: tuple[int, int]) -> bytes:
    """The preview drawn the Overview's way: HALFTONE StretchBlt into a screen-compatible back buffer."""
    screen = G.GetDC(0)
    back = G.CreateCompatibleBitmap(screen, size[0] + 8, size[1] + 8)
    dc = G.CreateCompatibleDC(screen)
    src = G.CreateCompatibleDC(screen)
    G.ReleaseDC(0, screen)
    old, old_src = G.SelectObject(dc, back), G.SelectObject(src, hbm)
    try:
        G.fill_rect(dc, (0, 0, size[0] + 8, size[1] + 8), G.rgb(32, 32, 32))
        G.SetStretchBltMode(dc, G.HALFTONE)
        G.SetBrushOrgEx(dc, 0, 0, None)
        G.StretchBlt(dc, 4, 4, size[0], size[1], src, 0, 0, w, h, G.SRCCOPY)
    finally:
        G.SelectObject(src, old_src)
        G.SelectObject(dc, old)
        G.DeleteDC(src)
        G.DeleteDC(dc)
    try:
        return colours(back, size[0] + 8, size[1] + 8)
    finally:
        G.DeleteObject(back)


class Snapshots(unittest.TestCase):
    def setUp(self):
        # The window's content: noise in every byte (alpha too), so the scaling has real work everywhere
        self.src, bits = G.create_dib(W, H)
        self.addCleanup(G.DeleteObject, self.src)
        ctypes.memmove(bits, random.Random(7).randbytes(W * H * 4), W * H * 4)
        self.snaps = S.WindowSnapshots()
        self.addCleanup(self.snaps.clear)

    def _print(self, _h, dc) -> bool:
        src = G.CreateCompatibleDC(dc)
        old = G.SelectObject(src, self.src)
        G.BitBlt(dc, 0, 0, W, H, src, 0, 0, G.SRCCOPY)
        G.SelectObject(src, old)
        G.DeleteDC(src)
        return True

    def capture(self, printer=None) -> S.Shot | None:
        with mock.patch.object(S.N, "is_window_visible", return_value=1), \
                mock.patch.object(S.N, "is_iconic", return_value=0), mock.patch.object(S.N, "is_hung", return_value=0), \
                mock.patch.object(S.N, "get_window_rect", return_value=(0, 0, W, H)), \
                mock.patch.object(S.N, "get_visible_frame", return_value=FRAME), \
                mock.patch.object(S.N, "print_window", side_effect=printer or self._print):
            self.snaps.capture(42)
        return self.snaps.get(42)

    def test_kept_as_24_bit_and_drawn_like_a_32_bit_one(self):
        shot = self.capture()
        self.assertIsNotNone(shot)
        fw, fh = FRAME[2] - FRAME[0], FRAME[3] - FRAME[1]
        self.assertEqual((shot.w, shot.h), (S.MAX_SIDE, int(fh * S.MAX_SIDE / fw)), "scaled to MAX_SIDE, frame only")
        bm = _BITMAP()
        self.assertTrue(_GetObjectW(shot.hbm, ctypes.sizeof(bm), ctypes.byref(bm)))
        self.assertEqual((bm.bmWidth, bm.bmHeight, bm.bmBitsPixel), (shot.w, shot.h, 24))
        # The same capture scaled into a 32-bit bitmap, as before: the same colours, and drawn the same
        ref, _ = G.create_dib(shot.w, shot.h)
        self.addCleanup(G.DeleteObject, ref)
        full, small = G.CreateCompatibleDC(0), G.CreateCompatibleDC(0)
        o1, o2 = G.SelectObject(full, self.src), G.SelectObject(small, ref)
        G.SetStretchBltMode(small, G.HALFTONE)
        G.SetBrushOrgEx(small, 0, 0, None)
        G.StretchBlt(small, 0, 0, shot.w, shot.h, full, FRAME[0], FRAME[1], fw, fh, G.SRCCOPY)
        G.SelectObject(full, o1)
        G.SelectObject(small, o2)
        G.DeleteDC(full)
        G.DeleteDC(small)
        self.assertEqual(colours(shot.hbm, shot.w, shot.h), colours(ref, shot.w, shot.h))
        for size in ((240, 150), (333, 208), (shot.w, shot.h), (611, 381), (157, 98)):
            self.assertEqual(drawn(shot.hbm, shot.w, shot.h, size), drawn(ref, shot.w, shot.h, size), size)

    def test_a_blank_capture_is_no_preview(self):
        def black(_h, dc):
            G.fill_rect(dc, (0, 0, W, H), G.rgb(0, 0, 0))
            return True
        self.assertIsNone(self.capture(black))
        self.assertIsNone(self.capture(lambda _h, _dc: False), "PrintWindow failed")

    def test_a_new_capture_replaces_the_old_one_and_closed_windows_are_purged(self):
        first = self.capture().hbm
        second = self.capture().hbm
        bm = _BITMAP()
        self.assertEqual(_GetObjectW(first, ctypes.sizeof(bm), ctypes.byref(bm)), 0, "the old bitmap is deleted")
        self.snaps.purge(lambda h: False)
        self.assertIsNone(self.snaps.get(42))
        self.assertEqual(_GetObjectW(second, ctypes.sizeof(bm), ctypes.byref(bm)), 0, "deleted with its window")


if __name__ == "__main__":
    unittest.main()
