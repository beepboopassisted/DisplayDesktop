"""native.py's fast window queries (kept-GIL DLL objects, c_void_p handles, a builtin EnumWindows
callback) give the same answers as plain WinDLL calls (a hidden window of the test; nothing shown)."""
from __future__ import annotations

import ctypes
import os
import threading
import unittest

from src import gui as G
from src import native as N

_ref = ctypes.WinDLL(os.path.join(N._SYS32, "user32.dll"), use_last_error=True)


def _ref_fn(name, restype, *argtypes):
    return N._f(_ref, name, restype, *argtypes)


_RefEnum = _ref_fn("EnumWindows", N.BOOL, N.WNDENUMPROC, N.LPARAM)
_RefVisible = _ref_fn("IsWindowVisible", N.BOOL, N.H)
_RefIsWindow = _ref_fn("IsWindow", N.BOOL, N.H)
_RefStyle = _ref_fn("GetWindowLongPtrW", ctypes.c_ssize_t, N.H, ctypes.c_int)
_RefAncestor = _ref_fn("GetAncestor", N.H, N.H, N.UINT)
_RefHung = _ref_fn("IsHungAppWindow", N.BOOL, N.H)


def ref_enum() -> list[int]:
    out = []

    def cb(h, _):
        out.append(h)
        return 1
    _RefEnum(N.WNDENUMPROC(cb), 0)
    return out


class Hidden(G.Window):
    def __init__(self):
        super().__init__()
        self.create("DisplayDesktop.NativeTest", 0, N.WS_POPUP, -32000, -32000, 10, 10, "Native test")


class DisplayChange(unittest.TestCase):
    def test_a_windows_monitor_is_always_asked_of_windows(self):
        # (a monitor handle may name another display right after a change: never a remembered answer)
        from unittest import mock
        N.screens()
        with mock.patch.object(N, "_MonitorFromWindow", return_value=123), \
                mock.patch.object(N, "_monitor_device", return_value="NOW") as asked:
            self.assertEqual(N.monitor_device_of_window(1), "NOW")
        asked.assert_called_once_with(123)
        N.displays_changed()
        self.assertEqual(N._device_hmon, {})

    def test_the_tray_forgets_it_on_wm_displaychange(self):
        from unittest import mock
        from src import tray as T
        with mock.patch.object(T.N, "displays_changed") as changed:
            T.TrayApp.on_message(object.__new__(T.TrayApp), T.WM_DISPLAYCHANGE, 32, 0)
        changed.assert_called_once()


class FastQueries(unittest.TestCase):
    def setUp(self):
        self.win = Hidden()
        self.addCleanup(self.win.destroy)
        self.h = self.win.hwnd

    def test_enum_windows_is_the_z_order_list_of_plain_ints(self):
        for _ in range(5):  # (another program may open or close a window between the two lists)
            ours, ref = N.enum_windows(), ref_enum()
            if ours == ref:
                break
        self.assertEqual(ours, ref)
        self.assertIn(self.h, ours)
        self.assertTrue(all(type(h) is int for h in ours))
        self.assertEqual(len(ours), len(set(ours)))

    def test_answers_match_plain_calls(self):
        for h in N.enum_windows() + [self.h]:
            self.assertEqual(bool(N.is_window_visible(h)), bool(_RefVisible(h)), h)
            self.assertEqual(N.get_exstyle(h), _RefStyle(h, N.GWL_EXSTYLE) & 0xFFFFFFFF, h)
            self.assertEqual(N.get_style(h), _RefStyle(h, N.GWL_STYLE) & 0xFFFFFFFF, h)
            self.assertEqual(N.get_root(h), _RefAncestor(h, N.GA_ROOT), h)

    def test_own_window(self):
        h = self.h
        self.assertTrue(N.is_window(h))
        self.assertFalse(N.is_window_visible(h))
        self.assertEqual(N.get_pid(h), os.getpid())
        self.assertEqual(N.get_class(h), "DisplayDesktop.NativeTest")
        self.assertEqual(N.get_title(h), "Native test")
        self.assertEqual(N.title_length(h), len("Native test"))
        self.assertEqual(N.get_root(h), h)
        self.assertEqual(N.get_root_anchor(h), h)
        self.assertEqual(bool(N.is_hung(h)), bool(_RefHung(h)))  # (a test thread pumps no messages: may be "hung")
        self.assertIsInstance(N.monitor_device_of_window(h), str)
        self.assertGreater(N.tick_count64(), 0)

    def test_bad_handles(self):
        for h in (0, -1, 2 ** 40):
            self.assertFalse(N.is_window(h))
            self.assertFalse(N.is_window_visible(h))
            self.assertEqual(N.get_pid(h), 0)
            self.assertEqual(N.get_class(h), "")
        self.win.destroy()
        self.assertFalse(N.is_window(self.h))
        self.assertFalse(_RefIsWindow(self.h))

    def test_enumerating_from_threads_at_once(self):
        # (the kept GIL never blocks another thread for long, and each call gets its own list)
        results, errors = [], []

        def run():
            try:
                for _ in range(50):
                    results.append(self.h in N.enum_windows())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertFalse(errors)
        self.assertEqual(results, [True] * 200)


if __name__ == "__main__":
    unittest.main()
