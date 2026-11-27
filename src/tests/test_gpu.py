"""gpu.py (DirectComposition layers for the slide): which GPU (the one driving each monitor), device life
cycle, layers on a hidden window (nothing is shown), the screen capture, the off switch and failures.
On-screen pixels are checked in live_ui.py."""
from __future__ import annotations

import unittest
from unittest import mock

import ctypes
import time
from types import SimpleNamespace

from src import gpu
from src import gui as G
from src import native as N

A = gpu.Adapter


class Hidden(G.Window):
    pass


def picture(w: int, h: int, color: int):
    dc = G.CreateCompatibleDC(0)
    bmp, _ = G.create_dib(w, h)
    old = G.SelectObject(dc, bmp)
    G.fill_rect(dc, (0, 0, w, h), color)
    return dc, bmp, old


def free(p) -> None:
    dc, bmp, old = p
    G.SelectObject(dc, old)
    G.DeleteObject(bmp)
    G.DeleteDC(dc)


def _primary() -> str | None:
    """The monitor at the screen's origin (where the hidden test windows are)."""
    return N.monitor_device_of_window(0) if N.screens() else None


@unittest.skipUnless(gpu.available(), "no GPU / DirectComposition on this machine")
class Layers(unittest.TestCase):
    """(A monitor's GPU devices are kept from test to test: making them takes ~0.1 s. Tests that need
    new ones reset them themselves.)"""

    @classmethod
    def tearDownClass(cls):
        gpu.reset()

    def setUp(self):
        self.win = Hidden()
        self.win.create("DisplayDesktop.GpuTest", N.WS_EX_TOOLWINDOW, N.WS_POPUP, 0, 0, 64, 32)  # never shown
        self.addCleanup(self.win.destroy)
        self.mon = N.monitor_device_of_window(self.win.hwnd)
        self.pics = [picture(64, 32, G.rgb(255, 0, 0)), picture(64, 32, G.rgb(0, 0, 255))]
        self.addCleanup(lambda: [free(p) for p in self.pics])

    def test_pictures_move_and_commit(self):
        layer = gpu.Layer(self.win.hwnd)
        self.addCleanup(layer.close)
        self.assertEqual(layer.device, gpu.devices_for(self.mon), "on the GPU that drives the window's monitor")
        self.assertEqual(layer.add(self.pics[0][0], 64, 32), 0)
        self.assertEqual(layer.add(self.pics[1][0], 64, 32, x=64), 1)
        self.assertEqual(layer.count, 2)
        for x in (0, -10, -32, -64):
            layer.set_offsets([x, x + 64])
            layer.commit()
        layer.commit(wait=True)

    def test_close_is_safe_twice_and_frees_everything(self):
        layer = gpu.Layer(self.win.hwnd)
        layer.add(self.pics[0][0], 64, 32)
        layer.close()
        layer.close()
        self.assertEqual(layer.count, 0)

    def test_many_layers_reuse_one_device(self):
        first = gpu.devices_for(self.mon)
        for _ in range(25):
            layer = gpu.Layer(self.win.hwnd)
            layer.add(self.pics[0][0], 64, 32)
            layer.commit()
            layer.close()
        self.assertIs(gpu.devices_for(self.mon), first, "one device per GPU, not one per animation")

    def test_reset_creates_a_new_device_next_time(self):
        before = gpu.devices_for(self.mon)
        gpu.reset()
        self.assertEqual(gpu._devices, {})
        self.assertTrue(gpu.available(self.mon))
        self.assertIsNot(gpu.devices_for(self.mon), before)

    def second_window(self) -> int:
        """Another hidden window (each slide has its own; a window takes one GPU target only)."""
        other = Hidden()
        other.create("DisplayDesktop.GpuTest", N.WS_EX_TOOLWINDOW, N.WS_POPUP, 0, 0, 64, 32)
        self.addCleanup(other.destroy)
        return other.hwnd

    def test_a_reset_never_releases_the_device_under_a_running_slide(self):
        # two slides; the devices are dropped (a failure elsewhere, a settings change)
        first, second = gpu.Layer(self.win.hwnd), gpu.Layer(self.second_window())
        owner = first.device
        first.add(self.pics[0][0], 64, 32)
        second.add(self.pics[1][0], 64, 32)
        gpu.reset()
        self.assertNotIn(owner, gpu._devices.values(), "no longer in use for new slides")
        self.assertTrue(owner.retired and owner.dcomp, "but still alive: two layers use it")
        for layer in (first, second):
            layer.set_offsets([-10])
            layer.commit(wait=True)  # still works
        first.close()
        self.assertTrue(owner.dcomp, "one layer left")
        second.commit()
        second.close()
        self.assertFalse(owner.dcomp, "released with its last layer")

    def test_a_failed_layer_drops_only_its_own_device(self):
        old = gpu.Layer(self.win.hwnd)
        old_device = old.device
        gpu.reset()                      # e.g. the setting turned off and on
        newer = gpu.Layer(self.second_window())  # the next slide: a new device
        self.assertIsNot(newer.device, old_device)
        gpu.reset(old_device)            # the old slide fails afterwards
        self.assertIs(gpu._devices[newer.device.gpu.luid], newer.device, "the newer, working device stays")
        old.close()
        self.assertFalse(old_device.dcomp)
        newer.close()

    def test_a_failing_call_raises_oserror(self):
        layer = gpu.Layer(self.win.hwnd)
        self.addCleanup(layer.close)
        with self.assertRaises(OSError):
            layer.add(self.pics[0][0], 0, 0)  # a 0x0 surface is refused by DirectComposition


