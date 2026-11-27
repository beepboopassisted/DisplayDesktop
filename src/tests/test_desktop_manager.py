"""Core logic tests against a simulated Windows (src/tests/fakes.py). No real window is touched."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock
from types import SimpleNamespace
from unittest import mock

from src import desktop_manager as DM
from src import native as N
from src.desktop_manager import MAX_DESKTOPS_PER_MONITOR, STARTUP_GRACE_MS, DesktopManager
from src.layout import title_hash
from src.paths import JOURNAL_FILE, PLACES_FILE

from fakes import (SETUP, FakeScreen, FakeSnapshots, FakeWin, all_desktops, all_showing, all_windows, layout_data,
                   setup_of)
from moving import move_active

D1, D2 = r"\\.\DISPLAY1", r"\\.\DISPLAY2"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ws-test-")
        from src import datafolder
        # (never left by another test; the registry: a key of its own, never the user's)
        for name, value in (("left_working", True), ("kept_new", False),
                            ("KEY", r"Software\DisplayDesktop (unit test)")):
            p = unittest.mock.patch.object(datafolder, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.win = FakeWin()
        self.shots = FakeSnapshots()
        self.switched = []
        self.starting = []
        self.edges = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def manager(self) -> DesktopManager:
        m = DesktopManager(win=self.win, snapshots=self.shots, directory=self.dir)
        m.desktop_switched.append(self.switched.append)
        m.switch_starting.append(lambda *a: self.starting.append(a))
        m.edge_reached.append(lambda *a: self.edges.append(a))
        return m

    def new_desktop(self, m):
        """What the overview's "+" does on the monitor under the cursor: add a desktop, go there."""
        m.create_desktop_and_switch(self.win.cursor_device)

    @property
    def journal(self):
        p = os.path.join(self.dir, JOURNAL_FILE)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    @property
    def layout_file(self):
        return os.path.join(self.dir, "display.desktop.layout.json")

    def visible(self, h):
        return self.win.is_window_visible(h)


class Adoption(Base):
    def test_windows_adopted_on_their_monitor(self):
        a = self.win.add("A", 0)
        b = self.win.add("B", 1)
        m = self.manager()
        m.sync()
        self.assertEqual(m.desktops_of(D1), [[a]])
        self.assertEqual(m.desktops_of(D2), [[b]])

    def test_ineligible_windows_ignored(self):
        w = self.win
        ok = w.add("Normal")
        w.add("Own", pid=os.getpid())
        w.add("Tool", exstyle=N.WS_EX_TOOLWINDOW)
        app_tool = w.add("AppTool", exstyle=N.WS_EX_TOOLWINDOW | N.WS_EX_APPWINDOW)
        w.add("")  # untitled
        w.add("Cloaked", cloaked=True)
        w.add("Hidden", visible=False)
        w.add("Desktop", cls="Progman")
        w.add("Taskbar", cls="Shell_TrayWnd")
        parent = w.add("Parent")
        w.add("Child", child_of=parent)
        m = self.manager()
        m.sync()
        self.assertEqual(sorted(m.desktops_of(D1)[0]), sorted([ok, app_tool, parent]))

    def test_window_that_changes_monitor_moves_to_that_monitors_current_desktop(self):
        a = self.win.add("A", 0)
        m = self.manager()
        m.sync()
        self.win.windows[a].rect = (2000, 100, 2800, 700)
        m.sync()
        self.assertEqual(m.desktops_of(D1), [[]])
        self.assertEqual(m.desktops_of(D2), [[a]])

    def test_closed_window_dropped(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.win.close(a)
        m.sync()
        self.assertEqual(m.desktops_of(D1), [[]])


class Switching(Base):
    def test_switch_hides_and_back_restores(self):
        a = self.win.add("A")
        b = self.win.add("B")
        self.win.foreground = a
        m = self.manager()
        self.new_desktop(m)
        self.assertEqual(m.current_of(D1), 1)
        self.assertFalse(self.visible(a) or self.visible(b))
        self.assertEqual(m.hidden, {a, b})
        j = self.journal
        self.assertEqual({r["Handle"] for r in j["Windows"]}, {a, b})
        self.assertEqual(set(j), {"UptimeMs", "Writer", "Windows", "Format"})
        self.assertEqual(set(j["Windows"][0]), {"Handle", "Pid", "Class", "Started"})
        self.assertEqual(j["Windows"][0]["Started"], self.win.process_started(j["Windows"][0]["Pid"]))
        self.assertEqual(self.starting, [(D1, 0, 1)])
        self.assertEqual(self.switched[-1].global_number, 2)
        # focused window is captured first
        self.assertEqual(self.shots.captured[0], a)

        m.switch_relative(-1)
        self.assertEqual(m.current_of(D1), 0)
        self.assertTrue(self.visible(a) and self.visible(b))
        self.assertEqual(m.hidden, set())
        self.assertIsNone(self.journal, "journal deleted when nothing is hidden")
        self.assertEqual(self.win.foreground, a, "focus returns to the last active window")
        self.assertEqual([sorted(d) for d in m.desktops_of(D1)], [sorted([a, b]), []], "the empty desktop is kept")

    def test_forward_from_the_last_desktop_reports_the_edge(self):
        a = self.win.add("A")
        m = self.manager()
        m.switch_relative(+1)
        self.assertEqual(m.desktops_of(D1), [[a]])
        self.assertEqual(self.edges, [(D1, +1)], "the edge is reported: the tray shows a bounce")

    def test_back_from_the_first_desktop_reports_the_edge(self):
        self.win.add("A")
        m = self.manager()
        self.new_desktop(m)
        m.switch_relative(-1)
        m.switch_relative(-1)
        self.assertEqual(m.current_of(D1), 0)
        self.assertEqual(self.edges, [(D1, -1)])

    def test_forward_to_an_existing_desktop_still_switches(self):
        a = self.win.add("A")
        m = self.manager()
        self.new_desktop(m)
        b = self.win.add("B")  # opened on desktop 2, so it is kept
        m.switch_relative(-1)
        m.switch_relative(+1)
        self.assertEqual(m.current_of(D1), 1)
        self.assertTrue(self.visible(b) and not self.visible(a))
        m.switch_relative(+1)
        self.assertEqual(self.edges, [(D1, +1)], "then the last desktop: edge")

    def test_switch_only_affects_monitor_under_cursor(self):
        a = self.win.add("A", 0)
        b = self.win.add("B", 1)
        m = self.manager()
        self.win.cursor_device = D2
        self.new_desktop(m)
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b))
        self.assertEqual(m.current_of(D1), 0)
        self.assertEqual(m.current_of(D2), 1)

    def test_max_desktops(self):
        m = self.manager()
        for i in range(MAX_DESKTOPS_PER_MONITOR + 3):
            self.win.add(f"W{i}")
            self.new_desktop(m)
        self.assertEqual(len(m.desktops_of(D1)), MAX_DESKTOPS_PER_MONITOR)
        self.assertEqual(m.current_of(D1), MAX_DESKTOPS_PER_MONITOR - 1)

    def test_global_numbering_left_to_right(self):
        # DISPLAY2 placed to the LEFT of DISPLAY1: it gets the first numbers
        self.win.screen_list[1] = FakeScreen(D2, (-1920, 0, 0, 1080), (-1920, 0, 0, 1040))
        a = self.win.add("A", 0)
        b = self.win.add("B", 1)
        m = self.manager()
        self.win.cursor_device = D2
        self.new_desktop(m)          # D2 now has 2 desktops: global 1, 2; D1 is global 3
        self.assertEqual(self.switched[-1].global_number, 2)
        self.assertEqual(self.switched[-1].ordinal, 1)
        self.win.cursor_device = D1
        self.new_desktop(m)          # D1: 1 -> 2 = global 4
        self.assertEqual(self.switched[-1].global_number, 4)
        self.assertEqual(self.switched[-1].ordinal, 2)
        m.switch_relative(-1)          # back to D1's first = global 3
        self.assertEqual(self.switched[-1].global_number, 3)
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b))  # still on D2's second desktop

    def test_switch_to_same_desktop_only_reports(self):
        self.win.add("A")
        m = self.manager()
        m.switch_to(D1, 0)
        self.assertEqual(self.starting, [])
        self.assertEqual(len(self.switched), 1)

    def test_create_desktop_and_switch_from_empty(self):
        m = self.manager()
        m.create_desktop_and_switch(D1)
        self.assertEqual(m.current_of(D1), 1)
        self.assertEqual(len(m.desktops_of(D1)), 2)

    def test_focus_window_switches_and_restores_minimized(self):
        a = self.win.add("A")
        m = self.manager()
        self.new_desktop(m)
        self.win.windows[a].show_cmd = 2  # minimized while hidden
        m.focus_window(a)
        self.assertEqual(m.current_of(D1), 0)
        self.assertTrue(self.visible(a))
        self.assertEqual(self.win.windows[a].show_cmd, 1)
        self.assertEqual(self.win.foreground, a)


