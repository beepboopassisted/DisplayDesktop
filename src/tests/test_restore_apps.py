"""Restore Applications On Restart: places.json records each window's command line and packaged app
id; after a Windows restart (never after a restart of DisplayDesktop alone) the apps that were open
start again, once, RELAUNCH_AFTER_MS after the start. No real program is ever started."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src import desktop_manager as DM
from src import paths
from src.desktop_manager import STARTUP_GRACE_MS, DesktopManager
from src.paths import PLACES_FILE

from fakes import FakeSnapshots, FakeWin, all_windows

STORE = dict(exe=r"C:\Windows\System32\ApplicationFrameHost.exe", cls="ApplicationFrameWindow")
CALC = "Calculator_8wekyb3d8bbwe!App"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dd-apps-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        from src import datafolder
        for name, value in (("left_working", True), ("kept_new", False),
                            ("KEY", r"Software\DisplayDesktop (unit test)")):
            p = mock.patch.object(datafolder, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.win = FakeWin()
        # Programs that exist (empty files in the test folder: never run) and one that is gone
        self.alpha = self.program("alpha.exe")
        self.beta = self.program("beta.exe")
        self.gone = os.path.join(self.dir, "Gone", "gamma.exe")
        self.win.command_lines = {}
        self.win.app_ids = {}

    def program(self, name):
        path = os.path.join(self.dir, "Programs", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb"):
            pass
        return path

    def open(self, title, exe, pid, cmd="", app="", **kw):
        h = self.win.add(title, 0, exe=exe, pid=pid, **kw)
        if cmd:
            self.win.command_lines[pid] = cmd
        if app:
            self.win.app_ids[h] = app
        return h

    def manager(self, restore_apps=True) -> DesktopManager:
        m = DesktopManager(win=self.win, snapshots=FakeSnapshots(), directory=self.dir)
        m.restore_apps = restore_apps
        return m

    def places(self):
        with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
            return json.load(fh)

    def run_then_reboot(self, *, reboot=True):
        """A session with these windows: alpha twice in one process and once in another, beta, a Store
        app in two windows, a Store app whose id is unknown, an elevated program (no command line) and
        a program whose file is gone. Then every exit's last record, and the computer restarts."""
        self.open("Alpha 1", self.alpha, 101, cmd=f'"{self.alpha}" --one')
        self.open("Alpha 2", self.alpha, 101)  # (same process)
        self.open("Alpha 3", self.alpha, 103, cmd=f'"{self.alpha}" --three')
        self.open("Beta", self.beta, 102, cmd=f'"{self.beta}"')
        self.open("Calculator", pid=201, app=CALC, **STORE)
        self.open("Calculator", pid=202, app=CALC, **STORE)  # (another frame host process, same app)
        self.open("Photos", pid=203, cmd=STORE["exe"] + " -Embedding", **STORE)  # (no app id)
        self.open("Admin tool", r"C:\Apps\admin.exe", 104)  # (elevated: no command line)
        self.open("Gamma", self.gone, 105, cmd=f'"{self.gone}"')
        m = self.manager()
        m.sync()
        self.win.advance(STARTUP_GRACE_MS + 1)
        m.sync()
        m.snapshot()
        m.restore_all()
        if reboot:
            self.win.reboot()
        return m


class Recorded(Base):
    def test_command_and_app_are_recorded_per_window(self):
        self.run_then_reboot(reboot=False)
        rec = {w["Pid"]: w for w in all_windows(self.places())}
        self.assertEqual(rec[101]["Command"], f'"{self.alpha}" --one')
        self.assertEqual(rec[101]["App"], "")
        self.assertEqual(rec[201]["App"], CALC)
        self.assertEqual(rec[104]["Command"], "", "a command line that can't be read: none")
        self.assertEqual(paths.FORMAT, self.places()["Format"])

    def test_the_command_line_is_read_once_per_process(self):
        a = self.open("A", self.alpha, 101, cmd="alpha")
        self.open("B", self.alpha, 101)
        m = self.manager()
        with mock.patch.object(self.win, "process_command_line", wraps=self.win.process_command_line) as read:
            m.sync()
            self.win.advance(STARTUP_GRACE_MS + 1)
            m.sync()
            self.win.windows[a].rect = (200, 200, 900, 700)
            self.win.advance(2_100)
            m.sync()
        self.assertEqual(read.call_count, 1)

    def test_the_records_of_the_last_boot_keep_their_command_until_taken(self):
        self.run_then_reboot()
        m2 = self.manager(restore_apps=False)
        m2.sync()
        rec = [w for w in all_windows(self.places()) if w["Command"] == f'"{self.beta}"']
        self.assertEqual(len(rec), 1, "written again with its command line (a restart now can still use it)")
        self.assertEqual((rec[0]["Pid"], rec[0]["Handle"]), (102, 0), "its old pid kept, no handle")


