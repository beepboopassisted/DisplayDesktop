"""Start with Windows and the run.cmd commands. Startup shortcut tests use a temp folder in place of
the Startup folder and throw-away keys in place of the Run and StartupApproved keys, so the real
startup entries are never touched and nothing is left to run at sign-in."""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
import winreg
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from src import cli, paths, settings, shortcut, startup

TEST_NAME = "DisplayDesktop (unit test)"
TEST_LNK = TEST_NAME + ".lnk"
TEST_RUN_KEY = r"Software\DisplayDesktop (unit test run key)"  # (in place of the Run key: never run at sign-in)
TEST_APPROVED_RUN = r"Software\DisplayDesktop (unit test approved run)"
TEST_APPROVED_FOLDER = r"Software\DisplayDesktop (unit test approved folder)"


def oem_folder_name() -> str | None:
    """A folder name outside ASCII that the OEM code page (cmd's echo) can write, or None."""
    for letter in "éüñçåЖαא":
        try:
            if letter.encode("oem").decode("oem") == letter:
                return "Run " + letter
        except UnicodeError:
            pass
    return None


@contextmanager
def no_path_search(test: unittest.TestCase):
    """Any PATH search (startup._is_file: each file on_path_all checks, on a probe thread too) fails
    the test: the Run value is never looked up on PATH."""
    searched = []
    with mock.patch.object(startup, "_is_file", side_effect=lambda p: searched.append(p) or False):
        yield
    test.assertEqual(searched, [], "PATH searched")


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
        want = (startup.LAUNCHER, "")
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "target", return_value=want), \
                mock.patch.object(startup, "set_enabled", return_value=True) as set_, \
                mock.patch.object(startup, "state", return_value="on"):
            self.assertEqual(self.run_cli("enable-startup"), (0, f"Start with Windows: on ({startup.LAUNCHER})\n"))
        set_.assert_called_once_with(True, want)
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "exists", return_value=True), \
                mock.patch.object(startup, "set_enabled", return_value=False) as set_:
            self.assertEqual(self.run_cli("disable-startup")[0], 0)
        set_.assert_called_once_with(False)
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "exists", return_value=True), \
                mock.patch.object(startup, "set_enabled", side_effect=OSError("denied")):
            self.assertEqual(self.run_cli("disable-startup"), (1, "Could not remove the startup shortcut.\n"))
        with mock.patch.object(startup, "can_manage", return_value=False), \
                mock.patch.object(startup, "set_enabled") as set_:
            self.assertEqual(self.run_cli("enable-startup"), (1, "Not available in test mode.\n"))
            self.assertEqual(self.run_cli("disable-startup"), (1, "Not available in test mode.\n"))
        set_.assert_not_called()

    def test_status_and_enable_startup_print_any_path(self):
        """run.cmd starts Python with -I (PYTHONIOENCODING ignored): a redirected stdout uses the ANSI
        code page with "surrogateescape" (or "strict"). A path character outside it is replaced, never
        an error."""
        odd = os.path.join("C:\\", "Dev\u0100\u4e00", "DisplayDesktop.pyw")
        for command, errors in (("status", "surrogateescape"), ("enable-startup", "surrogateescape"),
                                ("status", "strict")):
            raw = io.BytesIO()
            out = io.TextIOWrapper(raw, encoding="cp1252", errors=errors, write_through=True)
            with mock.patch.object(startup, "LAUNCHER", odd), mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(startup, "can_manage", return_value=True), \
                    mock.patch.object(startup, "windowless", return_value=True), \
                    mock.patch.object(startup, "set_enabled", return_value=True), \
                    mock.patch.object(startup, "state", return_value="on"):
                code = cli.run(command)
            self.assertEqual(code, 0, command)
            self.assertIn(b"Dev??", raw.getvalue(), command)

    def test_status_redirected_to_a_file_prints_any_path(self):
        """The real thing: `python -I -S -B` with stdout in a file (Windows: ANSI code page,
        surrogateescape). What it reads back depends on that code page (a letter it has is kept)."""
        import codecs
        import locale
        import subprocess
        acp = locale.getpreferredencoding(False)
        if codecs.lookup(acp).name == "utf-8":
            self.skipTest("ANSI code page 65001: every path prints as it is")
        want = "DevĀ一".encode(acp, "replace")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        out_file = os.path.join(tmp, "out.txt")
        code = ("import sys\n"
                f"sys.path.insert(0, {startup.REPO_DIR!r})\n"
                "from src import cli, startup\n"
                "startup.LAUNCHER = 'C:\\\\Dev\\u0100\\u4e00\\\\DisplayDesktop.pyw'\n"
                "sys.exit(cli.run('status'))\n")
        with open(out_file, "wb") as f:
            done = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code], stdout=f, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, timeout=60, creationflags=0x08000000)
        self.assertEqual(done.stderr.decode("ascii", "replace").strip().splitlines()[-1:], [])
        self.assertEqual(done.returncode, 0)
        with open(out_file, "rb") as f:
            self.assertIn(want, f.read())

    def run_cmd_with(self, python_cmd: str, repo: str = "", env: dict | None = None, py_exit: int = 0,
                     args: tuple[str, ...] = ("status",)) -> str:
        """Runs run.cmd `args` (default status) with only a stand-in python.cmd and py.cmd on PATH;
        returns the last call they logged. `repo`: a folder (made under a temp folder) to run a copy of
        run.cmd from; `env`: more environment variables; `py_exit`: py.cmd's exit code."""
        import subprocess
        from src import native
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        run_cmd = os.path.join(startup.REPO_DIR, "run.cmd")
        if repo:
            os.mkdir(os.path.join(tmp, repo))
            run_cmd = shutil.copy(run_cmd, os.path.join(tmp, repo, "run.cmd"))
        with open(os.path.join(tmp, "python.cmd"), "w", encoding="ascii") as f:
            f.write(python_cmd)
        with open(os.path.join(tmp, "py.cmd"), "w", encoding="ascii") as f:
            f.write(f'@>>"%~dp0log.txt" echo py %*\n@exit /b {py_exit}\n')
        system = native._SYS32
        out = subprocess.run([os.path.join(system, "cmd.exe"), "/d", "/c", run_cmd, *args], cwd=tmp,
                             env=dict(os.environ, PATH=os.pathsep.join([tmp, system]), **(env or {})),
                             capture_output=True, text=True, timeout=60, creationflags=0x08000000)
        with open(os.path.join(tmp, "log.txt"), encoding="oem") as f:  # (cmd's echo writes the OEM code page)
            return f.read().splitlines()[-1] + f"  ({out.returncode})"

    def test_an_errorlevel_variable_never_hides_the_exit_codes(self):
        """%errorlevel% is a variable's value when one is set (ERRORLEVEL=0 in the environment):
        run.cmd must still see the probe's failure and hand back the run's exit code."""
        flags = " ".join(startup.FLAGS)
        broken = '@>>"%~dp0log.txt" echo python %*\n@if "%~3"=="-c" exit /b 1\n@exit /b 4\n'
        for value in ("0", "7"):
            self.assertEqual(self.run_cmd_with(broken, env={"ERRORLEVEL": value}, py_exit=3),
                             f'py -3 {flags} "{startup.LAUNCHER}" status  (3)', value)
        new = '@>>"%~dp0log.txt" echo python %*\n@if "%~3"=="-c" exit /b 0\n@exit /b 4\n'
        self.assertEqual(self.run_cmd_with(new, env={"ERRORLEVEL": "0"}),
                         f'python {flags} "{startup.LAUNCHER}" status  (4)')

    def test_run_cmd_falls_back_to_py_when_python_lacks_the_flags(self):
        flags = " ".join(startup.FLAGS)
        old = '@>>"%~dp0log.txt" echo python %*\n@if "%~1"=="-I" exit /b 2\n@exit /b 0\n'  # (no -I: an old Python)
        self.assertEqual(self.run_cmd_with(old), f'py -3 {flags} "{startup.LAUNCHER}" status  (0)')
        new = '@>>"%~dp0log.txt" echo python %*\n@exit /b 0\n'
        self.assertEqual(self.run_cmd_with(new), f'python {flags} "{startup.LAUNCHER}" status  (0)')

    def test_run_cmd_falls_back_to_py_for_a_python_older_than_3_10(self):
        """Python 3.4-3.9 knows -I -S: the probe asks for the version (its code answers 1: too old)."""
        flags = " ".join(startup.FLAGS)
        old = '@>>"%~dp0log.txt" echo python %*\n@if "%~3"=="-c" if not "%~4"=="" exit /b 1\n@exit /b 0\n'
        self.assertEqual(self.run_cmd_with(old), f'py -3 {flags} "{startup.LAUNCHER}" status  (0)')
        import subprocess
        with open(os.path.join(startup.REPO_DIR, "run.cmd"), encoding="ascii") as f:
            probe = next(line for line in f if line.startswith("call python "))
        code = probe.split(' -c "', 1)[1].split('"', 1)[0]
        self.assertEqual([c for c in "<>;=" if c in code], [], "safe in a shim that passes it on unquoted")
        self.assertEqual(subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code], timeout=60,
                                        creationflags=0x08000000).returncode, 0, "this Python passes")
        for version, want in ((0x030900F0, 1), (0x03040000, 1), (0x030A0000, 0), (0x030D01F0, 0), (0x04000000, 0)):
            as_if = f"import sys\nsys.hexversion = {version}\nsys.version_info = ({version >> 24}, {version >> 16 & 255})\n"
            self.assertEqual(subprocess.run([sys.executable, "-I", "-S", "-B", "-c", as_if + code], timeout=60,
                                            creationflags=0x08000000).returncode, want, hex(version))

    def test_run_cmd_falls_back_to_py_when_python_crashes(self):
        """A broken python.exe ends with a negative NTSTATUS (e.g. 0xC0000135, a DLL not found)."""
        flags = " ".join(startup.FLAGS)
        broken = '@>>"%~dp0log.txt" echo python %*\n@if "%~3"=="-c" exit /b -1073741515\n@exit /b 0\n'
        self.assertEqual(self.run_cmd_with(broken), f'py -3 {flags} "{startup.LAUNCHER}" status  (0)')

    def test_run_cmd_passes_arguments_with_parentheses(self):
        """A closing parenthesis in the arguments ends no if/else block: both Pythons get them as given."""
        flags = " ".join(startup.FLAGS)
        new = '@>>"%~dp0log.txt" echo python %*\n@exit /b 0\n'
        old = '@>>"%~dp0log.txt" echo python %*\n@if "%~3"=="-c" exit /b 1\n@exit /b 0\n'
        for args in (("status", "a)b"), ("status", ")"), ("status", "(x)")):
            self.assertEqual(self.run_cmd_with(new, args=args),
                             f'python {flags} "{startup.LAUNCHER}" {" ".join(args)}  (0)', args)
            self.assertEqual(self.run_cmd_with(old, args=args),
                             f'py -3 {flags} "{startup.LAUNCHER}" {" ".join(args)}  (0)', args)

    def test_run_cmd_log_of_a_folder_outside_ascii(self):
        """The stand-ins' log is in the OEM code page: a repo folder outside ASCII reads back."""
        flags = " ".join(startup.FLAGS)
        new = '@>>"%~dp0log.txt" echo python %*\n@exit /b 0\n'
        name = oem_folder_name()
        if name is None:
            self.skipTest("the OEM code page has none of the letters tried")
        got = self.run_cmd_with(new, name)
        self.assertTrue(got.startswith("python " + flags + ' "') and
                        got.endswith(os.path.join(name, "DisplayDesktop.pyw") + '" status  (0)'), got)

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

    @staticmethod
    def python_runs_without_b(source: str) -> list[int]:
        """Lines of each argument list in `source` that starts with a Python (sys.executable, or a name
        with "python" in it: PYTHONW, python) and has no "-B" (one or several lines)."""
        import ast

        def is_python(node) -> bool:
            if isinstance(node, ast.Attribute):
                return node.attr == "executable" and isinstance(node.value, ast.Name) and node.value.id == "sys"
            return isinstance(node, ast.Name) and "python" in node.id.lower()
        return [node.lineno for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts and is_python(node.elts[0])
                and not any(isinstance(e, ast.Constant) and e.value == "-B" for e in node.elts)]

    def test_every_test_python_subprocess_writes_no_bytecode(self):
        """Every Python the tests start runs with -B (no __pycache__ left in the repo)."""
        sample = ("subprocess.run([sys.executable,\n    '-c', code])\n"
                  "subprocess.Popen([PYTHONW, '-m', 'src'])\n"
                  "subprocess.run((python, '-c', code))\n"
                  "subprocess.run([sys.executable, '-B', '-c', code])\n"
                  "subprocess.run([PYTHONW,\n    '-I', '-B', '-m', 'src'])\n")
        self.assertEqual(self.python_runs_without_b(sample), [1, 3, 4], "the guard sees what it must")
        here = os.path.dirname(os.path.abspath(__file__))
        missing = []
        for name in sorted(os.listdir(here)):
            if name.endswith(".py"):
                with open(os.path.join(here, name), encoding="utf-8") as f:
                    missing += [f"{name}:{n}" for n in self.python_runs_without_b(f.read())]
        self.assertEqual(missing, [])