class Safety(Base):
    def test_hung_window_is_never_hidden(self):
        a = self.win.add("A")
        hung = self.win.add("Hung", hung=True)
        m = self.manager()
        self.new_desktop(m)
        self.assertFalse(self.visible(a))
        self.assertTrue(self.visible(hung))
        self.assertNotIn(hung, m.hidden)
        m.sync()  # it follows to the current desktop
        self.assertIn(hung, m.desktops_of(D1)[1])
        m.switch_relative(-1)
        self.assertTrue(self.visible(a))
        self.assertTrue(self.visible(hung), "a hung window may be hidden later? never: it is skipped")

    def test_close_to_tray_window_is_not_reshown(self):
        a = self.win.add("Chat App")
        b = self.win.add("Other")
        m = self.manager()
        m.sync()
        self.win.windows[a].visible = False  # the app hides itself (close to tray)
        self.new_desktop(m)                # moves away (desktop has b)
        m.switch_relative(-1)
        self.assertFalse(self.visible(a), "an app-hidden window must stay hidden")
        self.assertTrue(self.visible(b))

    def test_app_reshows_window_while_on_other_desktop(self):
        a = self.win.add("A")
        m = self.manager()
        self.new_desktop(m)
        self.win.windows[a].visible = True  # e.g. a notification brings it back
        m.sync()
        self.assertIn(a, m.desktops_of(D1)[1])
        self.assertNotIn(a, m.hidden)

    def test_window_closed_while_hidden(self):
        a = self.win.add("A")
        b = self.win.add("B")
        m = self.manager()
        self.new_desktop(m)
        self.win.close(a)
        m.sync()
        self.assertEqual(m.hidden, {b})
        self.assertEqual([r["Handle"] for r in self.journal["Windows"]], [b])

    def test_floating_windows_follow_main_window(self):
        main = self.win.add("Editor")
        palette = self.win.add("Palette", anchor=main, exstyle=N.WS_EX_TOOLWINDOW)
        tooltip = self.win.add("tip", anchor=main, cls="tooltips_class32", exstyle=N.WS_EX_TOOLWINDOW)
        untitled_plain = self.win.add("", anchor=main, style=0)
        m = self.manager()
        self.new_desktop(m)
        self.assertFalse(self.visible(main))
        self.assertFalse(self.visible(palette))
        self.assertTrue(self.visible(tooltip), "transient UI is never hidden")
        self.assertTrue(self.visible(untitled_plain))
        m.switch_relative(-1)
        self.assertTrue(self.visible(main) and self.visible(palette))

    def test_floating_window_released_when_main_reshown_by_app(self):
        main = self.win.add("Editor")
        palette = self.win.add("Palette", anchor=main, exstyle=N.WS_EX_TOOLWINDOW)
        m = self.manager()
        self.new_desktop(m)
        self.win.windows[main].visible = True  # the app shows its main window again
        m.sync()
        self.assertTrue(self.visible(palette))
        self.assertEqual(m.hidden, set())

    def test_restore_all(self):
        a = self.win.add("A")
        b = self.win.add("B", 1)
        m = self.manager()
        self.new_desktop(m)
        self.win.cursor_device = D2
        self.new_desktop(m)
        self.assertFalse(self.visible(a) or self.visible(b))
        m.restore_all()
        self.assertTrue(self.visible(a) and self.visible(b))
        self.assertIsNone(self.journal)
        m.restore_all()  # idempotent

    def test_restore_all_keeps_app_hidden_windows_hidden(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.win.windows[a].visible = False
        m.sync()
        m.restore_all()
        self.assertFalse(self.visible(a))

    def test_no_monitors_reported_is_ignored(self):
        a = self.win.add("A")
        m = self.manager()
        self.new_desktop(m)
        saved = self.win.screen_list
        self.win.screen_list = []  # transient: Windows briefly reports no display
        m.sync()
        self.assertFalse(self.visible(a), "nothing changes while no display is reported")
        self.assertEqual(len(m.desktops_of(D1)), 2)
        self.win.screen_list = saved
        m.sync()
        m.switch_relative(-1)
        self.assertTrue(self.visible(a))

    def test_monitor_disconnect_shows_its_hidden_windows(self):
        a = self.win.add("A", 1)
        m = self.manager()
        self.win.cursor_device = D2
        self.new_desktop(m)
        self.assertFalse(self.visible(a))
        del self.win.screen_list[1]
        m.sync()
        self.assertTrue(self.visible(a))
        self.assertEqual(m.hidden, set())
        self.assertEqual(m.desktops_of(D2), [])


class DisplayDisconnected(Base):
    """A display is unplugged: every window that was on it (shown or on a hidden desktop) ends up
    visible on the main display, and goes back when the display is plugged in again."""

    def on_main_display(self, h):
        l, t, r, b = self.win.windows[h].rect
        cx, cy = (l + r) // 2, (t + b) // 2
        ml, mt, mr, mb = self.win.screen_list[0].work  # D1 is the main (primary) display
        return ml <= l and mt <= t and r <= mr and b <= mb and ml <= cx < mr

    def test_windows_of_every_desktop_move_to_the_main_display(self):
        shown = self.win.add("Shown", 1, rect=(2000, 100, 2800, 700))
        hidden = self.win.add("Hidden", 1, rect=(3000, 300, 3700, 900))
        m = self.manager()
        m.sync()
        self.win.cursor_device = D2
        self.new_desktop(m)
        m.move_window_to_desktop(shown, D2, 1)  # 'shown' on D2's desktop 2 (on screen)...
        m.switch_to(D2, 1)                      # ...'hidden' on D2's desktop 1 (hidden)
        self.assertFalse(self.visible(hidden))
        del self.win.screen_list[1]  # unplugged; Windows does not move hidden windows
        m.sync()
        for h in (shown, hidden):
            self.assertTrue(self.visible(h), "shown")
            self.assertTrue(self.on_main_display(h), f"moved onto the main display: {self.win.windows[h].rect}")
            self.assertIn(h, m.desktops_of(D1)[m.current_of(D1)], "on the main display's current desktop")
        self.assertEqual(m.hidden, set())

    def test_relative_position_and_size_are_kept_where_they_fit(self):
        a = self.win.add("A", 1, rect=(1920 + 960, 520, 1920 + 1460, 820))  # middle of D2
        m = self.manager()
        m.sync()
        del self.win.screen_list[1]
        m.sync()
        self.assertEqual(self.win.windows[a].rect, (960, 520, 1460, 820), "middle of the main display")

    def test_a_maximized_window_stays_maximized(self):
        a = self.win.add("A", 1, rect=(2000, 100, 2800, 700))
        self.win.windows[a].show_cmd = N.SW_SHOWMAXIMIZED
        m = self.manager()
        m.sync()
        del self.win.screen_list[1]
        m.sync()
        self.assertTrue(self.on_main_display(a))
        self.assertEqual(self.win.windows[a].show_cmd, N.SW_SHOWMAXIMIZED)

    def test_a_window_windows_put_on_another_display_goes_to_the_main_one(self):
        d3 = r"\\.\DISPLAY3"
        self.win.screen_list.append(FakeScreen(d3, (0, 1080, 1920, 2160), (0, 1080, 1920, 2120)))
        a = self.win.add("A", 1, rect=(2000, 100, 2800, 700))
        m = self.manager()
        m.sync()
        del self.win.screen_list[1]
        self.win.windows[a].rect = (100, 1200, 900, 1800)  # Windows moved it to display 3
        m.sync()
        self.assertTrue(self.on_main_display(a))

    def test_a_minimized_window_comes_back_on_the_main_display(self):
        # Windows 11 minimizes windows of an unplugged display: restored, it must open on the main one
        a = self.win.add("A", 1, rect=(2000, 100, 2800, 700))
        m = self.manager()
        m.sync()
        self.win.windows[a].show_cmd = N.SW_SHOWMINIMIZED
        del self.win.screen_list[1]
        m.sync()
        self.assertTrue(self.on_main_display(a), "its restore position is on the main display")
        self.assertIn(self.win.windows[a].show_cmd, (N.SW_SHOWMINIMIZED, N.SW_SHOWMINNOACTIVE), "still minimized")

    def test_a_window_that_is_not_responding_is_left_alone(self):
        a = self.win.add("A", 1, rect=(2000, 100, 2800, 700), hung=True)
        m = self.manager()
        m.sync()
        calls = len(self.win.calls)
        del self.win.screen_list[1]
        m.sync()
        self.assertNotIn(("placement", a), [c[:2] for c in self.win.calls[calls:]], "never blocks on it")

    def test_windows_already_on_the_main_display_are_not_touched(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        b = self.win.add("B", 1, rect=(2000, 100, 2800, 700))
        m = self.manager()
        m.sync()
        calls = len(self.win.calls)
        del self.win.screen_list[1]
        m.sync()
        self.assertEqual(self.win.windows[a].rect, (100, 100, 900, 700))
        self.assertNotIn(("placement", a), [c[:2] for c in self.win.calls[calls:]])

    def test_they_go_back_when_the_display_returns(self):
        a = self.win.add("A", 1, rect=(2000, 100, 2800, 700))
        m = self.manager()
        m.sync()
        self.win.cursor_device = D2
        self.new_desktop(m)
        m.move_window_to_desktop(a, D2, 1)
        unplugged = self.win.screen_list.pop(1)
        m.sync()
        self.assertTrue(self.on_main_display(a))
        self.win.screen_list.append(unplugged)
        m.sync()
        self.assertIn(a, m.desktops_of(D2)[1], "back on display 2, desktop 2")
        self.assertEqual(self.win.windows[a].rect, (2000, 100, 2800, 700))


class MonitorIds(Base):
    def test_read_again_only_when_the_display_setup_changes_or_after_30_s(self):
        reads = []
        real = self.win.get_monitor_id
        self.win.get_monitor_id = lambda d: (reads.append(d), real(d))[1]
        m = self.manager()
        m.sync()
        self.assertEqual(len(reads), 2, "both displays read once")
        for _ in range(5):
            self.win.advance(600)
            m.sync()
        self.assertEqual(len(reads), 2, "not again while nothing changes")
        del self.win.screen_list[1]
        m.sync()
        self.assertEqual(len(reads), 3, "the setup changed: read again")
        self.win.monitor_ids[D1] = "MON-SWAPPED"  # another monitor, nothing else visible changed
        self.win.advance(30_000)
        m.sync()
        self.assertEqual(m._monitor_ids[D1], "MON-SWAPPED", "picked up within 30 s")


class Recovery(Base):
    def write_journal(self, uptime, records):
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": uptime, "Windows": records}, fh)

    def test_recovers_windows_from_same_boot(self):
        a = self.win.add("A", visible=False, pid=77, cls="X")
        b = self.win.add("B", visible=False, pid=78, cls="Y")
        c = self.win.add("C", visible=False, pid=79, cls="Z")
        self.write_journal(self.win.tick - 5000, [
            {"Handle": a, "Pid": 77, "Class": "X"},
            {"Handle": b, "Pid": 999, "Class": "Y"},   # HWND reused by another process
            {"Handle": c, "Pid": 79, "Class": "Other"},  # class mismatch
            {"Handle": 123456, "Pid": 1, "Class": "Gone"},
        ])
        self.manager()
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b))
        self.assertFalse(self.visible(c))
        self.assertIsNone(self.journal, "journal removed after recovery")

    def test_journal_from_before_reboot_ignored(self):
        a = self.win.add("A", visible=False, pid=77, cls="X")
        self.write_journal(self.win.tick + 1, [{"Handle": a, "Pid": 77, "Class": "X"}])
        self.manager()
        self.assertFalse(self.visible(a))
        self.assertIsNone(self.journal)

    def test_corrupt_journal_dropped(self):
        with open(os.path.join(self.dir, JOURNAL_FILE), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.manager().recovery_problem, "damaged")
        self.assertIsNone(self.journal)

    def test_csharp_journal_format(self):
        a = self.win.add("A", visible=False, pid=23508, cls="BrowserWindowClass")
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"UptimeMs":%d,"Windows":[{"Handle":%d,"Pid":23508,"Class":"BrowserWindowClass"}]}'
                     % (self.win.tick - 1, a))
        self.manager()
        self.assertTrue(self.visible(a))


