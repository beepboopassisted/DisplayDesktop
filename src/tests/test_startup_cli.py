"""Start with Windows and the run.cmd commands. Registry tests use throw-away value names, so the
real startup entry is never touched."""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
import winreg
from contextlib import redirect_stdout
from unittest import mock

from src import cli, paths, settings, shortcut, startup

TEST_NAME = "DisplayDesktop (unit test)"


class Commands(unittest.TestCase):
    def test_normalize(self):
        for raw, want in [("start", "start"), ("Stop", "stop"), ("EnableStartup", "enablestartup"),
                          ("enable-startup", "enablestartup"), ("--status", "status"),
                          ("disable_startup", "disablestartup")]:
            self.assertEqual(cli.normalize(raw), want)

    def test_unknown_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.run("bogus"), 2)
        self.assertIn("Unknown command: bogus", out.getvalue())
        self.assertIn("enable-startup", out.getvalue())

    def run_cli(self, command: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.run(command)
        return code, out.getvalue()

    def test_start_when_already_running_does_nothing(self):
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "running_pid", return_value=42), \
                mock.patch("subprocess.Popen") as popen:
            code, out = self.run_cli("start")
        self.assertEqual(code, 0)
        self.assertIn("already running (PID 42)", out)
        popen.assert_not_called()

    def test_start_launches_and_waits_for_the_mutex(self):
        proc = mock.Mock(pid=7, returncode=None)
        proc.poll.return_value = None
        with mock.patch.object(cli, "is_running", side_effect=[False, True, True]), \
                mock.patch("subprocess.Popen", return_value=proc) as popen, \
                mock.patch.object(cli.time, "sleep"):
            code, out = self.run_cli("start")
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args[0][0], startup.app_command())
        self.assertIn("running in the tray (PID 7)", out)

    def test_recovery_starts_the_app_in_recovery_mode_or_opens_it_in_the_running_one(self):
        proc = mock.Mock(pid=7, returncode=None)
        proc.poll.return_value = None
        with mock.patch.object(cli, "is_running", side_effect=[False, False, True, True]), \
                mock.patch("subprocess.Popen", return_value=proc) as popen, \
                mock.patch.object(cli.time, "sleep"):
            code, out = self.run_cli("recovery")
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args[0][0], startup.app_command() + ["--recovery"])
        self.assertIn("Recovery", out)
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "_main_window", return_value=55), \
                mock.patch.object(cli, "_PostMessageW", return_value=1) as post, \
                mock.patch.object(cli.N, "get_pid", return_value=9):
            code, out = self.run_cli("Recovery")
        self.assertEqual(code, 0)
        post.assert_called_once_with(55, cli.RECOVERY_MESSAGE, 0, 0)
        self.assertIn("PID 9", out)
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "_main_window", return_value=0), \
                mock.patch.object(cli, "_FindWindowW", return_value=0):
            code, out = self.run_cli("recovery")
        self.assertEqual(code, 1, "running but no window yet: says so")
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "_main_window", return_value=0), \
                mock.patch.object(cli, "_FindWindowW", return_value=44), \
                mock.patch.object(cli.N, "get_pid", return_value=8):
            code, out = self.run_cli("recovery")
        self.assertEqual(code, 0)
        self.assertIn("already open", out, "still in Recovery at start: it is open")
        from src import tray
        self.assertEqual(cli.RECOVERY_MESSAGE, tray.WM_APP_RECOVERY)

    def test_stop_ends_a_start_still_in_recovery_mode_with_its_quit(self):
        from src import restore_window as RW
        self.assertEqual((cli.RECOVERY_CLASS, cli.RECOVERY_QUIT), (RW.RecoveryWindow.WINDOW_CLASS, RW.ID_QUIT))
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "running_pid", return_value=0), \
                mock.patch.object(cli, "_FindWindowW", return_value=44), \
                mock.patch.object(cli.N, "get_pid", return_value=8), \
                mock.patch.object(cli.N, "_OpenProcess", return_value=0), \
                mock.patch.object(cli, "_PostMessageW") as post, \
                mock.patch.object(cli, "signal_quit"), \
                mock.patch.object(cli, "_wait", return_value=True):
            code, _ = self.run_cli("stop")
        self.assertEqual(code, 0)
        post.assert_called_once_with(44, 0x0111, cli.RECOVERY_QUIT, 0)
        # the key popup at start: its Cancel
        self.assertEqual((cli.KEY_CLASS, cli.KEY_CANCEL), (RW.KeyWindow.WINDOW_CLASS, RW.IDCANCEL))
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "running_pid", return_value=0), \
                mock.patch.object(cli, "_FindWindowW", side_effect=lambda c, t: 55 if c == cli.KEY_CLASS else 0), \
                mock.patch.object(cli.N, "get_pid", return_value=8), \
                mock.patch.object(cli.N, "_OpenProcess", return_value=0), \
                mock.patch.object(cli, "_PostMessageW") as post, \
                mock.patch.object(cli, "signal_quit"), \
                mock.patch.object(cli, "_wait", return_value=True):
            code, _ = self.run_cli("stop")
        self.assertEqual(code, 0)
        post.assert_called_once_with(55, 0x0111, cli.KEY_CANCEL, 0)
        # refused (a copy running as administrator): says so at once, never a 15 s wait
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "running_pid", return_value=0), \
                mock.patch.object(cli, "_FindWindowW", return_value=44), \
                mock.patch.object(cli.N, "get_pid", return_value=8), \
                mock.patch.object(cli.N, "_OpenProcess", return_value=0), \
                mock.patch.object(cli, "_PostMessageW", return_value=0), \
                mock.patch.object(cli.ctypes, "get_last_error", return_value=cli.ACCESS_DENIED), \
                mock.patch.object(cli, "_wait") as wait:
            code, out = self.run_cli("stop")
        self.assertEqual(code, 1)
        self.assertIn("answer its window", out)
        wait.assert_not_called()
        # the window closed meanwhile (answered): no refusal, the stop goes on as usual
        with mock.patch.object(cli, "is_running", return_value=True), \
                mock.patch.object(cli, "running_pid", return_value=0), \
                mock.patch.object(cli, "_FindWindowW", return_value=44), \
                mock.patch.object(cli.N, "get_pid", return_value=8), \
                mock.patch.object(cli.N, "_OpenProcess", return_value=0), \
                mock.patch.object(cli, "_PostMessageW", return_value=0), \
                mock.patch.object(cli.ctypes, "get_last_error", return_value=1400), \
                mock.patch.object(cli, "signal_quit") as quit_, \
                mock.patch.object(cli, "_wait", return_value=True):
            code, out = self.run_cli("stop")
        self.assertEqual(code, 0)
        quit_.assert_called_once()

    def test_the_recovery_argument_runs_the_app_with_recovery_mode_first(self):
        from src import app as A
        with mock.patch.object(A, "_CreateMutexW", return_value=0), \
                mock.patch("src.gui.message_box") as box, mock.patch("src.cli.run") as run:
            self.assertEqual(A.main(["--recovery"]), 1)  # (already running: the app's own path, never cli)
        run.assert_not_called()
        box.assert_called_once()

    def test_start_reports_an_app_that_exits_at_once(self):
        proc = mock.Mock(pid=7, returncode=1)
        proc.poll.return_value = 1
        with mock.patch.object(cli, "is_running", return_value=False), \
                mock.patch("subprocess.Popen", return_value=proc):
            code, out = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertIn("exited right away (exit code 1)", out)

    def test_stop_asks_it_to_quit_and_never_kills(self):
        with mock.patch.object(cli, "is_running", return_value=False):
            self.assertEqual(self.run_cli("stop"), (0, "DisplayDesktop is not running.\n"))
        for exited, code_wanted, text in ((0, 0, "Stopped."), (0x102, 1, "did not exit")):  # 0x102 = timeout
            with mock.patch.object(cli, "is_running", return_value=True), \
                    mock.patch.object(cli, "running_pid", return_value=42), \
                    mock.patch.object(cli.N, "_OpenProcess", return_value=99), \
                    mock.patch.object(cli.N, "CloseHandle"), \
                    mock.patch.object(cli, "signal_quit") as quit_, \
                    mock.patch.object(cli, "WaitForSingleObject", return_value=exited):
                code, out = self.run_cli("stop")
            quit_.assert_called_once()
            self.assertEqual(code, code_wanted)
            self.assertIn(text, out)

    def test_restart_is_stop_then_start(self):
        calls = []
        with mock.patch.object(cli, "stop", side_effect=lambda: calls.append("stop") or 0), \
                mock.patch.object(cli, "start", side_effect=lambda: calls.append("start") or 0):
            self.assertEqual(cli.run("restart"), 0)
        self.assertEqual(calls, ["stop", "start"])
        with mock.patch.object(cli, "stop", return_value=1), mock.patch.object(cli, "start") as start:
            self.assertEqual(cli.run("restart"), 1, "a failed stop does not start a second copy")
        start.assert_not_called()

    def test_enable_and_disable_startup(self):
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "set_enabled", return_value=True) as set_, \
                mock.patch.object(startup, "state", return_value="on"):
            self.assertEqual(self.run_cli("enable-startup")[0], 0)
        set_.assert_called_once_with(True, startup.startup_command())
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "is_enabled", return_value=True), \
                mock.patch.object(startup, "set_enabled", return_value=False) as set_:
            self.assertEqual(self.run_cli("disable-startup")[0], 0)
        set_.assert_called_once_with(False)
        with mock.patch.object(startup, "can_manage", return_value=False), \
                mock.patch.object(startup, "set_enabled") as set_:
            self.assertEqual(self.run_cli("enable-startup"), (1, "Not available in test mode.\n"))
            self.assertEqual(self.run_cli("disable-startup"), (1, "Not available in test mode.\n"))
        set_.assert_not_called()

    def test_status_runs(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.run("status"), 0)
        self.assertIn("Start with Windows:", out.getvalue())

    def test_commands_load_neither_the_gui_nor_paths(self):
        """A command (here `status`) loads neither the GUI layer nor paths (with json and re) nor
        threading: it starts faster. The app run imports them itself (app.main)."""
        import subprocess
        code = ("import io, sys\n"
                "from contextlib import redirect_stdout\n"
                "from src import app, cli\n"
                "with redirect_stdout(io.StringIO()):\n"
                "    cli.run('status')\n"
                "print(sorted(m for m in ('src.gui', 'src.strings', 'src.paths', 'threading') if m in sys.modules))\n")
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=startup.REPO_DIR, capture_output=True,
                             text=True, timeout=60, creationflags=0x08000000)  # CREATE_NO_WINDOW
        self.assertEqual((out.stderr, out.stdout.strip()), ("", "[]"))


