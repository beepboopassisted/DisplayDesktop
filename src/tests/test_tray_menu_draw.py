"""The owner-drawn tray menu items (tray_menu.py): measured and drawn into an off-screen bitmap
(nothing is shown on screen)."""
from __future__ import annotations

import ctypes
import unittest
from unittest import mock

from src import gui as G
from src import tray as T
from src import tray_menu as TM

ARROW = TM.Item(1, "Overview", "Ctrl+Alt+", "up")
PLAIN = TM.Item(2, "Settings")


def painter(dpi: int = 96) -> TM.MenuPainter:
    p = TM.MenuPainter(0, [ARROW, PLAIN], dpi)  # no window: the plain (unthemed) background is used
    return p


def measure(p: TM.MenuPainter, item_id: int) -> tuple[int, int]:
    mis = TM.MEASUREITEMSTRUCT(TM.ODT_MENU, 0, item_id, 0, 0, 0)
    assert p.measure(ctypes.addressof(mis))
    return mis.itemWidth, mis.itemHeight


def render(p: TM.MenuPainter, item_id: int, w: int, h: int, hot: bool = False) -> list[list[int]]:
    """Draws one item into a w x h bitmap; returns rows of 0xRRGGBB pixels."""
    dc = G.CreateCompatibleDC(0)
    bmp, bits = G.create_dib(w, h)
    old = G.SelectObject(dc, bmp)
    try:
        G.fill_rect(dc, (0, 0, w, h), G.rgb(255, 0, 255))  # anything not drawn stays magenta
        dis = TM.DRAWITEMSTRUCT(TM.ODT_MENU, 0, item_id, 0, TM.ODS_SELECTED if hot else 0, 0, dc,
                                G.N.RECT(0, 0, w, h), 0)
        assert p.draw(ctypes.addressof(dis))
        px = (ctypes.c_uint32 * (w * h)).from_address(bits)
        return [[px[y * w + x] & 0xFFFFFF for x in range(w)] for y in range(h)]
    finally:
        G.SelectObject(dc, old)
        G.DeleteObject(bmp)
        G.DeleteDC(dc)


def dark(c: int) -> bool:
    return sum((c >> s) & 0xFF for s in (0, 8, 16)) < 3 * 128


class MenuItems(unittest.TestCase):
    def setUp(self):
        self.p = painter()
        self.addCleanup(self.p.close)

    def test_sizes(self):
        aw, ah = measure(self.p, ARROW.id)
        pw, ph = measure(self.p, PLAIN.id)
        self.assertGreater(aw, pw, "the shortcut column makes the item wider")
        self.assertEqual(ah, ph)
        _, sh = measure(self.p, TM.SEPARATOR_ID)
        self.assertLess(sh, ph, "a divider is thinner than an item")
        big = painter(192)
        self.addCleanup(big.close)
        self.assertEqual(measure(big, PLAIN.id)[1], ph * 2, "sizes follow the monitor's DPI")

    def test_width_fits_the_text_snugly(self):
        """No fixed minimum: the item is the text plus the insets (and the gap before a shortcut)."""
        dc = G.CreateCompatibleDC(0)
        try:
            label = self.p._width(dc, ARROW.label, self.p.font)
            keys = self.p._shortcut_width(dc, ARROW)
            plain = self.p._width(dc, PLAIN.label, self.p.font)
        finally:
            G.DeleteDC(dc)
        inset = self.p.s(TM.TEXT_LEFT) + self.p.s(TM.TEXT_RIGHT)
        self.assertEqual(measure(self.p, ARROW.id)[0], inset + label + self.p.s(TM.SHORTCUT_GAP) + keys)
        self.assertEqual(measure(self.p, PLAIN.id)[0], inset + plain)
        self.assertLessEqual(TM.SHORTCUT_GAP, 24, "a short gap: shorter labels give a narrower menu")

    def test_label_left_shortcut_and_arrow_right(self):
        w, h = 260, measure(self.p, ARROW.id)[1]
        rows = render(self.p, ARROW.id, w, h)
        self.assertFalse(any(c == 0xFF00FF for r in rows for c in r), "the whole item is painted")
        ink = [x for r in rows for x, c in enumerate(r) if dark(c)]
        self.assertTrue(ink and min(ink) >= self.p.s(TM.TEXT_LEFT) - 1, "label starts at the text inset")
        right_edge = w - self.p.s(TM.TEXT_RIGHT)
        self.assertTrue(any(right_edge - self.p.s(14) <= x <= right_edge for x in ink), "ink at the right edge")
        with mock.patch.object(TM, "arrow_glyph", return_value=(" ", False)):  # same item, no arrow
            no_arrow = render(self.p, ARROW.id, w, h)
        cols = range(right_edge - self.p.s(10), right_edge)
        self.assertTrue(any(rows[y][x] != no_arrow[y][x] for y in range(h) for x in cols),
                        "the arrow is drawn at the right edge")

    def test_icon_font_arrows_with_a_fallback(self):
        self.assertIn(TM.icon_face(), TM.ICON_FONTS + ("",))
        with mock.patch.object(TM, "_icon_face", ""):
            self.assertEqual(TM.arrow_glyph("up"), ("↑", False))
        with mock.patch.object(TM, "_icon_face", "Segoe Fluent Icons"):
            self.assertEqual(TM.arrow_glyph("left"), ("", True))

    def test_hover_highlights_the_item(self):
        w, h = 260, measure(self.p, PLAIN.id)[1]
        normal = render(self.p, PLAIN.id, w, h)
        hot = render(self.p, PLAIN.id, w, h, hot=True)
        y, x = h // 2, w - 30  # inside the highlight, away from the text
        self.assertNotEqual(normal[y][x], hot[y][x])

    def test_divider_is_a_thin_line(self):
        w, h = 200, measure(self.p, TM.SEPARATOR_ID)[1]
        rows = render(self.p, TM.SEPARATOR_ID, w, h)
        lines = [y for y, r in enumerate(rows) if r[w // 2] != rows[0][w // 2]]
        self.assertTrue(1 <= len(lines) <= 2, f"one thin line ({lines})")

    def test_tray_window_passes_the_menu_messages_to_the_painter(self):
        p = mock.Mock()
        p.measure.return_value = p.draw.return_value = True
        app = mock.Mock(_menu_painter=p)
        self.assertEqual(T.TrayApp.on_message(app, TM.WM_MEASUREITEM, 0, 123), 1)
        self.assertEqual(T.TrayApp.on_message(app, TM.WM_DRAWITEM, 0, 456), 1)
        p.measure.assert_called_once_with(123)
        p.draw.assert_called_once_with(456)
        app._menu_painter = None  # no menu open: default handling
        self.assertIsNone(T.TrayApp.on_message(app, TM.WM_DRAWITEM, 0, 456))


if __name__ == "__main__":
    unittest.main()
