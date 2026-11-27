"""Display setups: places, desktops and shown desktops kept per set of attached displays (by display
name), applied when displays are attached or detached; nothing vanishes on the way."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

from src import desktop_manager as DM
from src import native as N
from src.desktop_manager import STARTUP_GRACE_MS, DesktopManager, display_names
from src.paths import LAYOUT_FILE, PLACES_FILE, SHOWING_FILE
from src.places import REFRESH_MS

from fakes import FakeScreen, FakeSnapshots, FakeWin

D1, D2 = r"\\.\DISPLAY1", r"\\.\DISPLAY2"
B_RECT = (2100, 100, 2900, 700)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ws-test-")
        from src import datafolder
        for name, value in (("left_working", True), ("kept_new", False),
                            ("KEY", r"Software\DisplayDesktop (unit test)")):
            p = unittest.mock.patch.object(datafolder, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.win = FakeWin()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def manager(self) -> DesktopManager:
        m = DesktopManager(win=self.win, snapshots=FakeSnapshots(), directory=self.dir)
        m.sync()
        self.win.advance(STARTUP_GRACE_MS + 1)  # (past the startup grace: only setup changes place windows)
        m.sync()
        return m

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
            return json.load(fh)

    def settle(self, m):
        m.sync()
        self.win.advance(DM.SETUP_SETTLE_MS)
        m.sync()

    def unplug(self, m):
        scr = self.win.screen_list.pop(1)
        self.settle(m)
        return scr

    def plug(self, m, scr):
        self.win.screen_list.append(scr)
        self.settle(m)

    def rect(self, h):
        return self.win.windows[h].rect

    def visible(self, h):
        return self.win.is_window_visible(h)


class Names(unittest.TestCase):
    def test_the_name_comes_from_the_edid(self):
        edid = bytearray(128)
        edid[72:90] = b"\0\0\0\xfc\0" + b"Wide 27\n     "
        self.assertEqual(N.edid_name(bytes(edid)), "Wide 27")
        self.assertEqual(N.edid_name(bytes(128)), "", "no name descriptor")

    def test_displays_with_the_same_name_are_numbered_left_to_right(self):
        screens = [FakeScreen(D2, (1920, 0, 3840, 1080), (1920, 0, 3840, 1040)),
                   FakeScreen(D1, (0, 0, 1920, 1080), (0, 0, 1920, 1040), True),
                   FakeScreen(r"\\.\DISPLAY3", (3840, 0, 5760, 1080), (3840, 0, 5760, 1040))]
        names = {"p1": "Wide 27", "p2": "Wide 27", "p3": "Office"}
        got = display_names(screens, {D1: "p1", D2: "p2", r"\\.\DISPLAY3": "p3"}, names.get)
        self.assertEqual(got, {D1: "Wide 27", D2: "Wide 27 (2)", r"\\.\DISPLAY3": "Office"})

    def test_an_unknown_monitor_has_no_name(self):
        screens = [FakeScreen(D1, (0, 0, 1920, 1080), (0, 0, 1920, 1040), True)]
        self.assertEqual(display_names(screens, {D1: ""}, lambda p: p), {D1: ""})


class Files(Base):
    def test_files_are_kept_per_setup_by_display_name(self):
        self.win.monitor_names = {"MON-A": "Laptop", "MON-B": "Wide 27"}
        self.win.add("A", 0)
        self.win.add("B", 1)
        m = self.manager()
        m.save_desktops()
        self.unplug(m)
        m.save_desktops()
        self.win.advance(REFRESH_MS)
        m.sync()
        layout = self.read(LAYOUT_FILE)
        self.assertEqual(layout["Format"], 2)
        self.assertEqual([s["Displays"] for s in layout["Setups"]], [["Laptop", "Wide 27"], ["Laptop"]])
        self.assertEqual(sorted(e["Display"] for e in layout["Setups"][0]["Windows"]), ["Laptop", "Wide 27"])
        self.assertEqual([e["Display"] for e in layout["Setups"][1]["Windows"]], ["Laptop", "Laptop"])
        places = self.read(PLACES_FILE)
        self.assertEqual(places["Format"], 2)
        by = {tuple(s["Displays"]): s for s in places["Setups"]}
        self.assertEqual(set(by), {("Laptop", "Wide 27"), ("Laptop",)})
        self.assertEqual(by[("Laptop", "Wide 27")]["Desktops"], {"Laptop": 1, "Wide 27": 1})
        self.assertEqual(by[("Laptop",)]["Desktops"], {"Laptop": 1})

    def test_shown_desktops_are_kept_per_setup(self):
        self.win.add("A", 0)
        m = self.manager()
        m.create_desktop_and_switch(D1)  # desktop 2 shown with both displays
        self.unplug(m)
        m.switch_to(D1, 0)  # desktop 1 shown with one
        got = {tuple(s["Displays"]): s["Showing"] for s in self.read(SHOWING_FILE)["Setups"]}
        self.assertEqual(got, {("MON-A", "MON-B"): {"MON-A": 1}, ("MON-A",): {"MON-A": 0}})

    def test_format_1_place_files_start_empty_and_settings_are_kept(self):
        for name, data in ((LAYOUT_FILE, {"Windows": [{"Exe": "x", "MonitorId": "MON-A"}], "Showing": {"MON-A": 1}}),
                           (PLACES_FILE, {"Updated": 1, "Desktops": {"MON-A": 3}, "Windows": []}),
                           (SHOWING_FILE, {"Showing": {"MON-A": 2}}),
                           ("display.desktop.settings.json", {"UseGpu": False})):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
                json.dump(dict(data, Format=1), fh)
        m = self.manager()
        self.assertEqual(len(m.desktops_of(D1)), 1, "format 1 desktop counts are not used")
        self.assertEqual(m.current_of(D1), 0)
        from src import paths
        self.assertEqual(paths.read_json(os.path.join(self.dir, "display.desktop.settings.json")), {"UseGpu": False})


class Attach(Base):
    def test_each_setup_keeps_its_own_positions(self):
        self.win.add("A", 0)
        b = self.win.add("B", 1, rect=B_RECT)
        m = self.manager()
        scr = self.unplug(m)
        self.assertEqual(self.win.monitor_device_of_window(b), D1, "moved onto the main display")
        self.win.windows[b].rect = (500, 500, 1300, 1000)  # arranged for one display
        self.win.advance(REFRESH_MS)
        m.sync()
        self.plug(m, scr)
        self.assertEqual(self.rect(b), B_RECT, "back where it was with both displays")
        self.assertIn(b, m.desktops_of(D2)[0])
        self.unplug(m)
        self.assertEqual(self.rect(b), (500, 500, 1300, 1000), "where it was with one display")

    def test_attaching_waits_for_the_displays_to_settle(self):
        self.win.add("A", 0)
        b = self.win.add("B", 1, rect=B_RECT)
        m = self.manager()
        scr = self.unplug(m)
        self.win.windows[b].rect = (500, 500, 1300, 1000)
        self.win.advance(REFRESH_MS)
        m.sync()
        self.win.screen_list.append(scr)
        m.sync()
        self.win.advance(DM.SETUP_SETTLE_MS - 1)
        m.sync()
        self.assertEqual(self.rect(b), (500, 500, 1300, 1000), "not yet")
        self.win.advance(1)
        m.sync()
        self.assertEqual(self.rect(b), B_RECT)

    def test_a_display_on_another_port_is_the_same_display(self):
        self.win.monitor_names = {"MON-B": "Wide 27", "MON-B-port2": "Wide 27"}
        b = self.win.add("B", 1, rect=B_RECT)
        m = self.manager()
        scr = self.unplug(m)
        self.win.windows[b].rect = (500, 500, 1300, 1000)
        self.win.advance(REFRESH_MS)
        m.sync()
        self.win.monitor_ids[D2] = "MON-B-port2"
        self.plug(m, scr)
        self.assertEqual(self.rect(b), B_RECT)

    def test_desktops_and_the_shown_desktop_come_back(self):
        self.win.add("A", 0)
        b = self.win.add("B", 1, rect=B_RECT)
        m = self.manager()
        m.create_desktop_and_switch(D2)  # b hidden on desktop 1 of display 2
        c = self.win.add("C", 1, rect=(2200, 200, 3000, 800))
        m.sync()
        scr = self.unplug(m)
        self.assertTrue(self.visible(b) and self.visible(c), "every window of a detached display is shown")
        self.plug(m, scr)
        self.assertEqual(len(m.desktops_of(D2)), 2)
        self.assertEqual(m.current_of(D2), 1)
        self.assertEqual(m.desktops_of(D2), [[b], [c]])
        self.assertFalse(self.visible(b))
        self.assertTrue(self.visible(c))
        self.assertEqual(self.rect(b), B_RECT)


class NothingVanishes(Base):
    def arrange(self):
        """Display 2 shows its desktop 2 (c); b is on its desktop 1. Then display 2 is detached."""
        self.win.add("A", 0)
        b = self.win.add("B", 1, rect=B_RECT)
        m = self.manager()
        m.create_desktop_and_switch(D2)
        c = self.win.add("C", 1, rect=(2200, 200, 3000, 800))
        m.sync()
        return m, b, c, self.unplug(m)

    def test_a_window_moved_onto_an_attached_display_stays_in_view(self):
        m, b, c, scr = self.arrange()
        d = self.win.add("D", 0)  # opened while detached
        self.win.screen_list.append(scr)
        self.win.windows[d].rect = (2300, 300, 3100, 900)  # Windows puts it on the display attached
        self.settle(m)
        self.assertEqual(m.current_of(D2), 1, "the desktop it showed in this setup")
        self.assertTrue(self.visible(d), "a window with no place in this setup is never hidden")
        self.assertIn(d, m.desktops_of(D2)[1])
        self.win.advance(STARTUP_GRACE_MS + 1)
        m.sync()
        self.assertTrue(self.visible(d), "nor later")

    def test_the_window_in_use_stays_in_view(self):
        m, b, c, scr = self.arrange()
        self.win.foreground = b
        self.plug(m, scr)
        self.assertTrue(self.visible(b))
        self.assertEqual(self.win.monitor_device_of_window(b), D2, "on its display")
        self.assertIn(b, m.desktops_of(D2)[1], "on the desktop its display shows")
        self.assertEqual(self.win.foreground, b)

    def test_an_attached_display_never_switches_late(self):
        m, b, c, scr = self.arrange()
        self.win.screen_list.append(scr)
        m.sync()
        e = self.win.add("E", 1, rect=(2400, 400, 3200, 1000))  # opened on it before the setup applies
        m.sync()
        self.win.advance(DM.SETUP_SETTLE_MS)
        m.sync()
        self.assertTrue(self.visible(e))
        for _ in range(3):
            f = self.win.add("F", 1, rect=(2500, 500, 3300, 1000))
            self.win.advance(1000)
            m.sync()
            self.assertTrue(self.visible(e) and self.visible(f))

    def test_desktops_follow_their_monitor_when_display_numbers_change(self):
        self.win.add("A", 0)
        x = self.win.add("X", 1, rect=B_RECT)
        m = self.manager()
        m.create_desktop_and_switch(D2)  # x hidden on display 2's desktop 1
        y = self.win.add("Y", 1, rect=(2200, 200, 3000, 800))
        m.sync()
        before_a, before_b = m.desktops_of(D1), m.desktops_of(D2)
        s1, s2 = self.win.screen_list
        self.win.screen_list = [FakeScreen(D1, s2.bounds, s2.work, False, s2.hmon),
                                FakeScreen(D2, s1.bounds, s1.work, True, s1.hmon)]
        self.win.monitor_ids = {D1: "MON-B", D2: "MON-A"}
        self.settle(m)
        self.assertEqual(m.desktops_of(D1), before_b, "monitor B's desktops, now named DISPLAY1")
        self.assertEqual(m.desktops_of(D2), before_a)
        self.assertEqual(m.current_of(D1), 1)
        self.assertFalse(self.visible(x))
        self.assertTrue(self.visible(y))
        m.switch_to(D1, 0)
        self.assertTrue(self.visible(x))
        self.assertFalse(self.visible(y))


if __name__ == "__main__":
    unittest.main()