class Moving(Base):
    def test_move_window_to_other_monitor_keeps_relative_position(self):
        a = self.win.add("A", 0, rect=(960, 520, 1460, 820))  # starts in the middle of D1
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(a, D2, 0)
        self.assertEqual(m.desktops_of(D2), [[a]])
        self.assertEqual(self.win.windows[a].rect, (1920 + 960, 520, 1920 + 1460, 820))
        self.assertTrue(self.visible(a))

    def test_move_window_to_inactive_desktop_hides_it(self):
        a = self.win.add("A")
        b = self.win.add("B")
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)            # D1: [b] hidden, [a] current
        m.move_window_to_desktop(a, D1, 0)  # a goes to the inactive desktop
        self.assertFalse(self.visible(a))
        self.assertIn(a, m.hidden)
        self.assertEqual(m.desktops_of(D1), [[b, a], []])
        m.switch_to(D1, 0)
        self.assertTrue(self.visible(a) and self.visible(b))
        self.assertEqual(m.desktops_of(D1), [[b, a], []])

    def test_move_maximized_window_restores_and_remaximizes(self):
        a = self.win.add("A", 0, show_cmd=3)
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(a, D2, 0)
        self.assertEqual(self.win.windows[a].show_cmd, 3)
        self.assertEqual(self.win.monitor_device_of_window(a), D2)

    def test_move_desktop_to_monitor(self):
        a = self.win.add("A", 0)
        b = self.win.add("B", 0)
        c = self.win.add("C", 1)
        self.win.foreground = b
        m = self.manager()
        move_active(m, self.win, +1)       # D1: [a], [b] (current 1)
        m.move_desktop_to_monitor(D1, 1, D2)
        self.assertEqual(m.desktops_of(D1), [[a]])
        self.assertEqual(m.current_of(D1), 0)
        self.assertTrue(self.visible(a), "remaining active desktop shown")
        self.assertEqual(m.desktops_of(D2), [[c], [b]])
        self.assertFalse(self.visible(b), "moved desktop is not active on the target")
        self.assertEqual(self.win.monitor_device_of_window(b), D2)
        m.switch_to(D2, 1)
        self.assertTrue(self.visible(b))
        self.assertFalse(self.visible(c))

    def test_move_last_desktop_leaves_an_empty_one(self):
        a = self.win.add("A", 0)
        m = self.manager()
        m.sync()
        m.move_desktop_to_monitor(D1, 0, D2)
        self.assertEqual(m.desktops_of(D1), [[]])
        self.assertEqual(m.current_of(D1), 0)

    def three(self):
        """D1: [a], [b], [c], showing desktop 1."""
        a, b, c = self.win.add("A"), self.win.add("B"), self.win.add("C")
        m = self.manager()
        m.sync()
        for h in (b, c):
            m.create_desktop_and_switch(D1)
            m.move_window_to_desktop(h, D1, len(m.desktops_of(D1)) - 1)
        m.switch_to(D1, 0)
        self.assertEqual(m.desktops_of(D1), [[a], [b], [c]])
        return m, a, b, c

    def test_swap_desktops_renumbers_without_hiding_or_showing(self):
        m, a, b, c = self.three()
        calls = len(self.win.calls)
        m.swap_desktops(D1, 1, 2)  # desktops 2 and 3 trade places
        self.assertEqual(m.desktops_of(D1), [[a], [c], [b]])
        self.assertEqual(m.current_of(D1), 0)
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b) or self.visible(c))
        self.assertFalse([x for x in self.win.calls[calls:] if x[0] == "show"], "nothing hidden or shown")
        m.switch_to(D1, 1)
        self.assertTrue(self.visible(c) and not self.visible(b), "desktop 2 is now c's")

    def test_swapping_the_shown_desktop_keeps_it_on_screen(self):
        m, a, b, c = self.three()
        events = (len(self.switched), len(self.starting))
        m.swap_desktops(D1, 0, 2)
        self.assertEqual(m.desktops_of(D1), [[c], [b], [a]])
        self.assertEqual(m.current_of(D1), 2, "a is still shown, now as desktop 3")
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b) or self.visible(c))
        self.assertEqual((len(self.switched), len(self.starting)), events, "not a switch: no switch event, no slide")
        with open(os.path.join(self.dir, "display.desktop.showing.json"), encoding="utf-8") as fh:
            self.assertEqual(all_showing(json.load(fh))["MON-A"], 2, "the new number is remembered")

    def test_empty_desktops_are_never_removed_by_themselves(self):
        a = self.win.add("A")
        m = self.manager()
        self.new_desktop(m)  # empty desktop 2
        self.new_desktop(m)  # empty desktop 3
        m.switch_to(D1, 0)
        for _ in range(3):
            m.sync()
        self.assertEqual(m.desktops_of(D1), [[a], [], []])
        m.switch_relative(+1)
        self.assertEqual(m.current_of(D1), 1, "an empty desktop can be switched to")

    def test_delete_a_desktop_that_is_not_shown(self):
        m, a, b, c = self.three()          # [a] shown, [b], [c]
        m.delete_desktop(D1, 2)            # c joins desktop 2 (b's), still hidden
        self.assertEqual(m.desktops_of(D1), [[a], [b, c]])
        self.assertEqual(m.current_of(D1), 0)
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b) or self.visible(c))
        self.assertEqual(m.hidden, {b, c}, "still hidden by us: shown again when switched to")
        m.switch_to(D1, 1)
        self.assertTrue(self.visible(b) and self.visible(c) and not self.visible(a))

    def test_delete_the_shown_desktop(self):
        m, a, b, c = self.three()
        m.switch_to(D1, 2)                 # c shown
        m.delete_desktop(D1, 2)            # c joins desktop 2, which is shown instead
        self.assertEqual(m.desktops_of(D1), [[a], [b, c]])
        self.assertEqual(m.current_of(D1), 1)
        self.assertTrue(self.visible(b) and self.visible(c))
        self.assertFalse(self.visible(a))
        self.assertEqual(m.hidden, {a})

    def test_delete_the_first_desktop_joins_the_next_one(self):
        m, a, b, c = self.three()          # [a] shown
        m.delete_desktop(D1, 0)
        self.assertEqual(m.desktops_of(D1), [[b, a], [c]])
        self.assertEqual(m.current_of(D1), 0)
        self.assertTrue(self.visible(a) and self.visible(b), "the joined desktop is shown")
        self.assertFalse(self.visible(c))

    def test_delete_into_the_shown_desktop_shows_the_windows(self):
        m, a, b, c = self.three()          # [a] shown
        m.delete_desktop(D1, 1)            # b joins desktop 1, which is on screen
        self.assertEqual(m.desktops_of(D1), [[a, b], [c]])
        self.assertTrue(self.visible(a) and self.visible(b))
        self.assertEqual(m.hidden, {c})

    def test_delete_an_empty_desktop(self):
        m, a, b, c = self.three()
        self.new_desktop(m)                # empty desktop 4, shown
        m.delete_desktop(D1, 3)
        self.assertEqual(m.desktops_of(D1), [[a], [b], [c]])
        self.assertEqual(m.current_of(D1), 2)
        self.assertTrue(self.visible(c))

    def test_the_only_desktop_cannot_be_deleted(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.delete_desktop(D1, 0)
        m.delete_desktop(D1, 5)
        self.assertEqual(m.desktops_of(D1), [[a]])
        self.assertTrue(self.visible(a))

    def test_delete_is_remembered(self):
        m, a, b, c = self.three()
        m.switch_to(D1, 2)
        m.delete_desktop(D1, 1)            # b joins desktop 1; c's desktop is now number 2
        with open(os.path.join(self.dir, "display.desktop.showing.json"), encoding="utf-8") as fh:
            self.assertEqual(all_showing(json.load(fh))["MON-A"], 1)
        m.restore_all()
        m2 = self.manager()
        m2.sync()
        self.assertEqual([sorted(d) for d in m2.desktops_of(D1)], [sorted([a, b]), [c]])
        self.assertEqual(m2.current_of(D1), 1)

    def test_swap_is_recorded_as_last_known_place(self):
        m, a, b, c = self.three()
        m.swap_desktops(D1, 1, 2)
        with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
            desks = sorted(e["Desktop"] for e in all_windows(json.load(fh)))
        self.assertEqual(desks, [0, 1, 2])
        m.restore_all()
        m2 = self.manager()
        m2.sync()
        self.assertEqual(m2.desktops_of(D1), [[a], [c], [b]], "a restart keeps the new order")

    def test_swap_ignores_bad_indexes(self):
        m, a, b, c = self.three()
        for x, y in ((0, 0), (0, 3), (-1, 1)):
            m.swap_desktops(D1, x, y)
        m.swap_desktops(r"\\.\DISPLAY9", 0, 1)
        self.assertEqual(m.desktops_of(D1), [[a], [b], [c]])


class SavedLayout(Base):
    def save(self, m):
        return m.save_desktops()

    def test_nothing_is_saved_automatically(self):
        a = self.win.add("A")
        self.win.foreground = a
        m = self.manager()
        for _ in range(5):
            m.sync()
            self.new_desktop(m)
            move_active(m, self.win, +1)
            m.switch_relative(-1)
        m.load_desktops()
        self.assertFalse(os.path.exists(self.layout_file), "only Save/Clear write layout.json")

    def test_save_format(self):
        a = self.win.add("Doc - Editor", exe=r"C:\Apps\editor.exe", cls="EditorWnd")
        b = self.win.add("B", 1)
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)
        self.assertEqual(self.save(m), 2)
        with open(self.layout_file, encoding="utf-8") as fh:
            raw = fh.read()
        data = json.loads(raw)
        self.assertEqual(list(data), ["Setups", "Format"])  # (the setups, then the format number)
        self.assertEqual(len(data["Setups"]), 1)
        setup = data["Setups"][0]
        self.assertEqual(list(setup), ["Displays", "Windows", "Showing"])
        self.assertEqual(setup["Displays"], list(SETUP))
        self.assertEqual(setup["Showing"], {"MON-A": 1, "MON-B": 0})
        e = next(x for x in setup["Windows"] if x["Class"] == "EditorWnd")
        self.assertEqual(list(e), ["Exe", "Class", "TitleHash", "Display", "Desktop", "WorkLeft",
                                   "WorkTop", "ShowCmd", "Left", "Top", "Right", "Bottom", "TitleParts"],
                         "the keys in order; the title parts come after them")
        self.assertEqual(e["TitleHash"], title_hash("Doc - Editor"))
        self.assertEqual(e["TitleParts"], [title_hash("doc"), title_hash("editor")], "only hashes")
        self.assertEqual(e["Desktop"], 1)
        self.assertEqual(e["Display"], "MON-A")
        self.assertNotIn("Doc - Editor", raw, "titles are stored only as a hash")

    def test_window_closed_and_reopened_opens_where_created(self):
        a = self.win.add("Report.docx", exe=r"C:\Apps\word.exe", rect=(50, 60, 850, 660))
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)  # a on desktop 2
        self.save(m)
        m.switch_relative(-1)
        self.win.close(a)
        m.sync()
        # the user opens it again (new HWND, Windows puts it elsewhere), while desktop 1 is showing
        a2 = self.win.add("Report.docx", exe=r"C:\Apps\word.exe", rect=(300, 300, 700, 700))
        m.sync()
        self.assertIn(a2, m.desktops_of(D1)[0], "on the desktop it was created on, not the saved one")
        self.assertTrue(self.visible(a2))
        self.assertEqual(self.win.windows[a2].rect, (300, 300, 700, 700), "not moved")

    def test_loose_match_only_during_grace(self):
        a = self.win.add("Inbox (3) - Mail", exe=r"C:\Apps\mail.exe", cls="Mail")
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)
        self.save(m)
        # restart: a fresh manager, the app reopens with another title
        self.win.close(a)
        m2 = self.manager()
        a2 = self.win.add("Inbox (5) - Mail", exe=r"C:\Apps\mail.exe", cls="Mail")
        m2.sync()
        self.assertIn(a2, m2.desktops_of(D1)[1])
        self.assertEqual(m2.current_of(D1), 1, "the saved shown desktop is shown again")
        # after the grace time a different title is not matched
        self.win.close(a2)
        m3 = self.manager()
        self.win.advance(STARTUP_GRACE_MS + 1)
        a3 = self.win.add("Inbox (9) - Mail", exe=r"C:\Apps\mail.exe", cls="Mail")
        m3.sync()
        self.assertEqual(m3.desktops_of(D1), [[a3], []], "not placed (desktop 2 is kept, empty)")

    def test_exact_match_beats_loose_and_closest_wins(self):
        x1 = self.win.add("Same", rect=(0, 0, 500, 500))
        x2 = self.win.add("Same", rect=(1000, 0, 1500, 500))
        self.win.foreground = x2
        m = self.manager()
        move_active(m, self.win, +1)  # x2 on desktop 2, x1 on desktop 1
        self.save(m)
        self.win.close(x1)
        self.win.close(x2)
        m2 = self.manager()
        n2 = self.win.add("Same", rect=(990, 10, 1490, 510))  # opens near x2's place
        n1 = self.win.add("Same", rect=(10, 10, 510, 510))
        m2.sync()
        self.assertIn(n2, m2.desktops_of(D1)[1])
        self.assertIn(n1, m2.desktops_of(D1)[0])

    def test_displaced_window_returns_with_its_monitor(self):
        b = self.win.add("B", 1)
        m = self.manager()
        m.sync()
        self.save(m)
        # restart with monitor 2 missing: the window ends up on monitor 1
        self.win.close(b)
        screen2 = self.win.screen_list.pop(1)
        m2 = self.manager()
        b2 = self.win.add("B", 0)
        m2.sync()
        m2.sync()
        self.assertIn(b2, m2.desktops_of(D1)[0])
        self.win.screen_list.append(screen2)
        m2.sync()
        self.win.advance(DM.SETUP_SETTLE_MS)  # (once the displays settle)
        m2.sync()
        self.assertEqual(self.win.monitor_device_of_window(b2), D2)
        self.assertIn(b2, m2.desktops_of(D2)[0])

    def test_displaced_window_moved_by_user_keeps_that_place_in_its_own_setup(self):
        b = self.win.add("B", 1)
        m = self.manager()
        m.sync()
        self.save(m)
        self.win.close(b)
        screen2 = self.win.screen_list.pop(1)
        m2 = self.manager()
        b2 = self.win.add("B", 0)
        m2.sync()
        m2.sync()
        self.win.windows[b2].rect = (300, 300, 800, 800)  # the user moves it
        self.win.advance(2000)  # (recorded: places.REFRESH_MS)
        m2.sync()
        self.win.screen_list.append(screen2)
        m2.sync()
        self.win.advance(DM.SETUP_SETTLE_MS)  # (once the displays settle)
        m2.sync()
        self.assertEqual(self.win.monitor_device_of_window(b2), D2, "both displays: their setup's place")
        self.win.screen_list.pop(1)
        m2.sync()
        self.win.advance(DM.SETUP_SETTLE_MS)
        m2.sync()
        self.assertEqual(self.win.windows[b2].rect, (300, 300, 800, 800), "one display: where the user put it")

    def test_save_keeps_the_save_of_the_setup_with_the_detached_monitor(self):
        a = self.win.add("A", 0)
        b = self.win.add("B", 1)
        m = self.manager()
        m.sync()
        self.save(m)
        with open(self.layout_file, encoding="utf-8") as fh:
            both = setup_of(json.load(fh))
        self.assertEqual(sorted(e["Display"] for e in both["Windows"]), ["MON-A", "MON-B"])
        self.assertEqual(both["Showing"], {"MON-A": 0, "MON-B": 0})
        self.win.close(b)
        self.win.screen_list.pop(1)
        m.sync()
        self.save(m)
        with open(self.layout_file, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(setup_of(data), both, "the two-monitor setup's save: unchanged")
        one = setup_of(data, ("MON-A",))
        self.assertEqual([e["Display"] for e in one["Windows"]], ["MON-A"], "this setup: the windows where they are now")
        self.assertEqual(one["Showing"], {"MON-A": 0})

    def test_clear_desktops(self):
        a = self.win.add("A")
        b = self.win.add("B")
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)
        self.save(m)
        m.clear_desktops()
        self.assertEqual(len(m.desktops_of(D1)), 1)
        self.assertEqual(sorted(m.desktops_of(D1)[0]), sorted([a, b]))
        self.assertTrue(self.visible(a) and self.visible(b))
        with open(self.layout_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"Setups": [], "Format": 2})
        self.assertIsNone(self.journal)

    def test_load_desktops_reapplies(self):
        a = self.win.add("A", rect=(10, 10, 510, 510))
        b = self.win.add("B")
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)  # a on desktop 2 (shown)
        self.save(m)
        # the user rearranges: a back to desktop 1, moved
        move_active(m, self.win, -1)
        self.win.windows[a].rect = (400, 400, 900, 900)
        m.sync()
        m.load_desktops()
        self.assertEqual(self.win.windows[a].rect, (10, 10, 510, 510))
        self.assertIn(a, m.desktops_of(D1)[1])
        self.assertEqual(m.current_of(D1), 1, "the saved shown desktop is shown")
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b))

    def test_hung_window_never_placed(self):
        a = self.win.add("A")
        self.win.foreground = a
        m = self.manager()
        move_active(m, self.win, +1)
        self.save(m)
        self.win.close(a)
        m2 = self.manager()
        a2 = self.win.add("A", hung=True)
        m2.sync()
        self.assertTrue(self.visible(a2))
        self.assertNotIn(("placement", a2), [c[:2] for c in self.win.calls])