class MonitorGpu(unittest.TestCase):
    """Which GPU: always the one that drives the monitor (no setting); one device per GPU."""
    big, other = A("Big", "b", 2), A("Other", "o", 3)

    def setUp(self):
        gpu._gpu_of.clear()
        self.addCleanup(gpu._gpu_of.clear)
        gpu.reset()
        self.addCleanup(gpu.reset)
        gpu._failed_at.clear()
        self.addCleanup(gpu._failed_at.clear)

    def patched(self):
        return (mock.patch.object(gpu, "_monitor_gpus", return_value={r"\\.\display1": 2, r"\\.\display2": 2,
                                                                       r"\\.\display3": 9}),
                mock.patch.object(gpu, "adapters", return_value=[self.other, self.big]))

    def test_each_monitor_uses_the_gpu_that_drives_it(self):
        a, b = self.patched()
        with a, b:
            self.assertEqual(gpu.gpu_of(r"\\.\DISPLAY1"), self.big)
            self.assertEqual(gpu.gpu_of(r"\\.\DISPLAY2"), self.big)
            self.assertIsNone(gpu.gpu_of(r"\\.\DISPLAY3"), "a GPU that can't draw (virtual display): the CPU")
            self.assertIsNone(gpu.gpu_of(r"\\.\DISPLAY4"), "no GPU output: the CPU")

    def test_one_device_per_gpu_and_none_without_a_gpu(self):
        made = []
        a, b = self.patched()
        with a, b, mock.patch.object(gpu, "_Devices", side_effect=lambda g: made.append(g) or SimpleNamespace(
                gpu=g, layers=0, retired=False, retire=lambda: None)):
            d1, d2 = gpu.devices_for(r"\\.\DISPLAY1"), gpu.devices_for(r"\\.\DISPLAY2")
            self.assertIs(d1, d2, "two monitors on one GPU share its device")
            self.assertEqual(made, [self.big])
            self.assertIsNone(gpu.devices_for(r"\\.\DISPLAY3"))
        with mock.patch.object(gpu, "ENABLED", False):
            self.assertIsNone(gpu.devices_for(r"\\.\DISPLAY1"))

    def test_the_mapping_is_asked_again_after_a_display_change(self):
        a, b = self.patched()
        with a as ask, b, mock.patch.object(gpu, "warm_captures"):
            gpu.gpu_of(r"\\.\DISPLAY1")
            gpu.gpu_of(r"\\.\DISPLAY1")
            self.assertEqual(ask.call_count, 1, "remembered")
            gpu.displays_changed()
            gpu.gpu_of(r"\\.\DISPLAY1")
            self.assertEqual(ask.call_count, 2)

    def test_a_failed_device_is_not_tried_again_until_the_setting_changes_or_a_minute_passes(self):
        tries = []
        a, b = self.patched()
        with a, b, mock.patch.object(gpu, "warm_captures"), \
                mock.patch.object(gpu, "_Devices", side_effect=lambda g: (tries.append(g), (_ for _ in ()).throw(
                    OSError("no device")))[1]):
            for _ in range(5):
                self.assertIsNone(gpu.devices_for(r"\\.\DISPLAY1"))  # five switches
            self.assertEqual(len(tries), 1, "tried once, not on every switch")
            with mock.patch.object(gpu.N, "screens", return_value=[SimpleNamespace(device=r"\\.\DISPLAY1")]):
                gpu.set_enabled(True)  # the setting turned on again
            self.assertEqual(len(tries), 2, "a settings change tries again at once")
            with mock.patch.object(gpu.time, "monotonic", return_value=time.monotonic() + gpu.RETRY_AFTER_S + 1):
                gpu.devices_for(r"\\.\DISPLAY1")
            self.assertEqual(len(tries), 3, "and so does the next switch after a minute")

    def test_a_layer_that_fails_pauses_that_gpu_instead_of_making_a_device_every_switch(self):
        made = []
        a, b = self.patched()
        with a, b, mock.patch.object(gpu, "_Devices", side_effect=lambda g: made.append(g) or SimpleNamespace(
                gpu=g, layers=0, retired=False, retire=lambda: None)), \
                mock.patch.object(gpu, "Layer", side_effect=OSError("no composition target")):
            from src import slide as S
            def overlay():
                return SimpleNamespace(hwnd=0, _dc=0, _w=8, _h=8, _gpu=None, _device=r"\\.\DISPLAY1")
            for _ in range(5):  # five switches; each overlay tries the GPU
                ov = overlay()
                S._Overlay._put_on_gpu(ov)
                self.assertIsNone(ov._gpu, "the slide is drawn on the CPU")
            self.assertEqual(len(made), 1, "one device, not one per switch")
            with mock.patch.object(gpu.time, "monotonic", return_value=time.monotonic() + gpu.RETRY_AFTER_S + 1):
                S._Overlay._put_on_gpu(overlay())
            self.assertEqual(len(made), 2, "tried again after a minute")