class Paths(unittest.TestCase):
    def test_launcher_is_next_to_the_package(self):
        self.assertTrue(os.path.isfile(startup.LAUNCHER))
        self.assertEqual(os.path.dirname(startup.LAUNCHER), startup.REPO_DIR)
        self.assertTrue(os.path.isdir(os.path.join(startup.REPO_DIR, "src")))

    def test_the_app_command_always_names_this_python_s_pythonw_by_its_full_path(self):
        """Never a bare "pythonw", whatever a PATH search would find (this Python first, another, none):
        the sign-in's PATH, PATHEXT and variables play no part."""
        here = os.path.dirname(os.path.abspath(sys.executable))
        own = os.path.join(here, "pythonw.exe")
        if not os.path.isfile(own):
            self.skipTest("this Python has no pythonw.exe")
        with no_path_search(self), mock.patch.dict(os.environ, {"PATH": here}):
            self.assertEqual(startup.pythonw_command(), own)
            exe = f'"{own}"' if " " in own else own
            self.assertEqual(startup.command_line(), f'{exe} -I -S -B "{startup.LAUNCHER}"')
            with mock.patch.object(startup, "pyw_program", return_value=""):
                self.assertEqual(startup.target(), (own, f'-I -S -B "{startup.LAUNCHER}"'),
                                 "the Startup shortcut's fallback target")
            self.assertEqual(startup.app_command(), [own, "-I", "-S", "-B", startup.LAUNCHER])

    def test_status_prints_the_full_path(self):
        here = os.path.dirname(os.path.abspath(sys.executable))
        own = os.path.join(here, "pythonw.exe")
        if not os.path.isfile(own):
            self.skipTest("this Python has no pythonw.exe")
        out = io.StringIO()
        with no_path_search(self), \
                mock.patch.object(cli, "is_running", return_value=False), redirect_stdout(out):
            self.assertEqual(cli.run("status"), 0)
        exe = f'"{own}"' if " " in own else own
        self.assertIn(f'App command: {exe} -I -S -B "{startup.LAUNCHER}"\n', out.getvalue())

    def test_a_drive_relative_folder_is_not_path(self):
        r"""`\Python310` is relative to the current drive (isabs is True for it before Python 3.13)."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name in ("tool.exe", "pyw.exe"):
            open(os.path.join(d, name), "w").close()
        drive, rest = os.path.splitdrive(d)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(drive + "\\")
        with mock.patch.dict(os.environ, {"PATH": rest, "PATHEXT": ".EXE"}):
            self.assertIsNone(startup.on_path("tool"))
            self.assertIsNone(shortcut._launcher(), "the shortcut's pyw.exe search neither")
        with mock.patch.dict(os.environ, {"PATH": d, "PATHEXT": ".EXE"}):
            self.assertEqual(os.path.normcase(startup.on_path("tool")), os.path.normcase(os.path.join(d, "tool.exe")))

    def test_on_path_searches_absolute_path_folders_for_the_exact_name(self):
        """CreateProcess (the Run value, the shortcut) adds only ".exe", and only to a name without an
        extension: PATHEXT plays no part."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name in ("tool.exe", "tool.cmd", "script.bat", "other.com"):
            with open(os.path.join(d, name), "w", encoding="utf-8"):
                pass
        os.mkdir(os.path.join(d, "folder.exe"))  # (a folder never counts)
        self.addCleanup(os.chdir, os.getcwd())  # (runs before the rmtree)
        os.chdir(d)
        found = lambda cmd: os.path.normcase(startup.on_path(cmd) or "")  # noqa: E731
        for pathext in (".COM;.EXE;.CMD", ".CMD", ".BAT;.COM"):
            with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", d]), "PATHEXT": pathext}):
                self.assertEqual(found("tool"), os.path.normcase(os.path.join(d, "tool.exe")), pathext)
                self.assertEqual(found("TOOL.CMD"), os.path.normcase(os.path.join(d, "tool.cmd")), pathext)
                self.assertEqual(found("tool.exe"), os.path.normcase(os.path.join(d, "tool.exe")), pathext)
                self.assertEqual(found("script"), "", pathext)
                self.assertEqual(found("other"), "", pathext)
                self.assertEqual(found("folder"), "", pathext)
        with mock.patch.dict(os.environ, {"PATH": d, "PATHEXT": ".CMD"}):
            self.assertEqual(os.path.normcase(shortcut._launcher() or ""), "", "no pyw.exe")
            open(os.path.join(d, "pyw.exe"), "w").close()
            self.assertEqual(os.path.normcase(shortcut._launcher() or ""), os.path.normcase(os.path.join(d, "pyw.exe")),
                             "pyw.exe found whatever PATHEXT says")
        for env in ({"PATH": os.pathsep.join([".", ""])}, {"PATH": ""}, {}):
            with mock.patch.dict(os.environ):
                os.environ.pop("PATH", None)
                os.environ.update(env)
                self.assertIsNone(startup.on_path("tool"), env)

    def test_a_unc_share_root_is_a_path_folder(self):
        r"""`\\server\share` (no folder after it) is absolute: isabs says no before Python 3.13."""
        share = r"\\server\share"
        files = {share + r"\tool.exe", "//server/share\\tool.exe"}
        with mock.patch.object(startup, "_is_file", side_effect=lambda p: p in files):
            self.assertEqual(startup.on_path("tool", [share]), share + r"\tool.exe")
            self.assertEqual(startup.on_path("tool", [share + "\\"]), share + r"\tool.exe")
            self.assertEqual(startup.on_path("tool", ["//server/share"]), "//server/share\\tool.exe", "forward slashes")
            self.assertIsNone(startup.on_path("tool", [r"\\server"]), "no share: not a folder")

    def slow_search(self, folders: list[str], slow: tuple[str, ...], fixed: bool,
                    unanswered: set | None = None) -> tuple[str | None, float, list]:
        """on_path("tool", folders) where a check of a file under `slow` answers (True) only after 5 s
        (an offline share: the network timeout) and every drive letter is `fixed` or not; returns
        (found, seconds taken, the checked files). `unanswered`: the roots that did not answer before
        (default none). Every check thread is settled before it returns."""
        import threading
        import time
        from src import native
        late = threading.Event()
        lock = threading.Lock()
        running = [0]
        done = threading.Condition(lock)
        checked = []
        real = os.path.isfile

        def check(p):
            with lock:
                running[0] += 1
                checked.append(p)
            try:
                if p.startswith(slow):
                    late.wait(5)
                    return True
                return real(p)
            finally:
                with done:
                    running[0] -= 1
                    done.notify_all()
        try:
            with mock.patch.object(startup, "_is_file", check), mock.patch.object(startup, "SLOW_WAIT_S", 0.2), \
                    mock.patch.dict(startup.__dict__, {"_unanswered": set() if unanswered is None else unanswered}), \
                    mock.patch.object(native, "is_fixed_drive", side_effect=lambda root: fixed):
                began = time.monotonic()
                got = startup.on_path("tool", folders)
                took = time.monotonic() - began
        finally:
            late.set()
            with done:
                done.wait_for(lambda: not running[0], 10)
        return got, took, checked

    def test_a_folder_off_the_fixed_disks_that_does_not_answer_never_holds_the_search_up(self):
        r"""A share (UNC with either slash, a mapped drive) or any other drive that is not a fixed disk
        (removable, optical) may answer only after a timeout: the search waits for it at most
        SLOW_WAIT_S, then goes on without it. Made-up hosts are never looked up (the check is injected)."""
        local = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, local, True)
        open(os.path.join(local, "tool.exe"), "w").close()
        want = os.path.normcase(os.path.join(local, "tool.exe"))
        other = os.path.join(local, "other drive")  # (stands in for a folder on a drive that is not a fixed disk)
        for folders, slow, fixed in (([r"\\offline\share\bin", local], ("\\\\",), True),
                                     (["//offline/share/bin", local], ("//",), True),
                                     ([other, local], (other,), False)):
            got, took, checked = self.slow_search(folders, slow, fixed)
            self.assertEqual(os.path.normcase(got or ""), want, folders)
            self.assertLess(took, 2.0, folders)
            self.assertEqual(len(checked), 2, folders)
        with mock.patch.object(startup, "_is_file", side_effect=lambda p: p.startswith(("\\\\", "//"))):
            self.assertEqual(startup.on_path("tool", [r"\\server\share", local]), r"\\server\share\tool.exe",
                             "a share that answers in time keeps its place in PATH")

    def test_a_root_that_did_not_answer_is_not_checked_again_this_run(self):
        """No new stuck thread per search: a share (or drive) that did not answer in time is skipped by
        every later search; other shares are still checked."""
        local = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, local, True)
        open(os.path.join(local, "tool.exe"), "w").close()
        unanswered = set()
        folders = [r"\\offline\share\bin", local]
        _, _, checked = self.slow_search(folders, ("\\\\offline",), True, unanswered)
        self.assertEqual(len(checked), 2)
        for again in (folders, ["//OFFLINE/Share/other", local]):
            got, took, checked = self.slow_search(again, ("\\\\offline", "//OFFLINE"), True, unanswered)
            self.assertEqual(os.path.normcase(got or ""), os.path.normcase(os.path.join(local, "tool.exe")), again)
            self.assertEqual(len(checked), 1, again)
            self.assertLess(took, 0.15, again)
        _, _, checked = self.slow_search([r"\\other\share", local], ("\\\\offline",), True, unanswered)
        self.assertEqual(len(checked), 2, "another share is checked")

    def test_folders_after_the_first_match_are_not_probed(self):
        """on_path takes the first match: no thread is started for a share after it (PATH order kept:
        one before it is still waited for)."""
        local = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, local, True)
        open(os.path.join(local, "tool.exe"), "w").close()
        with mock.patch.object(startup, "_probe", wraps=startup._probe) as probe:
            got, _, _ = self.slow_search([local, r"\\offline\share\bin"], ("\\\\",), True)
        self.assertEqual((os.path.normcase(got or ""), probe.call_count),
                         (os.path.normcase(os.path.join(local, "tool.exe")), 0))
        with mock.patch.object(startup, "_probe", wraps=startup._probe) as probe:
            got, _, _ = self.slow_search([r"\\offline\share\bin", local], ("\\\\",), True)
        self.assertEqual((os.path.normcase(got or ""), probe.call_count),
                         (os.path.normcase(os.path.join(local, "tool.exe")), 1), "one before it is waited for")

    def test_no_thread_to_be_had_is_no_error(self):
        """A probe thread that can't start (RuntimeError) is a folder that answered no."""
        local = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, local, True)
        open(os.path.join(local, "tool.exe"), "w").close()
        import _thread
        with mock.patch.object(_thread, "start_new_thread", side_effect=RuntimeError("can't start new thread")), \
                mock.patch.dict(startup.__dict__, {"_unanswered": set()}), \
                mock.patch.object(startup, "_is_file", side_effect=lambda p: p.startswith(("\\\\", local))):
            self.assertEqual(os.path.normcase(startup.on_path("tool", [r"\\server\share", local]) or ""),
                             os.path.normcase(os.path.join(local, "tool.exe")))

    def test_paths_with_spaces_are_quoted(self):
        with mock.patch.object(startup, "pythonw_command", return_value=r"C:\Program Files\Py\pythonw.exe"), \
                mock.patch.object(startup, "LAUNCHER", r"C:\My Apps\DisplayDesktop.pyw"):
            self.assertEqual(startup.command_line(),
                             r'"C:\Program Files\Py\pythonw.exe" -I -S -B "C:\My Apps\DisplayDesktop.pyw"')


