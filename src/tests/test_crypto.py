"""Encrypt Settings: the file format (marker, AES-GCM, wrong key vs. junk), reading and writing data
files encrypted, re-encrypting a folder, the key popup at start and while running."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src import crypto, datafolder, layout, paths, settings
from src.paths import JOURNAL_FILE, LAYOUT_FILE, PLACES_FILE, SETTINGS_FILE

REAL_ITERATIONS = crypto.ITERATIONS


class Base(unittest.TestCase):
    def setUp(self):
        crypto.reset()
        self.addCleanup(crypto.reset)
        # (PBKDF2's cost is not what these check: a cheap count; Format checks the real one)
        p = mock.patch.object(crypto, "ITERATIONS", 1000)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(setattr, crypto, "on_locked", None)
        self.dir = tempfile.mkdtemp(prefix="dd-crypto-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        paths.forget_failures()
        self.addCleanup(paths.forget_failures)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def raw(self, name: str) -> bytes:
        with open(self.path(name), "rb") as fh:
            return fh.read()


class Format(Base):
    def test_plain_while_off(self):
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        self.assertEqual(self.raw(LAYOUT_FILE), b'{"Windows":[],"Format":2}')

    def test_encrypted_with_the_marker_and_read_back_with_the_key(self):
        self.assertEqual(REAL_ITERATIONS, 200_000, "part of the file format: files written before must open")
        with mock.patch.object(crypto, "ITERATIONS", REAL_ITERATIONS):
            crypto.use("secret", True)
            paths.write_json(self.path(LAYOUT_FILE), {"Windows": [1, 2]})
            raw = self.raw(LAYOUT_FILE)
            self.assertTrue(raw.startswith(crypto.MAGIC), "marked as ours")
            self.assertNotIn(b"Windows", raw, "nothing readable in it")
            self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {"Windows": [1, 2]})

    def test_a_wrong_key_is_locked_and_asks_while_junk_is_damaged(self):
        crypto.use("secret", True)
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        crypto.reset()
        crypto.use("other", False)
        asked = []
        crypto.on_locked = asked.append
        with self.assertRaises(crypto.Locked):
            paths.read_json(self.path(LAYOUT_FILE))
        self.assertEqual(asked, [self.path(LAYOUT_FILE)], "ours: the key popup")
        with open(self.path(SETTINGS_FILE), "wb") as fh:
            fh.write(b"\x00\x13 not ours at all")
        with self.assertRaises(ValueError):
            paths.read_json(self.path(SETTINGS_FILE))  # (damaged, as before: no key would help)
        self.assertEqual(len(asked), 1, "junk never asks for a key")

    def test_a_changed_byte_never_opens(self):
        crypto.use("secret", True)
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        raw = bytearray(self.raw(LAYOUT_FILE))
        raw[-1] ^= 1
        with open(self.path(LAYOUT_FILE), "wb") as fh:
            fh.write(bytes(raw))
        with self.assertRaises(crypto.Locked):
            paths.read_json(self.path(LAYOUT_FILE))

    def test_a_key_entered_later_opens_another_folders_files(self):
        crypto.use("theirs", True)
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        crypto.reset()
        crypto.use("mine", True)
        self.assertFalse(crypto.unlocks(self.dir, "mine"))
        self.assertTrue(crypto.unlocks(self.dir, "theirs"))
        crypto.add_key("theirs")
        self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {"Windows": []})

    def test_the_remembered_key_is_protected_by_windows(self):
        blob = crypto._dpapi(b"secret", protect=True)
        self.assertNotIn(b"secret", blob, "never stored as text")
        self.assertEqual(crypto._dpapi(blob, protect=False), b"secret")
        self.assertIsNone(crypto._dpapi(blob, protect=False, entropy=b"other"),
                          "a program asking DPAPI without DisplayDesktop's extra secret gets nothing")

    def test_the_extra_secret_is_a_one_way_hash_of_the_processor(self):
        ident = crypto.cpu_identity()
        self.assertTrue(ident.startswith((b"cpuid:", b"cpu:")) and len(ident) > 10)
        self.assertEqual(crypto.cpu_identity(), ident, "the same every time")
        secret = crypto.entropy()
        self.assertEqual(len(secret), 32)
        self.assertNotIn(ident[6:20], secret, "nothing of the processor's identity in it")
        blob = crypto._dpapi(b"secret", protect=True)
        with mock.patch.object(crypto, "_entropy", None), \
                mock.patch.object(crypto, "cpu_identity", return_value=b"cpuid:another processor"):
            self.assertNotEqual(crypto.entropy(), secret)
            self.assertIsNone(crypto._dpapi(blob, protect=False), "a new processor: the key is typed again")


class Remembered(Base):
    KEY = r"Software\DisplayDesktop (unit test)"

    def test_remember_key_stores_it_protected_and_forgets_it(self):
        import winreg
        with mock.patch.object(datafolder, "KEY", self.KEY):  # (never the real key)
            try:
                self.assertTrue(crypto.remember("my secret key"))
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as k:
                    value, kind = winreg.QueryValueEx(k, crypto.KEY_VALUE)
                self.assertEqual(kind, winreg.REG_BINARY)
                self.assertNotIn(b"my secret key", value)
                self.assertNotIn("my secret key".encode("utf-16-le"), value, "not as text in any form")
                self.assertEqual(crypto.stored_key(), "my secret key")
                self.assertTrue(crypto.remember(None))
                self.assertIsNone(crypto.stored_key())
            finally:
                datafolder._set_value(crypto.KEY_VALUE, None)
                datafolder._store(None)


class FileFormat(Base):
    """The saved files' own format number (independent of the app's version)."""

    def test_every_file_is_written_with_the_format_number_encrypted_too(self):
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        self.assertEqual(json.loads(self.raw(LAYOUT_FILE))["Format"], paths.FORMAT)
        crypto.use("secret", True)
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        raw = self.raw(SETTINGS_FILE)
        self.assertEqual(raw[:len(crypto.MAGIC)], b"DisplayDesktop encrypted file\x00", "the marker has no number")
        self.assertEqual(json.loads(crypto.decode(raw))["Format"], paths.FORMAT, "the number is inside")
        self.assertEqual(paths.read_json(self.path(SETTINGS_FILE)), {"StartMenu": True}, "not in what is read")

    def test_a_file_without_the_number_reads_as_format_1(self):
        with open(self.path(SETTINGS_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"StartMenu":true}')
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"Setups":[]}')
        with mock.patch.object(datafolder, "current", return_value=os.path.join(self.dir, "in use")):
            self.assertEqual(paths.read_json(self.path(SETTINGS_FILE)), {"StartMenu": True})
            self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {}, "format 1's step: places start empty")

    def test_a_newer_format_asks_and_otherwise_reads_as_damaged(self):
        newer = '{"Windows":[],"Showing":{},"Format":%d}' % (paths.FORMAT + 1)
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write(newer)
        asked = []
        with mock.patch.object(paths, "on_unreadable", asked.append):
            with self.assertRaises(ValueError):  # (NewerFormat is a ValueError: damaged as far as reading goes)
                paths.read_json(self.path(LAYOUT_FILE))
            self.assertEqual(asked, [self.path(LAYOUT_FILE)], "the question (Quit / Recreate Files)")
            self.assertEqual(paths.unreadable_files(self.dir), [self.path(LAYOUT_FILE)])
            self.assertEqual(len(asked), 1, "a scan never asks")

    def test_an_emptied_journal_carries_the_format_number(self):
        from src.desktop_manager import DesktopManager
        m = DesktopManager.__new__(DesktopManager)
        m._win, m._writer = SimpleNamespace(tick_count64=lambda: 5), "w"
        self.assertTrue(m._empty_journal(self.path(JOURNAL_FILE)))
        self.assertEqual(json.loads(self.raw(JOURNAL_FILE)),
                         {"UptimeMs": 5, "Writer": "w", "Windows": [], "Format": paths.FORMAT})

    def test_a_missing_upgrade_step_is_damaged_never_an_error(self):
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[],"Format":%d}' % paths.FORMAT)
        with mock.patch.object(paths, "FORMAT", paths.FORMAT + 1), mock.patch.dict(paths.MIGRATIONS, {}, clear=True):
            with self.assertRaises(ValueError):
                paths.read_json(self.path(LAYOUT_FILE))

    def test_an_upgraded_file_keeps_its_own_form(self):
        # (another folder's file encrypted with another key, read with that key added: never rewritten
        # plain or with the key in use)
        crypto.use("theirs", True)
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        before = self.raw(LAYOUT_FILE)
        crypto.reset()
        crypto.add_key("theirs")
        older = paths.FORMAT
        with mock.patch.object(paths, "FORMAT", older + 1), mock.patch.dict(paths.MIGRATIONS, {older: lambda n, d: d}), \
                mock.patch.object(datafolder, "current", return_value=self.dir):  # (the folder in use: the form decides)
            self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {"Windows": []})
        self.assertEqual(self.raw(LAYOUT_FILE), before, "left as it was")

    def test_the_apply_question_that_cant_open_reverts(self):
        from src import restore_window as RW
        with mock.patch.object(RW, "ApplyKeyWindow", side_effect=OSError("no window")):
            self.assertFalse(RW.ask_apply_key(0), "revert: never an error that ends the app")

    def test_a_bad_number_is_damaged(self):
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[],"Format":"x"}')
        with self.assertRaises(ValueError):
            paths.read_json(self.path(LAYOUT_FILE))

    def test_an_older_format_is_brought_up_to_date_when_read_and_rewritten(self):
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[],"Old":1,"Format":%d}' % paths.FORMAT)
        step = lambda name, data: {**{k: v for k, v in data.items() if k != "Old"}, "New": name}  # noqa: E731
        older, newer = paths.FORMAT, paths.FORMAT + 1
        with mock.patch.object(paths, "FORMAT", newer), mock.patch.dict(paths.MIGRATIONS, {older: step}), \
                mock.patch.object(datafolder, "current", return_value=self.dir):
            self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {"Windows": [], "New": LAYOUT_FILE})
            self.assertEqual(json.loads(self.raw(LAYOUT_FILE)),
                             {"Windows": [], "New": LAYOUT_FILE, "Format": newer}, "rewritten in the new format")

    def test_a_file_without_the_number_is_upgraded_as_format_1(self):
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[],"Old":1}')
        step = lambda name, data: {"Windows": data["Windows"], "Upgraded": True}  # noqa: E731
        with mock.patch.object(paths, "FORMAT", 2), mock.patch.dict(paths.MIGRATIONS, {1: step}), \
                mock.patch.object(datafolder, "current", return_value=self.dir):
            self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {"Windows": [], "Upgraded": True})

    def test_reading_never_rewrites_another_folders_file_nor_a_journal(self):
        step = lambda name, data: data  # noqa: E731
        older = paths.FORMAT
        written = '{"Windows":[],"Format":%d}' % older
        for name in (LAYOUT_FILE, JOURNAL_FILE):
            with open(self.path(name), "w", encoding="utf-8") as fh:
                fh.write(written)
        elsewhere = os.path.join(self.dir, "in use")
        with mock.patch.object(paths, "FORMAT", older + 1), mock.patch.dict(paths.MIGRATIONS, {older: step}), \
                mock.patch.object(datafolder, "current", return_value=elsewhere):
            paths.read_json(self.path(LAYOUT_FILE))
        with mock.patch.object(paths, "FORMAT", older + 1), mock.patch.dict(paths.MIGRATIONS, {older: step}), \
                mock.patch.object(datafolder, "current", return_value=self.dir):
            paths.read_json(self.path(JOURNAL_FILE))
        for name in (LAYOUT_FILE, JOURNAL_FILE):
            self.assertEqual(json.loads(self.raw(name))["Format"], older, f"{name}: left for its own writer")
        old = self.path("hidden.json")  # (an older journal name)
        with open(old, "w", encoding="utf-8") as fh:
            fh.write(written)
        with mock.patch.object(paths, "FORMAT", older + 1), mock.patch.dict(paths.MIGRATIONS, {older: step}), \
                mock.patch.object(datafolder, "current", return_value=self.dir):
            paths.read_json(old)
        self.assertEqual(json.loads(self.raw("hidden.json"))["Format"], older, "an older journal name too")

    def test_the_start_asks_and_quit_or_recreate_decides(self):
        from src import tray as T
        self.addCleanup(paths.kept.clear)
        newer = '{"Windows":[],"Format":%d}' % (paths.FORMAT + 1)
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write(newer)  # (a newer version's)
        with open(self.path(SETTINGS_FILE), "wb") as fh:
            fh.write(b"\x00\x13junk, not ours")  # (damaged: the same question)
        paths.write_json(self.path(JOURNAL_FILE), {"Windows": []})  # (ours: never touched)
        crypto.use("someone else's", True)
        paths.write_json(self.path("display.desktop.showing.json"), {"Showing": {}})
        crypto.reset()  # (encrypted with a key not entered: the key popup's, never counted here)
        with mock.patch.object(T, "is_test_mode", return_value=True), mock.patch.object(T.console, "log"):
            with mock.patch("src.restore_window.ask_recovery", return_value="quit") as ask:
                self.assertFalse(T.resolve_unreadable_files(self.dir), "Quit: the app ends")
            report = ask.call_args[0][0]
            self.assertEqual(len(report.unreadable), 2, "both files named")
            states = {r.shown: (r.kind, r.state) for r in report.rows}
            self.assertEqual(states[LAYOUT_FILE][0], "unreadable")
            self.assertIn("revision number is newer", states[LAYOUT_FILE][1], "why: a newer version's")
            self.assertIn("Damaged", states[SETTINGS_FILE][1], "why: damaged")
            self.assertEqual(states[JOURNAL_FILE], ("ok", T.L.t("recovery.ok")))
            self.assertEqual(states["display.desktop.showing.json"][0], "locked")
            self.assertTrue(os.path.exists(self.path(LAYOUT_FILE)), "Quit deletes nothing")
            with mock.patch("src.restore_window.ask_recovery", return_value="recreate"):
                self.assertTrue(T.resolve_unreadable_files(self.dir), "Recreate Files: the app goes on")
            self.assertFalse(os.path.exists(self.path(LAYOUT_FILE)) or os.path.exists(self.path(SETTINGS_FILE)))
            self.assertTrue(os.path.exists(self.path(JOURNAL_FILE)), "only the newer version's files go")
            with mock.patch("src.restore_window.ask_recovery") as ask:
                self.assertTrue(T.resolve_unreadable_files(self.dir))
            ask.assert_not_called()  # (none left: nothing asked)

    def test_a_question_that_cant_be_shown_or_a_file_that_cant_be_deleted_is_never_asked_again(self):
        from src import tray as T
        with open(self.path(SETTINGS_FILE), "w", encoding="utf-8") as fh:
            fh.write("{damaged")
        skip = set()
        with mock.patch.object(T, "is_test_mode", return_value=True), mock.patch.object(T.console, "log"):
            with mock.patch("src.restore_window.ask_recovery", return_value=None):
                self.assertTrue(T.resolve_unreadable_files(self.dir, skip=skip), "no screen: never Quit unasked")
            self.assertTrue(os.path.exists(self.path(SETTINGS_FILE)), "nor deleted unasked")
            with mock.patch("src.restore_window.ask_recovery") as ask:
                self.assertTrue(T.resolve_unreadable_files(self.dir, skip=skip))
            ask.assert_not_called()
            skip.clear()
            with mock.patch("src.restore_window.ask_recovery", return_value="recreate"), \
                    mock.patch.object(T.os, "remove", side_effect=PermissionError(13, "read-only")):
                self.assertTrue(T.resolve_unreadable_files(self.dir, skip=skip))
            self.assertEqual(skip, {os.path.normcase(self.path(SETTINGS_FILE))}, "Recreate that failed: not asked again")
        app = SimpleNamespace(exiting=False, _asking_unreadable=False, hwnd=1, _unreadable_skip=skip)
        with mock.patch.object(T.G, "PostMessageW") as post:
            T.TrayApp._unreadable_found(app, self.path(SETTINGS_FILE))
            T.TrayApp._unreadable_found(app, self.path(JOURNAL_FILE + ".ab12cd34.later.json"))
        post.assert_not_called()  # (skipped; and a journal of another name is the manager's to tell)

    def test_recreate_files_removes_every_settings_file_and_makes_it_new(self):
        from src import tray as T
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": [], "Showing": {"MON": 1}})
        with open(self.path(PLACES_FILE), "w", encoding="utf-8") as fh:
            fh.write("{damaged")
        with open(self.path(JOURNAL_FILE), "w", encoding="utf-8") as fh:
            fh.write("crash record")
        removed = []
        real_remove = T.os.remove
        with mock.patch.object(T.os, "remove", side_effect=lambda p: removed.append(os.path.basename(p))
                               or real_remove(p)):
            T.recreate_files(self.dir, bad=[self.path(PLACES_FILE)])
        self.assertEqual(sorted(removed), sorted([SETTINGS_FILE, LAYOUT_FILE, PLACES_FILE]),
                         "every settings file there removed; never the crash record")
        self.assertEqual(paths.read_json(self.path(SETTINGS_FILE)), {"StartMenu": True}, "made new, nothing lost")
        self.assertEqual(paths.read_json(self.path(LAYOUT_FILE)), {"Windows": [], "Showing": {"MON": 1}})
        self.assertFalse(os.path.exists(self.path(PLACES_FILE)), "the bad one: removed, made new by its writer")
        self.assertEqual(self.raw(JOURNAL_FILE).decode(), "crash record")

    def test_files_waiting_for_the_answer_are_never_written_over(self):
        from src import tray as T
        self.addCleanup(paths.kept.clear)
        newer = '{"Format":%d}' % (paths.FORMAT + 1)
        with open(self.path(PLACES_FILE), "w", encoding="utf-8") as fh:
            fh.write(newer)
        written = []

        def ask(report, icon=0, **kw):  # (the app keeps running while it is open: its writes must not land)
            written.append(paths.write_json(self.path(PLACES_FILE), {"Places": []}))
            return "quit"
        with mock.patch.object(T, "is_test_mode", return_value=True), mock.patch.object(T.console, "log"), \
                mock.patch("src.restore_window.ask_recovery", ask):
            self.assertFalse(T.resolve_unreadable_files(self.dir))
        self.assertEqual(written, [False])
        self.assertTrue(paths.write_json(self.path(PLACES_FILE), {}) is False, "Quit: still kept while the app ends")
        self.assertEqual(self.raw(PLACES_FILE).decode(), newer)
        paths.kept.clear()
        with mock.patch.object(T, "is_test_mode", return_value=True), mock.patch.object(T.console, "log"), \
                mock.patch("src.restore_window.ask_recovery", return_value="recreate"):
            T.resolve_unreadable_files(self.dir)
        self.assertEqual(paths.kept, set(), "answered: nothing kept")
        self.assertTrue(paths.write_json(self.path(PLACES_FILE), {}))

    def test_a_question_covers_only_its_own_folders_and_a_move_asks_once(self):
        from src import tray as T
        self.addCleanup(paths.kept.clear)
        app = SimpleNamespace(exiting=False, _asking_unreadable=False, hwnd=1, _unreadable_skip=set())
        other = os.path.join(self.dir, "other")
        with mock.patch.object(T.G, "PostMessageW") as post, mock.patch.object(T, "is_test_mode", return_value=True):
            self.assertTrue(T.TrayApp._unreadable_found(app, self.path(SETTINGS_FILE)))
            self.assertTrue(T.TrayApp._unreadable_found(app, self.path(LAYOUT_FILE)), "the open question's folder")
            self.assertFalse(T.TrayApp._unreadable_found(app, os.path.join(other, LAYOUT_FILE)), "another folder")
        post.assert_called_once()
        self.assertTrue(paths.is_kept(self.path(LAYOUT_FILE)))
        self.assertFalse(paths.is_kept(os.path.join(other, LAYOUT_FILE)))
        # a folder move: the manager's reads are covered by the move's own question, none is posted
        with open(self.path(SETTINGS_FILE), "w", encoding="utf-8") as fh:
            fh.write("{damaged")
        paths.kept.clear()
        seen = {}
        app = SimpleNamespace(exiting=False, _asking_unreadable=False, hwnd=1, _unreadable_skip=set(), _icon=0)
        app.mgr = SimpleNamespace(recovery_problem=None, journal_problems={}, relocate=lambda f: seen.update(
            kept=paths.is_kept(self.path(SETTINGS_FILE)),
            covered=T.TrayApp._unreadable_found(app, self.path(SETTINGS_FILE))) or True)
        with mock.patch.object(T, "is_test_mode", return_value=True), mock.patch.object(T.console, "log"), \
                mock.patch.object(T.crypto, "use"), mock.patch.object(T.G, "PostMessageW") as post, \
                mock.patch("src.restore_window.ask_recovery", return_value="quit") as ask:
            self.assertTrue(T.TrayApp.folder_changed(app, self.dir))
        self.assertEqual(seen, {"kept": True, "covered": True}, "kept before the manager moves there")
        ask.assert_called_once()
        post.assert_called_once_with(1, T.WM_APP_QUIT, 0, 0)  # (Quit; no second question posted)
        self.assertFalse(app._asking_unreadable)

    def test_kept_settings_are_never_deleted_and_answers_release_the_whole_folder(self):
        from src import tray as T
        self.addCleanup(paths.kept.clear)
        with open(self.path(SETTINGS_FILE), "w", encoding="utf-8") as fh:
            fh.write("{damaged")
        paths.kept.add(os.path.normcase(self.path(SETTINGS_FILE)))
        settings.forget()
        self.addCleanup(settings.forget)
        with mock.patch.object(settings, "_path", return_value=self.path(SETTINGS_FILE)):
            settings._write_known()  # (all defaults: would delete the file)
        self.assertTrue(os.path.exists(self.path(SETTINGS_FILE)), "kept: never deleted")
        paths.kept.add(os.path.normcase(self.path(LAYOUT_FILE)))  # (found while the question was open)
        os.remove(self.path(SETTINGS_FILE))  # (and the asked one replaced since: nothing found)
        with mock.patch.object(T, "is_test_mode", return_value=True), mock.patch.object(T.console, "log"):
            self.assertTrue(T.resolve_unreadable_files(self.dir))
        self.assertEqual(paths.kept, set(), "nothing waits: the folder's files are written again")

    def test_a_folder_known_offline_is_never_scanned(self):
        with mock.patch.object(paths, "known_offline", return_value=True), \
                mock.patch.object(paths, "read_json") as read:
            self.assertEqual(paths.unreadable_files(self.dir), [])
        read.assert_not_called()

    def test_a_bad_format_number_is_never_recoded(self):
        for bad in ('"2"', "0"):
            with open(self.path(SETTINGS_FILE), "w", encoding="utf-8") as fh:
                fh.write('{"Format":%s}' % bad)
            crypto.use("secret", True)
            self.assertEqual(crypto.recode_folder(self.dir), [self.path(SETTINGS_FILE)])
            self.assertEqual(self.raw(SETTINGS_FILE).decode(), '{"Format":%s}' % bad)
            crypto.reset()

    def test_the_scan_never_opens_the_key_popup(self):
        crypto.use("someone else's", True)
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        crypto.reset()
        with mock.patch.object(crypto, "on_locked") as popup:
            self.assertEqual(paths.unreadable_files(self.dir), [])
        popup.assert_not_called()

    def test_an_upgrade_step_that_fails_is_damaged_and_its_write_is_quiet(self):
        with open(self.path(LAYOUT_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[],"Format":%d}' % paths.FORMAT)
        older = paths.FORMAT
        with mock.patch.object(paths, "FORMAT", older + 1), \
                mock.patch.dict(paths.MIGRATIONS, {older: lambda n, d: d["Missing"]}):
            with self.assertRaises(ValueError):
                paths.read_json(self.path(LAYOUT_FILE))
        reported = []
        with mock.patch.object(paths, "on_write_failed", reported.append), \
                mock.patch.object(paths.os, "replace", side_effect=PermissionError(13, "refused", None, 5)), \
                mock.patch.object(paths.time, "sleep"):
            for _ in range(paths.HELD_LIMIT + 1):
                paths.write_json(self.path(LAYOUT_FILE), {}, quiet=True)
            paths.write_json(os.path.join(self.dir, "gone", "x", LAYOUT_FILE), {}, quiet=True)
        self.assertEqual(reported, [])
        self.assertFalse(paths.paused(self.dir))

    def test_a_newer_file_read_while_running_asks_quit_or_recreate(self):
        from src import tray as T
        app = SimpleNamespace(exiting=False, _asking_unreadable=False, hwnd=1, _icon=0, _unreadable_skip=set(),
                              reread=mock.Mock(), _pending_problem=0, mgr=SimpleNamespace(journal_problems={}), folder_changed=mock.Mock())
        with mock.patch.object(T.G, "PostMessageW") as post:
            T.TrayApp._unreadable_found(app, self.path(LAYOUT_FILE))
            T.TrayApp._unreadable_found(app, self.path(SETTINGS_FILE))
        post.assert_called_once_with(1, T.WM_APP_UNREADABLE, 0, 0)  # (one question at a time)
        for recreate, quit_posted in ((True, False), (False, True)):
            with mock.patch.object(T, "resolve_unreadable_files", return_value=recreate), \
                    mock.patch.object(T.G, "PostMessageW") as post:
                T.TrayApp.ask_unreadable(app)
            self.assertEqual(post.called, quit_posted, "Quit ends the app (every window shown first)")
            self.assertFalse(app._asking_unreadable)

    def test_a_closed_windows_label_never_takes_another_programs_name(self):
        from src import restore_window as RW
        exes = {}
        with mock.patch.object(RW.N, "get_pid", return_value=0), \
                mock.patch.object(RW.N, "get_exe_path", side_effect=[r"C:\a.exe", ""]), \
                mock.patch.object(RW.N, "get_title", side_effect=["A", "B"]):
            self.assertEqual(RW.label(1, exes), "A  (a.exe)")
            self.assertEqual(RW.label(2, exes), "B")

    def test_the_program_path_cache_drops_only_closed_windows(self):
        win = mock.Mock()
        win.is_window.side_effect = lambda h: h % 2 == 0  # (odd handles closed)
        lay = layout.WindowLayout(self.dir, win)
        lay._exe_cache = {(h, 1): f"p{h}" for h in range(layout.EXE_CACHE_MAX)}
        win.get_pid.return_value = 7
        win.get_exe_path.return_value = "new.exe"
        lay._exe_of(99999 * 2)
        self.assertTrue(all(h % 2 == 0 for h, _ in lay._exe_cache), "closed windows' entries dropped")
        self.assertIn((0, 1), lay._exe_cache, "open windows' entries kept")

    def test_save_and_load_tell_why_and_never_crash(self):
        from src import tray as T
        p = mock.patch.object(T.datafolder, "current", return_value=self.dir)  # (never the real registry value)
        p.start()
        self.addCleanup(p.stop)
        for code, key in ((layout.LOCKED, "msg.loadLocked"),):
            app = SimpleNamespace(exiting=False, mgr=mock.Mock(), balloon=mock.Mock(), ask_key=mock.Mock(),
                                  _confirm=lambda k: True)
            app.mgr.load_desktops.return_value = code
            T.TrayApp.run_command(app, T.ID_LOAD)
            app.balloon.assert_called_once_with(T.L.t(key), warning=True)
        for code, key in ((layout.LOCKED, "msg.saveLocked"),):
            app = SimpleNamespace(exiting=False, mgr=mock.Mock(), balloon=mock.Mock(), ask_key=mock.Mock(),
                                  _confirm=lambda k: True)
            app.mgr.save_desktops.return_value = code
            T.TrayApp.run_command(app, T.ID_SAVE)
            app.balloon.assert_called_once_with(T.L.t(key), warning=True)


class Folder(Base):
    def files(self):
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": [], "Showing": {}})
        paths.write_json(self.path(JOURNAL_FILE), {"UptimeMs": 1, "Windows": []})

    def test_turning_it_on_and_off_rewrites_every_file(self):
        self.files()
        self.assertFalse(crypto.folder_encrypted(self.dir))
        crypto.use("secret", True)
        self.assertEqual(crypto.recode_folder(self.dir), [])
        for name in (SETTINGS_FILE, LAYOUT_FILE, JOURNAL_FILE):
            self.assertTrue(self.raw(name).startswith(crypto.MAGIC), name)
        self.assertTrue(crypto.folder_encrypted(self.dir) and crypto.unlocks(self.dir, "secret"))
        crypto.use("secret", False)
        self.assertEqual(crypto.recode_folder(self.dir), [])
        self.assertEqual(json.loads(self.raw(SETTINGS_FILE)), {"StartMenu": True, "Format": 2}, "plain again")

    def test_a_question_that_fails_halfway_leaves_no_window(self):
        from src import gui as G
        from src import restore_window as RW
        before = set(G.dialogs)
        with mock.patch.object(RW.ApplyKeyWindow, "_build", side_effect=OSError("no font")):
            self.assertFalse(RW.ask_apply_key(0))
        from src import recovery
        report = recovery.Report("x", [], ["x"], False, True)
        done = mock.Mock()
        with mock.patch.object(RW.RecoveryWindow, "_build", side_effect=OSError("no font")):
            self.assertIsNone(RW.ask_recovery(report), "Recovery that can't open: neither Quit nor Recreate")
            with self.assertRaises(OSError):
                RW.RecoveryWindow(report, on_done=done)
        done.assert_not_called()  # (never opened: no answer, never Quit)
        self.assertEqual(set(G.dialogs), before, "the half-built window is gone")

    def test_a_new_key_re_encrypts(self):
        self.files()
        crypto.use("one", True)
        crypto.recode_folder(self.dir)
        crypto.use("two", True)
        crypto.recode_folder(self.dir)
        self.assertTrue(crypto.unlocks(self.dir, "two"))
        self.assertFalse(crypto.unlocks(self.dir, "one"))

    def test_an_encrypted_folder_has_data_without_asking(self):
        crypto.use("secret", True)
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": []})
        crypto.reset()
        asked = []
        crypto.on_locked = asked.append
        self.assertTrue(datafolder.has_data(self.dir), "a move there asks which data to keep")
        self.assertEqual(asked, [])

    def test_save_and_load_say_the_key_is_needed(self):
        crypto.use("secret", True)
        paths.write_json(self.path(LAYOUT_FILE), {"Windows": [], "Showing": {}})
        crypto.reset()
        lay = layout.WindowLayout(self.dir, mock.Mock())
        self.assertEqual(lay.read_problem(), layout.LOCKED)
        crypto.add_key("secret")
        self.assertEqual(lay.read_problem(), 0, "read once the key is in")

    def test_settings_encrypted_with_an_unknown_key_are_never_written_over(self):
        crypto.use("secret", True)
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        crypto.reset()
        settings.forget()
        self.addCleanup(settings.forget)
        with mock.patch.object(settings, "_path", return_value=self.path(SETTINGS_FILE)):
            self.assertFalse(settings.readable())
            settings.set("UseGpu", False)  # (kept for later)
            self.assertTrue(self.raw(SETTINGS_FILE).startswith(crypto.MAGIC), "untouched")
            crypto.add_key("secret")
            settings.retry()
            self.assertTrue(settings.get("StartMenu"), "read with the key")


class Tray(Base):
    def setUp(self):
        super().setUp()
        from src import tray
        self.T = tray
        p = mock.patch.object(paths, "data_dir", return_value=self.dir)
        p.start()
        self.addCleanup(p.stop)

    def encrypted(self, key="secret"):
        crypto.use(key, True)
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        crypto.reset()

    def test_nothing_encrypted_nothing_asked(self):
        with mock.patch.object(self.T.KeyWindow, "show") as show:
            self.assertTrue(self.T.unlock_at_start())
        show.assert_not_called()

    def test_the_remembered_key_opens_them_without_asking(self):
        self.encrypted()
        with mock.patch.object(crypto, "stored_key", return_value="secret"), \
                mock.patch.object(self.T.KeyWindow, "show") as show:
            self.assertTrue(self.T.unlock_at_start())
        show.assert_not_called()
        self.assertTrue(crypto.enabled and crypto.key() == "secret")

    def test_the_popup_asks_and_cancel_ends_the_start(self):
        self.encrypted()
        for typed, accepted in (("secret", True), (None, False)):
            crypto.reset()
            win = SimpleNamespace(hwnd=0, accepted=False)

            def show(folder, tried, *a, **k):
                if typed:
                    self.assertFalse(tried("wrong"), "a wrong key: asked again")
                    win.accepted = tried(typed)
                return win
            with mock.patch.object(crypto, "stored_key", return_value=None), \
                    mock.patch.object(self.T.KeyWindow, "show", side_effect=show), \
                    mock.patch.object(self.T.G, "run_until"):
                self.assertEqual(self.T.unlock_at_start(), accepted)
        crypto.reset()

    def test_a_key_typed_while_running_opens_the_folder_and_reads_again(self):
        self.encrypted()
        app = SimpleNamespace(mgr=mock.Mock(), _options_owed=False)
        with mock.patch.object(datafolder, "current", return_value=self.dir), \
                mock.patch.object(crypto, "remembered", return_value=False):
            self.assertFalse(self.T.TrayApp.key_entered(app, self.dir, "wrong"))
            app.reread = lambda: self.T.TrayApp.reread(app)
            self.assertTrue(self.T.TrayApp.key_entered(app, self.dir, "secret"))
        self.assertTrue(crypto.enabled and crypto.key() == "secret", "the folder in use: its key from now on")
        app.mgr.reread.assert_called_once()

    def test_settings_turns_it_on_and_off(self):
        paths.write_json(self.path(SETTINGS_FILE), {"StartMenu": True})
        app = SimpleNamespace()
        with mock.patch.object(datafolder, "current", return_value=self.dir), \
                mock.patch.object(crypto, "remember", return_value=True) as remember:
            self.assertTrue(self.T.TrayApp.set_encryption(app, "secret", True, True))
            self.assertTrue(self.raw(SETTINGS_FILE).startswith(crypto.MAGIC))
            remember.assert_called_with("secret")
            self.assertTrue(self.T.TrayApp.set_encryption(app, None, False, True))
            remember.assert_called_with(None)
        self.assertEqual(json.loads(self.raw(SETTINGS_FILE)), {"StartMenu": True, "Format": 2})


if __name__ == "__main__":
    unittest.main()