class SameNameWindows(Base):
    """Several windows with the same program, class and title (three windows of a second browser),
    and windows of one program whose titles change (two browser windows): each is saved and comes
    back on its own."""
    TWIN = dict(exe=r"C:\Program Files\Other Browser\browser2.exe", cls="BrowserWindowClass2")
    FF = dict(exe=r"C:\Program Files\Web Browser\browser.exe", cls="BrowserWindowClass")
    STORE = dict(exe=r"C:\Windows\System32\ApplicationFrameHost.exe", cls="ApplicationFrameWindow")
    TWIN_TITLE = "New Tab - Other Browser"

    def open_all(self, twin_rects, ff_titles, ff_rects):
        twins = [self.win.add(self.TWIN_TITLE, 0, rect=r, **self.TWIN) for r in twin_rects]
        ff1 = self.win.add(ff_titles[0], 0, rect=ff_rects[0], **self.FF)
        ff2 = self.win.add(ff_titles[1], 1, rect=ff_rects[1], **self.FF)
        return twins, ff1, ff2

    def where(self, m, h):
        for dev in (D1, D2):
            for i, s in enumerate(m.desktops_of(dev)):
                if h in s:
                    return dev, i
        return None

    def restart_after_reboot(self, m):
        """Every exit restores the windows; after a reboot all of them open again with new HWNDs."""
        m.restore_all()
        self.win.reboot()
        return self.manager()  # within the startup grace time

    def arrange_and_save(self):
        twin_rects = [(100, 100, 420, 600), (500, 100, 820, 600), (900, 100, 1220, 600)]
        ff_rects = [(0, 0, 1200, 900), (2000, 50, 3200, 950)]
        twins, ff1, ff2 = self.open_all(twin_rects, ["News - Web Browser", "Mail - Web Browser"],
                                        ff_rects)
        m = self.manager()
        m.sync()
        m.create_desktop_and_switch(D1)
        m.create_desktop_and_switch(D1)  # D1 has desktops 1, 2, 3
        for i, c in enumerate(twins):
            m.move_window_to_desktop(c, D1, i)  # one of the look-alike windows on each desktop
        m.move_window_to_desktop(ff1, D1, 1)
        m.create_desktop_and_switch(D2)
        m.move_window_to_desktop(ff2, D2, 1)
        m.switch_to(D1, 0)
        m.switch_to(D2, 0)
        self.assertEqual(m.save_desktops(), 5, "all five windows are saved")
        with open(self.layout_file, encoding="utf-8") as fh:
            saved = all_windows(json.load(fh))
        self.assertEqual(sorted(e["Desktop"] for e in saved if e["Class"] == "BrowserWindowClass2"),
                         [0, 1, 2], "three entries, one per look-alike window, although they look the same")
        self.assertEqual(sorted(e["Exe"] for e in saved if e["Class"] == "BrowserWindowClass"),
                         [self.FF["exe"]] * 2)
        return m, twin_rects, ff_rects

    def test_app_restart_keeps_every_window_on_its_desktop(self):
        m, _, _ = self.arrange_and_save()
        before = {h: self.where(m, h) for h in self.win.windows}
        m.restore_all()
        m2 = self.manager()  # same windows (same HWNDs and positions), fresh DisplayDesktop
        m2.sync()
        self.assertEqual({h: self.where(m2, h) for h in self.win.windows}, before)

    def test_reboot_every_look_alike_and_browser_window_comes_back(self):
        m, twin_rects, ff_rects = self.arrange_and_save()
        m2 = self.restart_after_reboot(m)
        # new HWNDs; the look-alike windows all open at one spot, the browser restores its windows
        # near where they were, with other pages (other titles)
        same_spot = [(300, 300, 620, 800)] * 3
        twins, ff1, ff2 = self.open_all(same_spot, ["Weather - Web Browser", "Docs - Web Browser"],
                                        [(10, 10, 1210, 910), (2010, 60, 3210, 960)])
        m2.sync()
        self.assertEqual(sorted(self.where(m2, c) for c in twins), [(D1, 0), (D1, 1), (D1, 2)],
                         "one look-alike window back on each saved desktop")
        self.assertEqual(sorted(self.win.windows[c].rect for c in twins), sorted(twin_rects),
                         "each at one of the saved positions")
        self.assertEqual(self.where(m2, ff1), (D1, 1))
        self.assertEqual(self.where(m2, ff2), (D2, 1))
        self.assertEqual(self.win.windows[ff1].rect, ff_rects[0])

    def test_fourth_look_alike_window_is_not_placed(self):
        m, _, _ = self.arrange_and_save()
        extra = self.win.add(self.TWIN_TITLE, 0, rect=(50, 50, 370, 550), **self.TWIN)
        m.sync()
        self.assertEqual(self.where(m, extra), (D1, 0), "no saved entry left: adopted where it is")
        self.assertTrue(self.visible(extra))

    def test_one_store_app_never_takes_another_store_apps_place(self):
        # All Store apps share ApplicationFrameHost.exe and the class ApplicationFrameWindow, so
        # the title is all that tells them apart: never matched loosely
        photos = self.win.add("Photos", 0, rect=(900, 100, 1220, 600), **self.STORE)
        m = self.manager()
        m.sync()
        m.create_desktop_and_switch(D1)
        m.create_desktop_and_switch(D1)
        m.move_window_to_desktop(photos, D1, 2)
        m.switch_to(D1, 0)
        m.save_desktops()
        m2 = self.restart_after_reboot(m)
        settings = self.win.add("Settings", 0, rect=(900, 100, 1220, 600), **self.STORE)  # where Photos was
        m2.sync()
        self.assertEqual(self.where(m2, settings), (D1, 0))
        self.assertTrue(self.visible(settings))
        photos2 = self.win.add("Photos", 0, **self.STORE)
        m2.sync()
        self.assertEqual(self.where(m2, photos2), (D1, 2), "the right app still gets its place")


class LastKnownPlaces(Base):
    """display.desktop.places.json: where every open window is, recorded automatically and used only at startup
    (after a crash, a restart of DisplayDesktop or of the computer). Separate from Save/Load."""

    @property
    def places_file(self):
        return os.path.join(self.dir, PLACES_FILE)

    def places(self):
        with open(self.places_file, encoding="utf-8") as fh:
            return json.load(fh)

    def where(self, m, h):
        for dev in (D1, D2):
            for i, s in enumerate(m.desktops_of(dev)):
                if h in s:
                    return dev, i
        return None

    def arrange(self):
        """a on desktop 1, b on desktop 2 of monitor 1, c on desktop 2 of monitor 2. Nothing saved."""
        a = self.win.add("Alpha", 0, exe=r"C:\Apps\alpha.exe", rect=(10, 20, 610, 520))
        b = self.win.add("Beta", 0, exe=r"C:\Apps\beta.exe", rect=(100, 120, 900, 720))
        c = self.win.add("Gamma", 1, exe=r"C:\Apps\gamma.exe", rect=(2000, 100, 2800, 700))
        m = self.manager()
        m.sync()
        m.create_desktop_and_switch(D1)
        m.move_window_to_desktop(b, D1, 1)
        m.create_desktop_and_switch(D2)
        m.move_window_to_desktop(c, D2, 1)
        m.switch_to(D1, 0)
        m.switch_to(D2, 0)
        return m, a, b, c

    def reopen(self):
        """After a reboot: the same windows open again, with new HWNDs, wherever Windows puts them."""
        a = self.win.add("Alpha", 0, exe=r"C:\Apps\alpha.exe", rect=(300, 300, 700, 700))
        b = self.win.add("Beta", 0, exe=r"C:\Apps\beta.exe", rect=(300, 300, 700, 700))
        c = self.win.add("Gamma", 1, exe=r"C:\Apps\gamma.exe", rect=(2300, 300, 2700, 700))
        return a, b, c

    def test_recorded_automatically_without_titles(self):
        m, a, b, c = self.arrange()
        data = self.places()
        self.assertEqual(sorted((e["Display"], e["Desktop"]) for e in all_windows(data)),
                         [("MON-A", 0), ("MON-A", 1), ("MON-B", 1)])
        self.assertNotIn("Alpha", json.dumps(data), "titles are stored only as a hash")
        self.assertFalse(os.path.exists(self.layout_file), "the saved desktops are not touched")

    def test_crash_puts_every_window_back(self):
        m, a, b, c = self.arrange()
        self.assertFalse(self.visible(b) or self.visible(c))
        # DisplayDesktop crashes (no restore at all): the next start recovers the hidden windows
        # from the journal and puts every window back on its desktop
        m2 = self.manager()
        m2.sync()
        self.assertEqual([self.where(m2, h) for h in (a, b, c)], [(D1, 0), (D1, 1), (D2, 1)])
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b) or self.visible(c))

    def test_windows_shutting_down_puts_every_window_back(self):
        m, a, b, c = self.arrange()
        m.restore_all()  # what WM_ENDSESSION does (shutdown, restart, sign-out): no user exit
        m2 = self.manager()
        m2.sync()
        self.assertEqual([self.where(m2, h) for h in (a, b, c)], [(D1, 0), (D1, 1), (D2, 1)])

    def quit(self, m):
        """What every exit does (tray Exit, run stop, Windows shutting down, an error): one last
        snapshot, then every hidden window is shown (DisplayDesktop's desktops are gone)."""
        m.snapshot()
        m.restore_all()

    def test_quitting_shows_every_window_and_the_next_start_puts_everything_back(self):
        m, a, b, c = self.arrange()
        self.new_desktop(m)              # an empty desktop 3 on display 1, shown
        m.switch_to(D1, 0)
        self.quit(m)
        self.assertTrue(all(self.visible(h) for h in (a, b, c)), "on quit every window is shown")
        m2 = self.manager()
        m2.sync()
        self.assertEqual([self.where(m2, h) for h in (a, b, c)], [(D1, 0), (D1, 1), (D2, 1)])
        self.assertEqual([len(m2.desktops_of(d)) for d in (D1, D2)], [3, 2], "every desktop, the empty one too")
        self.assertTrue(self.visible(a) and not self.visible(b) and not self.visible(c))

    def test_the_last_snapshot_has_the_latest_positions(self):
        m, a, b, c = self.arrange()
        self.win.advance(2_100)
        m.sync()
        self.win.windows[b].rect = (500, 400, 1300, 900)  # moved/resized just before quitting...
        self.win.advance(500)                             # ...less than 2 s ago: not recorded yet
        m.sync()
        self.assertNotEqual(self.win.windows[b].rect, next(
            (e["Left"], e["Top"], e["Right"], e["Bottom"]) for e in all_windows(self.places()) if e["Desktop"] == 1
            and e["Display"] == "MON-A"))
        self.quit(m)
        m2 = self.manager()
        self.win.windows[b].rect = (0, 0, 640, 480)  # wherever it is when DisplayDesktop starts again
        m2.sync()
        self.assertEqual(self.where(m2, b), (D1, 1))
        self.assertEqual(self.win.windows[b].rect, (500, 400, 1300, 900), "the position at the moment of quitting")

    def test_computer_restart_puts_reopened_windows_back(self):
        m, a, b, c = self.arrange()
        # Windows shuts down: the apps close while DisplayDesktop still runs
        for h in (a, b, c):
            self.win.close(h)
        m.sync()
        self.win.advance(5_000)
        m.restore_all()
        self.win.reboot()
        m2 = self.manager()
        a2, b2, c2 = self.reopen()
        m2.sync()
        self.assertEqual([self.where(m2, h) for h in (a2, b2, c2)], [(D1, 0), (D1, 1), (D2, 1)])
        self.assertEqual(self.win.windows[b2].rect, (100, 120, 900, 720), "at its last known position")
        self.assertFalse(self.visible(b2))

    def test_grace_syncs_match_places_only_when_a_new_window_opens(self):
        m, a, b, c = self.arrange()
        for h in (a, b, c):
            self.win.close(h)
        m.sync()
        m.restore_all()
        self.win.reboot()
        m2 = self.manager()
        m2.sync()
        with unittest.mock.patch.object(m2._places, "assign", wraps=m2._places.assign) as assign:
            for _ in range(3):
                self.win.advance(600)
                m2.sync()
            assign.assert_not_called()  # (the places waiting for their windows: not gone over every sync)
            a2, b2, c2 = self.reopen()
            m2.sync()
            assign.assert_called_once()
        self.assertEqual([self.where(m2, h) for h in (a2, b2, c2)], [(D1, 0), (D1, 1), (D2, 1)])

    def test_window_closed_by_the_user_is_not_put_back(self):
        m, a, b, c = self.arrange()
        self.win.close(b)
        m.sync()
        self.win.advance(61_000)  # well before DisplayDesktop stops
        m.sync()
        self.assertEqual(len(all_windows(self.places())), 2, "its place is forgotten")
        m.restore_all()
        m2 = self.manager()
        b2 = self.win.add("Beta", 0, exe=r"C:\Apps\beta.exe", rect=(300, 300, 700, 700))
        m2.sync()
        self.assertEqual(self.where(m2, b2), (D1, 0), "opens where it was created")
        self.assertTrue(self.visible(b2))

    def test_closed_shortly_before_a_crash_is_put_back(self):
        m, a, b, c = self.arrange()
        self.win.close(b)
        m.sync()
        self.win.advance(10_000)  # crash 10 s later, before its place was forgotten
        m2 = self.manager()
        b2 = self.win.add("Beta", 0, exe=r"C:\Apps\beta.exe")
        m2.sync()
        self.assertEqual(self.where(m2, b2), (D1, 1))

    def test_closed_and_reopened_during_startup_is_not_put_back(self):
        m, a, b, c = self.arrange()
        m.restore_all()
        m2 = self.manager()
        m2.sync()  # b is back on desktop 2
        self.win.close(b)
        m2.sync()
        b2 = self.win.add("Beta", 0, exe=r"C:\Apps\beta.exe")
        m2.sync()
        self.assertEqual(self.where(m2, b2), (D1, 0), "this session's places are never reused")

    def test_places_nobody_took_are_dropped_after_startup(self):
        m, a, b, c = self.arrange()
        for h in (a, b, c):
            self.win.close(h)
        m.sync()
        m.restore_all()
        self.win.reboot()
        m2 = self.manager()
        m2.sync()
        self.assertEqual(len(all_windows(self.places())), 3, "kept while the apps may still open")
        self.win.advance(STARTUP_GRACE_MS + 1)
        m2.sync()
        self.assertEqual(all_windows(self.places()), [])
        a2, b2, c2 = self.reopen()
        m2.sync()
        self.assertEqual(self.where(m2, b2), (D1, 0))

    FF = dict(exe=r"C:\Program Files\Web Browser\browser.exe", cls="BrowserWindowClass")

    def test_crash_puts_identical_windows_back_each_on_its_own_desktop(self):
        # Two browser windows that look exactly alike (same title, same position) on two desktops,
        # and a third on the other display: only the window handle tells them apart
        same = (100, 100, 1500, 1000)
        f1 = self.win.add("Web Browser", 0, rect=same, **self.FF)
        f2 = self.win.add("Web Browser", 0, rect=same, **self.FF)
        f3 = self.win.add("Web Browser", 1, rect=(2000, 100, 3400, 1000), **self.FF)
        m = self.manager()
        m.sync()
        m.create_desktop_and_switch(D1)
        m.move_window_to_desktop(f1, D1, 1)  # f1 on desktop 2, f2 on desktop 1
        m.create_desktop_and_switch(D2)
        m.move_window_to_desktop(f3, D2, 1)
        m.switch_to(D1, 0)
        m.switch_to(D2, 0)
        expected = {f1: (D1, 1), f2: (D1, 0), f3: (D2, 1)}
        self.assertEqual({h: self.where(m, h) for h in expected}, expected)
        # crash (no exit at all), then a fresh start: the journal shows them, places put them back
        for order in ([f1, f2, f3], [f3, f2, f1]):
            self.win.z = [h for h in order] + [h for h in self.win.z if h not in order]
            m2 = self.manager()
            m2.sync()
            self.assertEqual({h: self.where(m2, h) for h in expected}, expected,
                             f"every window back on its own desktop (z-order {order})")
            m2.restore_all()

    def test_reboot_puts_same_app_windows_back_on_their_own_displays(self):
        # Before: browser A near the right edge of display 1 (desktop 2), browser B on display 2
        # (desktop 2). After the reboot both have other titles, and B reopens at the left edge of
        # display 2: in pixels nearer to A's old place than to its own. The display decides.
        a = self.win.add("Inbox - Web Browser", 0, rect=(1000, 100, 1900, 900), **self.FF)
        b = self.win.add("News - Web Browser", 1, rect=(2800, 100, 3700, 900), **self.FF)
        m = self.manager()
        m.sync()
        m.create_desktop_and_switch(D1)
        m.move_window_to_desktop(a, D1, 1)
        m.create_desktop_and_switch(D2)
        m.move_window_to_desktop(b, D2, 1)
        m.switch_to(D1, 0)
        m.switch_to(D2, 0)
        for h in (a, b):  # Windows shuts down: the apps close while DisplayDesktop still runs
            self.win.close(h)
        m.sync()
        m.restore_all()
        with open(self.places_file, encoding="utf-8") as fh:
            snapshot = fh.read()
        for b_first in (True, False):
            with open(self.places_file, "w", encoding="utf-8") as fh:
                fh.write(snapshot)  # the same file for both orders
            self.win.reboot()
            m2 = self.manager()
            # B opens mostly on display 2 (its centre is there) but its left edge is at 1450:
            # 450 px from A's old place, 1350 px from its own
            opened = [("B", 1, (1450, 100, 2450, 900)), ("A", 0, (900, 100, 1800, 900))]
            made = {}
            for name, mon, rect in (opened if b_first else opened[::-1]):
                made[name] = self.win.add(name + " page - Web Browser", mon, rect=rect, **self.FF)
            m2.sync()
            self.assertEqual(self.where(m2, made["A"]), (D1, 1), f"A back on display 1, desktop 2 (B first: {b_first})")
            self.assertEqual(self.where(m2, made["B"]), (D2, 1), f"B back on display 2, desktop 2 (B first: {b_first})")
            self.assertEqual(self.win.windows[made["B"]].rect, (2800, 100, 3700, 900), "at B's own position")
            m2.restore_all()

    def test_positions_are_recorded_every_2_seconds(self):
        m, a, b, c = self.arrange()
        pos = lambda: next(e for e in all_windows(self.places()) if e["Display"] == "MON-A" and e["Desktop"] == 0)  # noqa: E731
        self.assertEqual((pos()["Left"], pos()["Top"]), (10, 20))
        self.win.windows[a].rect = (400, 300, 1000, 800)  # the user moves a (same desktop)
        self.win.advance(1_000)
        m.sync()
        self.assertEqual(pos()["Left"], 10, "not yet (less than 2 s)")
        self.win.advance(1_100)
        m.sync()
        self.assertEqual((pos()["Left"], pos()["Top"]), (400, 300), "recorded within 2 s")

    def test_not_written_when_nothing_changed(self):
        m, a, b, c = self.arrange()
        old = os.path.getmtime(self.places_file) - 100
        os.utime(self.places_file, (old, old))
        for _ in range(10):  # 20 s of 2-second refreshes with nothing moving
            self.win.advance(2_000)
            m.sync()
        self.assertEqual(os.path.getmtime(self.places_file), old)
        self.win.advance(45_000)  # ...but "Updated" is refreshed at least every minute
        m.sync()
        self.assertNotEqual(os.path.getmtime(self.places_file), old)

    def test_save_and_load_do_not_use_or_change_them(self):
        m, a, b, c = self.arrange()
        m.save_desktops()
        m.move_window_to_desktop(a, D1, 1)  # recorded: a on desktop 2 now
        before = self.places()
        m.load_desktops()  # the saved desktops put a back on desktop 1...
        self.assertEqual(self.where(m, a), (D1, 0))
        after = self.places()
        # ...which is recorded as its last known place, like any move; Load itself reads nothing here
        self.assertEqual(sorted(e["Desktop"] for e in all_windows(after) if e["Display"] == "MON-A"), [0, 1])
        self.assertNotEqual(all_windows(before), all_windows(after))
        with open(self.layout_file, encoding="utf-8") as fh:
            self.assertEqual(len(all_windows(json.load(fh))), 3)

    def test_unreadable_file_is_ignored(self):
        m, a, b, c = self.arrange()
        m.restore_all()
        with open(self.places_file, "w") as fh:
            fh.write('{"Windows":[{"Desktop":"x"}]')
        m2 = self.manager()
        m2.sync()
        self.assertEqual([sorted(s) for s in m2.desktops_of(D1)], [sorted([a, b])])