class Shortcut(unittest.TestCase):
    def test_location_and_icon(self):
        self.assertTrue(os.path.isdir(shortcut.programs_dir()))
        self.assertEqual(os.path.basename(shortcut.path()), "DisplayDesktop.lnk")
        self.assertTrue(os.path.isfile(shortcut.ICON))

    def test_the_startup_shortcut_is_in_the_startup_folder(self):
        """Read only: the real Startup folder (Windows names it), "DisplayDesktop.lnk"."""
        lnk = startup.path()
        self.assertEqual(os.path.basename(lnk), "DisplayDesktop.lnk")
        self.assertEqual(os.path.dirname(lnk), shortcut.folder(shortcut.CSIDL_STARTUP))
        self.assertTrue(os.path.isdir(os.path.dirname(lnk)))
        self.assertEqual(os.path.basename(os.path.dirname(lnk)).lower(), "startup")

    def test_add_to_start_menu_is_never_left_on_without_a_windowless_python(self):
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(shortcut, "target", side_effect=OSError("no pythonw.exe")), \
                mock.patch.object(shortcut, "exists", return_value=False), \
                mock.patch.object(settings, "set") as stored:
            self.assertFalse(shortcut.set_enabled(True))
        stored.assert_not_called()

    def test_start_with_windows_needs_a_python_that_runs_without_a_console(self):
        with mock.patch.object(startup, "can_manage", return_value=True), \
                mock.patch.object(startup, "pyw_program", return_value=""), \
                mock.patch.object(startup, "windowless", return_value=False), \
                mock.patch.object(startup, "path", return_value=r"C:\none\x.lnk"), \
                mock.patch.object(shortcut, "write_link") as write:
            self.assertFalse(startup.set_enabled(True))
        write.assert_not_called()  # (a console window at every sign-in otherwise)

    def test_the_console_python_skips_a_stub_alias_for_a_real_python_later_on_path(self):
        from src import console as C
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        stub_dir = os.path.join(tmp, "Microsoft", "WindowsApps")
        real_dir = os.path.join(tmp, "Python")
        for d in (stub_dir, real_dir):
            os.makedirs(d)
            open(os.path.join(d, "python.exe"), "w").close()
        with mock.patch.object(C.sys, "executable", os.path.join(tmp, "none", "pythonw.exe")), \
                mock.patch.dict(os.environ, {"PATH": os.pathsep.join((stub_dir, real_dir))}), \
                mock.patch.object(C, "_alias_runs", return_value=False):
            self.assertEqual(C._python_exe(), os.path.join(real_dir, "python.exe"))

    def test_the_console_python_needs_a_drive_or_unc_root(self):
        r"""`\Python310` is relative to the current drive (isabs is True for it before Python 3.13): not
        a PATH folder for the console's python.exe either."""
        from src import console as C
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        open(os.path.join(d, "python.exe"), "w").close()
        drive, rest = os.path.splitdrive(d)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(drive + "\\")
        with mock.patch.object(C.sys, "executable", os.path.join(d, "none", "pythonw.exe")):
            with mock.patch.dict(os.environ, {"PATH": rest}):
                self.assertIsNone(C._python_exe())
            with mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(os.path.normcase(C._python_exe() or ""), os.path.normcase(os.path.join(d, "python.exe")))

    def test_target_is_the_running_python_s_pythonw_else_the_launcher(self):
        with mock.patch.object(shortcut, "_launcher", return_value=r"C:\Windows\pyw.exe"):
            exe, args = shortcut.target()
        self.assertEqual(os.path.normcase(os.path.dirname(exe)),
                         os.path.normcase(os.path.dirname(os.path.abspath(sys.executable))),
                         "the Python it runs on, as the startup entry")
        self.assertTrue(os.path.isabs(exe) and exe.lower().endswith("pythonw.exe"))
        self.assertEqual(args, f'-I -S -B "{startup.LAUNCHER}"')
        with mock.patch.object(shortcut.os.path, "isfile", return_value=False), \
                mock.patch.object(shortcut.sys, "executable", r"C:\Py\python.exe"), \
                mock.patch.object(shortcut, "_launcher", return_value=r"C:\Windows\pyw.exe"):
            self.assertEqual(shortcut.target(), (r"C:\Windows\pyw.exe", f'-3 -I -S -B "{startup.LAUNCHER}"'),
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
                             ("args", f'-B "{startup.LAUNCHER}"'),  # (this copy, older flags)
                             ("work_dir", r"D:\moved"), ("icon", r"D:\moved\src\app.ico"), ("app_id", "Other.Id")]:
            old = dict(good, **{field: value})
            shortcut.write_link(lnk, old["exe"], old["args"], old["work_dir"], old["icon"], "desc", old["app_id"])
            self.assertFalse(shortcut.is_current(), field)
            self.assertEqual(shortcut.ensure(), "updated", field)
            self.assertTrue(shortcut.is_current(), field)

    def test_paths_in_another_letter_case_are_current(self):
        """The repo path typed in another case (abspath(__file__) keeps it): not rewritten each launch.
        Flags are case-sensitive."""
        lnk = self.in_temp_start_menu()
        exe, args = shortcut.target()
        odd = args.replace(startup.LAUNCHER, startup.LAUNCHER.upper())
        self.assertNotEqual(odd, args)
        shortcut.write_link(lnk, exe.upper(), odd, startup.REPO_DIR, shortcut.ICON, "d", shortcut.APP_ID)
        self.assertTrue(shortcut.is_current())
        with mock.patch.object(shortcut, "write_link") as write:
            self.assertEqual(shortcut.ensure(), "")
        write.assert_not_called()
        shortcut.write_link(lnk, exe, odd.replace("-I -S -B", "-i -s -b"), startup.REPO_DIR, shortcut.ICON, "d",
                            shortcut.APP_ID)
        self.assertFalse(shortcut.is_current())

    def test_without_pythonw_turning_it_on_or_a_launch_searches_path_once(self):
        """target() searches PATH for pyw.exe when this Python has no pythonw.exe: set_enabled and
        ensure ask it once, not once per step (check, compare, write)."""
        lnk = self.in_temp_start_menu()
        pyw = r"C:\Launcher\pyw.exe"
        with mock.patch.object(shortcut.os.path, "isfile", side_effect=lambda p: p == lnk), \
                mock.patch.object(shortcut.sys, "executable", r"C:\Py\python.exe"), \
                mock.patch.object(shortcut, "_launcher", return_value=pyw) as search:
            shortcut.write_link(lnk, pyw, '-3 -B "D:\\moved\\DisplayDesktop.pyw"', r"D:\moved", shortcut.ICON, "d",
                                shortcut.APP_ID)
            self.assertTrue(shortcut.set_enabled(True))
            self.assertEqual(search.call_count, 1, "set_enabled")
            self.assertEqual(shortcut.read_link(lnk)["args"], f"-3 {startup.launcher_args()}")
            shortcut.write_link(lnk, pyw, '-3 -B "D:\\moved\\DisplayDesktop.pyw"', r"D:\moved", shortcut.ICON, "d",
                                shortcut.APP_ID)
            search.reset_mock()
            self.assertEqual(shortcut.ensure(), "updated")
            self.assertEqual(search.call_count, 1, "ensure")

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