class AppsToRestart(Base):
    def test_only_after_windows_restarted(self):
        self.run_then_reboot(reboot=False)
        m2 = self.manager()
        self.assertFalse(m2._places.last_boot, "the same boot: DisplayDesktop alone restarted")
        m2.sync()
        self.assertEqual(m2._places.apps_to_restart(), [])

    def test_once_per_process_and_once_per_packaged_app(self):
        self.run_then_reboot()
        m2 = self.manager()
        self.assertTrue(m2._places.last_boot)
        m2.sync()
        got = m2._places.apps_to_restart()
        self.assertEqual(sorted(got), sorted([
            (self.alpha, f'"{self.alpha}" --one', ""),    # pid 101 (two windows): once
            (self.alpha, f'"{self.alpha}" --three', ""),  # pid 103: an instance of its own
            (self.beta, f'"{self.beta}"', ""),
            (STORE["exe"], "", CALC),                     # two windows, two processes: once
            (STORE["exe"], STORE["exe"] + " -Embedding", ""),  # (listed; never started: _relaunch_apps)
            (self.gone, f'"{self.gone}"', ""),            # (listed; never started: the file is gone)
        ]), "no elevated program (no command line)")

    def test_windows_already_back_are_not_listed(self):
        self.run_then_reboot()
        m2 = self.manager()
        m2.sync()
        self.open("Beta", self.beta, 6102)  # opened again before the relaunch (its place taken)
        m2.sync()
        self.assertNotIn(self.beta, [exe for exe, _, _ in m2._places.apps_to_restart()])

    def test_a_format_4_file_without_app_ids_reads(self):
        data = {"Updated": 1, "UptimeMs": 1, "Format": 4, "Setups": [{"Displays": ["MON-A", "MON-B"], "Windows": [
            {"Exe": self.beta, "Class": "AppWindow", "Display": "MON-A", "Pid": 7, "Command": "beta"},
            {"Exe": STORE["exe"], "Class": STORE["cls"], "Display": "MON-A", "Pid": 8}]}]}
        with open(os.path.join(self.dir, PLACES_FILE), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        m = self.manager()
        m.sync()
        self.assertTrue(m._places.last_boot)
        self.assertEqual(m._places.apps_to_restart(), [(self.beta, "beta", "")], "no App: not a packaged app")


class Relaunch(Base):
    def relaunch(self, m, *, test_mode=False, ms=DM.RELAUNCH_AFTER_MS):
        """Syncs until RELAUNCH_AFTER_MS after the start; returns the Popen calls."""
        with mock.patch("subprocess.Popen") as popen, mock.patch.object(DM.paths, "is_test_mode",
                                                                       return_value=test_mode):
            m.sync()
            self.win.advance(ms)
            m.sync()
        return popen

    def started(self, popen):
        return sorted(c.args[0] for c in popen.call_args_list)

    def test_each_app_starts_again_once_with_its_command_in_its_folder(self):
        self.run_then_reboot()
        m2 = self.manager()
        popen = self.relaunch(m2)
        self.assertEqual(self.started(popen), sorted([f'"{self.alpha}" --one', f'"{self.alpha}" --three',
                                                      f'"{self.beta}"']),
                         "never a Store app's frame host without its app, nor a program whose file is gone")
        for c in popen.call_args_list:
            self.assertEqual(c.kwargs["cwd"], os.path.dirname(self.alpha))
            self.assertEqual(c.kwargs["creationflags"], DM.CREATE_NEW_CONSOLE)
        self.assertEqual(self.win.started_apps, [CALC], "a packaged app by its id, once")

    def test_not_before_its_time_and_only_once(self):
        self.run_then_reboot()
        m2 = self.manager()
        popen = self.relaunch(m2, ms=DM.RELAUNCH_AFTER_MS - 1)
        popen.assert_not_called()
        popen = self.relaunch(m2, ms=1)
        self.assertEqual(popen.call_count, 3)
        popen = self.relaunch(m2, ms=60_000)
        popen.assert_not_called()
        self.assertEqual(self.win.started_apps, [CALC])

    def test_programs_and_apps_that_already_run_are_not_started(self):
        self.run_then_reboot()
        m2 = self.manager()
        m2.sync()
        self.win.add("Splash", 0, exe=self.alpha.upper(), cls="Splash", visible=False)  # (alpha runs already)
        calc = self.win.add("Calculator", 0, visible=False, **STORE)
        self.win.app_ids[calc] = CALC  # (started by Windows meanwhile)
        popen = self.relaunch(m2)
        self.assertEqual(self.started(popen), [f'"{self.beta}"'])
        self.assertFalse(getattr(self.win, "started_apps", []))

    def test_never_after_displaydesktop_alone_restarted(self):
        self.run_then_reboot(reboot=False)
        for h in list(self.win.windows):
            self.win.close(h)  # (even with every app closed)
        popen = self.relaunch(self.manager())
        popen.assert_not_called()
        self.assertFalse(getattr(self.win, "started_apps", []))

    def test_never_in_test_mode(self):
        self.run_then_reboot()
        popen = self.relaunch(self.manager(), test_mode=True)
        popen.assert_not_called()
        self.assertFalse(getattr(self.win, "started_apps", []))

    def test_never_with_the_setting_off(self):
        self.run_then_reboot()
        popen = self.relaunch(self.manager(restore_apps=False))
        popen.assert_not_called()
        self.assertFalse(getattr(self.win, "started_apps", []))

    def test_a_program_that_fails_to_start_is_logged_and_the_others_still_start(self):
        self.run_then_reboot()
        m2 = self.manager()
        logged = []
        m2.log = logged.append
        with mock.patch("subprocess.Popen", side_effect=[OSError("refused"), mock.DEFAULT, mock.DEFAULT]) as popen, \
                mock.patch.object(DM.paths, "is_test_mode", return_value=False):
            m2.sync()
            self.win.advance(DM.RELAUNCH_AFTER_MS)
            m2.sync()
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(sum("did not start" in t for t in logged), 1, logged)
        self.assertEqual(sum("started" in t and "did not" not in t for t in logged), 3, logged)

    def test_the_restarted_windows_go_back_to_their_places(self):
        self.run_then_reboot()
        m2 = self.manager()
        self.relaunch(m2)
        b = self.win.add("Beta", 0, exe=self.beta, pid=6102, rect=(300, 300, 700, 700))  # Beta opens again
        m2.sync()
        self.assertEqual(self.win.windows[b].rect, (100, 100, 900, 700), "its last known place")


class Setting(unittest.TestCase):
    def setUp(self):
        from src import settings
        self.settings = settings
        self.tmp = tempfile.mkdtemp(prefix="dd-apps-settings-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(settings, "_path", return_value=os.path.join(self.tmp, "display.desktop.settings.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_off_by_default(self):
        self.assertFalse(self.settings.get("RestoreApps"))

    def test_the_settings_checkbox_stores_it(self):
        from src import settings_window as SW
        with mock.patch.object(SW.console, "log") as log:
            self.assertIs(SW.SettingsWindow._set_restore_apps(True), True)
            self.assertTrue(self.settings.get("RestoreApps"))
            self.assertIs(SW.SettingsWindow._set_restore_apps(False), False)
        self.assertFalse(self.settings.get("RestoreApps"))
        self.assertIn("Restore Applications On Restart: off.", log.call_args.args[0])

    def test_the_checkbox_is_the_third_option_row(self):
        from src import settings_window as SW
        from src import strings as L
        self.assertEqual(SW.OPTION_ROWS, 5)
        self.assertEqual(L.t("settings.restoreApps"), "Restore Applications On Restart")
        import inspect
        src = inspect.getsource(SW.SettingsWindow.__init__)
        at = [src.index(f'"{k}"') for k in ("settings.startMenu", "settings.restoreApps", "settings.gpu")]
        self.assertEqual(at, sorted(at), "right under Add To Start Menu, above Use GPU Acceleration")

    def test_a_folder_move_applies_it_to_the_manager(self):
        from src import tray as T
        app = SimpleNamespace(mgr=SimpleNamespace(restore_apps=True))
        self.settings.set("RestoreApps", False)
        with mock.patch.object(T.gpu, "set_enabled"), mock.patch.object(T.shortcut, "set_enabled"), \
                mock.patch.object(T.console, "is_open", return_value=False):
            T.TrayApp._apply_options(app)
        self.assertIs(app.mgr.restore_apps, False)


if __name__ == "__main__":
    unittest.main()
