"""Recovery's report (recovery.py): every data file with what is wrong with it, in words."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from src import crypto, paths, recovery
from src import strings as L
from src.desktop_manager import StartRestorer
from src.paths import JOURNAL_FILE, LAYOUT_FILE, PLACES_FILE, SETTINGS_FILE, SHOWING_FILE

from fakes import FakeWin


class Report(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ws-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(crypto.reset)
        self.addCleanup(paths.forget_failures)
        p = mock.patch.object(crypto, "ITERATIONS", 1000)  # (PBKDF2's cost is not what these check)
        p.start()
        self.addCleanup(p.stop)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write(self, name: str, raw: bytes) -> None:
        with open(self.path(name), "wb") as fh:
            fh.write(raw)

    def states(self, report) -> dict:
        return {r.shown: (r.kind, r.state) for r in report.rows}

    def test_each_file_says_what_is_wrong_with_it(self):
        self.write(SETTINGS_FILE, b'{"StartMenu":true,\n "x": }')
        self.write(LAYOUT_FILE, b'{"Format":%d}' % (paths.FORMAT + 1))
        self.write(PLACES_FILE, b"\xff\xfe\x00junk")
        self.write(SHOWING_FILE, b'{"Format":"2"}')
        report = recovery.build(self.dir)
        s = self.states(report)
        self.assertEqual(s[SETTINGS_FILE], ("unreadable", L.f("recovery.notJson", 2, 7, "Expecting value")))
        self.assertEqual(s[LAYOUT_FILE][0], "unreadable")
        self.assertIn("revision number is newer", s[LAYOUT_FILE][1])
        self.assertRegex(s[LAYOUT_FILE][1], r'<a href="https://[^"]+">check for updates</a>', "the words are the link")
        self.assertEqual(s[PLACES_FILE], ("unreadable", L.t("recovery.notText")))
        self.assertEqual(s[SHOWING_FILE][0], "unreadable")
        self.assertIn("bad format", s[SHOWING_FILE][1])
        self.assertEqual(s[JOURNAL_FILE], ("missing", L.t("recovery.missing")))
        self.assertEqual(len(report.unreadable), 4)
        self.assertTrue(report.problem)
        self.assertNotIn("http", report.summary, "the project link is the title, not the text")
        self.assertIn(os.path.basename(self.dir), report.summary, "the folder is named")

    def test_files_that_cant_be_read_now_are_not_recreate_s(self):
        crypto.use("someone else's", True)
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        crypto.reset()
        report = recovery.build(self.dir)
        self.assertEqual(self.states(report)[SETTINGS_FILE], ("locked", L.t("recovery.locked")))
        self.assertTrue(report.locked)
        self.assertEqual(report.unreadable, [])
        self.assertIn(L.t("recovery.some"), report.summary)
        self.assertEqual(recovery.describe(PermissionError(13, "held"))[1], "held")
        with mock.patch.object(paths, "known_offline", return_value=True):
            self.assertEqual(recovery.check(self.path(SETTINGS_FILE)), (L.t("recovery.offline"), "held"))

    def test_a_crash_record_that_could_not_be_read_says_why_and_is_never_recreate_s(self):
        self.write(JOURNAL_FILE, b"{damaged")
        s = self.states(recovery.build(self.dir))
        self.assertEqual(s[JOURNAL_FILE][0], "journal", "(the manager tells and removes it)")
        os.remove(self.path(JOURNAL_FILE))  # (removed by the manager, its error kept)
        try:
            json.loads("{damaged")
        except ValueError as exc:
            error = exc
        aside = self.path(JOURNAL_FILE + ".ab12.later.json")
        report = recovery.build(self.dir, journals={self.path(JOURNAL_FILE): error, aside: "unread",
                                                    self.path("old.json"): "notJournal"})
        s = self.states(report)
        self.assertEqual(s[JOURNAL_FILE][0], "journal")
        self.assertIn("Removed", s[JOURNAL_FILE][1])
        self.assertIn("Damaged", s[JOURNAL_FILE][1])
        self.assertEqual(s[os.path.basename(aside)], ("journal", L.t("recovery.journalUnread")))
        self.assertIn(L.t("recovery.notJournal"), s["old.json"][1])
        self.assertEqual(report.unreadable, [])
        self.assertIn(L.t("problem.damaged"), report.summary)

    def test_nothing_wrong(self):
        report = recovery.build(self.dir)
        self.assertFalse(report.problem)
        self.assertTrue(report.summary.startswith(L.t("recovery.none")), "no \"opened on request\" sentence")
        self.assertTrue(all(r.kind == "missing" for r in report.rows))
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        self.assertEqual(self.states(recovery.build(self.dir))[SETTINGS_FILE], ("ok", L.t("recovery.ok")))

    def test_the_folder_problem_comes_first_and_other_folders_list_their_unreadable_files(self):
        other = os.path.join(self.dir, "default")
        os.makedirs(other)
        with open(os.path.join(other, SETTINGS_FILE), "w") as fh:
            fh.write("{damaged")
        skipped = os.path.join(other, LAYOUT_FILE)
        with open(skipped, "w") as fh:
            fh.write("{damaged")
        report = recovery.build(self.dir, problem="The folder is gone.", others=(other,),
                                skip={os.path.normcase(skipped)})
        self.assertTrue(report.summary.startswith("The folder is gone."))
        self.assertEqual(report.unreadable, [os.path.join(other, SETTINGS_FILE)], "skipped: listed, never asked")
        self.assertEqual(sum(r.kind == "unreadable" for r in report.rows), 2)


class StartRestore(unittest.TestCase):
    def test_hidden_windows_before_any_desktop_exists(self):
        win = FakeWin()
        a = win.add("A", visible=False)
        win.add("B")  # (shown: not listed)
        restorer = StartRestorer(win)
        self.assertEqual(restorer.hidden_windows(), [a])
        self.assertEqual(restorer.restore_windows([a, 12345]), 1, "a closed one is skipped")
        self.assertTrue(win.is_window_visible(a))


if __name__ == "__main__":
    unittest.main()