class StartupShortcut(unittest.TestCase):
    """Start With Windows = a shortcut in a temp folder in place of the Startup folder, throw-away keys
    in place of the Run and StartupApproved keys: a killed run leaves nothing that runs at sign-in.
    The real Startup folder, Run key and StartupApproved values are compared before and after."""
    REAL_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def setUp(self):
        self.assertFalse(paths.is_test_mode(), "unset DISPLAYDESKTOP_DATA to run these tests")
        real = self.real_state()
        self.addCleanup(lambda: self.assertEqual(self.real_state(), real, "the real startup entries changed"))
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.lnk = os.path.join(d.name, TEST_LNK)
        notify = shortcut._SHChangeNotify  # (told without SHCNF_FLUSH: no wait for the shell)
        for p in (mock.patch.object(startup, "VALUE_NAME", TEST_NAME), mock.patch.object(startup, "LNK", TEST_LNK),
                  mock.patch.object(startup, "RUN_KEY", TEST_RUN_KEY),
                  mock.patch.object(startup, "APPROVED_RUN", TEST_APPROVED_RUN),
                  mock.patch.object(startup, "APPROVED_FOLDER", TEST_APPROVED_FOLDER),
                  mock.patch.object(startup, "path", return_value=self.lnk),
                  mock.patch.object(shortcut, "_SHChangeNotify",
                                    lambda event, flags, item, extra: notify(event, flags & ~shortcut.SHCNF_FLUSH,
                                                                             item, extra))):
            p.start()
            self.addCleanup(p.stop)
        self._delete_key()
        self.addCleanup(self._delete_key)

    def real_state(self):
        """The real Run value, StartupApproved values and Startup shortcut (read only)."""
        r = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
        lnk = os.path.join(shortcut.folder(shortcut.CSIDL_STARTUP), "DisplayDesktop.lnk")
        try:
            st = os.stat(lnk)
            lnk_state = (st.st_size, st.st_mtime_ns)
        except OSError:
            lnk_state = None
        return (startup._value(self.REAL_RUN_KEY, "DisplayDesktop"), startup._value(r + r"\Run", "DisplayDesktop"),
                startup._value(r + r"\StartupFolder", "DisplayDesktop.lnk"), lnk_state)

    def _delete_key(self):
        """The throw-away keys, and a test value an older test run left in the real Run key."""
        for key in (TEST_RUN_KEY, TEST_APPROVED_RUN, TEST_APPROVED_FOLDER):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except FileNotFoundError:
                pass
        self._delete(self.REAL_RUN_KEY)

    @staticmethod
    def _delete(key_path: str):
        """The test value in `key_path` (only that value: never "DisplayDesktop")."""
        assert TEST_NAME != "DisplayDesktop"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, TEST_NAME)
        except FileNotFoundError:
            pass

    def test_a_test_value_left_in_the_real_run_key_is_deleted(self):
        """Older test runs wrote "DisplayDesktop (unit test)" into the real Run key: setUp deletes it
        and only it. Shown on the throw-away key (the real one is never written)."""
        self._set(TEST_RUN_KEY, TEST_NAME, 0, winreg.REG_DWORD)
        self._set(TEST_RUN_KEY, "DisplayDesktop", "the real entry")
        self._delete(TEST_RUN_KEY)
        self.assertIsNone(startup._value(TEST_RUN_KEY, TEST_NAME))
        self.assertEqual(startup._value(TEST_RUN_KEY, "DisplayDesktop"), "the real entry",
                         "the real entry is left as it is")
        self._delete(TEST_RUN_KEY)  # (nothing there: no error)

    def _set(self, key, name, value, kind=winreg.REG_SZ):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, kind, value)

    def set_run(self, value="pythonw.exe old entry", kind=winreg.REG_SZ):
        self._set(TEST_RUN_KEY, TEST_NAME, value, kind)

    def mark(self, key, first_byte):
        """A StartupApproved value: first byte + 11 bytes (the page writes a FILETIME after 01)."""
        self._set(key, TEST_NAME if key == TEST_APPROVED_RUN else TEST_LNK, bytes([first_byte]) + bytes(11),
                  winreg.REG_BINARY)

    def run_value(self):
        return startup._value(TEST_RUN_KEY, TEST_NAME)

    def approved(self, key):
        return startup._value(key, TEST_NAME if key == TEST_APPROVED_RUN else TEST_LNK)

    def link_is(self, want):
        got = shortcut.read_link(self.lnk)
        self.assertEqual(os.path.normcase(got["exe"]), os.path.normcase(want[0]))
        self.assertEqual(got["args"], want[1])
        self.assertEqual(os.path.normcase(got["work_dir"]), os.path.normcase(startup.REPO_DIR))
        self.assertEqual(os.path.normcase(got["icon"]), os.path.normcase(shortcut.ICON))
        self.assertEqual(got["app_id"], shortcut.APP_ID)

    def test_the_target_is_the_pyw_itself_when_pyw_files_open_with_a_python(self):
        """Listed as "DisplayDesktop.pyw" on the Startup apps page (a pythonw.exe target is "Python")."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pyw = os.path.join(tmp, "pyw.exe")
        open(pyw, "w").close()
        with mock.patch.object(startup, "pyw_program", return_value=pyw):
            self.assertTrue(startup.pyw_opens())
            self.assertEqual(startup.target(), (startup.LAUNCHER, ""))
            self.assertTrue(startup.set_enabled(True))
            self.link_is((startup.LAUNCHER, ""))
            self.assertEqual(startup.state(), "on")
        fallback = (startup.pythonw_command(), startup.launcher_args())
        for program in ("", os.path.join(tmp, "missing", "pyw.exe"), os.path.join(tmp, "editor.exe")):
            if program.endswith("editor.exe"):
                open(program, "w").close()
            with mock.patch.object(startup, "pyw_program", return_value=program), \
                    mock.patch.object(startup, "windowless", return_value=True):
                self.assertFalse(startup.pyw_opens(), program)
                self.assertEqual(startup.target(), fallback, f"no working association ({program}): pythonw.exe")
                self.assertEqual(startup.state(), "outdated", "the .pyw target no longer opens here")
                startup.refresh()
                self.link_is(fallback)
                self.assertEqual(startup.state(), "on")
            startup.set_enabled(False)
            with mock.patch.object(startup, "pyw_program", return_value=pyw):
                self.assertTrue(startup.set_enabled(True))

    def test_the_association_is_read_from_windows(self):
        program = startup.pyw_program()
        self.assertIsInstance(program, str)
        if startup.pyw_opens():
            self.assertIn(os.path.basename(program).lower(), startup.PYW_PROGRAMS)
            self.assertTrue(os.path.isfile(program))

    def test_no_python_to_start_refuses_and_writes_nothing(self):
        self.set_run()
        with mock.patch.object(startup, "pyw_program", return_value=""), \
                mock.patch.object(startup, "windowless", return_value=False):
            self.assertRaises(OSError, startup.target)
            self.assertFalse(startup.set_enabled(True))
            startup.refresh()
        self.assertFalse(os.path.exists(self.lnk))
        self.assertIsNotNone(self.run_value(), "the Run value stays: no shortcut could be made")

    def test_on_off(self):
        self.assertEqual(startup.state(), "off")
        self.assertTrue(startup.set_enabled(True))
        self.assertTrue(os.path.isfile(self.lnk))
        self.link_is(startup.target())
        self.assertEqual(startup.state(), "on")
        self.assertFalse(startup.set_enabled(False))
        self.assertFalse(os.path.exists(self.lnk))
        self.assertFalse(startup.set_enabled(False))
        self.assertEqual(startup.state(), "off")

    def test_an_off_mark_of_the_startup_apps_page(self):
        """First byte odd = off (01 the page, 03/07 others); even or none = on. On clears it, off deletes it."""
        self.assertTrue(startup.set_enabled(True))
        for byte, on in ((0, True), (2, True), (6, True), (1, False), (3, False), (7, False)):
            self.mark(TEST_APPROVED_FOLDER, byte)
            self.assertEqual(startup.is_enabled(), on, byte)
            self.assertEqual(startup.state(), "on" if on else "off", byte)
        self._set(TEST_APPROVED_FOLDER, TEST_LNK, b"", winreg.REG_BINARY)
        self.assertTrue(startup.is_enabled(), "no data: on")
        self.mark(TEST_APPROVED_FOLDER, 1)
        self.assertTrue(startup.set_enabled(True), "on again clears the mark")
        self.assertIsNone(self.approved(TEST_APPROVED_FOLDER))
        self.mark(TEST_APPROVED_FOLDER, 1)
        self.assertFalse(startup.set_enabled(False))
        self.assertFalse(os.path.exists(self.lnk))
        self.assertIsNone(self.approved(TEST_APPROVED_FOLDER), "off deletes the mark too")

    def test_an_old_run_value_that_is_on_moves_to_the_shortcut(self):
        """Any data (an old full-path or bare pythonw command, another type, none at all)."""
        values = (("pythonw -B x", winreg.REG_SZ), (1, winreg.REG_DWORD), (b"", winreg.REG_BINARY),
                  (None, winreg.REG_NONE))
        for (value, kind), mark in zip(values * 2, (None, None, None, None, 0, 2, 6, 0)):
            self.set_run(value, kind)
            if mark is not None:
                self.mark(TEST_APPROVED_RUN, mark)
            startup.refresh()
            self.link_is(startup.target())
            self.assertEqual(startup.state(), "on", (value, mark))
            self.assertIsNone(self.run_value(), (value, mark))
            self.assertIsNone(self.approved(TEST_APPROVED_RUN), (value, mark))
            startup.set_enabled(False)

    def test_an_old_run_value_that_is_off_is_deleted_and_stays_off(self):
        for mark in (1, 3, 7):
            self.set_run()
            self.mark(TEST_APPROVED_RUN, mark)
            startup.refresh()
            self.assertFalse(os.path.exists(self.lnk), mark)
            self.assertIsNone(self.run_value(), mark)
            self.assertIsNone(self.approved(TEST_APPROVED_RUN), mark)
        self.mark(TEST_APPROVED_RUN, 0)  # (a mark with no Run value: removed)
        startup.refresh()
        self.assertIsNone(self.approved(TEST_APPROVED_RUN))

    def test_the_run_value_stays_when_the_shortcut_can_t_be_written(self):
        self.set_run()
        with mock.patch.object(shortcut, "write_link", side_effect=OSError("denied")):
            startup.refresh()  # (never raises)
            self.assertRaises(OSError, startup.set_enabled, True)
        self.assertFalse(os.path.exists(self.lnk))
        self.assertIsNotNone(self.run_value())

    def test_turning_it_on_or_off_also_deletes_an_old_run_value(self):
        for on in (True, False):
            self.set_run()
            self.mark(TEST_APPROVED_RUN, 1)
            self.assertEqual(startup.set_enabled(on), on)
            self.assertIsNone(self.run_value(), on)
            self.assertIsNone(self.approved(TEST_APPROVED_RUN), on)

    def test_an_outdated_shortcut_is_rewritten_while_on(self):
        want = startup.target()
        for exe, args, work in ((r"D:\moved\DisplayDesktop.pyw", "", r"D:\moved"),
                                (r"C:\Old Python\pythonw.exe", f'-B "{startup.LAUNCHER}"', startup.REPO_DIR),
                                (want[0], "-x", startup.REPO_DIR), (want[0], want[1], r"D:\moved")):
            shortcut.write_link(self.lnk, exe, args, work, shortcut.ICON, "d", shortcut.APP_ID)
            self.assertEqual(startup.state(), "outdated", exe)
            startup.refresh()
            self.link_is(want)
        shortcut.write_link(self.lnk, r"D:\moved\DisplayDesktop.pyw", "", r"D:\moved", shortcut.ICON, "d",
                            shortcut.APP_ID)
        self.mark(TEST_APPROVED_FOLDER, 1)
        startup.refresh()
        self.assertEqual(shortcut.read_link(self.lnk)["exe"], r"D:\moved\DisplayDesktop.pyw",
                         "turned off on the Startup apps page: left as it is")
        self.assertIsNotNone(self.approved(TEST_APPROVED_FOLDER), "and the off mark is kept")

    def test_paths_in_another_letter_case_are_current(self):
        exe, args = startup.target()
        odd = args.replace(startup.LAUNCHER, startup.LAUNCHER.upper())
        shortcut.write_link(self.lnk, exe.upper(), odd, startup.REPO_DIR.upper(), shortcut.ICON.upper(), "d",
                            shortcut.APP_ID)
        self.assertEqual(startup.state(), "on")
        with mock.patch.object(shortcut, "write_link") as write:
            startup.refresh()
            self.assertTrue(startup.set_enabled(True))
        write.assert_not_called()

    def test_a_shortcut_that_can_t_be_deleted_raises(self):
        self.assertTrue(startup.set_enabled(True))
        with mock.patch.object(shortcut.os, "remove", side_effect=PermissionError("in use")):
            self.assertRaises(OSError, startup.set_enabled, False)
        self.assertTrue(os.path.isfile(self.lnk))

    def test_test_mode_never_writes(self):
        self.set_run()
        with mock.patch.dict(os.environ, {"DISPLAYDESKTOP_DATA": "x"}):
            self.assertFalse(startup.set_enabled(True))
            startup.refresh()
        self.assertFalse(os.path.exists(self.lnk))
        self.assertIsNotNone(self.run_value())
        self.assertTrue(startup.set_enabled(True))
        with mock.patch.dict(os.environ, {"DISPLAYDESKTOP_DATA": "x"}):
            self.assertTrue(startup.set_enabled(False))
        self.assertTrue(os.path.isfile(self.lnk))

    def test_status_and_the_commands_show_the_shortcut(self):
        exe, args = startup.target()
        opens = f"{exe} {args}" if args else exe

        def run(command):
            out = io.StringIO()
            with mock.patch.object(cli, "is_running", return_value=False), redirect_stdout(out):
                return cli.run(command), out.getvalue()
        code, out = run("status")
        self.assertIn("Start with Windows: off\n", out)
        self.assertNotIn("Startup shortcut", out)
        self.assertEqual(run("enable-startup"), (0, f"Start with Windows: on ({opens})\n"))
        code, out = run("status")
        self.assertIn(f"Start with Windows: on\nStartup shortcut: {opens}\n", out)
        self.mark(TEST_APPROVED_FOLDER, 1)
        self.assertIn("Start with Windows: off (turned off in Settings > Apps > Startup)\n", run("status")[1])
        self.assertEqual(run("disable-startup"), (0, "Start with Windows: off\n"))
        self.assertEqual(run("disable-startup"), (0, "Start with Windows was already off.\n"))
        self.set_run()
        self.assertIn("An older startup entry (Run value) is there", run("status")[1])
        self.assertEqual(run("disable-startup"), (0, "Start with Windows: off\n"))
        self.assertIsNone(self.run_value())
        with mock.patch.object(shortcut, "write_link", side_effect=OSError("denied")):
            self.assertEqual(run("enable-startup"), (1, "Could not add the startup shortcut.\n"))
        with mock.patch.object(startup, "pyw_program", return_value=""), \
                mock.patch.object(startup, "windowless", return_value=False):
            self.assertEqual(run("enable-startup")[0], 1)
        self.assertFalse(os.path.exists(self.lnk))

    def test_settings_warns_when_it_can_t_be_changed(self):
        from src import settings_window as SW
        with mock.patch.object(startup, "set_enabled", side_effect=OSError("denied")), \
                mock.patch.object(startup, "is_enabled", return_value=False), \
                mock.patch.object(SW.G, "message_box") as box:
            self.assertFalse(SW.SettingsWindow._set_startup(True))
        box.assert_called_once()
        self.assertEqual(box.call_args[0][0], SW.L.t("msg.startupFail"))


if __name__ == "__main__":
    unittest.main()