class BrowsersBySite(Base):
    """Several windows of one browser after a reboot: new handles, other pages, all reopened at one
    spot. The site in the title (a title part like "Webmail") decides where each goes; a window whose
    site matches nothing takes a remaining place of that browser."""
    FF = dict(exe=r"C:\Program Files\Web Browser\browser.exe", cls="BrowserWindowClass")

    def where(self, m, h):
        for dev in (D1, D2):
            for i, s in enumerate(m.desktops_of(dev)):
                if h in s:
                    return dev, i
        return None

    def arrange_and_reboot(self, before):
        """before: {title: (monitor index, desktop)}. Returns a manager right after the reboot."""
        m = self.manager()
        made = {t: self.win.add(t, mon, rect=((100, 100, 1500, 1000) if mon == 0 else (2000, 100, 3400, 1000)),
                                **self.FF) for t, (mon, _) in before.items()}
        m.sync()
        for dev in (D1, D2):
            while len(m.desktops_of(dev)) < 2:
                m.create_desktop_and_switch(dev)
        for t, h in made.items():
            mon, desk = before[t]
            m.move_window_to_desktop(h, (D1, D2)[mon], desk)
        m.switch_to(D1, 0)
        m.switch_to(D2, 0)
        for h in made.values():  # Windows restarts: the browser closes while DisplayDesktop runs
            self.win.close(h)
        m.sync()
        m.restore_all()
        self.win.reboot()
        m2 = self.manager()
        m2._places.use_setup(SETUP, {})  # (the setup the first sync picks: tests call assign before any)
        return m2

    BEFORE = {
        "Inbox (3) - Webmail - Web Browser": (0, 0),
        "Cat video - Video Hub — Web Browser": (0, 1),
        "Budget 2026 - Pine Docs - Web Browser": (1, 1),
        "Front page - The News - Web Browser": (1, 0),
    }
    AFTER = {  # same sites, other pages; the news site was not reopened, a weather site was
        "Inbox (7) - Webmail - Web Browser": (D1, 0),
        "Another video - Video Hub — Web Browser": (D1, 1),
        "Trip plan - Pine Docs - Web Browser": (D2, 1),
        "Weather - Web Browser": (D2, 0),  # no site in common: the browser's remaining place
    }

    def test_each_site_goes_back_to_its_own_place(self):
        for order in (list(self.AFTER), list(self.AFTER)[::-1]):
            self.tearDown()  # a fresh start for each order (the last one is cleaned up as usual)
            self.setUp()
            m2 = self.arrange_and_reboot(self.BEFORE)
            made = {t: self.win.add(t, 0, rect=(300, 300, 1200, 900), **self.FF) for t in order}
            m2.sync()
            got = {t: self.where(m2, h) for t, h in made.items()}
            self.assertEqual(got, self.AFTER, f"opened in the order {order}")

    def test_the_site_beats_the_display_and_the_position(self):
        # Webmail reopens on display 2, right where the Docs window used to be: still goes to Webmail's place
        m2 = self.arrange_and_reboot({"Inbox - Webmail - Web Browser": (0, 1),
                                      "Notes - Pine Docs - Web Browser": (1, 1)})
        webmail = self.win.add("Sent - Webmail - Web Browser", 1, rect=(2000, 100, 3400, 1000), **self.FF)
        m2.sync()
        self.assertEqual(self.where(m2, webmail), (D1, 1))

    def test_one_saved_place_still_goes_to_its_own_site(self):
        # A single saved browser place: the site in its title still picks the right one of two windows
        from src.layout import SITE
        m2 = self.arrange_and_reboot({"Inbox - Mail - Web Browser": (0, 1)})
        news = self.win.add("News - Daily - Web Browser", 0, rect=(100, 100, 900, 700), **self.FF)
        mail = self.win.add("Sent - Mail - Web Browser", 0, rect=(1200, 600, 1900, 1000), **self.FF)
        got = m2._places.assign([news, mail], SITE)
        self.assertEqual([h for h, _ in got], [mail], "the Mail window, by its site")

    def test_the_browser_name_alone_is_not_a_site(self):
        # "Web Browser" is in every title: it does not make one place a better match than another
        from src.layout import SITE
        m2 = self.arrange_and_reboot({"A - Site One - Web Browser": (0, 1),
                                      "B - Site Two - Web Browser": (1, 1)})
        h = self.win.add("Weather - Web Browser", 0, **self.FF)
        self.assertEqual(m2._places.assign([h], SITE), [], "no site match")
        m2.sync()
        self.assertIsNotNone(self.where(m2, h), "it still gets a place by the browser name")

    def test_one_odd_title_does_not_make_the_browser_name_a_site(self):
        # A window without the browser's name in its title ("Downloads") must not empty the parts
        # every title shares: "Web Browser" would then count as a site
        from src.layout import SITE
        m2 = self.arrange_and_reboot({"News - Web Browser": (0, 1), "Docs - Web Browser": (1, 1)})
        mail = self.win.add("Mail - Web Browser", 0, **self.FF)
        odd = self.win.add("Downloads", 0, **self.FF)
        self.assertEqual(m2._places.assign([mail, odd], SITE), [], "no site match by the browser name")

    def test_one_odd_saved_title_does_not_make_the_browser_name_a_site(self):
        from src.layout import SITE
        m2 = self.arrange_and_reboot({"News - Web Browser": (0, 1), "Docs - Web Browser": (1, 1),
                                      "Downloads": (0, 0)})
        mail = self.win.add("Mail - Web Browser", 0, **self.FF)
        self.assertEqual(m2._places.assign([mail], SITE), [], "no site match by the browser name")

    def test_repeated_odd_open_titles_do_not_make_the_browser_name_a_site(self):
        from src.layout import SITE
        m2 = self.arrange_and_reboot({"News - Web Browser": (0, 1)})
        odd = [self.win.add("Downloads", 0, **self.FF) for _ in range(3)]
        mail = self.win.add("Mail - Web Browser", 0, **self.FF)
        got = m2._places.assign(odd + [mail], SITE)
        self.assertNotIn(mail, [h for h, _ in got], "no site match by the browser name")

    def test_repeated_odd_open_titles_like_a_saved_one_do_not_make_the_browser_name_a_site(self):
        from src.layout import SITE
        m2 = self.arrange_and_reboot({"News - Web Browser": (0, 1), "Downloads": (1, 1)})
        odd = [self.win.add("Downloads", 0, **self.FF) for _ in range(3)]
        mail = self.win.add("Mail - Web Browser", 0, **self.FF)
        got = m2._places.assign(odd + [mail], SITE)
        self.assertNotIn(mail, [h for h, _ in got], "no site match by the browser name")

    def test_odd_saved_titles_as_frequent_as_the_browser_name_do_not_make_it_a_site(self):
        from src.layout import program_parts
        titles = [{"news", "web browser"}, {"docs", "web browser"}, {"downloads"}, {"downloads"}]
        self.assertIn("web browser", program_parts(titles))
        self.assertEqual(program_parts([{"a", "x"}, {"b", "y"}]), set(), "nothing repeats: nothing shared")
        self.assertEqual(program_parts([{"news", "web browser"}]), {"news", "web browser"}, "one title: all")

    def test_unread_counters_are_ignored(self):
        from src.layout import title_parts
        self.assertEqual(title_parts("Inbox (3) - Webmail - Web Browser"),
                         title_parts("Inbox (12) - Webmail - Web Browser"))
        self.assertEqual(title_parts("(99+) Home | X"), title_parts("(4) Home | X"))

    def test_unsaved_marks_are_ignored(self):
        from src.layout import title_parts
        self.assertEqual(title_parts("*b.txt - Notepad"), title_parts("b.txt - Notepad"))
        self.assertEqual(title_parts("● main.py - project - Code Editor"),
                         title_parts("main.py - project - Code Editor"))


