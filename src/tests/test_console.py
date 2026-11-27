"""Show console: setting file, log history, output capture, and the separate viewer process."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from src import console, paths


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dd-console-")
        # Test mode: settings go to the temp folder, and the viewer never shows a window
        self._env = mock.patch.dict(os.environ, {"DISPLAYDESKTOP_DATA": self.tmp})
        self._env.start()
        console._history.clear()

    def tearDown(self):
        console.close_console()
        self._env.stop()
        console._history.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setting_round_trip(self):
        self.assertFalse(console.load_enabled())
        console.save_enabled(True)
        with open(os.path.join(self.tmp, "display.desktop.settings.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"ShowConsole":true,"Format":%d}' % paths.FORMAT)
        self.assertTrue(console.load_enabled())
        console.save_enabled(False)
        self.assertFalse(console.load_enabled())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "display.desktop.settings.json")))

    def test_history_is_bounded_and_timestamped(self):
        for i in range(console.HISTORY_LINES + 20):
            console.log(f"line {i}")
        self.assertEqual(len(console._history), console.HISTORY_LINES)
        self.assertTrue(console._history[-1].endswith(f"line {console.HISTORY_LINES + 19}"))
        self.assertRegex(console._history[-1], r"^\[\d\d:\d\d:\d\d\] ")

    def test_tee_logs_whole_lines_and_forwards(self):
        forwarded = []
        orig = mock.Mock(write=lambda s: forwarded.append(s))
        tee = console._Tee(orig, "out")
        tee.write("partial ")
        self.assertEqual(len(console._history), 0)
        tee.write("line\nsecond\r\n")
        self.assertEqual([h.split("] ", 1)[1] for h in console._history], ["partial line", "second"])
        self.assertEqual("".join(forwarded), "partial line\nsecond\r\n")
        console._Tee(None, "err").write("no original stream is fine\n")  # pythonw: sys.stderr is None

    def test_viewer_opens_closes_and_never_blocks(self):
        console.log("before open")
        self.assertTrue(console.open_console())
        self.assertTrue(console.is_open())
        self.assertTrue(console.open_console())  # already open: same viewer
        for i in range(2000):
            console.log(f"burst {i} " + "x" * 100)  # far more than a pipe buffer: must not block
        console.close_console()
        self.assertFalse(console.is_open())

    def test_viewer_closed_from_its_window(self):
        self.assertTrue(console.open_console())
        proc = console._viewer.proc
        proc.kill()  # what closing the console window does to the viewer
        proc.wait(5)
        self.assertTrue(console.closed_by_user())
        self.assertFalse(console.closed_by_user())  # reported once
        self.assertFalse(console.is_open())
        console.log("after the viewer is gone: still fine")

    def test_toggle_off_ends_viewer_quickly(self):
        self.assertTrue(console.open_console())
        proc = console._viewer.proc
        start = time.monotonic()
        console.close_console()
        self.assertIsNotNone(proc.poll())
        self.assertLess(time.monotonic() - start, 2.5)

    def test_app_exit_lets_viewer_finish(self):
        self.assertTrue(console.open_console())
        proc = console._viewer.proc
        console.close_console(app_exiting=True)
        self.assertEqual(proc.wait(5), 0)  # test mode: no "press Enter" wait

    def test_missing_python_exe(self):
        with mock.patch.object(console, "_python_exe", return_value=None):
            self.assertFalse(console.open_console())
        self.assertFalse(console.is_open())


if __name__ == "__main__":
    unittest.main()
