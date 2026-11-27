"""datafolder.py (Settings > Settings Folder) on temp folders and a throw-away registry key: the real
data folder and the real registry value are never touched."""
from __future__ import annotations

import json
import os
import shutil
import string
import tempfile
import unittest
import winreg
from unittest import mock

from src import datafolder as DF
from src import paths
from src.desktop_manager import DesktopManager
from src.paths import JOURNAL_FILE, LAYOUT_FILE, PLACES_FILE, SETTINGS_FILE

from fakes import FakeSnapshots, FakeWin

TEST_KEY = r"Software\DisplayDesktop (unit test)"


def write(folder, name, text="{}"):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def read(folder, name):
    with open(os.path.join(folder, name), encoding="utf-8") as fh:
        return fh.read()


def own(name: str) -> str:
    """The test's own data file `name` (in the current format: never upgraded when read)."""
    return f'{{"file":"{name}","Format":{paths.FORMAT}}}'


def missing_drive() -> str:
    used = {d for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")}
    return next(d for d in reversed(string.ascii_uppercase) if d not in used)


class Folder(unittest.TestCase):
    def setUp(self):
        self.assertFalse(paths.is_test_mode(), "unset DISPLAYDESKTOP_DATA to run these tests")
        self.tmp = tempfile.mkdtemp(prefix="dd-folder-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.default = os.path.join(self.tmp, "LocalAppData", "DisplayDesktop")
        self.other = os.path.join(self.tmp, "Elsewhere", "My Settings")
        for p in (mock.patch.object(DF, "KEY", TEST_KEY),
                  mock.patch.object(DF, "default_folder", return_value=self.default),
                  mock.patch.object(DF, "_in_use", None),  # a fresh run: no folder settled yet
                  mock.patch.object(DF, "unreachable", None),
                  mock.patch.object(DF, "kept_new", False),
                  mock.patch.object(DF, "settings_not_copied", False),
                  mock.patch.object(DF, "left_working", True),
                  mock.patch.object(paths, "on_write_failed", None)):
            p.start()
            self.addCleanup(p.stop)
        paths.forget_failures()
        self.addCleanup(paths.forget_failures)
        self.addCleanup(DF._store, None)
        self.addCleanup(DF._set_value, DF.LEFT_JOURNAL, None)
        for name in DF.MOVED:
            write(self.default, name, own(name))

    def stored(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
                return winreg.QueryValueEx(key, DF.VALUE_NAME)[0]
        except OSError:
            return None

    def test_default_until_a_folder_is_chosen(self):
        self.assertIsNone(DF.chosen_folder())
        self.assertEqual(DF.current(), self.default)
        self.assertTrue(DF.is_default())

    def test_apply_moves_every_file_and_removes_the_empty_old_folder(self):
        seen = []

        def switch(new):  # the app points its files at the new folder here
            seen.append((new, all(os.path.isfile(os.path.join(new, n)) for n in DF.MOVED),
                         all(os.path.isfile(os.path.join(self.default, n)) for n in DF.MOVED)))

        new = DF.move_to(self.other, switch)
        self.assertEqual(seen, [(os.path.normpath(self.other), True, True)],
                         "switched once everything is copied, before the old files go")
        self.assertTrue(DF.same(DF.current(), self.other))
        self.assertEqual(self.stored(), new)
        for name in DF.MOVED:
            self.assertEqual(read(self.other, name), own(name))
        self.assertFalse(os.path.exists(self.default), "the old folder was empty: removed")

    def test_an_old_folder_with_other_files_is_kept(self):
        write(self.default, "my notes.txt", "keep me")
        write(self.default, "display.desktop.layout.json.tmp")  # a left-over temp file of ours goes too
        DF.move_to(self.other)
        self.assertEqual(os.listdir(self.default), ["my notes.txt"], "only DisplayDesktop's files moved")

    def test_default_moves_everything_back_and_forgets_the_choice(self):
        DF.move_to(self.other)
        DF.move_to(self.default)
        self.assertIsNone(self.stored(), "the registry value is removed")
        self.assertTrue(DF.is_default())
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED))
        self.assertFalse(os.path.exists(self.other), "the chosen folder was empty: removed")
        self.assertTrue(os.path.isdir(os.path.dirname(self.other)), "its parent is not touched")

    def test_files_already_in_the_new_folder_are_replaced_others_kept(self):
        write(self.other, "display.desktop.settings.json", '{"old":true}')
        write(self.other, "photo.jpg", "x")
        DF.move_to(self.other)
        self.assertEqual(read(self.other, "display.desktop.settings.json"), own("display.desktop.settings.json"))
        self.assertTrue(os.path.isfile(os.path.join(self.other, "photo.jpg")))

    def test_the_same_folder_changes_nothing(self):
        DF.move_to(self.default + "\\")
        self.assertIsNone(self.stored())
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED))

    def test_bad_folders_change_nothing(self):
        write(self.tmp, "a file")
        for bad in ("relative\\folder", "", os.path.join(self.tmp, "a file"), f"{missing_drive()}:\\DisplayDesktop"):
            with self.assertRaises(OSError, msg=bad):
                DF.move_to(bad)
            self.assertIsNone(self.stored(), bad)
            self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED), bad)

    def test_a_failed_copy_leaves_both_folders_as_they_were(self):
        real = shutil.copy2
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(src, dst)

        with mock.patch.object(shutil, "copy2", flaky):
            with self.assertRaises(OSError):
                DF.move_to(self.other)
        self.assertIsNone(self.stored())
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED))
        self.assertFalse(os.path.exists(self.other), "the copies were removed, and the folder made for the move")

    def test_shown_with_variables_and_typed_variables_work(self):
        local = os.environ["LOCALAPPDATA"]
        self.assertEqual(DF.shown(os.path.join(local, "DisplayDesktop")), r"%LOCALAPPDATA%\DisplayDesktop")
        self.assertEqual(DF.shown(os.path.join(os.environ["APPDATA"], "X")), r"%APPDATA%\X")
        self.assertEqual(DF.shown(local), "%LOCALAPPDATA%")
        self.assertEqual(DF.shown(r"Q:\Data\DisplayDesktop"), r"Q:\Data\DisplayDesktop", "elsewhere: as it is")
        self.assertTrue(DF.same(r"%LOCALAPPDATA%\DisplayDesktop", os.path.join(local, "DisplayDesktop")))
        self.assertIsNone(DF.problem(r"%APPDATA%\DisplayDesktop"))
        self.assertIsNotNone(DF.problem(r"%NO_SUCH_VARIABLE%\DisplayDesktop"))
        with mock.patch.dict(os.environ, {"DD_TEST_DIR": self.tmp}):
            new = DF.move_to(r"%DD_TEST_DIR%\Typed")
        self.assertEqual(new, os.path.join(self.tmp, "Typed"), "stored expanded")
        self.assertEqual(self.stored(), new)
        self.assertEqual(sorted(os.listdir(new)), sorted(DF.MOVED))

    def test_problem_texts(self):
        self.assertIsNone(DF.problem(self.other))
        self.assertIsNotNone(DF.problem("folder"))
        self.assertIsNotNone(DF.problem("  "))

    def test_test_mode_never_uses_or_changes_the_choice(self):
        DF._store(self.other)
        with mock.patch.dict(os.environ, {paths.DATA_ENV: self.tmp}):
            self.assertIsNone(DF.chosen_folder())
            self.assertEqual(DF.current(), self.tmp)
            with self.assertRaises(OSError):
                DF.move_to(os.path.join(self.tmp, "x"))

    def test_a_chosen_folder_that_cannot_be_made_falls_back_to_the_default(self):
        DF._store(f"{missing_drive()}:\\Gone\\DisplayDesktop")  # e.g. a USB drive that is not plugged in
        self.assertEqual(paths.data_dir(), self.default)

    def test_a_test_copy_never_falls_back_to_the_real_folder(self):
        gone = f"{missing_drive()}:\\Test data"
        with mock.patch.dict(os.environ, {paths.DATA_ENV: gone}):
            with self.assertRaises(OSError):
                paths.data_dir()
            self.assertEqual(DF.current(), gone)

    def test_a_run_keeps_one_folder_even_when_the_chosen_one_comes_back(self):
        DF._store(f"{missing_drive()}:\\Gone\\DisplayDesktop")
        self.assertEqual(paths.data_dir(), self.default)  # the drive is missing at start
        DF._store(self.other)                              # (the chosen folder can be reached now)
        os.makedirs(self.other)
        self.assertEqual(paths.data_dir(), self.default, "settings and journal stay with the manager's folder")
        self.assertEqual(DF.current(), self.default, "Settings shows the folder really in use")

    def test_apply_in_a_run_that_fell_back_moves_the_files_really_in_use(self):
        DF._store(f"{missing_drive()}:\\Gone\\DisplayDesktop")
        paths.data_dir()
        self.assertFalse(DF.default_in_use(), "Default can forget the chosen folder")
        DF.move_to(self.other)
        self.assertEqual(sorted(os.listdir(self.other)), sorted(DF.MOVED))
        self.assertTrue(DF.same(paths.data_dir(), self.other))

    def test_default_in_a_run_that_fell_back_forgets_the_chosen_folder(self):
        DF._store(f"{missing_drive()}:\\Gone\\DisplayDesktop")
        paths.data_dir()
        DF.move_to(self.default)
        self.assertIsNone(self.stored())
        self.assertTrue(DF.default_in_use())
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED), "nothing moved or lost")

    def test_displaydesktop_files_already_in_the_new_folder_never_take_effect(self):
        os.remove(os.path.join(self.default, "display.desktop.settings.json"))  # every setting at its default: no file
        write(self.other, "display.desktop.settings.json", '{"UseGpu":false}')   # another computer's, in a synced folder
        write(self.other, "display.desktop.layout.json", '{"theirs":true}')
        DF.move_to(self.other)
        self.assertFalse(os.path.exists(os.path.join(self.other, "display.desktop.settings.json")), "yours: all defaults")
        self.assertEqual(read(self.other, "display.desktop.layout.json"), own("display.desktop.layout.json"))

    def test_a_failed_copy_leaves_files_already_there_untouched(self):
        write(self.other, "display.desktop.layout.json", '{"theirs":true}')
        real = shutil.copy2
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(src, dst)

        with mock.patch.object(shutil, "copy2", flaky):
            with self.assertRaises(OSError):
                DF.move_to(self.other)
        self.assertEqual(os.listdir(self.other), ["display.desktop.layout.json"], "no copies left behind")
        self.assertEqual(read(self.other, "display.desktop.layout.json"), '{"theirs":true}', "not overwritten")
        self.assertTrue(DF.is_default())

    def test_folder_names_with_percent_signs_but_no_root_relative_paths(self):
        self.assertIsNone(DF.problem(os.path.join(self.tmp, "100%Data")))
        self.assertIsNone(DF.problem(os.path.join(self.tmp, "a%b", "c%d")), "% signs in two folder names")
        self.assertIsNotNone(DF.problem(r"%NO_SUCH_VARIABLE%\DisplayDesktop"))
        for relative in ("\\DisplayDesktop", "C:DisplayDesktop", "DisplayDesktop"):
            self.assertIsNotNone(DF.problem(relative), relative)
        DF._store("\\DisplayDesktop")  # stored by an older version
        self.assertIsNone(DF.chosen_folder(), "a path without a drive is never used")

    # ---- fourth review

    def test_old_names_are_renamed_only_in_the_default_folder(self):
        write(self.other, "settings.json", '{"another program":1}')
        DF._store(self.other)
        paths.data_dir()
        self.assertTrue(os.path.exists(os.path.join(self.other, "settings.json")), "another program's file stays")
        self.assertFalse(os.path.exists(os.path.join(self.other, "display.desktop.settings.json")))

    def test_a_failure_before_anyone_listens_is_reported_at_the_next_one(self):
        reported = []
        target = os.path.join(self.default, PLACES_FILE)
        with mock.patch.object(paths.os, "replace", side_effect=PermissionError("read-only")), \
                mock.patch.object(paths, "PAUSE_REFUSED_S", 0):
            self.assertFalse(paths.write_json(target, {}))  # the first sync, before the tray listens
            paths.on_write_failed = reported.append
            self.assertFalse(paths.write_json(target, {}))
        self.assertEqual(reported, [self.default], "told at the first failure someone can hear")

    def test_a_folder_that_refuses_writes_does_not_slow_every_switch(self):
        reported, tries = [], []
        paths.on_write_failed = reported.append
        target = os.path.join(self.default, PLACES_FILE)
        with mock.patch.object(paths.os, "replace", side_effect=lambda *a: tries.append(1) or
                               (_ for _ in ()).throw(PermissionError(13, "denied"))), \
                mock.patch.object(paths.time, "sleep", side_effect=AssertionError("no waiting on the UI thread")):
            for _ in range(20):  # syncs and switches
                self.assertFalse(paths.write_json(target, {}))
        self.assertEqual(len(tries), 1, "tried once, then paused (no write, no wait)")
        self.assertEqual(reported, [self.default])
        with mock.patch.object(paths.time, "monotonic", return_value=paths.time.monotonic() + paths.PAUSE_REFUSED_S + 1):
            self.assertTrue(paths.write_json(target, {"ok": 1}), "tried again by itself after the pause")
        self.assertFalse(paths.failed(self.default))

    def test_a_folder_gone_for_a_moment_is_written_again_later(self):
        gone_since = paths.time.monotonic()
        target = os.path.join(self.default, JOURNAL_FILE)
        with mock.patch.object(paths.os, "replace", side_effect=FileNotFoundError("offline")), \
                mock.patch.object(paths.os.path, "isdir", return_value=False), \
                mock.patch.object(paths.os, "makedirs", side_effect=OSError("offline")):
            self.assertFalse(paths.write_json(target, {}))  # e.g. a network drive not back after resume
        self.assertTrue(paths.failed(self.default))
        with mock.patch.object(paths.time, "monotonic", return_value=gone_since + paths.PAUSE_GONE_S + 1):
            self.assertTrue(paths.write_json(target, {"Windows": []}), "back: the journal is written again")

    def test_a_file_held_for_a_moment_is_written_after_a_short_wait(self):
        reported = []
        paths.on_write_failed = reported.append
        target = os.path.join(self.default, PLACES_FILE)
        real = os.replace
        tries = []

        def held_once(src, dst):
            tries.append(dst)
            if len(tries) == 1:
                raise PermissionError(13, "in use by a sync client", None, 32)  # (a sharing violation)
            return real(src, dst)

        with mock.patch.object(paths.os, "replace", held_once), mock.patch.object(paths.time, "sleep") as wait:
            self.assertTrue(paths.write_json(target, {"ok": 1}))
        wait.assert_called_once_with(paths.RETRY_DELAYS_S[0])  # (the short wait, not slept here)
        self.assertEqual(reported, [], "no error for a lock that passes")
        self.assertEqual(read(self.default, PLACES_FILE), '{"ok":1,"Format":%d}' % paths.FORMAT)

    def test_a_folder_with_files_that_cant_be_read_is_never_switched_to(self):
        from src import settings_window as SW
        write(self.other, LAYOUT_FILE, '{"Format":%d}' % (paths.FORMAT + 1))
        write(self.other, PLACES_FILE, "{damaged")
        self.assertEqual(sorted(os.path.basename(p) for p in DF.unreadable_in(self.other)),
                         sorted([LAYOUT_FILE, PLACES_FILE]))
        with mock.patch("src.restore_window.show_bad_folder") as shown, \
                mock.patch.object(SW.G, "message_box") as box, mock.patch.object(SW.console, "log"):
            self.assertFalse(SW.move_folder(self.other, owner=5))
        shown.assert_called_once()
        self.assertEqual(shown.call_args[0][:2], (DF.expand(self.other), 5), "listed, over the dialog it came from")
        box.assert_not_called()  # (never asked which data to keep: nothing moves)
        self.assertTrue(DF.same(DF.current(), self.default))
        self.assertEqual(read(self.other, PLACES_FILE), "{damaged", "nothing repaired")

    def test_a_folder_whose_only_problem_is_its_crash_journal_is_used(self):
        # (a damaged journal is told and removed when read, never a reason to refuse the folder)
        write(self.other, JOURNAL_FILE, "{damaged")
        self.assertEqual(DF.unreadable_in(self.other), [])

    def test_a_move_with_no_manager_yet_keeps_the_old_journal_for_the_next_start(self):
        # (Recovery at start: nothing has read a crashed run's journal yet)
        write(self.default, JOURNAL_FILE, '{"Windows":[]}')
        DF.move_to(self.other)  # (no switch)
        old = os.path.join(self.default, JOURNAL_FILE)
        self.assertTrue(os.path.exists(old), "never deleted unread")
        self.assertIn(old, DF.journals(self.other), "the next start reads it there")

    def test_a_move_keeping_the_new_folder_s_data_leaves_the_old_junk_too(self):
        write(self.default, LAYOUT_FILE, '{"Format":%d}' % (paths.FORMAT + 1))
        write(self.other, SETTINGS_FILE, "{}")
        DF.move_to(self.other, keep_new=True)
        self.assertTrue(os.path.exists(os.path.join(self.default, LAYOUT_FILE)), "a newer version's: left as it is")

    def test_a_move_never_copies_nor_deletes_the_old_folder_s_junk(self):
        write(self.default, LAYOUT_FILE, '{"Format":%d}' % (paths.FORMAT + 1))  # (a newer version's)
        DF.move_to(self.other)
        self.assertFalse(os.path.exists(os.path.join(self.other, LAYOUT_FILE)), "never copied")
        self.assertTrue(os.path.exists(os.path.join(self.default, LAYOUT_FILE)), "nor deleted: left as it is")
        self.assertEqual(read(self.other, SETTINGS_FILE), own(SETTINGS_FILE), "the others moved")

    def test_a_failed_move_removes_every_folder_it_made(self):
        deep = os.path.join(self.tmp, "a", "b", "c")
        with mock.patch.object(shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                DF.move_to(deep)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a")), "a, a\\b and a\\b\\c are gone")

    # ---- which data a move keeps (Settings asks; by request)

    def choose(self, target, answer):
        from src import settings_window as SW
        with mock.patch.object(SW.G, "message_box", return_value=answer) as box:
            keep = SW.SettingsWindow.choose_data(target)
        return keep, box.call_args[0][0], box.call_args[0][1]

    def test_moving_to_an_empty_folder_moves_the_current_data(self):
        keep, text, flags = self.choose(self.other, 6)
        self.assertIs(keep, False)
        self.assertIn("Move saved settings?", text)
        self.assertEqual(self.choose(self.other, 7)[0], None, "No: nothing happens")

    def test_a_folder_with_data_when_the_current_one_has_none_uses_its_data(self):
        for name in DF.MOVED:
            os.remove(os.path.join(self.default, name))
        write(self.other, "display.desktop.settings.json", '{"UseGpu":false}')
        keep, text, _ = self.choose(self.other, 6)
        self.assertIs(keep, True)
        self.assertIn("Use the settings in that folder?", text)

    def test_both_folders_with_data_asks_which_to_keep(self):
        write(self.other, "display.desktop.layout.json", '{"Setups":[],"Format":%d}' % paths.FORMAT)
        from src import settings_window as SW
        keep, text, flags = self.choose(self.other, 6)
        self.assertIs(keep, True, "Yes: that folder's")
        self.assertIn("Both folders have settings", text)
        self.assertEqual(flags & 0xF, SW.MB_YESNOCANCEL)
        self.assertIs(self.choose(self.other, 7)[0], False, "No: the current ones")
        self.assertIsNone(self.choose(self.other, 2)[0], "Cancel")

    def test_files_that_are_not_valid_data_count_as_none(self):
        write(self.other, "display.desktop.settings.json", "{not json")
        write(self.other, JOURNAL_FILE, '{"Windows":[]}')  # a journal is not data
        self.assertFalse(DF.has_data(self.other))
        self.assertIs(self.choose(self.other, 6)[0], False)

    def test_choosing_the_folder_a_run_could_not_reach_again_uses_its_data(self):
        gone = os.path.join(self.tmp, "Late drive")
        DF._store(gone)
        with mock.patch.object(DF, "_full_path", return_value=True), \
                mock.patch.object(DF.os, "makedirs", side_effect=[OSError("not yet"), None]):
            paths.data_dir()  # the run falls back to the default folder
        self.assertTrue(DF.same(DF.unreachable, gone))
        saved = '{"Setups":[{"Displays":["MON-A"],"Windows":[{"Exe":"x"}]}],"Format":%d}' % paths.FORMAT
        write(gone, "display.desktop.layout.json", saved)  # the user's Save
        keep, text, _ = self.choose(gone, 6)
        self.assertIs(keep, True, "no question of replacing it: its data is the real one")
        DF.move_to(gone, keep_new=True)
        self.assertEqual(read(gone, "display.desktop.layout.json"), saved)
        self.assertTrue(DF.same(DF.current(), gone))

    def test_keeping_the_new_folders_data_copies_nothing_and_drops_the_old(self):
        write(self.other, "display.desktop.settings.json", '{"UseGpu":false}')
        switched = []
        DF.move_to(self.other, switched.append, keep_new=True)
        self.assertEqual(read(self.other, "display.desktop.settings.json"), '{"UseGpu":false}')
        self.assertEqual(os.listdir(self.other), ["display.desktop.settings.json"], "nothing copied in")
        self.assertFalse(os.path.exists(self.default), "the other data was dropped (the user chose)")
        self.assertEqual(switched, [os.path.normpath(self.other)])

    def test_moving_away_from_a_failed_folder_keeps_the_settings_known(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        write(self.default, "display.desktop.settings.json", '{"StartMenu":true,"UseGpu":false}')
        paths.data_dir()
        self.assertFalse(settings.get("UseGpu"))
        paths._failed[os.path.normcase(os.path.normpath(self.default))] = float("inf")  # offline now
        self.assertFalse(settings.get("UseGpu"), "while offline: the values known (no disk read)")
        DF.move_to(self.other)
        settings.write_if_missing()  # (the tray does this after the move)
        self.assertEqual(read(self.other, "display.desktop.settings.json"), '{"StartMenu":true,"UseGpu":false,"Format":%d}' % paths.FORMAT)

    # ---- third review

    def test_files_with_the_older_names_get_the_new_ones(self):
        for name in DF.MOVED:
            os.remove(os.path.join(self.default, name))
        write(self.default, "settings.json", '{"old":1}')
        write(self.default, "layout.json", '{"old":1}')
        write(self.default, "display.desktop.layout.json", '{"new":1}')  # a newer file is never replaced
        paths.data_dir()
        self.assertEqual(read(self.default, "display.desktop.settings.json"), '{"old":1}')
        self.assertEqual(read(self.default, "display.desktop.layout.json"), '{"new":1}')
        self.assertTrue(os.path.exists(os.path.join(self.default, "layout.json")), "the older one is left alone")
        self.assertTrue(all(n.startswith("display.desktop.") for n in DF.MOVED))

    def test_another_programs_settings_json_is_never_touched(self):
        write(self.other, "settings.json", '{"another program":1}')
        write(self.other, "layout.json", '{"another program":1}')
        DF.move_to(self.other)
        self.assertEqual(read(self.other, "settings.json"), '{"another program":1}')
        self.assertEqual(read(self.other, "layout.json"), '{"another program":1}')
        DF.move_to(self.default)
        self.assertEqual(sorted(os.listdir(self.other)), ["layout.json", "settings.json"], "and stays when we leave")

    def test_the_same_folder_reached_another_way_deletes_nothing(self):
        import subprocess
        link = os.path.join(self.tmp, "Link")
        subprocess.run(["cmd", "/c", "mklink", "/J", link, self.default], check=True, capture_output=True)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))  # (removes the junction only)
        DF.move_to(link)
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED), "nothing deleted")
        self.assertTrue(os.path.isdir(self.default))
        DF.move_to(self.default + ".")  # a trailing dot names the same folder too
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED))

    def test_a_chosen_folder_out_of_reach_at_start_is_remembered_for_the_message(self):
        gone = f"{missing_drive()}:\\Gone\\DisplayDesktop"
        DF._store(gone)
        paths.data_dir()
        self.assertEqual(DF.unreachable, gone)

    def test_a_folder_that_goes_away_is_reported_once_and_not_tried_again(self):
        reported = []
        paths.on_write_failed = reported.append
        gone = f"{missing_drive()}:\\Gone"
        target = os.path.join(gone, JOURNAL_FILE)
        calls = []
        real_open = open
        with mock.patch("builtins.open", side_effect=lambda *a, **k: calls.append(a[0]) or real_open(*a, **k)):
            for _ in range(5):  # five switches
                self.assertFalse(paths.write_json(target, {}))
        self.assertEqual(reported, [gone], "reported once")
        self.assertEqual(len(calls), 1, "tried once (it can't be made again), then never: no freeze on each switch")
        self.assertTrue(paths.failed(gone))
        paths.forget_failures()
        self.assertFalse(paths.failed(gone))

    def test_a_file_held_too_long_fails_only_that_write(self):
        reported = []
        paths.on_write_failed = reported.append
        target = os.path.join(self.default, PLACES_FILE)
        journal = os.path.join(self.default, JOURNAL_FILE)
        held = PermissionError(13, "in use", None, 32)  # a sharing violation (a sync client)
        with mock.patch.object(paths.os, "replace", side_effect=held), mock.patch.object(paths, "RETRY_DELAYS_S", ()):
            self.assertFalse(paths.write_json(target, {}))
        self.assertEqual(reported, [], "not an error to tell the user about")
        self.assertFalse(paths.paused(self.default), "the folder is not paused")
        self.assertTrue(paths.write_json(journal, {"Windows": []}), "the crash journal is still written")
        self.assertTrue(paths.write_json(target, {"ok": 1}), "and the file itself once free")

    def test_moving_away_from_a_folder_that_failed_neither_reads_nor_cleans_it(self):
        paths.data_dir()
        paths._failed[os.path.normcase(os.path.normpath(self.default))] = float("inf")  # it went offline
        switched = []
        real_isfile = os.path.isfile

        def isfile(p):
            if os.path.normcase(p).startswith(os.path.normcase(self.default)):
                raise AssertionError(f"read the failed folder: {p}")
            return real_isfile(p)

        with mock.patch.object(DF.os.path, "isfile", isfile):
            DF.move_to(self.other, switched.append)
        self.assertEqual(switched, [os.path.normpath(self.other)], "the app writes everything it knows there")
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED), "the failed folder is left alone")
        self.assertFalse(paths.failed(self.default), "writes are tried again")

    def test_choosing_a_failed_folder_again_writes_everything_there_again(self):
        paths.data_dir()
        paths._failed[os.path.normcase(os.path.normpath(self.default))] = float("inf")
        switched = []
        DF.move_to(self.default, switched.append)
        self.assertEqual(switched, [self.default])
        self.assertFalse(paths.failed(self.default))
        self.assertEqual(sorted(os.listdir(self.default)), sorted(DF.MOVED), "its files are not taken as foreign")

    def test_default_keeps_other_values_in_the_registry_key(self):
        DF.move_to(self.other)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Other", 0, winreg.REG_SZ, "keep me")
        DF.move_to(self.default)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            self.assertEqual(winreg.QueryValueEx(key, "Other")[0], "keep me")
        self.assertIsNone(self.stored())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "Other")
        DF._store(None)
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY)  # empty: removed

    def test_a_failure_while_putting_files_in_place_puts_the_new_folder_back(self):
        write(self.other, "display.desktop.layout.json", '{"theirs":true}')
        write(self.other, "display.desktop.settings.json", '{"theirs":true}')
        real = os.replace
        seen = []

        def locked(src, dst):
            seen.append(dst)
            if dst.endswith(PLACES_FILE):  # a sync client holds it
                raise PermissionError("in use")
            return real(src, dst)

        with mock.patch.object(DF.os, "replace", locked):
            with self.assertRaises(OSError):
                DF.move_to(self.other)
        self.assertEqual(sorted(os.listdir(self.other)), ["display.desktop.layout.json", "display.desktop.settings.json"])
        self.assertEqual(read(self.other, "display.desktop.layout.json"), '{"theirs":true}', "put back")
        self.assertEqual(read(self.other, "display.desktop.settings.json"), '{"theirs":true}')
        self.assertTrue(DF.is_default())

    def test_the_old_journal_stays_when_the_new_one_could_not_be_written(self):
        write(self.default, JOURNAL_FILE, '{"Windows":[1]}')
        DF.move_to(self.other, lambda new: False)  # (the app could not write its journal there)
        self.assertEqual(read(self.default, JOURNAL_FILE), '{"Windows":[1]}', "the only record is kept")
        self.assertFalse(os.path.exists(os.path.join(self.default, "display.desktop.settings.json")))

    def test_an_error_in_the_switch_does_not_undo_the_move(self):
        write(self.default, JOURNAL_FILE, '{"Windows":[1]}')

        def broken(new):
            raise RuntimeError("unexpected")
        with mock.patch("traceback.print_exc"):
            new = DF.move_to(self.other, broken)
        self.assertTrue(DF.same(DF.current(), new), "the move stands (the registry already says so)")
        self.assertTrue(os.path.exists(os.path.join(self.default, JOURNAL_FILE)), "and the old journal is kept")

    def test_a_folder_that_just_went_offline_counts_as_unreadable(self):
        paths.data_dir()
        real_isdir = os.path.isdir
        with mock.patch.object(DF.os.path, "isdir", side_effect=lambda p: False if DF.same(p, self.default) else real_isdir(p)):
            DF.move_to(self.other)  # (no write failed there yet: it is not marked failed)
        self.assertTrue(DF.settings_not_copied, "the settings known are written by the tray")

    def test_a_setting_changed_during_a_pause_is_written_later(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        paths.note_failure(self.default, gone=True)
        settings.set("UseGpu", False)
        self.assertNotIn('"UseGpu":false', read(self.default, "display.desktop.settings.json"))
        self.assertFalse(settings.get("UseGpu"), "in use meanwhile")
        paths.forget_failures()
        settings.flush()  # (the tray's sync timer)
        self.assertIn('"UseGpu":false', read(self.default, "display.desktop.settings.json"))

    def test_access_denied_while_replacing_is_a_busy_file_not_a_folder_problem(self):
        reported = []
        paths.on_write_failed = reported.append
        target = os.path.join(self.default, PLACES_FILE)
        denied = PermissionError(13, "access denied", None, 5)
        with mock.patch.object(paths.os, "replace", side_effect=denied), mock.patch.object(paths, "RETRY_DELAYS_S", ()):
            self.assertFalse(paths.write_json(target, {}))
        self.assertEqual(reported, [])
        self.assertFalse(paths.paused(self.default), "another program had it open: nothing paused")
        real_open = open
        with mock.patch("builtins.open", side_effect=lambda p, *a, **k: (_ for _ in ()).throw(denied)
                        if str(p).endswith(".tmp") else real_open(p, *a, **k)):
            self.assertFalse(paths.write_json(target, {}))
        self.assertTrue(paths.paused(self.default), "the folder itself refuses new files: paused")
        self.assertEqual(reported, [self.default])

    def test_a_file_that_can_never_be_replaced_is_reported_after_a_few_writes(self):
        reported = []
        paths.on_write_failed = reported.append
        target = os.path.join(self.default, PLACES_FILE)
        denied = PermissionError(13, "access denied", None, 5)
        clock = [1000.0]
        with mock.patch.object(paths.os, "replace", side_effect=denied), \
                mock.patch.object(paths.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(paths.time, "sleep") as sleep:
            for _ in range(paths.HELD_LIMIT * 2):  # quick writes (switches): held for a moment, not refused
                self.assertFalse(paths.write_json(target, {}))
                self.assertFalse(paths.paused(self.default))
                clock[0] += 1
            clock[0] += paths.HELD_REFUSED_S
            self.assertFalse(paths.write_json(target, {}))
            sleep.assert_not_called()  # (access denied: no waiting on the UI thread)
            self.assertFalse(paths.paused(self.default), "only that file can't be replaced: the folder works")
            self.assertFalse(paths.write_json(target, {}), "that file is skipped for a while")
            journal = os.path.join(self.default, JOURNAL_FILE)
        self.assertTrue(paths.write_json(journal, {}), "the journal is never skipped with it")
        self.assertEqual(reported, [self.default], "and the user is told")

    def test_a_share_that_does_not_answer_never_holds_up_the_caller(self):
        import time as _time
        real = os.path.isdir
        slow = r"\\nas-off\share\DD"
        with mock.patch.object(paths, "PROBE_WAIT_S", 0.01), mock.patch.object(paths.os.path, "isdir",
                               side_effect=lambda p: _time.sleep(3) or False if p.startswith(r"\\nas-off") else real(p)):
            t0 = _time.monotonic()
            self.assertTrue(paths.offline(slow))
            self.assertLess(_time.monotonic() - t0, 2 * paths.PROBE_WAIT_S + 0.5, "not the network timeout")
            t0 = _time.monotonic()
            self.assertTrue(paths.offline(slow))
            self.assertLess(_time.monotonic() - t0, 0.05, "remembered: not probed again")

    def test_a_refused_file_does_not_silence_a_later_folder_failure(self):
        reported = []
        paths.on_write_failed = reported.append
        places = os.path.join(self.default, PLACES_FILE)
        paths.report(self.default, file=places)  # (one file refused: told)
        paths.fail(self.default, gone=False)  # the folder itself fails later
        self.assertEqual(reported, [self.default, self.default], "told again: hidden windows are off record")

    def test_a_write_to_a_share_known_offline_never_touches_it(self):
        share = r"\\nas-down\share\DD"
        paths._offline_roots[os.path.normcase(os.path.splitdrive(share)[0])] = paths.time.monotonic() + 60
        with mock.patch("builtins.open", side_effect=AssertionError("no disk access")):
            self.assertFalse(paths.write_json(os.path.join(share, PLACES_FILE), {}))
        self.assertTrue(paths.paused_as_gone(share))

    def test_a_folder_known_offline_counts_as_having_data_without_any_disk_access(self):
        share = r"\\nas-down\share\DD"
        paths._offline_roots[os.path.normcase(os.path.splitdrive(share)[0])] = paths.time.monotonic() + 60
        with mock.patch("builtins.open", side_effect=AssertionError("no disk access")):
            self.assertTrue(DF.has_data(share), "a move there asks: never replaces it unasked")

    def test_a_share_slow_to_answer_once_is_not_reported_and_its_late_answer_resumes_writes(self):
        import time as _time
        reported = []
        paths.on_write_failed = reported.append
        real = os.path.isdir
        share = r"\\nas-waking\share\DD"
        with mock.patch.object(paths, "PROBE_WAIT_S", 0.02), \
                mock.patch.object(paths.os.path, "isdir", side_effect=lambda p: _time.sleep(paths.PROBE_WAIT_S + 0.1)
                                  or True if p.startswith(r"\\nas-waking") else real(p)):
            self.assertTrue(paths.offline(share), "no answer in time")
            self.assertFalse(paths.write_json(os.path.join(share, PLACES_FILE), {}))
            self.assertEqual(reported, [], "only slow once (waking up): not told")
            self.assertTrue(paths.paused_as_gone(share))
            end = _time.monotonic() + 5  # (the late "it is there")
            while (paths.known_offline(share) or paths.paused_as_gone(share)) and _time.monotonic() < end:
                _time.sleep(0.01)
        self.assertFalse(paths.known_offline(share))
        self.assertFalse(paths.paused_as_gone(share), "its late answer takes the pause back")

    def test_a_late_answer_keeps_a_real_failures_pause_and_its_told_mark(self):
        reported = []
        paths.on_write_failed = reported.append
        share = r"\\nas-real\share\DD"
        paths.fail(share, gone=True)  # (a write was tried there and failed for real)
        paths._found_again(paths._root(share))
        paths.fail(share, gone=True)
        self.assertTrue(paths.paused_as_gone(share))
        self.assertEqual(reported, [share], "told once, not again after every late answer")

    def test_a_real_failure_paused_again_for_its_offline_share_keeps_its_told_mark(self):
        reported = []
        paths.on_write_failed = reported.append
        share = r"\\nas-real2\share\DD"
        paths.fail(share, gone=True)  # (a write tried there failed: told)
        paths._failed[paths._key(share)] = 0.0  # (its pause over)
        paths._offline_roots[paths._root(share)] = paths.time.monotonic() + 60  # (its share known offline)
        self.assertFalse(paths.write_json(os.path.join(share, PLACES_FILE), {}))
        paths._found_again(paths._root(share))
        self.assertFalse(paths.paused(share), "the share answers: tried again at the next write")
        paths.fail(share, gone=True)
        self.assertEqual(reported, [share], "a real failure: told once")

    def test_a_late_answer_keeps_the_told_mark_of_a_folder_that_failed_for_real_meanwhile(self):
        reported = []
        paths.on_write_failed = reported.append
        share = r"\\nas-race\share\DD"
        key = paths._key(share)

        class Snapshot(set):  # (listed as offline-only, then a real failure on the UI thread)
            def __iter__(self):
                return iter([key])

            def __contains__(self, k):
                return False
        paths.fail(share, gone=True)
        with mock.patch.object(paths, "_offline_paused", Snapshot()):
            paths._found_again(paths._root(share))
        paths.fail(share, gone=True)
        self.assertEqual(reported, [share], "never told twice")

    def test_after_a_late_answer_and_a_write_that_works_a_new_failure_is_told_again(self):
        reported = []
        paths.on_write_failed = reported.append
        share = r"\\nas-back\share\DD"
        paths.fail(share, gone=True)
        paths._found_again(paths._root(share))
        self.assertFalse(paths.failed(share), "usable again (has_data, a move's old folder)")
        with mock.patch.object(paths.os, "replace"), mock.patch("builtins.open", mock.mock_open()):
            self.assertTrue(paths.write_json(os.path.join(share, PLACES_FILE), {}))
        paths.fail(share, gone=True)
        self.assertEqual(reported, [share, share], "worked in between: told again")

    def test_a_late_answer_never_ends_a_refusal_pause(self):
        share = r"\\nas-refuses\share\DD"
        paths.fail(share, gone=True)
        paths._pause(share, gone=False)  # (then a refused write, on the UI thread)
        paths._found_again(paths._root(share))
        self.assertTrue(paths.paused(share), "a refusal keeps its pause")

    def test_a_folder_whose_write_failed_while_its_share_dropped_works_once_it_answers(self):
        share = r"\\nas-blip\share\DD"
        paths.fail(share, gone=True)  # (a network error mid-write)
        paths._found_again(paths._root(share))
        self.assertFalse(paths.paused(share), "not kept waiting PAUSE_GONE_S")

    def test_a_folder_gone_between_two_steps_of_a_read_holds_nothing_saved(self):
        gone = os.path.join(self.tmp, "vanished", PLACES_FILE)
        self.assertFalse(paths.unreadable(gone, FileNotFoundError(2, "gone")), "the drive answers: nothing saved")
        self.assertTrue(paths.unreadable(os.path.join(self.default, PLACES_FILE), PermissionError(13, "held")))

    def test_a_pause_dropped_by_a_probe_thread_meanwhile_is_no_error(self):
        class Racing(dict):  # (the key is popped between "in" and the lookup)
            def __contains__(self, k):
                return True

            def __getitem__(self, k):
                raise KeyError(k)
        with mock.patch.object(paths, "_failed", Racing()):
            self.assertFalse(paths.paused(r"\\nas\share\DD"))

    def test_two_slow_answers_far_apart_are_not_taken_as_offline_for_sure(self):
        import time as _time
        real = os.path.isdir
        share = r"\\nas-sleepy\share\DD"
        clock = [_time.monotonic()]
        # (the answers come long after this test: none is seen in between)
        with mock.patch.object(paths, "PROBE_WAIT_S", 0.01), mock.patch.object(paths.os.path, "isdir", side_effect=lambda p: _time.sleep(30)
                               or False if p.startswith(r"\\nas-sleepy") else real(p)), \
                mock.patch.object(paths.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(paths.offline(share))
            clock[0] += 2 * paths.OFFLINE_ROW_S  # (hours later, nothing written meanwhile)
            self.assertTrue(paths.offline(share))
            self.assertFalse(paths.offline_for_sure(share), "slow once each time: not sure")

    def test_a_dead_share_probed_by_the_regular_writes_is_taken_as_offline_for_sure(self):
        import time as _time
        real = os.path.isdir
        share = r"\\nas-dead\share\DD"
        clock = [_time.monotonic()]
        with mock.patch.object(paths, "PROBE_WAIT_S", 0.01), mock.patch.object(paths.os.path, "isdir", side_effect=lambda p: _time.sleep(30)
                               or False if p.startswith(r"\\nas-dead") else real(p)), \
                mock.patch.object(paths.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(paths.offline(share))
            clock[0] += 3 * paths.PAUSE_GONE_S  # (the user idle: the next write minutes later)
            self.assertTrue(paths.offline(share))
            self.assertTrue(paths.offline_for_sure(share), "no answer twice, nothing in between: told")

    def test_a_share_that_answers_not_there_is_reported(self):
        reported = []
        paths.on_write_failed = reported.append
        real = os.path.isdir
        share = r"\\nas-gone\share\DD"
        with mock.patch.object(paths.os.path, "isdir",
                               side_effect=lambda p: False if p.startswith(r"\\nas-gone") else real(p)):
            self.assertTrue(paths.offline(share))
            self.assertFalse(paths.write_json(os.path.join(share, PLACES_FILE), {}))
        self.assertEqual(reported, [share], "sure: told")

    def test_a_folder_that_failed_and_still_does_not_answer_counts_as_data_without_reading_it(self):
        share = r"\\nas-down\share\DD"
        paths.note_failure(share, gone=False)
        with mock.patch.object(paths, "offline", return_value=True), \
                mock.patch("builtins.open", side_effect=AssertionError("no disk access")):
            self.assertTrue(DF.has_data(share), "a move there asks, never blocks on it")

    def test_a_run_that_ended_cleanly_is_remembered_for_this_boot(self):
        self.addCleanup(DF._set_value, DF.ENDED_RUNS, None)
        self.assertIsNone(DF.ended_run("123-abc"))
        DF.add_ended_run("123-abc")
        self.assertEqual(DF.ended_run("123-abc"), set(), "ended, nothing left hidden")
        DF.add_ended_run("123-abc", [65538, 131074])
        self.assertEqual(DF.ended_run("123-abc"), {65538, 131074}, "recorded again: replaced")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, DF.ENDED_RUNS, 0, winreg.REG_MULTI_SZ, ["123-abc|another-boot"])
        self.assertIsNone(DF.ended_run("123-abc"), "another boot's: means nothing now")
        for i in range(DF.ENDED_KEEP + 5):
            DF.add_ended_run(f"w{i}")
        self.assertIsNone(DF.ended_run("w0"), "only the most recent are kept")
        self.assertEqual(DF.ended_run(f"w{DF.ENDED_KEEP + 4}"), set())

    def test_a_local_disk_is_never_offline(self):
        with mock.patch.object(paths.os.path, "isdir", side_effect=lambda p: p == "C:\\"):
            self.assertFalse(paths.offline(r"C:\Gone\Folder"), "a fixed disk: a missing folder, not offline")

    def test_a_fixed_disk_that_is_not_ready_is_offline(self):
        # (locked, or not ready: no root folder; nothing there is taken as "nothing saved")
        with mock.patch.object(paths.os.path, "isdir", side_effect=lambda p: False):
            self.assertTrue(paths.offline(r"C:\Locked\Folder"))

    def test_a_dollar_sign_in_a_folder_name_stays(self):
        self.assertEqual(DF.expand(r"D:\$TEMP files\DD"), r"D:\$TEMP files\DD")
        with mock.patch.dict(os.environ, {"DDTESTVAR": r"C:\X"}):
            self.assertEqual(DF.expand(r"%DDTESTVAR%\DD"), r"C:\X\DD")

    def test_the_crash_journal_is_never_skipped_for_a_refusal(self):
        journal = os.path.join(self.default, JOURNAL_FILE)
        denied = PermissionError(13, "access denied", None, 5)
        clock = [1000.0]
        with mock.patch.object(paths.os, "replace", side_effect=denied), \
                mock.patch.object(paths.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in range(paths.HELD_LIMIT + 1):
                paths.write_json(journal, {})
                clock[0] += paths.HELD_REFUSED_S
        self.assertTrue(paths.write_json(journal, {}), "free again: written at once")

    def test_a_held_file_is_not_rewritten_by_every_sync(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        tries = []
        locked = PermissionError(13, "in use", None, 32)
        with mock.patch.object(paths.os, "replace", side_effect=lambda *a: tries.append(1) or (_ for _ in ()).throw(locked)), \
                mock.patch.object(paths.time, "sleep"):
            settings.set("StartMenu", True)
            n = len(tries)
            for _ in range(5):
                settings.flush()  # (sync ticks)
        self.assertEqual(len(tries), n, "tried again only after READ_RETRY_S")

    def test_a_folder_that_failed_elsewhere_still_shows_its_data(self):
        write(self.other, "display.desktop.layout.json", '{"Setups":[],"Format":%d}' % paths.FORMAT)
        paths.data_dir()
        paths.note_failure(self.other, gone=True)  # (e.g. looked for while it was offline)
        self.assertTrue(DF.has_data(self.other), "a move there must ask: its own data is there")

    def test_the_unreachable_chosen_folders_journal_is_read_once_it_is_back(self):
        gone = f"{missing_drive()}:\\Gone\\DisplayDesktop"
        DF._store(gone)
        paths.data_dir()
        self.assertTrue(DF.unreachable)
        found = DF.journals(DF.current())
        self.assertIn(os.path.join(gone, JOURNAL_FILE), found, "read (later, while offline)")
        self.assertIn(os.path.join(gone, JOURNAL_FILE), DF._left_journals(), "and by later starts")

    def test_left_journals_of_an_earlier_boot_are_dropped(self):
        here = os.path.join(self.other, JOURNAL_FILE)
        DF.add_left_journal(here)
        self.assertEqual(DF._left_journals(), [here], "this boot's: kept")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, DF.LEFT_JOURNAL, 0, winreg.REG_MULTI_SZ,
                              [f"{here}|{DF._uptime_ms() + 86_400_000}", "E:\\Old\\x.json"])
        self.assertEqual(DF._left_journals(), [], "an earlier boot's (or none recorded): can't match any window")
        DF.add_left_journal(here)
        with mock.patch("time.time", return_value=0.0):  # (a clock set meanwhile changes nothing)
            self.assertEqual(DF._left_journals(), [here])

    def test_a_held_temp_file_pauses_nothing(self):
        target = os.path.join(self.default, PLACES_FILE)
        os.makedirs(self.default, exist_ok=True)
        with open(target + ".tmp", "w") as fh:  # (left "delete pending" by a program that has it open)
            fh.write("x")
        real_open = open
        with mock.patch("builtins.open", side_effect=lambda p, *a, **k: (_ for _ in ()).throw(
                PermissionError(13, "Permission denied")) if str(p).endswith(".tmp") else real_open(p, *a, **k)), \
                mock.patch.object(paths.os, "remove"):
            self.assertFalse(paths.write_json(target, {}))
        self.assertFalse(paths.paused(self.default), "held for a moment: no pause, no report")

    def test_a_sharing_violation_in_between_restarts_the_count(self):
        target = os.path.join(self.default, PLACES_FILE)
        denied, locked = PermissionError(13, "denied", None, 5), PermissionError(13, "in use", None, 32)
        clock = [1000.0]
        with mock.patch.object(paths.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(paths.time, "sleep"):
            for err in (denied, denied, locked, denied, denied):
                with mock.patch.object(paths.os, "replace", side_effect=err):
                    paths.write_json(target, {})
                clock[0] += paths.HELD_REFUSED_S
            self.assertFalse(paths.paused(self.default))

    def test_a_file_locked_for_a_moment_never_pauses_the_folder_however_often(self):
        reported = []
        paths.on_write_failed = reported.append
        target = os.path.join(self.default, JOURNAL_FILE)
        locked = PermissionError(13, "in use", None, 32)
        with mock.patch.object(paths.os, "replace", side_effect=locked), mock.patch.object(paths.time, "sleep"):
            for _ in range(paths.HELD_LIMIT * 2):
                self.assertFalse(paths.write_json(target, {}))
        self.assertFalse(paths.paused(self.default))
        self.assertEqual(reported, [])

    def test_a_folder_gone_during_a_retry_is_paused(self):
        target = os.path.join(self.default, PLACES_FILE)
        locked = PermissionError(13, "in use", None, 32)
        real_isdir = os.path.isdir
        state = {"n": 0}

        def replace(*_a):
            state["n"] += 1
            raise locked
        with mock.patch.object(paths.os, "replace", side_effect=replace), \
                mock.patch.object(paths.time, "sleep"), \
                mock.patch.object(paths.os.path, "isdir", side_effect=lambda p: real_isdir(p) and state["n"] < 2), \
                mock.patch.object(paths.os, "makedirs", side_effect=OSError(21, "not ready")):
            self.assertFalse(paths.write_json(target, {}))
        self.assertTrue(paths.paused(self.default), "gone: paused, the next write doesn't block on it")

    def test_a_pending_change_wins_once_the_file_is_readable(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        write(self.default, "display.desktop.settings.json", '{"UseGpu":true}')
        real_open = open
        busy = PermissionError(13, "in use", None, 32)
        with mock.patch("builtins.open", side_effect=lambda p, *a, **k: (_ for _ in ()).throw(busy)
                        if str(p).endswith("settings.json") else real_open(p, *a, **k)):
            self.assertFalse(settings.readable())
            settings.set("UseGpu", False)
        self.assertFalse(settings.readable(), "(read again at most every READ_RETRY_S)")
        paths._read_next.clear()  # (that long later)
        self.assertTrue(settings.readable())
        self.assertFalse(settings.get("UseGpu"), "the change made, before flush merges it")
        settings.set("UseGpu", True)  # changed back once readable
        settings.flush()
        self.assertTrue(settings.get("UseGpu"), "the older pending change never comes back")

    def test_settings_known_that_could_not_be_written_after_a_move_are_written_later(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        settings.set("StartMenu", True)
        os.remove(os.path.join(self.default, "display.desktop.settings.json"))  # (a new folder: none there)
        with mock.patch.object(settings, "write_json", return_value=False):
            settings.write_if_missing()
        self.assertTrue(settings.get("StartMenu"), "still known, not read as the missing file's defaults")
        settings.flush()
        self.assertEqual(read(self.default, "display.desktop.settings.json"),
                         '{"StartMenu":true,"Format":%d}' % paths.FORMAT)

    def test_our_journal_kept_in_the_folder_left_is_read_by_the_next_start(self):
        write(self.default, JOURNAL_FILE, '{"Windows":[]}')
        DF.move_to(self.other, lambda _f: False)  # (our journal could not be written in the new folder)
        self.assertTrue(os.path.exists(os.path.join(self.default, JOURNAL_FILE)), "kept")
        DF._store(self.other)
        self.assertIn(os.path.join(self.default, JOURNAL_FILE), DF.journals(self.other))
        os.remove(os.path.join(self.default, JOURNAL_FILE))  # (read and deleted by that start)
        DF.journals(self.other)
        self.assertEqual(DF._left_journals(), [], "forgotten once it is gone")

    def test_a_setting_changed_while_the_file_cant_be_read_never_erases_it(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        write(self.default, "display.desktop.settings.json", '{"StartMenu":true,"UseGpu":false}')
        real_open = open
        busy = PermissionError(13, "in use", None, 32)
        with mock.patch("builtins.open", side_effect=lambda p, *a, **k: (_ for _ in ()).throw(busy)
                        if str(p).endswith("settings.json") else real_open(p, *a, **k)):
            settings.set("ShowConsole", True)
            settings.flush()
        self.assertEqual(read(self.default, "display.desktop.settings.json"), '{"StartMenu":true,"UseGpu":false}')
        settings.flush()
        self.assertNotIn("ShowConsole", read(self.default, "display.desktop.settings.json"),
                         "(read again at most every READ_RETRY_S)")
        paths._read_next.clear()  # (that long later)
        settings.flush()  # readable again: the change is merged in
        self.assertEqual(json.loads(read(self.default, "display.desktop.settings.json")),
                         {"StartMenu": True, "UseGpu": False, "ShowConsole": True, "Format": paths.FORMAT})

    def test_a_settings_file_with_a_byte_order_mark_is_read(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        with open(os.path.join(self.default, "display.desktop.settings.json"), "w", encoding="utf-8-sig") as fh:
            fh.write('{"StartMenu":true,"UseGpu":false}')
        self.assertTrue(settings.get("StartMenu"))
        settings.set("ShowConsole", True)
        self.assertIn('"StartMenu":true', read(self.default, "display.desktop.settings.json"), "nothing lost")

    def test_choosing_a_folders_own_data_drops_an_unwritten_old_change(self):
        from src import settings
        settings.forget()
        self.addCleanup(settings.forget)
        paths.data_dir()
        paths.note_failure(self.default, gone=True)
        settings.set("UseGpu", False)  # waiting to be written
        write(self.other, "display.desktop.settings.json", '{"StartMenu":true,"Format":%d}' % paths.FORMAT)
        DF.move_to(self.other, keep_new=True)
        self.assertTrue(DF.kept_new)
        settings.forget()  # (what the tray does after such a move)
        self.assertTrue(settings.get("UseGpu"), "that folder's settings, not the old unwritten change")
        settings.flush()
        self.assertEqual(read(self.other, "display.desktop.settings.json"),
                         '{"StartMenu":true,"Format":%d}' % paths.FORMAT)

    def test_default_in_a_fallback_run_forgets_the_unreachable_folder(self):
        gone = f"{missing_drive()}:\\Gone\\DisplayDesktop"
        DF._store(gone)
        paths.data_dir()
        self.assertTrue(DF.unreachable)
        DF.move_to(self.default)
        self.assertIsNone(DF.unreachable)

    def test_a_move_never_copies_or_replaces_a_crash_journal(self):
        write(self.default, JOURNAL_FILE, '{"ours":1}')
        write(self.other, JOURNAL_FILE, '{"a crashed run":1}')
        DF.move_to(self.other)
        self.assertEqual(read(self.other, JOURNAL_FILE), '{"a crashed run":1}',
                         "left for the app to read (DesktopManager.relocate), then replace")

    def test_journals_read_at_start(self):
        for name in (JOURNAL_FILE, f"{JOURNAL_FILE}.ab{paths.LATER_SUFFIX}", "hidden.json"):
            self.assertTrue(paths.is_journal(name), name)
        self.assertFalse(paths.is_journal(paths.SETTINGS_FILE))
        self.assertEqual(DF.journals(self.other), [
            os.path.join(self.other, JOURNAL_FILE), os.path.join(self.default, JOURNAL_FILE),
            os.path.join(self.default, "hidden.json")])
        self.assertEqual(DF.journals(self.default), [
            os.path.join(self.default, JOURNAL_FILE), os.path.join(self.default, "hidden.json")])
        # crashed runs' journals set aside (unreadable when found), in both folders, oldest first
        os.makedirs(self.other, exist_ok=True)
        older, newer = (os.path.join(self.default, f"{JOURNAL_FILE}.{n}{paths.LATER_SUFFIX}") for n in ("b", "a"))
        aside = os.path.join(self.other, f"{JOURNAL_FILE}.c{paths.LATER_SUFFIX}")
        for i, p in enumerate((older, newer, aside)):
            with open(p, "w") as fh:
                fh.write("{}")
            os.utime(p, (1000 + i, 1000 + i))
        found = DF.journals(self.other)
        self.assertLess(found.index(aside), found.index(os.path.join(self.other, JOURNAL_FILE)), "before ours")
        self.assertLess(found.index(older), found.index(newer))
        with mock.patch.dict(os.environ, {paths.DATA_ENV: self.tmp}):
            self.assertTrue(all(DF.same(os.path.dirname(p), self.tmp) for p in DF.journals(self.tmp)),
                            "a test copy never reads the real folder")


class ManagerFollows(unittest.TestCase):
    """The running app keeps writing to the new folder after a move (DesktopManager.relocate)."""

    def test_every_file_is_written_to_the_new_folder(self):
        tmp = tempfile.mkdtemp(prefix="dd-folder-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = mock.patch.object(DF, "KEY", TEST_KEY)  # (never the real key)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(DF._set_value, DF.LEFT_JOURNAL, None)
        old, new = os.path.join(tmp, "old"), os.path.join(tmp, "new")
        os.makedirs(old)
        os.makedirs(new)
        win = FakeWin()
        a = win.add("A")
        m = DesktopManager(win=win, snapshots=FakeSnapshots(), directory=old)
        m.sync()
        m.relocate(new)
        m.create_desktop_and_switch(win.cursor_device)  # journal (a hidden), places, showing.json
        m.save_desktops()                                # layout.json
        win.advance(5_000)
        m.sync()
        self.assertEqual(sorted(os.listdir(new)), [JOURNAL_FILE, "display.desktop.layout.json", PLACES_FILE, "display.desktop.showing.json"])
        self.assertNotIn(JOURNAL_FILE, os.listdir(old))
        m.end()
        self.assertTrue(win.is_window_visible(a))
        self.assertNotIn(JOURNAL_FILE, os.listdir(new), "the journal is removed in the new folder")

    def test_windows_hidden_by_a_run_that_fell_back_come_back_at_the_next_start(self):
        """Start 1 can't reach the chosen folder (drive missing), uses the default one, hides windows
        and is killed. Start 2 can reach the chosen folder again: it still finds start 1's journal."""
        tmp = tempfile.mkdtemp(prefix="dd-folder-")
        self.addCleanup(shutil.rmtree, tmp, True)
        default, chosen = os.path.join(tmp, "Default"), os.path.join(tmp, "Chosen")
        for p in (mock.patch.object(DF, "KEY", TEST_KEY), mock.patch.object(DF, "default_folder", return_value=default),
                  mock.patch.object(DF, "_in_use", None)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(DF._store, None)
        self.addCleanup(DF._set_value, DF.LEFT_JOURNAL, None)
        win = FakeWin()
        a = win.add("A")
        DF._store(f"{missing_drive()}:\\Chosen")
        m1 = DesktopManager(win=win, snapshots=FakeSnapshots())  # the app's own folder (no directory)
        m1.sync()
        m1.create_desktop_and_switch(win.cursor_device)          # a hidden; then the app is killed
        self.assertFalse(win.is_window_visible(a))
        self.assertIn(JOURNAL_FILE, os.listdir(default))
        DF._in_use = None                                         # the next run
        DF._store(chosen)
        DesktopManager(win=win, snapshots=FakeSnapshots())
        self.assertTrue(win.is_window_visible(a), "shown again")
        self.assertNotIn(JOURNAL_FILE, os.listdir(default), "that journal is done with")
        self.assertTrue(os.path.isdir(chosen), "this run uses the chosen folder")


if __name__ == "__main__":
    unittest.main()