class ThreeOfAKind(Base):
    """Three windows of the same app (the code takes any number), moved around a lot, then brought
    back after a crash and after a reboot. Order of evidence: the same window, the same app
    instance (process), the title (exact, then its parts), then any window of that app. Windows
    that nothing tells apart (three "Untitled - Notepad") still each get a place of their own: the
    tiles come back as tiles, never stacked on one spot."""
    NOTEPAD = dict(exe=r"C:\Windows\System32\notepad.exe", cls="Notepad")
    STACKED = (400, 300, 1000, 800)  # where Windows opens all of them after a reboot

    def where(self, m, h):
        for dev in (D1, D2):
            for i, s in enumerate(m.desktops_of(dev)):
                if h in s:
                    return dev, i
        return None

    def state(self, m, h):
        w = self.win.windows[h]
        return self.where(m, h), w.rect, w.show_cmd

    def recorded(self):
        with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
            return sorted(((e["Display"], e["Desktop"]), (e["Left"], e["Top"], e["Right"], e["Bottom"]), e["ShowCmd"])
                          for e in all_windows(json.load(fh)))

    def expect_recorded(self, m, wins, note):
        ids = {D1: "MON-A", D2: "MON-B"}
        want = sorted(((ids[st[0][0]], st[0][1]), st[1], st[2]) for st in (self.state(m, h) for h in wins))
        self.assertEqual(self.recorded(), want, f"recorded within 2 s: {note}")

    def settle(self, m):
        self.win.advance(2_100)  # one 2-second refresh
        m.sync()

    def move(self, m, h, rect=None, dev=None, desk=None, show_cmd=None):
        """The user moves a window: to another desktop/display, and/or drags or resizes it."""
        if dev is not None:
            m.move_window_to_desktop(h, dev, desk)
        if rect is not None:
            self.win.windows[h].rect = rect
        if show_cmd is not None:
            self.win.windows[h].show_cmd = show_cmd
        self.settle(m)

    def setup_desktops(self, m):
        for dev in (D1, D2):
            while len(m.desktops_of(dev)) < 3:
                m.create_desktop_and_switch(dev)
            m.switch_to(dev, 0)

    def shuffle_around(self, wins):
        """Three rounds of rearranging (by the DisplayDesktop running now: self.m); after each one
        the record must match what is on screen."""
        a, b, c = wins
        rounds = [
            # tiled side by side on display 1, the third on display 2
            [(a, dict(rect=(0, 0, 960, 1040))), (b, dict(rect=(960, 0, 1920, 1040))),
             (c, dict(dev=D2, desk=0, rect=(1920, 0, 3840, 1040)))],
            # re-tiled in quarters, spread over desktops and displays
            [(a, dict(dev=D1, desk=1, rect=(0, 0, 960, 520))), (b, dict(rect=(960, 520, 1920, 1040))),
             (c, dict(dev=D2, desk=2, rect=(2880, 0, 3840, 520)))],
            # moved again: one maximized, one dragged to the other display
            [(a, dict(dev=D2, desk=1, rect=(1920, 520, 2880, 1040))), (b, dict(show_cmd=N.SW_SHOWMAXIMIZED)),
             (c, dict(dev=D1, desk=2, rect=(100, 600, 900, 1000)))],
        ]
        for n, moves in enumerate(rounds, start=1):
            for h, how in moves:
                self.move(self.m, h, **how)
            self.expect_recorded(self.m, wins, f"round {n}")
            yield n

    def stacked(self, titles, pids):
        return [self.win.add(t, 0, rect=self.STACKED, pid=p, **self.NOTEPAD) for t, p in zip(titles, pids)]

    # ---- A: identical windows, nothing but their places to go on

    def test_untitled_notepads_come_back_tiled_not_stacked(self):
        titles = ["Untitled - Notepad"] * 3
        wins = [self.win.add(t, 0, pid=7001 + i, **self.NOTEPAD) for i, t in enumerate(titles)]
        m = self.manager()
        m.sync()
        self.setup_desktops(m)
        self.m = m
        for n in self.shuffle_around(wins):
            before = sorted(self.state(self.m, h) for h in wins)
            desks = {d: len(self.m.desktops_of(d)) for d in (D1, D2)}
            # DisplayDesktop crashes now (same boot, same windows): the next one puts every window
            # back exactly where it is, and carries on (the next round uses it)
            self.m = self.manager()
            self.m.sync()
            self.assertEqual(sorted(self.state(self.m, h) for h in wins), before, f"after a crash in round {n}")
            self.assertEqual({d: len(self.m.desktops_of(d)) for d in (D1, D2)}, desks,
                             f"every desktop is back too, empty ones included (round {n})")
        final = sorted(self.state(self.m, h) for h in wins)
        self.m.restore_all()
        for order in ([7101, 7102, 7103], [7103, 7102, 7101]):
            with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
                snapshot = fh.read()
            self.win.reboot()
            m2 = self.manager()
            new = self.stacked(titles, order)  # all three open on the same spot
            m2.sync()
            got = sorted(self.state(m2, h) for h in new)
            self.assertEqual(got, final, f"every tile, desktop and state back (opened {order})")
            self.assertEqual(len({self.win.windows[h].rect for h in new}), 3, "no two stacked on one spot")
            m2.restore_all()
            with open(os.path.join(self.dir, PLACES_FILE), "w", encoding="utf-8") as fh:
                fh.write(snapshot)  # same starting point for the other order

    # ---- B: documents: the title tells each one apart

    def test_documents_each_come_back_to_their_own_place(self):
        titles = ["a.txt - Notepad", "*b.txt - Notepad", "c.txt - Notepad"]  # b has unsaved changes
        wins = [self.win.add(t, 0, pid=7001 + i, **self.NOTEPAD) for i, t in enumerate(titles)]
        m = self.manager()
        m.sync()
        self.setup_desktops(m)
        self.m = m
        for _ in self.shuffle_around(wins):
            pass
        final = {t: self.state(m, h) for t, h in zip(["a", "b", "c"], wins)}
        m.restore_all()
        self.win.reboot()
        m2 = self.manager()
        # reopened stacked, in the opposite order, b saved meanwhile (no "*")
        new = dict(zip(["c", "b", "a"], self.stacked(["c.txt - Notepad", "b.txt - Notepad", "a.txt - Notepad"],
                                                       [7201, 7202, 7203])))
        m2.sync()
        self.assertEqual({k: self.state(m2, h) for k, h in new.items()}, final, "each file in its own place")

    # ---- C: the same app instance beats the title

    def test_the_same_instance_beats_the_title(self):
        wins = [self.win.add(t, 0, pid=p, **self.NOTEPAD)
                for t, p in (("a.txt - Notepad", 7001), ("b.txt - Notepad", 7002), ("c.txt - Notepad", 7003))]
        m = self.manager()
        m.sync()
        self.setup_desktops(m)
        self.m = m
        for _ in self.shuffle_around(wins):
            pass
        final = {self.win.windows[h].pid: self.state(m, h) for h in wins}
        # DisplayDesktop crashes; each Notepad then recreates its window (new handle, same process)
        # with another file open: the titles now point at the wrong places, the processes do not
        for h in wins:
            self.win.close(h)
        m2 = self.manager()
        again = self.stacked(["b.txt - Notepad", "c.txt - Notepad", "a.txt - Notepad"], [7001, 7002, 7003])
        m2.sync()
        self.assertEqual({self.win.windows[h].pid: self.state(m2, h) for h in again}, final,
                         "each instance back in its own place, whatever file it shows now")


class LoadBrowserWindows(Base):
    """Two windows of one browser (one process), each with its own page, saved. One is closed and
    opened again on another desktop or display; Load puts it back, and each display shows the
    desktop it showed when saved, so the window is in view again."""
    BROWSER = dict(exe=r"C:\Apps\browser\browser.exe", cls="BrowserWindowClass", pid=4242)

    def where(self, m, h):
        for dev in (D1, D2):
            for i, s in enumerate(m.desktops_of(dev)):
                if h in s:
                    return dev, i
        return None

    def check(self, reopened_title, on_other_display):
        a = self.win.add("Page A - Browser", 0, rect=(100, 100, 1000, 800), **self.BROWSER)
        b = self.win.add("Page B - Browser", 1, rect=(2100, 100, 3000, 800), **self.BROWSER)
        m = self.manager()
        m.sync()
        m.save_desktops()  # both on desktop 1 of their display, desktop 1 showing
        self.win.close(b)
        m.sync()
        self.win.advance(STARTUP_GRACE_MS + 1)
        dev, mon = (D1, 0) if on_other_display else (D2, 1)
        self.win.cursor_device = dev
        m.create_desktop_and_switch(dev)  # the user goes to a new desktop and opens it there
        b2 = self.win.add(reopened_title, mon, rect=((300, 300, 1200, 900) if mon == 0 else (2300, 300, 3200, 900)),
                          **self.BROWSER)
        m.sync()
        self.assertEqual(self.where(m, b2), (dev, 1))
        m.load_desktops()
        self.assertEqual(self.where(m, b2), (D2, 0), "back on its saved desktop and display")
        self.assertEqual(self.win.windows[b2].rect, (2100, 100, 3000, 800), "at its saved position")
        self.assertEqual((m.current_of(D1), m.current_of(D2)), (0, 0), "each display shows its saved desktop")
        self.assertTrue(self.visible(b2) and self.visible(a), "so both windows are in view")

    def test_reopened_on_another_desktop_same_page(self):
        self.check("Page B - Browser", on_other_display=False)

    def test_reopened_on_another_desktop_new_page(self):
        self.check("New Tab - Browser", on_other_display=False)

    def test_reopened_on_another_display_same_page(self):
        self.check("Page B - Browser", on_other_display=True)

    def test_reopened_on_another_display_new_page(self):
        self.check("New Tab - Browser", on_other_display=True)


class LastShownDesktop(Base):
    """display.desktop.showing.json: the desktop each monitor showed last, remembered on every switch and shown again
    at startup. Separate from what "Save" stores; "Load" never changes it."""

    @property
    def showing_file(self):
        return os.path.join(self.dir, "display.desktop.showing.json")

    @property
    def last_shown(self):
        if not os.path.exists(self.showing_file):
            return None
        with open(self.showing_file, encoding="utf-8") as fh:
            return setup_of(json.load(fh)).get("Showing")  # (the two-monitor setup's)

    def three_desktops(self):
        """c on desktop 1, a on desktop 2, b on desktop 3 (showing); saved with desktop 3 showing."""
        a, b, c = self.win.add("A"), self.win.add("B"), self.win.add("C")
        m = self.manager()
        self.win.foreground = a
        move_active(m, self.win, +1)
        m.switch_relative(-1)
        self.win.foreground = b
        move_active(m, self.win, +1)
        move_active(m, self.win, +1)
        self.assertEqual(m.desktops_of(D1), [[c], [a], [b]])
        m.save_desktops()
        return m, a, b, c

    def restart(self, m):
        m.restore_all()  # what every exit path does
        m2 = self.manager()
        m2.sync()
        return m2

    def test_every_switch_is_remembered_per_monitor(self):
        m, a, b, c = self.three_desktops()
        self.assertEqual(self.last_shown, {"MON-A": 2})
        m.switch_relative(-1)
        self.assertEqual(self.last_shown, {"MON-A": 1})
        self.win.cursor_device = D2
        self.win.foreground = self.win.add("D", 1)
        move_active(m, self.win, +1)
        self.assertEqual(self.last_shown, {"MON-A": 1, "MON-B": 1})

    def test_startup_shows_last_shown_not_saved(self):
        m, a, b, c = self.three_desktops()
        m.switch_relative(-1)  # after saving, the user goes to desktop 2
        m2 = self.restart(m)
        self.assertEqual(m2.current_of(D1), 1, "back on the desktop shown last, not the saved one")
        self.assertTrue(self.visible(a))
        self.assertFalse(self.visible(b) or self.visible(c))

    def test_load_desktops_does_not_change_it(self):
        m, a, b, c = self.three_desktops()
        m.switch_relative(-1)
        m.load_desktops()
        self.assertEqual(m.current_of(D1), 2, "Load shows the saved desktop")
        self.assertEqual(self.last_shown, {"MON-A": 1}, "Load does not overwrite the last shown one")
        m2 = self.restart(m)
        self.assertEqual(m2.current_of(D1), 1)

    def test_never_switched_shows_the_first_desktop(self):
        m, a, b, c = self.three_desktops()
        os.remove(self.showing_file)  # nothing remembered
        m2 = self.restart(m)
        self.assertEqual(m2.current_of(D1), 0)
        self.assertEqual(m2.desktops_of(D1), [[c], [a], [b]], "windows still back on their desktops")
        self.assertIsNone(self.last_shown, "bringing desktops back is not a switch by the user")

    def test_clear_desktops_remembers_first_desktop(self):
        m, a, b, c = self.three_desktops()
        m.clear_desktops()
        self.assertEqual(self.last_shown, {"MON-A": 0, "MON-B": 0})

    def test_moving_a_desktop_updates_the_number(self):
        m, a, b, c = self.three_desktops()  # showing desktop 3
        m.move_desktop_to_monitor(D1, 0, D2)  # desktop 1 goes away: the shown one is now number 2
        self.assertEqual(m.current_of(D1), 1)
        self.assertEqual(self.last_shown["MON-A"], 1)

    def test_unreadable_file_is_ignored(self):
        m, a, b, c = self.three_desktops()
        with open(self.showing_file, "w") as fh:
            fh.write('{"Showing":{"MON-A":"x"')
        m2 = self.restart(m)
        self.assertEqual(m2.current_of(D1), 0, "nothing remembered: the first desktop")

    def test_not_written_when_nothing_changes(self):
        m, a, b, c = self.three_desktops()
        old = os.path.getmtime(self.showing_file) - 100
        os.utime(self.showing_file, (old, old))
        for _ in range(3):
            m.sync()
            m.switch_to(D1, 2)  # already showing
        self.assertEqual(os.path.getmtime(self.showing_file), old)