class Paths(unittest.TestCase):
    def test_launcher_is_next_to_the_package(self):
        self.assertTrue(os.path.isfile(startup.LAUNCHER))
        self.assertEqual(os.path.dirname(startup.LAUNCHER), startup.REPO_DIR)
        self.assertTrue(os.path.isdir(os.path.join(startup.REPO_DIR, "src")))

    def test_bare_pythonw_when_path_finds_this_python(self):
        here = os.path.dirname(os.path.abspath(sys.executable))
        with mock.patch.object(startup, "which", return_value=os.path.join(here, "pythonw.exe")):
            self.assertEqual(startup.pythonw_command(), "pythonw")
            self.assertEqual(startup.startup_command(), f'pythonw -B "{startup.LAUNCHER}"')

    def test_full_path_when_path_finds_another_python_or_none(self):
        own = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "pythonw.exe")
        for found in (r"D:\Other Python\pythonw.exe", None):
            with mock.patch.object(startup, "which", return_value=found):
                self.assertEqual(startup.pythonw_command(), own)

    def test_which_finds_what_shutil_which_finds(self):
        import shutil
        import tempfile
        here = os.path.dirname(os.path.abspath(sys.executable))
        with tempfile.TemporaryDirectory() as d:
            for name in ("tool.exe", "tool.cmd", "script.bat"):
                with open(os.path.join(d, name), "w", encoding="utf-8"):
                    pass
            os.mkdir(os.path.join(d, "folder.exe"))  # (a folder never counts)
            path = os.pathsep.join([d, here, d.upper(), ""])
            cases = ("tool", "tool.exe", "TOOL.CMD", "script", "folder", "pythonw", "python.exe", "nothing_here_xyz")
            # (PATHEXT as Windows sets it, another one, none at all; PATH set, empty, missing)
            for env in ({"PATH": path}, {"PATH": path, "PATHEXT": ".CMD;.EXE"}, {"PATH": path, "PATHEXT": None},
                        {"PATH": ""}, {"PATH": None}):
                with mock.patch.dict(os.environ):
                    for k, v in env.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                    for cmd in cases:
                        self.assertEqual(startup.which(cmd), shutil.which(cmd), (cmd, env))

    def test_paths_with_spaces_are_quoted(self):
        with mock.patch.object(startup, "pythonw_command", return_value=r"C:\Program Files\Py\pythonw.exe"), \
                mock.patch.object(startup, "LAUNCHER", r"C:\My Apps\DisplayDesktop.pyw"):
            self.assertEqual(startup.startup_command(),
                             r'"C:\Program Files\Py\pythonw.exe" -B "C:\My Apps\DisplayDesktop.pyw"')