class ScreenCapture(unittest.TestCase):
    """The GPU capture of a monitor (Desktop Duplication): only with the GPU on; a failure is not tried
    again for a while; a settings or display change closes them all."""

    def setUp(self):
        self.addCleanup(gpu.drop_captures)
        gpu.drop_captures()

    def test_never_with_the_gpu_off(self):
        with mock.patch.object(gpu, "ENABLED", False), mock.patch.object(gpu, "Capture") as cap:
            self.assertIsNone(gpu.capture_for(r"\\.\DISPLAY1"))
        cap.assert_not_called()

    def test_a_failure_waits_and_a_settings_change_tries_again(self):
        with mock.patch.object(gpu, "Capture", side_effect=OSError("no output")) as cap:
            self.assertIsNone(gpu.capture_for(r"\\.\DISPLAY1"))
            self.assertIsNone(gpu.capture_for(r"\\.\DISPLAY1"))
            self.assertEqual(cap.call_count, 1, "not tried again at once")
            gpu.drop_captures()
            gpu.capture_for(r"\\.\DISPLAY1")
            self.assertEqual(cap.call_count, 2, "a settings or display change: tried again")

    def test_one_capture_per_monitor_kept_open(self):
        made = mock.Mock()
        made.grab.return_value = True
        with mock.patch.object(gpu, "Capture", return_value=made) as cap:
            self.assertIs(gpu.capture_for(r"\\.\DISPLAY1"), made)
            self.assertIs(gpu.capture_for(r"\\.\DISPLAY1"), made)
        self.assertEqual(cap.call_count, 1)
        gpu.drop_captures()
        made.retire.assert_called_once()

    def test_a_display_change_opens_nothing_and_lost_captures_reopen_later_one_per_idle_tick(self):
        # (a full-screen app changes the display mode at each switch to or from it: opening every
        # monitor's capture then held the slide still for most of a second)
        screens = [SimpleNamespace(device=r"\\.\DISPLAY1"), SimpleNamespace(device=r"\\.\DISPLAY2")]
        opened = []

        def made(name):
            m = mock.Mock(device_name=name)
            opened.append(name)
            return m
        with mock.patch.object(gpu, "Capture", side_effect=made), mock.patch.object(gpu.N, "screens", return_value=screens):
            gpu.displays_changed()
            self.assertEqual(opened, [], "nothing opened at the display change")
            self.assertIsNone(gpu.capture_for(r"\\.\DISPLAY1", open_now=False), "a slide never opens one")
            self.assertEqual(opened, [])
            gpu.warm_due(idle=True)
            self.assertEqual(opened, [], "not while the displays still change")
            with mock.patch.object(gpu.time, "monotonic", return_value=time.monotonic() + gpu.WARM_AFTER_S + 1):
                gpu.warm_due(idle=False)
                self.assertEqual(opened, [], "not while a slide runs")
                gpu.warm_due(idle=True)
                self.assertEqual(opened, [r"\\.\DISPLAY1"], "one monitor per tick")
                gpu.warm_due(idle=True)
                self.assertEqual(opened, [r"\\.\DISPLAY1", r"\\.\DISPLAY2"])
                gpu.warm_due(idle=True)
                self.assertEqual(len(opened), 2, "all open: nothing more")
            self.assertIsNotNone(gpu.capture_for(r"\\.\DISPLAY1", open_now=False))

    def test_a_capture_dropped_during_a_slide_closes_only_when_that_slide_ends(self):
        # (a display change or the setting turned off while a slide reads the capture every frame: it
        # was closed under the slide, whose next read hit a released object, "NULL pointer access")
        cap = gpu.Capture.__new__(gpu.Capture)
        cap.users, cap.retired, cap.devices = 0, False, None
        cap.texture, cap._dup, cap._old = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        with mock.patch.object(gpu.Capture, "close", autospec=True) as close:
            cap.use()                      # a slide starts
            cap.retire()                   # dropped meanwhile
            close.assert_not_called()
            cap.done()                     # the slide ends
            close.assert_called_once_with(cap)
            idle = gpu.Capture.__new__(gpu.Capture)
            idle.users, idle.retired = 0, False
            idle.retire()
            self.assertEqual(close.call_count, 2, "a capture no slide uses closes at once")

    def test_a_call_on_a_released_gpu_object_is_a_gpu_failure_not_an_app_error(self):
        for call in (lambda: gpu._call(ctypes.c_void_p(), 3), lambda: gpu._vcall(ctypes.c_void_p(), 8, None)):
            with self.assertRaises(OSError):
                call()
        layer = gpu.Layer.__new__(gpu.Layer)
        with self.assertRaises(OSError):
            layer._fill(ctypes.c_void_p(1), ctypes.c_void_p())  # (a released picture: never copied)