class Ending(Base):
    """end(): what every way the app ends calls. Every window comes back, then nothing that still
    runs before the process is gone (timer, shortcut key, an error message's message loop) can hide
    a window again or record the restored windows over the last snapshot."""

    def arrange(self):
        a = self.win.add("A")
        b = self.win.add("B", 1)
        m = self.manager()
        m.sync()
        self.new_desktop(m)               # D1: [a] hidden, [] shown
        m.move_window_to_desktop(b, D2, 0)
        self.win.cursor_device = D2
        self.new_desktop(m)               # D2: [b] hidden, [] shown
        m.snapshot()
        return m, a, b

    def test_end_shows_everything_and_refuses_every_later_change(self):
        m, a, b = self.arrange()
        with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
            places = fh.read()
        m.end()
        self.assertTrue(m.ended)
        self.assertTrue(self.visible(a) and self.visible(b), "every window shown")
        before = (m.desktops_of(D1), m.desktops_of(D2))
        c = self.win.add("C opened meanwhile")
        self.win.advance(5_000)
        m.sync()                              # the 600 ms timer
        m.switch_relative(-1)                 # a shortcut key
        m.switch_to(D1, 0)
        self.new_desktop(m)                   # the overview's "+"
        m.move_window_to_desktop(a, D1, 1)
        m.move_desktop_to_monitor(D1, 0, D2)
        m.swap_desktops(D1, 0, 1)
        m.delete_desktop(D1, 1)
        m.focus_window(a)
        m.load_desktops()
        m.clear_desktops()
        m.snapshot()
        self.assertEqual(m.save_desktops(), 0)
        self.assertTrue(self.visible(a) and self.visible(b) and self.visible(c), "nothing hidden again")
        self.assertEqual((m.desktops_of(D1), m.desktops_of(D2)), before, "nothing merged or moved")
        self.assertEqual(m.hidden, set())
        with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), places, "the last snapshot is kept, not overwritten")
        self.assertFalse(os.path.exists(self.layout_file), "nothing saved")

    def test_the_next_start_puts_every_window_back_on_its_desktop(self):
        m, a, b = self.arrange()
        m.end()
        m.sync()  # (what a timer firing during the error message used to do)
        m2 = self.manager()
        m2.sync()
        self.assertIn(a, m2.desktops_of(D1)[0])
        self.assertIn(b, m2.desktops_of(D2)[0])
        self.assertEqual((len(m2.desktops_of(D1)), len(m2.desktops_of(D2))), (2, 2), "both desktops back")

    def test_a_switch_the_app_ends_during_hides_nothing_and_writes_nothing(self):
        """An error while the slide starts (switch_starting) ends the app in the middle of a switch:
        when the switch resumes it must not hide the windows the ending just showed, nor record
        anything over the last snapshot."""
        a = self.win.add("A")
        m = self.manager()
        m.sync()

        def files():
            found = {}
            for n in os.listdir(self.dir):
                if n.endswith(".json"):
                    with open(os.path.join(self.dir, n), encoding="utf-8") as fh:
                        found[n] = fh.read()
            return found
        at_end = {}

        def crash(*_):  # what on_fatal does: the last snapshot, then every window shown
            m.snapshot()
            m.end()
            at_end.update(files())
        m.switch_starting.append(crash)
        self.new_desktop(m)
        self.assertTrue(self.visible(a), "still shown")
        self.assertEqual(m.hidden, set())
        self.assertIsNone(self.journal, "no journal written after the end")
        self.assertTrue(at_end)
        self.assertEqual(files(), at_end, "places and the shown desktop stay as the last snapshot left them")

    def test_restore_all_alone_keeps_managing(self):
        # (used by tests and by restarts in place; only end() stops the manager)
        m, a, b = self.arrange()
        m.restore_all()
        self.assertFalse(m.ended)
        self.win.cursor_device = D1
        m.switch_to(D1, 0)
        self.assertTrue(self.visible(a))


