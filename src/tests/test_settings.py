"""display.desktop.settings.json: defaults, only non-default values stored, damaged files ignored."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from src import paths, settings


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dd-settings-")
        self.file = os.path.join(self.tmp, "display.desktop.settings.json")
        p = mock.patch.object(settings, "_path", return_value=self.file)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def read(self) -> str | None:
        if not os.path.exists(self.file):
            return None
        with open(self.file, encoding="utf-8") as fh:
            return fh.read()

    def test_defaults_without_a_file(self):
        self.assertFalse(settings.get("ShowConsole"))
        self.assertFalse(settings.get("StartMenu"))
        self.assertTrue(settings.get("UseGpu"), "Use GPU Acceleration is on by default")
        self.assertFalse(settings.get("ShowAdvanced"), "Settings' Advanced options are hidden by default")

    def test_only_values_that_differ_from_the_default_are_stored(self):
        settings.set("StartMenu", True)
        self.assertEqual(self.read(), '{"StartMenu":true,"Format":%d}' % paths.FORMAT)
        settings.set("ShowConsole", True)
        self.assertEqual(self.read(), '{"StartMenu":true,"ShowConsole":true,"Format":%d}' % paths.FORMAT)
        self.assertEqual((settings.get("StartMenu"), settings.get("ShowConsole")), (True, True))
        settings.set("StartMenu", False)
        self.assertEqual(self.read(), '{"ShowConsole":true,"Format":%d}' % paths.FORMAT)
        settings.set("ShowConsole", False)
        self.assertIsNone(self.read(), "all defaults: no file")

    def test_damaged_file_or_wrong_types_mean_defaults(self):
        for text in ("{not json", "[1,2]", "", '{"StartMenu":"yes","ShowConsole":1,"Other":true}'):
            with open(self.file, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.assertFalse(settings.get("StartMenu"), text)
            self.assertFalse(settings.get("ShowConsole"), text)

    def test_use_gpu_off_is_stored_on_is_the_default(self):
        settings.set("UseGpu", False)
        self.assertEqual(self.read(), '{"UseGpu":false,"Format":%d}' % paths.FORMAT)
        self.assertFalse(settings.get("UseGpu"))
        settings.set("UseGpu", True)
        self.assertIsNone(self.read())
        self.assertTrue(settings.get("UseGpu"))

    def test_restore_apps_is_off_by_default_and_on_is_stored(self):
        self.assertFalse(settings.get("RestoreApps"), "Restore Applications On Restart is off by default")
        settings.set("RestoreApps", True)
        self.assertEqual(self.read(), '{"RestoreApps":true,"Format":%d}' % paths.FORMAT)
        self.assertTrue(settings.get("RestoreApps"))
        settings.set("RestoreApps", False)
        self.assertIsNone(self.read(), "off again: the default, no file")
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write('{"RestoreApps":"yes"}')
        self.assertFalse(settings.get("RestoreApps"), "a wrong type reads as the default")

    def test_a_wrong_gpu_value_reads_as_the_default(self):
        for wrong in ('{"UseGpu":"yes"}', '{"UseGpu":1}'):
            with open(self.file, "w", encoding="utf-8") as fh:
                fh.write(wrong)
            self.assertTrue(settings.get("UseGpu"), wrong)

    def test_a_damaged_file_is_replaced_on_the_next_change(self):
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        settings.set("StartMenu", True)
        self.assertEqual(self.read(), '{"StartMenu":true,"Format":%d}' % paths.FORMAT)


class RepoLink(unittest.TestCase):
    def test_the_version_link_opens_the_github_page(self):
        from src import settings_window as SW
        with mock.patch.object(SW, "_ShellExecuteW") as shell:  # nothing is opened here
            SW.open_repo_page()
        self.assertEqual(shell.call_args[0][1:3], ("open", "https://github.com/beepboopassisted/DisplayDesktop"))


if __name__ == "__main__":
    unittest.main()