def fake_desc(name, ven, dev, luid, dedicated_mb=8192, flags=0, sub=0x1234, rev=0xA1):
    d = gpu._DESC1()
    d.Description, d.VendorId, d.DeviceId, d.SubSysId, d.Revision = name, ven, dev, sub, rev
    d.DedicatedVideoMemory = dedicated_mb << 20
    d.AdapterLuid.Low, d.Flags = luid, flags
    return d


class Listing(unittest.TestCase):
    """adapters() on made-up DXGI/kernel answers (no real GPU involved)."""
    RENDER, SOFT, INDIRECT = gpu._KMT_RENDER, gpu._KMT_SOFTWARE, gpu._KMT_INDIRECT

    def run_with(self, descs, kernel):
        with mock.patch.object(gpu, "_factory", return_value=ctypes.c_void_p()), \
                mock.patch.object(gpu, "_each_adapter", return_value=[(ctypes.c_void_p(), d) for d in descs]), \
                mock.patch.object(gpu, "_kernel_types", return_value=kernel):
            return gpu.adapters()

    def test_only_gpus_that_render_count(self):
        descs = [fake_desc("Microsoft Basic Render Driver", 0x1414, 0x8C, 1, 0, flags=2),
                 fake_desc("Big GPU", 0xAAAA, 0x0001, 2),                       # a virtual display borrowing the name
                 fake_desc("Built-in GPU", 0xBBBB, 0x0002, 3, 512),
                 fake_desc("Big GPU", 0xAAAA, 0x0001, 4),
                 fake_desc("Other GPU", 0xAAAA, 0x0003, 5),
                 fake_desc("Microsoft Basic Render Driver", 0x1414, 0x8C, 6, 0)]
        kernel = {1: self.RENDER | self.SOFT, 2: self.INDIRECT, 3: self.RENDER, 4: self.RENDER,
                  5: self.RENDER, 6: self.INDIRECT}
        got = self.run_with(descs, kernel)
        self.assertEqual([(g.name, g.luid) for g in got], [("Built-in GPU", 3), ("Big GPU", 4), ("Other GPU", 5)])

    def test_identical_twins_get_distinct_keys(self):
        descs = [fake_desc("Twin", 0xAAAA, 0x0001, 7), fake_desc("Twin", 0xAAAA, 0x0001, 8)]
        got = self.run_with(descs, {7: self.RENDER, 8: self.RENDER})
        self.assertEqual([g.key for g in got], ["AAAA-0001-00001234-A1", "AAAA-0001-00001234-A1#2"])

    def test_no_dxgi_means_no_gpus(self):
        with mock.patch.object(gpu, "_factory", side_effect=OSError("no DXGI")):
            self.assertEqual(gpu.adapters(), [])