class MovingAcrossMonitors(Base):
    """Moving windows and desktops to another monitor: done through the window placement, so a
    minimized window really moves, and a window of a desktop that is not on screen is never shown
    (no flash, no focus) on the way."""

    def shown_calls(self, h, since):
        """Every call after `since` that showed window h (show commands, placements that show)."""
        return [c for c in self.win.calls[since:]
                if c[1] == h and ((c[0] == "show" and c[2] != N.SW_HIDE) or (c[0] == "placement" and c[2] != N.SW_HIDE))]

    def test_a_minimized_window_moves_with_its_restore_position(self):
        a = self.win.add("A", 0, rect=(960, 520, 1460, 820), show_cmd=2)
        m = self.manager()
        m.sync()
        since = len(self.win.calls)
        m.move_window_to_desktop(a, D2, 0)
        w = self.win.windows[a]
        self.assertEqual(w.rect, (1920 + 960, 520, 1920 + 1460, 820), "restores on the other monitor")
        self.assertEqual(w.show_cmd, 2, "still minimized")
        self.assertEqual(self.win.monitor_device_of_window(a), D2)
        self.assertFalse([c for c in self.win.calls[since:] if c[0] == "show" and c[1] == a],
                         "not restored (no show commands)")
        m.sync()
        self.assertEqual(m.desktops_of(D2), [[a]], "and sync keeps it there")

    def test_a_hidden_maximized_desktop_moves_without_any_window_showing(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        b = self.win.add("B", 0, rect=(200, 200, 1000, 800))
        for h in (a, b):
            self.win.show_window(h, N.SW_MAXIMIZE)
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # D1 desktop 1 (with a and b) is not on screen now
        self.assertFalse(self.visible(a) or self.visible(b))
        since = len(self.win.calls)
        m.move_desktop_to_monitor(D1, 0, D2)
        for h in (a, b):
            w = self.win.windows[h]
            self.assertFalse(self.shown_calls(h, since), f"never shown on the way: {self.win.calls[since:]}")
            self.assertFalse(w.visible)
            self.assertEqual(w.show_cmd, 3, "still maximized")
            self.assertEqual(self.win.monitor_device_of_window(h), D2, "its maximized frame is on D2")
            self.assertEqual(w.frame, (1920 - 8, -8, 3840 + 8, 1040 + 8), "covers D2's work area")
            self.assertGreaterEqual(w.rect[0], 1920, "restores on D2 too")
        self.assertNotIn(("fg", a), self.win.calls[since:], "never activated")
        m.switch_to(D2, len(m.desktops_of(D2)) - 1)
        self.assertTrue(self.visible(a) and self.visible(b))
        self.assertTrue(self.win.is_zoomed(a) and self.win.monitor_device_of_window(a) == D2,
                        "shown maximized on D2")

    def test_a_visible_window_moved_to_a_hidden_desktop_is_hidden_before_it_moves(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        self.win.show_window(a, N.SW_MAXIMIZE)
        m = self.manager()
        m.sync()
        self.win.cursor_device = D2
        self.new_desktop(m)  # D2 shows its desktop 2; desktop 1 is not on screen
        since = len(self.win.calls)
        m.move_window_to_desktop(a, D2, 0)
        calls = [c for c in self.win.calls[since:] if c[1] == a]
        self.assertEqual(calls[0][:3], ("show", a, N.SW_HIDE), f"hidden first: {calls}")
        self.assertFalse(self.shown_calls(a, since))
        self.assertFalse(self.visible(a))
        self.assertEqual(self.win.monitor_device_of_window(a), D2)
        self.assertEqual(self.win.windows[a].show_cmd, 3)

    def test_a_visible_maximized_window_moved_to_a_shown_desktop_is_maximized_there(self):
        a = self.win.add("A", 0, show_cmd=3)
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(a, D2, 0)
        self.assertTrue(self.visible(a) and self.win.is_zoomed(a))
        self.assertEqual(self.win.monitor_device_of_window(a), D2)

    def test_a_window_that_is_not_responding_is_moved_without_waiting(self):
        a = self.win.add("A", 0, rect=(960, 520, 1460, 820), hung=True)
        m = self.manager()
        m.sync()
        since = len(self.win.calls)
        m.move_window_to_desktop(a, D2, 0)
        calls = [c for c in self.win.calls[since:] if c[1] == a]
        self.assertTrue(calls and all(c[0] == "pos" and c[6] & N.SWP_ASYNCWINDOWPOS for c in calls), calls)


class DockedBars(Base):
    """A window's placement is in workspace coordinates: measured from the top-left of the main
    display's work area. With a taskbar or app bar docked at the main display's left or top, they
    are not screen coordinates, and windows moved between monitors must still land inside the
    target monitor's work area."""

    def dock(self, left=0, top=0):
        main = self.win.screen_list[0]
        main.work = (left, top, main.work[2], main.work[3])

    def test_a_window_moved_onto_the_main_display_stays_inside_it(self):
        self.dock(left=48)
        a = self.win.add("A", 1, rect=(2940, 100, 3840, 700))  # flush right on the second monitor
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(a, D1, 0)
        l, t, r, b = self.win.windows[a].rect
        self.assertTrue(48 <= l and r <= 1920, f"inside the main display's work area: {(l, t, r, b)}")
        self.assertEqual(r, 1920, "flush right there too")

    def test_a_window_moved_off_the_main_display_keeps_its_place(self):
        self.dock(top=48)
        a = self.win.add("A", 0, rect=(100, 48, 900, 648))  # at the top of the main work area
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(a, D2, 0)
        self.assertEqual(self.win.windows[a].rect[1], 0, "at the top of the second monitor's work area")
        m.move_window_to_desktop(a, D1, 0)
        self.assertEqual(self.win.windows[a].rect[1], 48, "and back at the top of the main one")

    def test_a_window_of_an_unplugged_display_lands_inside_the_main_one(self):
        self.dock(left=48)
        a = self.win.add("A", 1, rect=(2940, 100, 3840, 700))
        m = self.manager()
        m.sync()
        del self.win.screen_list[1]
        m.sync()
        l, t, r, b = self.win.windows[a].rect
        self.assertTrue(48 <= l and r <= 1920, f"inside the main display's work area: {(l, t, r, b)}")

    def test_tool_windows_use_screen_coordinates(self):
        self.dock(left=48)
        a = self.win.add("Tools", 1, rect=(2940, 100, 3840, 700),
                         exstyle=N.WS_EX_TOOLWINDOW | N.WS_EX_APPWINDOW)
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(a, D1, 0)
        self.assertEqual(self.win.windows[a].rect[2], 1920)


class UnplugDpi(Base):
    """A DPI-unaware window sees the desktop scaled (here by 2/3). Its placement is read and
    written in those units, so the displays must be read in the same units."""

    def test_a_dpi_unaware_window_lands_at_the_same_relative_place_on_the_main_display(self):
        a = self.win.add("Old app", 1, rect=(1920 + 960, 520, 1920 + 1460, 820), dpi_scale=2 / 3)
        m = self.manager()
        m.sync()
        del self.win.screen_list[1]
        m.sync()
        l, t, r, b = self.win.windows[a].rect
        self.assertTrue(abs(l - 960) <= 2 and abs(t - 520) <= 2, f"middle of the main display: {(l, t, r, b)}")
        self.assertTrue(r <= 1920 and b <= 1040, "inside it")
        self.assertIn(a, m.desktops_of(D1)[0])

    def test_a_dpi_unaware_window_already_on_the_main_display_is_not_touched(self):
        a = self.win.add("Old app", 0, rect=(1500, 300, 1800, 600), dpi_scale=2 / 3)
        b = self.win.add("B", 1)
        m = self.manager()
        m.sync()
        since = len(self.win.calls)
        del self.win.screen_list[1]
        m.sync()
        self.assertNotIn(("placement", a), [c[:2] for c in self.win.calls[since:]],
                         "its scaled position is compared with the scaled display")


class LoadFloatingWindows(Base):
    def test_a_hidden_palette_never_takes_a_saved_main_windows_place(self):
        forms = dict(exe=r"C:\Apps\forms.exe", cls="WindowsForms10.Window")
        m1 = self.win.add("Doc A", 0, **forms)
        m2 = self.win.add("Doc B", 0, **forms)
        palette = self.win.add("Tools", 0, anchor=m2, exstyle=N.WS_EX_TOOLWINDOW, **forms)
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m.switch_to(D1, 0)
        m.move_window_to_desktop(m2, D1, 1)   # its palette is hidden with it
        self.assertFalse(self.visible(palette))
        self.assertEqual(m.save_desktops(), 2)
        self.win.close(m1)                    # one saved entry has no window now
        m.load_desktops()
        self.assertFalse(any(palette in s for dev in (D1, D2) for s in m.desktops_of(dev)),
                         "the palette is not a desktop window")
        self.assertFalse(self.visible(palette), "and stays hidden with its main window")
        m.switch_to(D1, 1)
        self.assertTrue(self.visible(m2) and self.visible(palette), "it comes back with its main window")


class SameBoot(Base):
    def test_rule(self):
        from src.places import BOOT_TOLERANCE_S, same_boot
        w = self.win
        now_boot = w.wall_time() - w.tick / 1000
        written = lambda uptime: now_boot + uptime / 1000  # noqa: E731 - written in this boot
        self.assertTrue(same_boot(w.tick - 5000, written(w.tick - 5000), w))
        self.assertTrue(same_boot(w.tick - 5000, None, w), "older files: uptime only")
        self.assertFalse(same_boot(w.tick + 1, written(w.tick + 1), w), "uptime ahead of now: an older boot")
        older_boot = now_boot - 3600
        self.assertFalse(same_boot(w.tick - 5000, older_boot + (w.tick - 5000) / 1000, w),
                         "a boot an hour earlier, although its uptime is lower than now")
        self.assertTrue(same_boot(1000, now_boot + 1 + BOOT_TOLERANCE_S - 1, w), "clock jitter tolerated")
        for bad in ("1", 1.5, True, None):
            self.assertFalse(same_boot(bad, None, w), repr(bad))
        self.assertFalse(same_boot(1000, "x", w))

    def reboot_later(self):
        """The computer restarts, and DisplayDesktop starts later in the new boot than the files
        were written in the old one (so the uptime alone would say 'same boot')."""
        old_uptime = self.win.tick
        self.win.reboot()
        self.win.tick = old_uptime + 3_600_000
        return old_uptime

    def test_a_journal_from_an_earlier_boot_shows_nothing(self):
        a = self.win.add("A", 0, pid=4242, cls="X")
        m = self.manager()
        self.new_desktop(m)
        journal = self.journal
        self.reboot_later()
        self.win._next = a  # an unrelated invisible window got the same handle, pid and class
        b = self.win.add("Other app", visible=False, pid=4242, cls="X")
        self.assertEqual(a, b)
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump(journal, fh)
        self.manager()
        self.assertFalse(self.visible(b), "not shown: the journal is from another boot")

    def test_a_journal_from_this_boot_still_recovers(self):
        a = self.win.add("A", 0, pid=4242, cls="X")
        m = self.manager()
        self.new_desktop(m)  # a hidden; the app "crashes" here (no restore)
        self.win.advance(60_000)
        self.manager()
        self.assertTrue(self.visible(a))

    def test_a_journal_still_recovers_after_the_clock_was_set(self):
        """The clock is corrected (time sync after a dual boot, the user) between hiding and the
        crash, or between the crash and the restart: the windows must still come back."""
        for hours in (+1, -5):
            with self.subTest(hours=hours):
                a = self.win.add("A", 0, pid=4242, cls="X")
                m = self.manager()
                self.new_desktop(m)  # a hidden, journal written
                self.win.set_clock(hours * 3600)
                self.win.advance(60_000)
                self.manager()       # the restart after the crash
                self.assertTrue(self.visible(a), "shown again")
                self.assertIsNone(self.journal)
                self.win.close(a)

    def test_a_reused_pid_in_the_same_boot_is_not_the_same_process(self):
        a = self.win.add("A", 0, pid=4242, cls="X")
        m = self.manager()
        self.new_desktop(m)
        journal = self.journal
        self.win.close(a)  # its program exits; a new one gets the same pid, handle and class
        self.win._next = a
        b = self.win.add("Other app", visible=False, pid=4242, cls="X")
        self.win.process_started = lambda pid: 1  # (another start time)
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump(journal, fh)
        self.manager()
        self.assertFalse(self.visible(b))

    def test_an_older_journal_without_start_times_still_recovers(self):
        a = self.win.add("A", 0, pid=4242, cls="X")
        self.win.windows[a].visible = False
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": self.win.tick - 1000, "Windows": [{"Handle": a, "Pid": 4242, "Class": "X"}]}, fh)
        self.manager()
        self.assertTrue(self.visible(a))

    def test_places_from_an_earlier_boot_never_bind_a_window_by_handle(self):
        a = self.win.add("Alpha", 0, pid=4242, exe=r"C:\Apps\alpha.exe", cls="AlphaClass")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m.move_window_to_desktop(a, D1, 1)  # on the shown desktop 2
        m.snapshot()
        m.restore_all()
        self.reboot_later()
        self.win._next = a  # the same handle and pid again, an unrelated program
        b = self.win.add("Unrelated", 0, pid=4242, exe=r"C:\Apps\other.exe", cls="OtherClass")
        m2 = self.manager()
        m2.sync()
        self.assertIn(b, m2.desktops_of(D1)[m2.current_of(D1)], "adopted where it is, not put on a desktop")
        self.assertTrue(self.visible(b))


class CaptureBudget(Base):
    def test_load_captures_previews_for_the_pass_not_for_every_window(self):
        hs = [self.win.add(f"W{i}", 0, exe=rf"C:\Apps\w{i}.exe") for i in range(8)]
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        for h in hs:
            m.move_window_to_desktop(h, D1, 1)  # all on desktop 2 (shown)
        m.switch_to(D1, 0)
        m.save_desktops()                        # saved: all on desktop 2, desktop 1 shown
        for h in hs:
            m.move_window_to_desktop(h, D1, 0)  # back on desktop 1 (shown)
        self.shots.captured.clear()
        self.shots.capture_s = 0.06              # a PrintWindow can take tens of ms
        m.load_desktops()                        # puts all 8 back on desktop 2 (hidden)
        self.assertTrue(all(not self.visible(h) for h in hs))
        self.assertTrue(1 <= len(self.shots.captured) <= 2,
                        f"captures stop after the pass's 50 ms ({len(self.shots.captured)} of 8)")

    def test_a_switch_captures_only_windows_without_a_preview(self):
        a, b, c = self.win.add("A"), self.win.add("B"), self.win.add("C")
        m = self.manager()
        m.sync()
        self.shots.get = lambda h: SimpleNamespace(at=time.monotonic()) if h == a else None  # (background)
        self.shots.failed_recently = lambda h, s: h == c          # (C's capture gave nothing a moment ago)
        self.new_desktop(m)
        self.assertEqual(self.shots.captured, [b], "a switch never waits on a capture it need not take")


class BackgroundPreviews(Base):
    """Previews of the shown desktops' windows are taken in the background (no switch waits on one)."""

    def shot(self, age_s: float):
        return SimpleNamespace(at=time.monotonic() - age_s)

    def test_the_shown_windows_that_need_one_are_asked_for(self):
        new, old, used, idle, failed = (self.win.add(t) for t in ("New", "Old", "Used", "Idle", "Failed"))
        m = self.manager()
        m.sync()
        shots = {old: self.shot(DM.PREVIEW_MAX_AGE_S + 1), used: self.shot(DM.PREVIEW_FRESH_S + 1),
                 idle: self.shot(DM.PREVIEW_FRESH_S + 1)}
        self.shots.get = shots.get
        self.shots.failed_recently = lambda h, s: h == failed
        self.win.set_foreground(used)  # in use since its preview
        m.sync()
        self.assertEqual(sorted(self.shots.refreshed[-1]), sorted([new, old, used]))
        shots[used] = self.shot(1.0)
        m.sync()
        self.assertNotIn(used, self.shots.refreshed[-1], "a window in use: at most every PREVIEW_FRESH_S")

    def test_hidden_windows_are_not_asked_for(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # A hidden on desktop 1
        m.sync()
        self.assertNotIn(a, self.shots.refreshed[-1])

    def test_a_capture_of_a_window_hidden_meanwhile_is_dropped(self):
        a, b = self.win.add("A"), self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m.switch_to(D1, 0)
        started = time.perf_counter()
        m.move_window_to_desktop(b, D1, 1)  # B hidden after the capture began
        m.move_window_to_desktop(b, D1, 0)  # and shown again before the result is collected
        self.assertTrue(self.visible(b))
        kept = []
        self.shots.collect = lambda keep: kept.extend(h for h in (a, b) if keep(h, started))
        m._collect_previews()
        self.assertEqual(kept, [a])


class SnapshotWorker(unittest.TestCase):
    """WindowSnapshots' background worker (a stand-in capture: no real window)."""

    def setUp(self):
        from src import snapshots as SN
        self.SN = SN
        self.store = SN.WindowSnapshots()
        self.addCleanup(self.store.clear)
        self.taken = []
        self.store.take = lambda h: self.taken.append(h) or (SN.Shot(0, 1, 1, time.monotonic()) if h != 9 else None)

    def wait_taken(self, n):
        end = time.monotonic() + 2
        while len(self.store._done) < n and time.monotonic() < end:
            time.sleep(0.005)

    def test_captures_are_taken_in_the_background_and_installed_by_collect(self):
        with mock.patch.object(self.SN, "REFRESH_GAP_S", 0.0), mock.patch.object(self.SN.G, "DeleteObject"):
            self.store.refresh([7, 8, 9])
            self.wait_taken(3)
            self.assertIsNone(self.store.get(7), "not installed before collect (the main thread does it)")
            self.store.collect(lambda h, started: h != 8)
            self.assertIsNotNone(self.store.get(7))
            self.assertIsNone(self.store.get(8), "dropped: hidden meanwhile")
            self.assertTrue(self.store.failed_recently(9, 60.0), "a capture that gave nothing")
            self.store._shots.clear()

    def test_nothing_starts_while_held(self):
        with mock.patch.object(self.SN, "REFRESH_GAP_S", 0.0):
            self.store.hold(0.3)
            self.store.refresh([7])
            time.sleep(0.15)
            self.assertEqual(self.taken, [], "a slide runs")
            self.wait_taken(1)
            self.assertEqual(self.taken, [7])
            self.store.collect(lambda h, started: False)


class Layout(Base):
    def test_format_1_places_read_as_empty_and_format_1_settings_keep_their_values(self):
        from src import paths
        from src.paths import SETTINGS_FILE, SHOWING_FILE
        entry = ('{"Exe":"C:\\\\Apps\\\\app.exe","Class":"AppWindow","TitleHash":"%s","MonitorId":"MON-A",'
                 '"Desktop":1,"WorkLeft":0,"WorkTop":0,"ShowCmd":1,"Left":5,"Top":6,"Right":705,"Bottom":606}'
                 % title_hash("Hello"))
        files = {  # (the C# version's layout.json has no number; the others are written as format 1)
            os.path.basename(self.layout_file): '{"Windows":[%s],"Showing":{"MON-A":1}}' % entry,
            PLACES_FILE: '{"Updated":1,"UptimeMs":1,"Desktops":{"MON-A":3},"Windows":[%s],"Format":1}' % entry,
            SHOWING_FILE: '{"Showing":{"MON-A":2},"Format":1}',
            SETTINGS_FILE: '{"StartMenu":true,"Format":1}',
        }
        for name, text in files.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        m = self.manager()
        a = self.win.add("Hello", rect=(300, 300, 900, 900))
        m.sync()
        self.assertEqual(m.desktops_of(D1), [[a]], "no desktops nor places from format 1")
        self.assertEqual(m.current_of(D1), 0)
        self.assertEqual(m.load_desktops(), 0, "nothing saved")
        self.assertEqual(m.desktops_of(D1), [[a]])
        self.assertEqual(self.win.windows[a].rect, (300, 300, 900, 900), "not moved")
        self.assertEqual(paths.read_json(os.path.join(self.dir, SETTINGS_FILE)), {"StartMenu": True},
                         "settings: the same in format 2")

    def test_unreadable_layout_is_ignored(self):
        with open(self.layout_file, "w") as fh:
            fh.write('{"Windows":[{"Desktop":"x"}]}')
        m = self.manager()
        a = self.win.add("A")
        m.sync()
        self.assertEqual(m.desktops_of(D1), [[a]])

    def test_title_hash_vector(self):
        self.assertEqual(title_hash("abc"), "BA7816BF8F01CFEA414140DE")
        self.assertEqual(len(title_hash("")), 24)
        self.assertEqual(title_hash("Ünïcödé 日本"), "2FEE2EE490B48EE742625FA0")  # UTF-8 before hashing


class PlacesWrites(Base):
    """places.json is written only when what it holds changes (a move, a title, a window closed, a
    desktop added), for the heartbeat, and when forced; an unchanged record (every 2 s) writes nothing,
    and a failed write is made again at the next record."""

    def setUp(self):
        super().setUp()
        from src import places as P
        self.P = P
        self.written = []
        self.fail = False
        real = P.write_json

        def write(path, data):
            if self.fail:
                return False
            self.written.append(json.loads(json.dumps(data)))
            return real(path, data)
        p = unittest.mock.patch.object(P, "write_json", side_effect=write)
        p.start()
        self.addCleanup(p.stop)

    def run_syncs(self, m, n):
        for _ in range(n):
            self.win.advance(600)
            m.sync()

    def test_written_only_when_what_it_holds_changes(self):
        a = self.win.add("Doc - App", 0)
        b = self.win.add("Other - App", 1)
        m = self.manager()
        m.sync()
        self.win.advance(STARTUP_GRACE_MS + 1)
        m.sync()  # (the grace's end: recorded once more)
        self.written.clear()
        self.run_syncs(m, 10)  # 6 s: three records of the same places
        self.assertEqual(self.written, [], "unchanged: no disk write")
        self.win.windows[a].rect = (150, 120, 950, 720)
        self.run_syncs(m, 4)
        self.assertEqual(len(self.written), 1, "moved: written once")
        rec = {w["Handle"]: w for w in all_windows(self.written[-1])}
        self.assertEqual((rec[a]["Left"], rec[a]["Top"], rec[a]["Right"], rec[a]["Bottom"]), (150, 120, 950, 720))
        self.assertEqual(set(self.written[-1]), {"Updated", "UptimeMs", "Setups"})
        self.assertEqual(len(self.written[-1]["Setups"]), 1)
        self.assertEqual(set(self.written[-1]["Setups"][0]), {"Displays", "Windows", "Desktops"})
        self.assertEqual(self.written[-1]["Setups"][0]["Displays"], list(SETUP))
        self.assertEqual(set(rec[b]), {"Exe", "Class", "TitleHash", "Display", "Desktop", "WorkLeft", "WorkTop",
                                       "ShowCmd", "Left", "Top", "Right", "Bottom", "TitleParts", "Closed", "Handle",
                                       "Pid"})
        self.assertEqual(all_desktops(self.written[-1]), {"MON-A": 1, "MON-B": 1})
        self.win.windows[b].title = "Changed - App"
        self.run_syncs(m, 4)
        self.assertEqual(len(self.written), 2, "a new title: written")
        self.assertEqual({w["Handle"]: w["TitleHash"] for w in all_windows(self.written[-1])}[b],
                         title_hash("Changed - App"))
        self.win.windows[a].show_cmd = 3
        self.run_syncs(m, 4)
        self.assertEqual(len(self.written), 3, "maximized: written")
        self.new_desktop(m)
        self.assertEqual(all_desktops(self.written[-1]), {"MON-A": 2, "MON-B": 1}, "a desktop added: written")
        count = len(self.written)
        self.run_syncs(m, 4)
        self.assertEqual(len(self.written), count, "then nothing again")
        self.win.close(b)
        m.sync()
        self.assertEqual(len(self.written), count + 1, "closed: written (kept as closed)")
        self.assertTrue({w["Handle"]: w for w in all_windows(self.written[-1])}[b]["Closed"] > 0)

    def test_the_heartbeat_and_a_forced_write_write_unchanged_places(self):
        self.win.add("Doc - App", 0)
        m = self.manager()
        m.sync()
        self.win.advance(STARTUP_GRACE_MS + 1)
        m.sync()
        self.written.clear()
        self.run_syncs(m, 3)
        self.assertEqual(self.written, [])
        self.win.advance(self.P.HEARTBEAT_S * 1000)
        m.sync()
        self.assertEqual(len(self.written), 1, "the heartbeat: \"Updated\" refreshed")
        self.run_syncs(m, 4)
        self.assertEqual(len(self.written), 1)
        m.snapshot()
        self.assertEqual(len(self.written), 2, "the last snapshot: written even if unchanged")

    def test_a_failed_write_is_made_at_the_next_record(self):
        a = self.win.add("Doc - App", 0)
        m = self.manager()
        m.sync()
        self.win.advance(STARTUP_GRACE_MS + 1)
        m.sync()
        self.written.clear()
        self.fail = True
        self.win.windows[a].rect = (150, 120, 950, 720)
        self.run_syncs(m, 4)
        self.fail = False
        self.assertEqual(self.written, [])
        self.run_syncs(m, 4)
        self.assertEqual(len(self.written), 1, "not written then: written now, nothing else changed")
        self.assertEqual(all_windows(self.written[0])[0]["Left"], 150)


if __name__ == "__main__":
    unittest.main()