class Shortcut(unittest.TestCase):
    def test_location_and_icon(self):
        self.assertTrue(os.path.isdir(shortcut.programs_dir()))
        self.assertEqual(os.path.basename(shortcut.path()), "DisplayDesktop.lnk")
        self.assertTrue(os.path.isfile(shortcut.ICON))

    def test_add_to_start_menu_is_never_left_on_without_a_windowless_python(self):
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(shortcut, "target", side_effect=OSError("no pythonw.exe")), \
                mock.patch.object(shortcut, "exists", return_value=False), \
                mock.patch.object(shortcut.settings, "set") as stored:
            self.assertFalse(shortcut.set_enabled(True))
        stored.assert_not_called()

    def test_start_with_windows_needs_a_windowless_python(self):
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "windowless", return_value=False), \
                mock.patch.object(startup, "is_enabled", return_value=False), \
                mock.patch.object(startup.winreg, "SetValueEx") as reg:
            self.assertFalse(startup.set_enabled(True, "python.exe -B x"))
        reg.assert_not_called()  # (a console window at every sign-in otherwise)

    def test_the_console_python_skips_a_stub_alias_for_a_real_python_later_on_path(self):
        from src import console as C
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        stub_dir = os.path.join(tmp, "Microsoft", "WindowsApps")
        real_dir = os.path.join(tmp, "Python")
        for d in (stub_dir, real_dir):
            os.makedirs(d)
            open(os.path.join(d, "python.exe"), "w").close()
        with mock.patch.object(C.os.path, "isfile", side_effect=lambda p: os.path.exists(p) and tmp in p), \
                mock.patch.dict(os.environ, {"PATH": os.pathsep.join((stub_dir, real_dir))}), \
                mock.patch.object(C, "_alias_runs", return_value=False):
            self.assertEqual(C._python_exe(), os.path.join(real_dir, "python.exe"))

    def test_target_is_the_running_python_s_pythonw_else_the_launcher(self):
        with mock.patch.object(shortcut, "_launcher", return_value=r"C:\Windows\pyw.exe"):
            exe, args = shortcut.target()
        self.assertEqual(os.path.normcase(os.path.dirname(exe)),
                         os.path.normcase(os.path.dirname(os.path.abspath(sys.executable))),
                         "the Python it runs on, as the startup entry")
        self.assertTrue(os.path.isabs(exe) and exe.lower().endswith("pythonw.exe"))
        self.assertEqual(args, f'-B "{startup.LAUNCHER}"')
        with mock.patch.object(shortcut.os.path, "isfile", return_value=False), \
                mock.patch.object(shortcut.sys, "executable", r"C:\Py\python.exe"), \
                mock.patch.object(shortcut, "_launcher", return_value=r"C:\Windows\pyw.exe"):
            self.assertEqual(shortcut.target(), (r"C:\Windows\pyw.exe", f'-3 -B "{startup.LAUNCHER}"'),
                             "no pythonw.exe here: the launcher")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        open(os.path.join(tmp, "pyw.exe"), "w").close()
        with mock.patch.dict(os.environ, {"PATH": "."}), mock.patch.object(os, "getcwd", return_value=tmp):
            self.assertIsNone(shortcut._launcher(), "never the current folder")
        with mock.patch.dict(os.environ, {"PATH": tmp}):
            self.assertEqual(os.path.normcase(shortcut._launcher()), os.path.normcase(os.path.join(tmp, "pyw.exe")))
        # a Python without pythonw.exe: no shortcut to a console python.exe
        with mock.patch.object(shortcut.os.path, "isfile", return_value=False), \
                mock.patch.object(shortcut, "_launcher", return_value=None), \
                mock.patch.object(shortcut.sys, "executable", r"C:\Py\python.exe"):
            with self.assertRaises(OSError):
                shortcut.target()

    def in_temp_start_menu(self):
        """Points the shortcut and settings.json at a temp folder (the real Start menu and settings
        are never touched)."""
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        lnk = os.path.join(d.name, "DisplayDesktop.lnk")
        notify = shortcut._SHChangeNotify  # (told without SHCNF_FLUSH: no wait for the shell, ~4 ms a call)
        for p in (mock.patch.object(shortcut, "path", return_value=lnk),
                  mock.patch.object(shortcut, "_SHChangeNotify",
                                    lambda event, flags, item, extra: notify(event, flags & ~shortcut.SHCNF_FLUSH,
                                                                             item, extra)),
                  mock.patch.object(settings, "_path", return_value=os.path.join(d.name, "display.desktop.settings.json")),
                  mock.patch.object(startup, "can_manage", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        settings.set("StartMenu", True)  # Add To Start Menu on (it is off by default)
        return lnk

    def test_off_by_default_nothing_is_added(self):
        lnk = self.in_temp_start_menu()
        settings.set("StartMenu", False)
        self.assertEqual(shortcut.ensure(), "")
        self.assertFalse(os.path.exists(lnk))

    def test_add_to_start_menu_off_removes_it_and_keeps_it_away(self):
        lnk = self.in_temp_start_menu()
        self.assertEqual(shortcut.ensure(), "added")
        self.assertFalse(shortcut.set_enabled(False))
        self.assertFalse(os.path.exists(lnk))
        self.assertFalse(settings.get("StartMenu"))
        self.assertEqual(shortcut.ensure(), "", "off: the next start does not add it back")
        self.assertFalse(os.path.exists(lnk))
        self.assertTrue(shortcut.set_enabled(True))
        self.assertTrue(os.path.isfile(lnk) and shortcut.is_current() and settings.get("StartMenu"))

    def test_ensure_adds_a_missing_one_and_leaves_a_current_one(self):
        lnk = self.in_temp_start_menu()
        self.assertEqual(shortcut.ensure(), "added")
        self.assertTrue(os.path.isfile(lnk) and shortcut.is_current())
        with mock.patch.object(shortcut, "write_link") as write:
            self.assertEqual(shortcut.ensure(), "")
        write.assert_not_called()

    def test_ensure_rewrites_an_outdated_one(self):
        lnk = self.in_temp_start_menu()
        exe, args = shortcut.target()
        good = dict(exe=exe, args=args, work_dir=startup.REPO_DIR, icon=shortcut.ICON, app_id=shortcut.APP_ID)
        for field, value in [("exe", r"C:\Old Python\pythonw.exe"), ("args", r'-B "D:\moved\DisplayDesktop.pyw"'),
                             ("work_dir", r"D:\moved"), ("icon", r"D:\moved\src\app.ico"), ("app_id", "Other.Id")]:
            old = dict(good, **{field: value})
            shortcut.write_link(lnk, old["exe"], old["args"], old["work_dir"], old["icon"], "desc", old["app_id"])
            self.assertFalse(shortcut.is_current(), field)
            self.assertEqual(shortcut.ensure(), "updated", field)
            self.assertTrue(shortcut.is_current(), field)

    def test_turning_it_on_rewrites_an_outdated_one(self):
        lnk = self.in_temp_start_menu()
        exe, _ = shortcut.target()
        shortcut.write_link(lnk, exe, '-B "D:\\moved\\DisplayDesktop.pyw"', r"D:\moved", shortcut.ICON, "d",
                            shortcut.APP_ID)
        self.assertTrue(shortcut.set_enabled(True))
        self.assertTrue(shortcut.is_current())

    def test_test_mode_never_changes_the_setting_or_the_shortcut(self):
        lnk = self.in_temp_start_menu()  # setting on, no shortcut
        with mock.patch.object(startup, "can_manage", return_value=False):
            self.assertFalse(shortcut.set_enabled(False))
            self.assertTrue(settings.get("StartMenu"), "setting unchanged")
            shortcut.write_link(lnk, *shortcut.target(), startup.REPO_DIR, shortcut.ICON, "d", shortcut.APP_ID)
            self.assertFalse(shortcut.remove(), "remove refused: the shortcut is still there")
            self.assertTrue(os.path.isfile(lnk))

    def test_an_unreadable_one_is_rewritten(self):
        lnk = self.in_temp_start_menu()
        with open(lnk, "wb") as f:
            f.write(b"not a shortcut")
        self.assertFalse(shortcut.is_current())
        self.assertEqual(shortcut.ensure(), "updated")
        self.assertTrue(shortcut.is_current())

    def test_ensure_never_raises(self):
        self.in_temp_start_menu()
        with mock.patch.object(shortcut, "write_link", side_effect=RuntimeError("boom")):
            self.assertEqual(shortcut.ensure(), "")

    def test_test_mode_never_creates_it(self):
        with mock.patch.dict(os.environ, {"DISPLAYDESKTOP_DATA": "x"}), \
                mock.patch.object(shortcut, "write_link") as write:
            self.assertFalse(shortcut.create())
            self.assertEqual(shortcut.ensure(), "")
        write.assert_not_called()

    def test_link_file_round_trip(self):
        lnk = self.in_temp_start_menu()
        exe, args = shortcut.target()
        shortcut.write_link(lnk, exe, args, startup.REPO_DIR, shortcut.ICON, "desc", shortcut.APP_ID)
        got = shortcut.read_link(lnk)
        self.assertEqual(os.path.normcase(got["exe"]), os.path.normcase(exe))
        self.assertEqual((got["args"], got["app_id"]), (args, "DisplayDesktop.App"))
        self.assertEqual(os.path.normcase(got["work_dir"]), os.path.normcase(startup.REPO_DIR))
        self.assertEqual(os.path.normcase(got["icon"]), os.path.normcase(shortcut.ICON))
        with open(lnk, "rb") as f:
            self.assertEqual(f.read(4), b"\x4c\x00\x00\x00")           # a shell link header


class Registry(unittest.TestCase):
    def setUp(self):
        self.assertFalse(paths.is_test_mode(), "unset DISPLAYDESKTOP_DATA to run these tests")
        self.patch = mock.patch.object(startup, "VALUE_NAME", TEST_NAME)
        self.patch.start()
        self._delete()

    def tearDown(self):
        self._delete()
        self.patch.stop()

    def _delete(self):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, startup.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, TEST_NAME)
            except FileNotFoundError:
                pass

    def _set(self, name, value):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, startup.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def test_on_off(self):
        self.assertEqual(startup.state(), "off")
        self.assertTrue(startup.set_enabled(True))
        self.assertEqual(startup.state(), "on")
        self.assertEqual(startup._read(TEST_NAME), startup.startup_command())
        self.assertFalse(startup.set_enabled(False))
        self.assertFalse(startup.set_enabled(False))
        self.assertEqual(startup.state(), "off")

    def test_moved_folder_is_refreshed_and_disabled_stays_disabled(self):
        self._set(TEST_NAME, 'pythonw -B "D:\\old place\\DisplayDesktop.pyw"')
        self.assertEqual(startup.state(), "outdated")
        startup.refresh()
        self.assertEqual(startup.state(), "on")
        startup.set_enabled(False)
        startup.refresh()
        self.assertEqual(startup.state(), "off")

    def test_test_mode_never_writes(self):
        with mock.patch.dict(os.environ, {"DISPLAYDESKTOP_DATA": "x"}):
            self.assertFalse(startup.set_enabled(True))
            startup.refresh()
        self.assertEqual(startup.state(), "off")


if __name__ == "__main__":
    unittest.main()