@unittest.skipUnless(gpu.adapters(), "no GPU on this machine")
class RealGpus(unittest.TestCase):
    def tearDown(self):
        gpu._gpu_of.clear()
        gpu.reset()

    def test_listing(self):
        gs = gpu.adapters()
        self.assertEqual(len({g.key for g in gs}), len(gs), "keys are unique")
        self.assertEqual(len({g.luid for g in gs}), len(gs), "each GPU once")
        self.assertFalse(any("Basic Render" in g.name for g in gs), "no software renderer")

    def test_a_monitor_s_gpu_is_a_listed_gpu(self):
        mon = _primary()
        g = gpu.gpu_of(mon) if mon else None
        self.assertTrue(g is None or g in gpu.adapters())
        if g is not None:
            self.assertIs(gpu.devices_for(mon).gpu, g)
            self.assertIn(g.name, gpu.describe())

    def test_no_gpus_means_cpu(self):
        with mock.patch.object(gpu, "adapters", return_value=[]):
            gpu._gpu_of.clear()
            gpu.reset()
            self.assertFalse(gpu.available())
            self.assertEqual(gpu.describe(), "the CPU")


class Switches(unittest.TestCase):
    def tearDown(self):
        gpu.ENABLED = True
        gpu.reset()
        gpu._failed_at.clear()

    def test_use_gpu_acceleration_setting(self):
        self.assertFalse(gpu.set_enabled(False), "off: animations on the CPU")
        self.assertFalse(gpu.ENABLED)
        self.assertEqual(gpu._devices, {}, "off releases every GPU device")
        self.assertFalse(gpu.available())
        on = gpu.set_enabled(True)
        self.assertTrue(gpu.ENABLED)
        self.assertEqual(on, gpu.available(), "on: each monitor's GPU, where there is one")

    def test_disabled_means_no_gpu(self):
        with mock.patch.object(gpu, "ENABLED", False):
            self.assertFalse(gpu.available())
            with self.assertRaises(OSError):
                gpu.Layer(0)

    def test_no_device_means_no_gpu(self):
        gpu.reset()
        gpu._failed_at.clear()
        with mock.patch.object(gpu, "_D3D11CreateDevice", side_effect=lambda *a: -2147467259):  # E_FAIL
            self.assertFalse(gpu.available())
        gpu.reset()
        gpu._failed_at.clear()

    def test_windows_without_directcomposition(self):
        gpu.reset()
        with mock.patch.object(gpu, "_D3D11CreateDevice", None):
            self.assertFalse(gpu.available())


if __name__ == "__main__":
    unittest.main()
