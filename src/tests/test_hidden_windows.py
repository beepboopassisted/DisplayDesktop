"""Hidden windows: the last session's journal (start times, a damaged file), Restore Hidden Windows
(hidden_windows / restore_windows), and windows that must never show on the way (Load onto a hidden
desktop, an unplugged display, floating windows moved with their window). Simulated Windows only."""
from __future__ import annotations

import json
import os
import shutil
import unittest
import unittest.mock

from src import native as N
from src.paths import JOURNAL_FILE, LAYOUT_FILE, PLACES_FILE

from test_desktop_manager import D1, D2, Base


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Journal(Base):
    def write_journal(self, records):
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": self.win.tick - 1000, "Windows": records}, fh)

    def test_a_start_time_that_cant_be_read_now_does_not_keep_the_window_hidden(self):
        a = self.win.add("A", visible=False, pid=77, cls="X")
        self.write_journal([{"Handle": a, "Pid": 77, "Class": "X", "Started": self.win.process_started(77)}])
        self.win.process_started = lambda pid: 0  # e.g. the program runs elevated, DisplayDesktop not
        self.manager()
        self.assertTrue(self.visible(a))

    def test_a_newer_versions_journal_is_damaged_never_asked(self):
        # (the Quit / Recreate Files question is for the settings files only: its Quit would leave a
        # crashed run's windows hidden. A journal that can't be read is told with Restore Hidden Windows)
        from src import paths
        newer = os.path.join(self.dir, "other", JOURNAL_FILE)
        os.makedirs(os.path.dirname(newer))
        with open(newer, "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": 1, "Windows": [], "Format": paths.FORMAT + 1}, fh)
        m = self.manager()
        with unittest.mock.patch.object(paths, "on_unreadable", lambda p: True):
            m._recover_journal(newer)
        self.assertEqual(m.recovery_problem, "damaged")
        self.assertFalse(os.path.exists(newer))
        self.assertNotIn(JOURNAL_FILE, paths.ASKED_FILES)

    def test_a_damaged_journal_is_reported(self):
        with open(os.path.join(self.dir, JOURNAL_FILE), "w") as fh:
            fh.write("{not json")
        m = self.manager()
        self.assertEqual(m.recovery_problem, "damaged")
        self.assertIsNone(self.journal)
        self.write_journal("not a list")  # (JSON, but not a journal: damaged, as before)
        self.assertEqual(self.manager().recovery_problem, "damaged")
        self.write_journal([])
        self.assertIsNone(self.manager().recovery_problem, "a readable journal is no problem")

    def test_a_journal_of_a_run_that_ended_cleanly_is_never_recovered(self):
        # Left in a folder that was offline when that run ended (every window shown then): a window
        # hidden since by its own app must stay hidden
        from src import datafolder
        a = self.win.add("A", visible=False, pid=77, cls="X")
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": self.win.tick - 1000, "Writer": "old-run",
                       "Windows": [{"Handle": a, "Pid": 77, "Class": "X", "Started": self.win.process_started(77)}]}, fh)
        with unittest.mock.patch.object(datafolder, "ended_run", side_effect=lambda w: set() if w == "old-run" else None):
            self.manager()
        self.assertFalse(self.visible(a), "its app hid it: stays hidden")
        self.assertIsNone(self.journal, "and the old journal is deleted")

    def test_a_run_that_ends_with_every_window_shown_is_recorded(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        m._fallback_allowed = True  # (the real app's)
        with unittest.mock.patch.object(datafolder, "add_ended_run") as ended:
            m.end()
        ended.assert_called_once_with(m._writer, [])

    def test_only_the_windows_a_run_could_not_show_at_its_end_are_shown_from_its_journals(self):
        # Its journals (wherever, maybe in a folder offline at exit) list every window it hid; a
        # start shows only those left hidden at its end, never the others (their apps may hide them)
        from src import datafolder
        a = self.win.add("A")
        b = self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # A, B hidden, on record
        m._fallback_allowed = True
        full = read_text(os.path.join(self.dir, JOURNAL_FILE))
        real = self.win.show_window_safe
        self.win.show_window_safe = lambda h, cmd: False if h == a else real(h, cmd)
        self.win.show_by_placement = lambda h, wp: None
        ended = {}
        with unittest.mock.patch.object(datafolder, "add_ended_run",
                                        side_effect=lambda w, hs: ended.update({w: set(hs)}) or True):
            m.end()
        self.assertEqual(ended, {m._writer: {a}})
        self.win.show_window_safe = real
        self.win.show_window(b, N.SW_HIDE)  # (B's app hides it)
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            fh.write(full)  # (an old copy listing both, as one left in an offline folder would)
        with unittest.mock.patch.object(datafolder, "ended_run", side_effect=lambda w: ended.get(w)):
            self.manager()  # the next start
        self.assertTrue(self.visible(a), "left hidden at the end: shown")
        self.assertFalse(self.visible(b), "shown at the end, hidden by its app since: left alone")

    def test_windows_left_hidden_keep_every_journal_and_the_run_is_recorded_with_them(self):
        from src import datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._fallback_allowed = True
        stale = os.path.join(self.dir, "old", JOURNAL_FILE)
        m._stale_journals.append(stale)
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None
        m._write_records = lambda handles, at_exit=False: False  # (nowhere to write it)
        with unittest.mock.patch.object(datafolder, "add_ended_run", return_value=True) as ended, \
                unittest.mock.patch.object(datafolder, "forget_left_journal") as forget:
            m.end()
        ended.assert_called_once_with(m._writer, [a])
        self.assertNotIn(unittest.mock.call(stale), forget.call_args_list, "it may be the only record left")

    def test_the_last_resort_end_records_what_is_still_hidden_then(self):
        from src import datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._fallback_allowed = True
        real = self.win.show_window_safe
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None
        with unittest.mock.patch.object(datafolder, "add_ended_run", return_value=True) as ended:
            m.end()
            self.win.show_window_safe = real
            m.end()  # (the last-resort exit: shown now)
        self.assertEqual(ended.call_args_list, [unittest.mock.call(m._writer, [a]), unittest.mock.call(m._writer, [])])
        self.assertTrue(self.visible(a))
        self.assertIsNone(self.journal, "everything shown: no record outlives the run")

    def test_a_window_restore_all_alone_could_not_show_stays_on_record_while_managing_goes_on(self):
        a = self.win.add("A")
        b = self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # A, B hidden
        real = self.win.show_window_safe
        self.win.show_window_safe = lambda h, cmd: False if h == a else real(h, cmd)
        self.win.show_by_placement = lambda h, wp: None
        m.restore_all()
        m.sync()
        self.assertIn(a, {r["Handle"] for r in self.journal["Windows"]}, "still on record")
        self.win.show_window_safe = real
        m.restore_all()
        self.assertTrue(self.visible(a), "tried again")
        self.assertIsNone(self.journal)

    def test_ours_given_up_earlier_and_in_use_again_is_never_erased_at_exit(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._given_up.append(m._state_file)  # (left earlier, moved back since)
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None
        m.end()
        self.assertEqual({r["Handle"] for r in self.journal["Windows"]}, {a}, "the exit record stays")

    def test_an_error_bringing_ours_down_at_exit_still_keeps_the_windows_left_hidden_on_record(self):
        from src import datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._fallback_allowed = True
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None

        def boom(handles, at_exit=False):
            raise OSError("ctypes")
        m._write_records = boom
        with unittest.mock.patch.object(datafolder, "add_ended_run", return_value=True) as ended:
            m.end()
        ended.assert_called_once_with(m._writer, [a])
        self.assertIn(a, {r["Handle"] for r in self.journal["Windows"]}, "the journal as it was")

    def test_a_restore_without_end_forgets_a_stale_journal_it_could_not_erase(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        m._fallback_allowed = True
        stale = os.path.join(self.dir, "old", JOURNAL_FILE)
        m._stale_journals.append(stale)
        m._erase_journal = lambda path: False
        with unittest.mock.patch.object(datafolder, "forget_left_journal") as forget:
            m.restore_all()  # (the last-resort exit, end() never ran: nothing records the run)
        self.assertIn(unittest.mock.call(stale), forget.call_args_list)

    def test_a_stale_journal_kept_for_ended_runs_is_forgotten_if_recording_fails(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        m._fallback_allowed = True
        stale = os.path.join(self.dir, "old", JOURNAL_FILE)
        m._stale_journals.append(stale)
        m._erase_journal = lambda path: False
        with unittest.mock.patch.object(datafolder, "add_ended_run", return_value=False), \
                unittest.mock.patch.object(datafolder, "forget_left_journal") as forget:
            m.end()
        self.assertIn(unittest.mock.call(stale), forget.call_args_list)

    def test_a_window_whose_show_is_refused_quietly_is_the_only_one_kept_on_record(self):
        from src import datafolder
        a = self.win.add("A")
        b = self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # A and B hidden, on record
        m._fallback_allowed = True
        real = self.win.show_window_safe
        self.win.show_window_safe = lambda h, cmd: False if h == a else real(h, cmd)  # (refused, no error)
        self.win.show_by_placement = lambda h, wp: None
        with unittest.mock.patch.object(datafolder, "add_ended_run", return_value=True):
            m.end()
        self.assertTrue(self.visible(b))
        self.assertEqual({r["Handle"] for r in self.journal["Windows"]}, {a}, "only A: B was shown")

    def test_a_later_restore_with_every_window_shown_erases_the_exit_record(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        real = self.win.show_window_safe
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None
        m.restore_all()
        self.assertIsNotNone(self.journal)
        self.win.show_window_safe = real
        m._hidden[a] = None  # (tracked again, then shown)
        m.restore_all()
        self.assertIsNone(self.journal, "everything shown: no record outlives the run")

    def test_a_stale_journal_that_could_not_be_erased_at_exit_stays_registered(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        stale = os.path.join(self.dir, "old", JOURNAL_FILE)
        m._stale_journals.append(stale)
        m._erase_journal = lambda path: False
        m._fallback_allowed = True  # (the real app's: the run is recorded as ended, so a start skips it)
        with unittest.mock.patch.object(datafolder, "forget_left_journal") as forget, \
                unittest.mock.patch.object(datafolder, "add_ended_run"):
            m.end()
        self.assertNotIn(unittest.mock.call(stale), forget.call_args_list)

    def test_a_stale_journal_left_at_exit_is_forgotten_when_the_run_is_not_recorded_as_ended(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        stale = os.path.join(self.dir, "old", JOURNAL_FILE)
        m._stale_journals.append(stale)
        m._erase_journal = lambda path: False
        with unittest.mock.patch.object(datafolder, "forget_left_journal") as forget:
            m.end()  # (not the real data folder: never recorded as ended)
        self.assertIn(unittest.mock.call(stale), forget.call_args_list)

    def test_exit_never_writes_to_a_folder_that_may_be_offline(self):
        from src import paths
        from src import desktop_manager as DM
        self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None
        with unittest.mock.patch.object(paths, "offline", return_value=True), \
                unittest.mock.patch.object(DM, "write_json", side_effect=AssertionError("waited on the share")):
            m.end()

    def test_a_second_restore_keeps_the_record_of_a_window_that_could_not_be_shown(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        self.win.show_window_safe = lambda h, cmd: False
        self.win.show_by_placement = lambda h, wp: None
        m.end()
        m.restore_all()  # (the last-resort exit runs it again)
        self.assertEqual({r["Handle"] for r in self.journal["Windows"]}, {a})

    def test_a_journal_that_could_not_be_erased_stays_looked_for(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        stale = os.path.join(self.dir, "old", JOURNAL_FILE)
        m._given_up.append(stale)
        m._erase_journal = lambda path: False
        with unittest.mock.patch.object(datafolder, "forget_left_journal") as forget:
            m.end()
        self.assertNotIn(unittest.mock.call(stale), forget.call_args_list)

    def test_a_folder_gone_from_a_local_disk_holds_nothing_saved(self):
        # Not "in use" nor "can't be reached": the drive answers, so nothing is saved there, and a
        # save makes the folder again
        from src.layout import WindowLayout
        from src.last_shown import LastShown
        gone = os.path.join(self.dir, "gone", "deeper")
        lay = WindowLayout(gone, self.win)
        self.assertEqual(lay.read_problem(), 0)
        self.assertFalse(LastShown(gone)._unread)

    def test_a_journal_given_up_and_erased_at_exit_is_no_longer_looked_for(self):
        from src import datafolder
        m = self.manager()
        m.sync()
        other = os.path.join(self.dir, "old")
        os.makedirs(other)
        stale = os.path.join(other, JOURNAL_FILE)
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[]}')
        m._given_up.append(stale)
        with unittest.mock.patch.object(datafolder, "forget_left_journal") as forget:
            m.end()
        self.assertFalse(os.path.exists(stale))
        self.assertIn(unittest.mock.call(stale), forget.call_args_list)


class RestoreByHand(Base):
    """Settings > Restore Hidden Windows: the user picks among the hidden windows."""

    def test_the_list_has_hidden_windows_but_not_our_desktops_or_untitled_ones(self):
        lost = self.win.add("Lost editor", visible=False)        # e.g. hidden by a run that crashed
        tray_app = self.win.add("Chat", visible=False)            # hidden by its own app
        self.win.add("", visible=False)                           # untitled helper
        self.win.add("Shell", visible=False, cls="Progman")
        self.win.add("Own", visible=False, pid=os.getpid())
        on_desktop = self.win.add("Mine")
        m = self.manager()
        m.sync()
        self.new_desktop(m)                                       # on_desktop hidden by us
        self.assertFalse(self.visible(on_desktop))
        self.assertEqual(m.hidden_windows(), [tray_app, lost], "sorted by title; ours come back with their desktop")

    def test_a_window_whose_show_is_refused_stays_ours_and_in_the_journal(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # (A hidden by us)
        self.assertIn(a, m.hidden)
        real = self.win.show_window
        self.win.show_window = lambda h, cmd: False if h == a and cmd != N.SW_HIDE else real(h, cmd)
        m.switch_relative(-1)  # (back: its show does not take)
        m.sync()
        self.assertFalse(self.visible(a))
        self.assertIn(a, m.hidden, "never taken for a window its own app hid")
        self.assertIn(a, [r["Handle"] for r in self.journal["Windows"]], "kept in the journal")
        self.win.show_window = real  # (its app answers again)
        m.sync()
        self.assertTrue(self.visible(a), "the next sync shows it (it should be on screen)")
        self.assertNotIn(a, m.hidden)

    def test_a_refused_floating_window_goes_with_its_main_window_never_alone(self):
        main, pal = self.win.add("Main"), self.win.add("Palette")
        m = self.manager()
        m.sync()
        m._hidden[pal] = None
        m._refused[pal] = [0, 0]
        m._refused_main[pal] = main
        m._hidden[main] = None  # (its main window hidden again: on another desktop)
        self.win.windows[pal].visible = False
        calls = len(self.win.calls)
        m._retry_refused_shows()
        self.assertNotIn(("show", pal, N.SW_SHOWNA), self.win.calls[calls:], "never shown on its own")
        self.assertIn(pal, m._floating_hidden[main], "it comes back with its main window")
        self.assertNotIn(pal, m._refused)

    def test_a_refused_window_restored_by_hand_is_no_longer_ours(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        real = self.win.show_window
        self.win.show_window = lambda h, cmd: False if h == a and cmd != N.SW_HIDE else real(h, cmd)
        m.switch_relative(-1)
        self.assertIn(a, m._refused)
        self.win.show_window = real
        self.assertEqual(m.restore_windows([a]), 1)
        self.assertNotIn(a, m._refused)
        self.assertNotIn(a, m.hidden)

    def test_restore_never_counts_a_window_still_hidden(self):
        a = self.win.add("A", visible=False)
        m = self.manager()
        real = self.win.show_window
        self.win.show_window = lambda h, cmd: False if h == a else real(h, cmd)
        self.assertEqual(m.restore_windows([a]), 0)

    def test_a_switch_never_focuses_a_window_whose_show_was_refused(self):
        a, b = self.win.add("A"), self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # (A and B hidden on desktop 1)
        real = self.win.show_window
        self.win.show_window = lambda h, cmd: False if h == a and cmd != N.SW_HIDE else real(h, cmd)
        st = next(iter(m._monitors.values()))
        st.last_active[0] = a
        focused = []
        self.win.set_foreground = focused.append
        m.switch_relative(-1)
        self.assertNotIn(a, focused, "never the invisible one")
        self.assertEqual(focused, [b])

    def test_a_hidden_window_on_no_desktop_is_shown_by_the_next_sync(self):
        # (its monitor gone while its show was refused: on no desktop, still ours)
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        real = self.win.show_window
        self.win.show_window = lambda h, cmd: False if h == a and cmd != N.SW_HIDE else real(h, cmd)
        m.switch_relative(-1)  # (refused)
        for st in m._monitors.values():  # (then its monitor goes: A on no desktop)
            for s in st.desktops:
                s.pop(a, None)
        self.win.show_window = real
        m.sync()
        self.assertTrue(self.visible(a))
        self.assertNotIn(a, m.hidden)

    def test_a_show_its_app_keeps_refusing_is_never_given_up(self):
        from src import desktop_manager as DM
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        real = self.win.show_window

        def refused(h, cmd):
            if h == a and cmd != N.SW_HIDE:
                self.win.calls.append(("show", h, cmd))
                return False
            return real(h, cmd)
        self.win.show_window = refused
        m.switch_relative(-1)
        for _ in range(30):  # (many syncs at once, e.g. the Overview's: time decides, never their count)
            m.sync()
        self.assertIn(a, m.hidden, "tried again for a while")
        shows = [c for c in self.win.calls if c[0] == "show" and c[1] == a and c[2] != N.SW_HIDE]
        self.assertGreater(len(shows), 2)
        self.assertTrue(all(c[2] == N.SW_SHOWNA for c in shows[1:]), "retries never activate it")
        self.assertNotIn(a, m.hidden_windows(), "not listed yet: still being tried")
        self.win.advance(DM.REFUSED_SHOW_MS)
        m.sync()
        before = len(self.win.calls)
        for _ in range(30):
            m.sync()
        self.assertEqual([c for c in self.win.calls[before:] if c[1] == a], [], "then only now and then")
        self.assertIn(a, m.hidden, "still ours: never given up")
        self.assertIn(a, [r["Handle"] for r in self.journal["Windows"]], "and in the journal")
        self.assertIn(a, m.hidden_windows(), "listed by Restore Hidden Windows")
        self.win.advance(DM.REFUSED_RETRY_MS)
        m.sync()
        self.assertTrue(any(c[1] == a for c in self.win.calls[before:]), "tried again later")
        self.win.show_window = real
        m.end()
        self.assertTrue(self.visible(a), "exit shows it")

    def test_crash_record_problems_are_reported_until_restore_or_a_move(self):
        with open(os.path.join(self.dir, JOURNAL_FILE), "w") as fh:
            fh.write("{damaged")
        m = self.manager()
        self.assertEqual(list(m.journal_problems), [os.path.join(self.dir, JOURNAL_FILE)])
        m.journal_problems["Q:\\x.later.json"] = "unread"
        m.restore_windows([])  # (Restore Hidden Windows used)
        self.assertEqual(m.journal_problems, {"Q:\\x.later.json": "unread"}, "dealt with; one still unread stays")
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        m.relocate(other)
        self.assertEqual(m.journal_problems, {}, "another folder: its own are reported")

    def test_restored_windows_are_shown_on_desktop_1(self):
        lost = self.win.add("Lost", visible=False)
        b = self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)                                       # desktop 2 shown
        m.restore_windows([lost])
        self.assertTrue(self.visible(lost))
        self.assertIn(lost, m.desktops_of(D1)[0])
        self.assertEqual(m.current_of(D1), 0, "desktop 1 is shown, so it is on screen")
        self.assertTrue(self.visible(b))

    def test_a_restored_window_that_desktops_dont_manage_is_just_shown(self):
        tool = self.win.add("Palette", visible=False, exstyle=N.WS_EX_TOOLWINDOW)
        m = self.manager()
        m.sync()
        m.restore_windows([tool])
        self.assertTrue(self.visible(tool))
        self.assertFalse(any(tool in s for s in m.desktops_of(D1)))


class NeverShownOnTheWay(Base):
    def shows(self, h, since):
        return [c for c in self.win.calls[since:]
                if c[1] == h and ((c[0] == "show" and c[2] != N.SW_HIDE) or (c[0] == "placement" and c[2] != N.SW_HIDE))]

    def test_load_onto_a_desktop_not_shown_keeps_it_hidden_and_maximizes_it_when_shown(self):
        a = self.win.add("A", rect=(10, 10, 510, 510))
        b = self.win.add("B")
        m = self.manager()
        m.sync()
        self.win.show_window(a, N.SW_MAXIMIZE)
        self.win.foreground = a
        from moving import move_active
        move_active(m, self.win, +1)             # a alone on desktop 2 (shown)
        m.save_desktops()                        # saved: a maximized on desktop 2, desktop 2 shown
        move_active(m, self.win, -1)             # a back on desktop 1 with b
        self.win.show_window(a, N.SW_RESTORE)
        m.switch_to(D1, 1)                       # desktop 2 (empty) shown: a and b hidden
        self.win.cursor_device = D1
        with open(self.layout_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        saved["Showing"] = {}                    # Load must not switch: a lands on a desktop not shown
        for e in saved["Windows"]:
            e["Desktop"] = 2
        with open(self.layout_file, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        m2 = self.manager()
        m2.sync()
        since = len(self.win.calls)
        m2._layout._load_file()
        m2.load_desktops()
        self.assertFalse(self.visible(a))
        self.assertEqual(self.shows(a, since), [], "not shown (nor activated) on the way")
        m2.switch_to(D1, 2)
        self.assertTrue(self.visible(a) and self.win.is_zoomed(a), "maximized as saved once its desktop shows")

    def test_an_unplugged_display_never_shows_a_window_its_app_hid(self):
        a = self.win.add("Chat", 1)
        m = self.manager()
        m.sync()
        self.win.windows[a].visible = False       # closed to tray, then the display is unplugged
        since = len(self.win.calls)
        del self.win.screen_list[1]
        m.sync()
        self.assertFalse(self.visible(a))
        self.assertEqual(self.shows(a, since), [])
        self.assertFalse(any(a in s for s in m.desktops_of(D1)))

    def test_an_unplugged_display_keeps_restore_to_maximized(self):
        a = self.win.add("A", 1, rect=(2000, 100, 2800, 700), show_cmd=2)
        self.win.windows[a].flags = N.WPF_RESTORETOMAXIMIZED  # minimized from maximized
        m = self.manager()
        m.sync()
        del self.win.screen_list[1]
        m.sync()
        self.assertTrue(self.win.windows[a].flags & N.WPF_RESTORETOMAXIMIZED, "still restores maximized")

    def test_floating_windows_move_with_their_window(self):
        main = self.win.add("Editor", 0, rect=(100, 100, 900, 700))
        palette = self.win.add("Tools", 0, rect=(950, 100, 1150, 400), anchor=main, exstyle=N.WS_EX_TOOLWINDOW)
        m = self.manager()
        m.sync()
        m.move_window_to_desktop(main, D2, 0)
        self.assertEqual(self.win.monitor_device_of_window(main), D2)
        self.assertEqual(self.win.monitor_device_of_window(palette), D2, "the palette went too")
        self.win.cursor_device = D1
        self.new_desktop(m)
        hidden_main = self.win.add("Editor 2", 0, rect=(100, 100, 900, 700))
        m.sync()
        palette2 = self.win.add("Tools 2", 0, rect=(950, 100, 1150, 400), anchor=hidden_main,
                                exstyle=N.WS_EX_TOOLWINDOW)
        m.switch_to(D1, 0)                        # hidden_main + palette2 hidden together
        self.assertFalse(self.visible(palette2))
        m.move_window_to_desktop(hidden_main, D2, 0)
        self.assertEqual(self.win.monitor_device_of_window(palette2), D2, "a hidden palette goes too")


class PlacedWhileHidden(Base):
    """Fourth review: a window placed while hidden gets its saved state, on the monitor it was placed
    on, when it is shown, and a state kept for later never outlives the reason for it."""

    def saved_layout(self, desktop, monitor_id, show_cmd, rect):
        with open(self.layout_file, "w", encoding="utf-8") as fh:
            json.dump({"Windows": [{"Exe": r"C:\Apps\app.exe", "Class": "AppWindow",
                                    "TitleHash": __import__("src.layout", fromlist=["title_hash"]).title_hash("A"),
                                    "MonitorId": monitor_id, "Desktop": desktop, "WorkLeft": 1920 if monitor_id == "MON-B" else 0,
                                    "WorkTop": 0, "ShowCmd": show_cmd, "Left": rect[0], "Top": rect[1],
                                    "Right": rect[2], "Bottom": rect[3]}], "Showing": {}}, fh)

    def test_a_hidden_maximized_window_placed_on_another_monitor_shows_maximized_there(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        self.win.show_window(a, N.SW_MAXIMIZE)
        m = self.manager()
        m.sync()
        self.win.cursor_device = D2
        self.new_desktop(m)  # D2 shows its desktop 2
        self.win.cursor_device = D1
        self.new_desktop(m)  # a hidden (maximized) on D1's desktop 1
        self.saved_layout(0, "MON-B", N.SW_SHOWMAXIMIZED, (2000, 100, 2800, 700))  # D2's desktop 1, not shown
        m._layout._load_file()
        m.load_desktops()
        self.assertFalse(self.visible(a))
        m.switch_to(D2, 0)
        self.assertTrue(self.visible(a) and self.win.is_zoomed(a))
        self.assertEqual(self.win.monitor_device_of_window(a), D2, "maximized on the monitor it was placed on")

    def test_load_onto_the_shown_desktop_keeps_the_saved_maximized_state(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # a hidden on desktop 1; desktop 2 shown
        self.saved_layout(1, "MON-A", N.SW_SHOWMAXIMIZED, (100, 100, 900, 700))  # onto desktop 2 (shown)
        m._layout._load_file()
        m.load_desktops()
        self.assertTrue(self.visible(a) and self.win.is_zoomed(a), "shown maximized as saved")

    def test_a_saved_normal_state_restores_a_window_hidden_maximized(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        self.win.show_window(a, N.SW_MAXIMIZE)
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        self.new_desktop(m)  # desktop 3 shown, a hidden maximized on desktop 1
        self.saved_layout(1, "MON-A", 1, (200, 200, 800, 600))  # (1 = SW_SHOWNORMAL)
        m._layout._load_file()
        m.load_desktops()
        m.switch_to(D1, 1)
        self.assertTrue(self.visible(a) and not self.win.is_zoomed(a), "normal, as saved")
        self.assertEqual(self.win.windows[a].rect, (200, 200, 800, 600))

    def test_a_state_kept_for_later_is_dropped_when_its_app_shows_the_window(self):
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        self.saved_layout(0, "MON-A", N.SW_SHOWMAXIMIZED, (100, 100, 900, 700))
        m._layout._load_file()
        with mock_no_switch(m):
            m.load_desktops()
        self.assertIn(a, m._show_later)
        self.win.windows[a].visible = True  # its app shows it itself
        m.sync()
        self.assertNotIn(a, m._show_later)


class FifthReview(Base):
    def placed_hidden(self):
        """A window placed (by Load) on a desktop that is not shown, with a saved state for later."""
        a = self.win.add("A", 0, rect=(100, 100, 900, 700))
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        PlacedWhileHidden.saved_layout(self, 0, "MON-A", N.SW_SHOWMAXIMIZED, (100, 100, 900, 700))
        m._layout._load_file()
        with mock_no_switch(m):
            m.load_desktops()
        self.assertIn(a, m._show_later)
        return m, a

    def test_a_placement_that_fails_still_shows_the_window(self):
        m, a = self.placed_hidden()
        self.win.placement_fails = True
        m.end()  # exit
        self.assertTrue(self.visible(a), "shown plainly: never left hidden")

    def test_a_show_that_raises_at_exit_is_retried_plainly(self):
        m, a = self.placed_hidden()
        real = m._show_if_ours
        m._show_if_ours = lambda h: (_ for _ in ()).throw(RuntimeError("placement crashed"))
        m.end()
        m._show_if_ours = real
        self.assertTrue(self.visible(a))

    def test_a_bad_record_does_not_stop_the_others(self):
        a = self.win.add("A", visible=False, pid=77, cls="X")
        with open(os.path.join(self.dir, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": self.win.tick - 1000, "Windows": [
                {"Handle": "not a handle", "Pid": 1, "Class": "Y"}, {"Handle": a, "Pid": 77, "Class": "X"}]}, fh)
        real = self.win.is_window
        self.win.is_window = lambda h: real(h) if isinstance(h, int) else (_ for _ in ()).throw(
            __import__("ctypes").ArgumentError("wrong type"))  # (what ctypes raises for a bad handle)
        m = self.manager()
        self.assertTrue(self.visible(a), "the good record after it is recovered")
        self.assertEqual(m.recovery_problem, "damaged", "and the user is told")

    def test_moving_onto_a_folder_reads_its_crash_journal_first(self):
        a = self.win.add("A", 0)
        lost = self.win.add("Lost", 0, visible=False, pid=4242, cls="L")  # hidden by a run that crashed
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # a hidden by this run
        other = os.path.join(self.dir, "X")
        os.makedirs(other)
        with open(os.path.join(other, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            json.dump({"UptimeMs": self.win.tick - 1000, "Windows": [
                {"Handle": lost, "Pid": 4242, "Class": "L"}, {"Handle": a, "Pid": self.win.get_pid(a), "Class": "AppWindow"}]}, fh)
        m.relocate(other)
        self.assertTrue(self.visible(lost), "the crashed run's window is shown")
        self.assertFalse(self.visible(a), "this run's hidden window stays on its desktop")
        with open(os.path.join(other, JOURNAL_FILE), encoding="utf-8") as fh:
            self.assertEqual([r["Handle"] for r in json.load(fh)["Windows"]], [a], "then our journal is written")

    def test_a_save_that_was_not_written_changes_nothing(self):
        a = self.win.add("A", rect=(10, 10, 510, 510))
        m = self.manager()
        m.sync()
        self.assertGreater(m.save_desktops(), 0)
        saved = [e.to_json() for e in m._layout._entries]
        self.win.windows[a].rect = (300, 300, 800, 800)
        with unittest.mock.patch("src.layout.write_json", return_value=False):
            self.assertEqual(m.save_desktops(), -1)
        self.assertEqual([e.to_json() for e in m._layout._entries], saved, "Load still uses the last real save")


class SixthReview(Base):
    def test_a_journal_removal_skipped_during_a_pause_is_done_later(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)                      # a hidden: journal written
        self.assertIsNotNone(self.journal)
        paths.note_failure(self.dir, gone=True)  # the folder is paused (offline a moment ago)
        self.addCleanup(paths.forget_failures)
        m.switch_relative(-1)                    # nothing hidden: the journal should go, but can't now
        self.assertIsNotNone(self.journal)
        paths.forget_failures()                  # back
        m.sync()
        self.assertIsNone(self.journal, "removed once the folder works: no stale record left behind")

    def test_reading_back_this_runs_own_journal_after_a_move_shows_nothing(self):
        a = self.win.add("Chat", 0)
        m = self.manager()
        m.sync()
        self.new_desktop(m)                      # Chat hidden by us: journal lists it
        with open(os.path.join(self.dir, JOURNAL_FILE), encoding="utf-8") as fh:
            ours = fh.read()
        m.switch_relative(-1)                    # shown again
        self.win.windows[a].visible = False      # then its app closes it to the tray
        m.sync()
        other = os.path.join(self.dir, "X")
        os.makedirs(other)
        with open(os.path.join(other, JOURNAL_FILE), "w", encoding="utf-8") as fh:
            fh.write(ours)                       # the folder still holds this run's out-of-date journal
        m.relocate(other)
        self.assertFalse(self.visible(a), "a window its app hid is never shown")

    def test_an_earlier_damaged_journal_is_not_reported_again_after_a_move(self):
        with open(os.path.join(self.dir, JOURNAL_FILE), "w") as fh:
            fh.write('{"Windows": "not a list"}')
        m = self.manager()
        self.assertEqual(m.recovery_problem, "damaged")
        other = os.path.join(self.dir, "clean")
        os.makedirs(other)
        m.relocate(other)
        self.assertIsNone(m.recovery_problem)

    def test_relocate_says_whether_its_journal_was_written(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        good = os.path.join(self.dir, "good")
        os.makedirs(good)
        self.assertTrue(m.relocate(good))
        with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
            self.assertFalse(m.relocate(os.path.join(self.dir, "locked")))

    def test_clear_desktops_whose_write_failed_is_written_later(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.save_desktops()
        with unittest.mock.patch("src.layout.write_json", return_value=False):
            m.clear_desktops()
        with open(self.layout_file, encoding="utf-8") as fh:
            self.assertNotEqual(json.load(fh)["Windows"], [], "(not written yet)")
        m.sync()
        with open(self.layout_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["Windows"], [], "written by the next sync: the save never comes back")


class UnreadableAtStart(Base):
    """A data file there but not readable for a moment (held at sign-in) is never written over."""

    def setUp(self):
        super().setUp()
        from src import paths
        self.addCleanup(paths.forget_failures)

    def test_a_crash_journal_held_at_start_is_kept_and_recovered_later(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        self.assertFalse(self.visible(a))
        m._ended = True  # (killed: the journal stays, a stays hidden)
        journal = os.path.join(self.dir, JOURNAL_FILE)
        with self.held(JOURNAL_FILE):
            m2 = self.manager()
        self.assertIsNone(m2.recovery_problem, "held is not damaged")
        self.assertFalse(self.visible(a))
        aside = [n for n in os.listdir(self.dir) if n.startswith(JOURNAL_FILE) and n.endswith(paths.LATER_SUFFIX)]
        self.assertEqual(len(aside), 1, "moved aside from ours, kept")
        paths._read_next.clear()
        m2.sync()
        self.assertTrue(self.visible(a), "read later: shown again")
        self.assertFalse(os.path.exists(os.path.join(self.dir, aside[0])))

    def test_a_crash_journal_that_cant_even_be_moved_aside_is_never_written_over(self):
        from src import paths
        from src import desktop_manager as DM
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True  # (killed)
        journal = os.path.join(self.dir, JOURNAL_FILE)
        crashed = read_text(journal)
        real_replace = os.replace

        def replace(src, dst):
            if os.path.normcase(src) == os.path.normcase(journal) or os.path.normcase(dst) == os.path.normcase(journal):
                raise PermissionError(13, "in use", None, 32)
            return real_replace(src, dst)
        with self.held(JOURNAL_FILE), unittest.mock.patch.object(DM.os, "replace", replace), \
                unittest.mock.patch("src.paths.os.replace", replace):
            m2 = self.manager()
            self.win.add("B")
            m2.sync()
            self.new_desktop(m2)  # our journal wants to be written there
            m2.switch_relative(-1)  # (and removed: nothing hidden)
        self.assertEqual(read_text(journal), crashed, "the crashed run's journal is intact")
        paths._read_next.clear()
        m2.sync()
        self.assertTrue(self.visible(a), "read once free: its window shown again")

    def test_a_window_left_out_of_the_journal_ahead_is_journaled_when_hidden_after_all(self):
        a, b = self.win.add("A"), self.win.add("B")
        m = self.manager()
        m.sync()
        self.win.windows[b].hung = True
        real_ahead = m._journal_ahead

        def ahead(extra):
            real_ahead(extra)
            self.win.windows[b].hung = False  # (responding again before its hide)
        m._journal_ahead = ahead
        seen = {}
        real = self.win.show_window

        def hide(h, cmd):
            if cmd == N.SW_HIDE:
                seen[h] = {r["Handle"] for r in self.journal["Windows"]}
            return real(h, cmd)
        self.win.show_window = hide
        self.new_desktop(m)
        self.assertIn(b, seen, "hidden after all")
        self.assertIn(b, seen[b], "and in the journal before its hide")

    def test_a_crashed_runs_unread_journal_survives_a_folder_move(self):
        from src import paths, datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True
        journal = os.path.join(self.dir, JOURNAL_FILE)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        real_replace = os.replace

        def replace(src, dst):
            if os.path.normcase(src) == os.path.normcase(journal):
                raise PermissionError(13, "in use", None, 32)
            return real_replace(src, dst)
        self.addCleanup(datafolder._store_left, [])
        with self.held(JOURNAL_FILE), unittest.mock.patch.object(os, "replace", replace):
            m2 = self.manager()
            self.assertFalse(m2.relocate(other), "move_to must keep the old journal")
            paths._read_next.clear()
            m2.sync()
        self.assertTrue(os.path.exists(journal), "never deleted as ours")
        self.assertIn(journal, datafolder._left_journals(), "listed for the next start")

    def test_a_late_journal_read_never_shows_a_window_its_app_hid_since(self):
        from src import paths
        b = self.win.add("B")
        m = self.manager()
        m.sync()
        m._journal_ahead({b})  # (a crashed run journaled B ahead, then was killed before hiding it)
        m._ended = True
        with self.held(JOURNAL_FILE):
            m2 = self.manager()
            m2.sync()  # B adopted, visible
        self.win.windows[b].visible = False  # its app closes it to the tray
        m2.sync()
        paths._read_next.clear()
        m2.sync()  # the late read
        self.assertFalse(self.visible(b), "never re-shown: its own app hid it")

    def test_a_read_journal_that_cant_be_deleted_is_emptied(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True  # (crashed)
        journal = os.path.join(self.dir, JOURNAL_FILE)
        real = os.remove

        def remove(p):
            if os.path.normcase(p) == os.path.normcase(journal):
                raise PermissionError(13, "in use", None, 32)
            return real(p)
        with unittest.mock.patch("src.desktop_manager.os.remove", remove):
            self.manager()
        self.assertTrue(self.visible(a), "recovered")
        with open(journal, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["Windows"], [], "emptied: a later start finds nothing to show")

    def test_a_failed_journal_ahead_is_tried_again_before_the_hide(self):
        from src import desktop_manager as DM
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        real = DM.write_json
        calls = []

        def flaky(path, data):
            calls.append(path)
            return False if len(calls) == 1 else real(path, data)  # (held for the first write only)
        seen = []
        real_show = self.win.show_window

        def hide(h, cmd):
            if cmd == N.SW_HIDE:
                seen.append({r["Handle"] for r in (self.journal or {"Windows": []})["Windows"]})
            return real_show(h, cmd)
        self.win.show_window = hide
        with unittest.mock.patch.object(DM, "write_json", flaky):
            self.new_desktop(m)
        self.assertTrue(seen and a in seen[0], "on record before its hide, after all")

    def test_while_the_folder_is_offline_ours_goes_to_the_default_folder(self):
        from src import paths, datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m._fallback_allowed = True  # (the real app's; never in tests otherwise)
        default = os.path.join(self.dir, "default")
        os.makedirs(default)
        self.addCleanup(paths.forget_failures)
        with unittest.mock.patch.object(datafolder, "default_folder", return_value=default):
            paths.note_failure(self.dir, gone=True)  # the folder in use drops offline
            self.new_desktop(m)
            fallback = os.path.join(default, JOURNAL_FILE)
            self.assertTrue(os.path.exists(fallback), "A is on record in the default folder")
            paths.forget_failures()  # back
            m.sync()
            self.assertFalse(os.path.exists(fallback), "deleted once ours is written where it belongs")
            self.assertIn(a, {r["Handle"] for r in self.journal["Windows"]})

    def test_moving_to_the_default_folder_keeps_the_journal_written_there(self):
        from src import paths, datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m._fallback_allowed = True
        default = os.path.join(self.dir, "default")
        os.makedirs(default)
        self.addCleanup(paths.forget_failures)
        with unittest.mock.patch.object(datafolder, "default_folder", return_value=default):
            paths.note_failure(self.dir, gone=True)
            self.new_desktop(m)  # fallback written in the default folder
            paths.forget_failures()
            m.relocate(default)  # the user picks Default
            m.sync()
        self.assertTrue(os.path.exists(os.path.join(default, JOURNAL_FILE)), "ours there, kept")

    def test_a_shown_window_leaves_the_fallback_at_once(self):
        from src import paths, datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m._fallback_allowed = True
        default = os.path.join(self.dir, "default")
        os.makedirs(default)
        self.addCleanup(paths.forget_failures)
        with unittest.mock.patch.object(datafolder, "default_folder", return_value=default):
            paths.note_failure(self.dir, gone=True)
            self.new_desktop(m)
            m.switch_relative(-1)  # A shown again while the folder is still offline
            self.assertFalse(os.path.exists(os.path.join(default, JOURNAL_FILE)), "no fallback listing a shown window")
            m.switch_relative(1)  # A hidden again, the folder still offline
            with open(os.path.join(default, JOURNAL_FILE), encoding="utf-8") as fh:
                self.assertIn(a, {r["Handle"] for r in json.load(fh)["Windows"]}, "on record again")

    def test_a_move_keeps_the_old_journal_while_ours_is_only_in_the_fallback(self):
        from src import paths, datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m._fallback_allowed = True
        self.new_desktop(m)  # A hidden, ours in self.dir (which is the "default" folder here)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        self.addCleanup(paths.forget_failures)
        real = paths.write_json
        with unittest.mock.patch.object(datafolder, "default_folder", return_value=self.dir), \
                unittest.mock.patch("src.desktop_manager.write_json",
                                    side_effect=lambda p, d: False if p.startswith(other) else real(p, d)):
            self.assertFalse(m.relocate(other), "ours is only in the default folder's file: move_to keeps it")
        self.assertTrue(os.path.exists(os.path.join(self.dir, JOURNAL_FILE)))

    def test_an_unexpected_error_never_deletes_an_unread_journal(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True  # (crashed)
        journal = os.path.join(self.dir, JOURNAL_FILE)
        with unittest.mock.patch.object(paths, "offline", side_effect=RuntimeError("can't start a thread")):
            self.manager()
        self.assertTrue(os.path.exists(journal) or any(n.endswith(paths.LATER_SUFFIX) for n in os.listdir(self.dir)),
                        "kept for a later read")

    def test_an_old_journal_is_retried_while_nothing_is_hidden(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        old = os.path.join(self.dir, JOURNAL_FILE)
        with open(old, "w", encoding="utf-8") as fh:
            fh.write('{"Windows":[]}')
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        real = os.remove
        held = [1, 1]

        def remove(p):
            if os.path.normcase(p) == os.path.normcase(old) and held:
                held.pop()
                raise PermissionError(13, "in use", None, 32)
            return real(p)
        from src import paths
        self.addCleanup(paths.forget_failures)
        with unittest.mock.patch("src.desktop_manager.os.remove", remove):
            m.relocate(other)
            m.sync()
            self.assertTrue(os.path.exists(old), "(held: not tried again on the very next sync)")
            for _ in range(3):
                paths.forget_failures()  # (each its pause later)
                m._stale_next.clear()  # (and its wait)
                m.sync()
        self.assertFalse(os.path.exists(old), "deleted on a later sync")

    def test_a_damaged_journal_found_late_is_told(self):
        from src import paths
        journal = os.path.join(self.dir, JOURNAL_FILE)
        with open(journal, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.held(JOURNAL_FILE):
            m = self.manager()
        self.assertFalse(m.late_problem)
        paths._read_next.clear()
        with unittest.mock.patch.object(paths, "on_unreadable", lambda p: None):
            m.sync()
        self.assertTrue(m.late_problem, "set aside (another name): no question covers it, told as damaged")

    def test_the_journal_ahead_leaves_out_windows_that_wont_be_hidden(self):
        a, b = self.win.add("A"), self.win.add("B")
        m = self.manager()
        m.sync()
        self.win.windows[b].hung = True
        seen = []
        real = self.win.show_window

        def hide(h, cmd):
            if cmd == N.SW_HIDE and not seen:
                seen.append({r["Handle"] for r in self.journal["Windows"]})
            return real(h, cmd)
        self.win.show_window = hide
        self.new_desktop(m)
        self.assertEqual(seen, [{a}], "the hung window is not listed")

    def held(self, name):
        real = open
        path = os.path.join(self.dir, name)
        busy = PermissionError(13, "in use", None, 32)
        return unittest.mock.patch("builtins.open", side_effect=lambda p, *a, **k: (_ for _ in ()).throw(busy)
                                   if os.path.normcase(str(p)) == os.path.normcase(path) else real(p, *a, **k))

    def test_places_held_at_start_are_not_overwritten_and_read_later(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.snapshot()
        before = read_text(os.path.join(self.dir, PLACES_FILE))
        self.win.add("B")
        with self.held(PLACES_FILE):
            m2 = self.manager()
            m2.sync()
            m2.snapshot()
        self.assertEqual(read_text(os.path.join(self.dir, PLACES_FILE)), before, "not written over")
        m2.snapshot()  # readable again: read, then written
        m2.snapshot()
        self.assertNotEqual(read_text(os.path.join(self.dir, PLACES_FILE)), before)

    def test_places_unreadable_at_start_then_gone_are_not_doubled(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.snapshot()
        with self.held(PLACES_FILE):
            m2 = self.manager()
            m2.sync()
        os.remove(os.path.join(self.dir, PLACES_FILE))  # (gone meanwhile)
        paths._read_next.clear()
        m2.snapshot()
        with open(os.path.join(self.dir, PLACES_FILE), encoding="utf-8") as fh:
            handles = [w["Handle"] for w in json.load(fh)["Windows"]]
        self.assertEqual(handles, [a], "this run's record once, not twice")

    def test_shown_desktops_read_late_are_brought_back(self):
        from src import paths
        a, b = self.win.add("A"), self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m.move_window_to_desktop(b, D1, 1)
        m.snapshot()
        m.end()
        m.restore_all()
        from src.paths import SHOWING_FILE
        with self.held(SHOWING_FILE):
            m2 = self.manager()
            m2.sync()
        paths._read_next.clear()
        m2.sync()  # (the flush reads it now)
        m2.sync()
        self.assertEqual(m2.current_of(D1), 1, "the desktop it showed")

    def test_ours_under_its_own_name_follows_a_move(self):
        from src import datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True
        journal = os.path.join(self.dir, JOURNAL_FILE)
        real_replace = os.replace

        def replace(src, dst):
            if os.path.normcase(src) == os.path.normcase(journal):
                raise PermissionError(13, "in use", None, 32)
            return real_replace(src, dst)
        self.addCleanup(datafolder._store_left, [])
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        b = self.win.add("B")
        with self.held(JOURNAL_FILE), unittest.mock.patch.object(os, "replace", replace):
            m2 = self.manager()
            m2.sync()
            self.new_desktop(m2)  # b hidden: ours goes under its own name
            alt = m2._alt_journal
            self.assertTrue(alt and os.path.exists(alt))
            with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
                m2.relocate(other)  # ours can't be written there yet
        self.assertIn(alt, datafolder._left_journals(), "listed for the next start")
        m2.sync()
        self.assertFalse(os.path.exists(alt), "deleted once ours is in the new folder")
        self.assertNotIn(alt, datafolder._left_journals())

    def test_applying_the_same_folder_again_keeps_ours_under_its_own_name(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True
        journal = os.path.join(self.dir, JOURNAL_FILE)
        real_replace = os.replace

        def replace(src, dst):
            if os.path.normcase(src) == os.path.normcase(journal):
                raise PermissionError(13, "in use", None, 32)
            return real_replace(src, dst)
        b = self.win.add("B")
        with self.held(JOURNAL_FILE), unittest.mock.patch.object(os, "replace", replace):
            m2 = self.manager()
            m2.sync()
            self.new_desktop(m2)
            alt = m2._alt_journal
            with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
                m2.relocate(self.dir)  # the same folder again; ours can't be rewritten
        self.assertTrue(os.path.exists(alt), "ours (B hidden) is still on record")

    def test_an_old_journal_held_once_is_still_erased_at_exit(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        old = os.path.join(self.dir, JOURNAL_FILE)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        real = os.remove
        held = [1]

        def remove(p):
            if os.path.normcase(p) == os.path.normcase(old) and held:
                held.pop()
                raise PermissionError(13, "in use", None, 32)
            return real(p)
        with unittest.mock.patch("src.desktop_manager.os.remove", remove):
            m.relocate(other)
        self.assertTrue(os.path.exists(old), "(held once)")
        m.end()
        m.restore_all()
        self.assertFalse(os.path.exists(old), "erased at exit")

    def test_a_move_recovers_journals_set_aside_in_the_new_folder(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m._ended = True  # (a crashed run)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        os.replace(os.path.join(self.dir, JOURNAL_FILE), os.path.join(other, f"{JOURNAL_FILE}.ab12cd34{paths.LATER_SUFFIX}"))
        m2 = self.manager()
        self.assertFalse(self.visible(a))
        m2.relocate(other)
        self.assertTrue(self.visible(a), "the set-aside journal there is read")

    def test_saved_desktops_held_at_start_refuse_a_save_until_read(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.save_desktops()
        before = read_text(self.layout_file)
        with self.held(LAYOUT_FILE):
            m2 = self.manager()
            m2.sync()
            from src.layout import NOT_READ_YET
            self.assertEqual(m2.save_desktops(), NOT_READ_YET, "not read: a save would drop the others")
            m2.sync()
        self.assertEqual(read_text(self.layout_file), before)
        self.assertGreaterEqual(m2.save_desktops(), 1, "read now: saves")

    def test_shown_desktops_held_at_start_are_not_overwritten(self):
        from src.paths import SHOWING_FILE
        from src.last_shown import LastShown
        path = os.path.join(self.dir, SHOWING_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"Showing": {"MON-A": 2, "MON-B": 1}}, fh)
        with self.held(SHOWING_FILE):
            ls = LastShown(self.dir)
            ls.set("MON-A", 3)
        self.assertEqual(json.loads(read_text(path))["Showing"], {"MON-A": 2, "MON-B": 1})
        from src import paths
        paths._read_next.clear()  # (READ_RETRY_S later)
        ls.flush()
        self.assertEqual(json.loads(read_text(path))["Showing"], {"MON-A": 3, "MON-B": 1},
                         "read, then this run's change written over it (nothing else lost)")


class EighthReview(Base):
    def test_a_clear_not_written_yet_survives_a_folder_move(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.save_desktops()
        with unittest.mock.patch("src.layout.write_json", return_value=False):
            m.clear_desktops()
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        shutil.copy(self.layout_file, other)  # (the move copied the old, uncleared save)
        m.relocate(other)
        with open(os.path.join(other, os.path.basename(self.layout_file)), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["Windows"], [], "the Clear is written there, not undone")

    def test_our_journal_left_in_the_old_folder_goes_once_ours_is_in_the_new_one(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        old = os.path.join(self.dir, JOURNAL_FILE)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
            self.assertFalse(m.relocate(other))
        self.assertTrue(os.path.exists(old), "kept while the new folder has none of ours")
        m.sync()
        self.assertTrue(os.path.exists(os.path.join(other, JOURNAL_FILE)))
        self.assertFalse(os.path.exists(old), "deleted once ours is written there")

    def test_our_journal_left_in_the_old_folder_goes_at_exit(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        old = os.path.join(self.dir, JOURNAL_FILE)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
            m.relocate(other)
            m.restore_all()  # exit before any journal was written in the new folder
        self.assertFalse(os.path.exists(old))

    def test_a_stale_journal_that_cant_be_deleted_pauses_its_folder_and_is_given_up(self):
        from src import paths
        from src import desktop_manager as DM
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        old = os.path.join(self.dir, JOURNAL_FILE)
        real = os.remove
        tries = []

        def remove(p):
            if p == old:
                tries.append(p)
                raise PermissionError(13, "in use", None, 32)
            return real(p)
        self.addCleanup(paths.forget_failures)
        with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
            m.relocate(other)
        with unittest.mock.patch.object(DM.os, "remove", remove):
            m.sync()  # journal written there: one try, then the old folder is paused
            self.assertEqual(len(tries), 1)
            self.assertTrue(paths.paused(self.dir))
            for _ in range(DM.STALE_TRIES + 2):
                paths.forget_failures()
                m._stale_next.clear()  # (each try's wait over)
                m._persisted = {0}
                m.sync()
        self.assertEqual(len(tries), DM.STALE_TRIES, "given up after STALE_TRIES")

    def test_a_failed_old_folders_journal_is_not_touched_while_running_but_erased_at_exit(self):
        from src import datafolder
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        old = os.path.join(self.dir, JOURNAL_FILE)
        real = os.remove
        with unittest.mock.patch.object(datafolder, "left_working", False), \
                unittest.mock.patch("src.desktop_manager.os.remove", side_effect=real) as remove:
            m.relocate(other)
            m.sync()
            self.assertFalse([c for c in remove.call_args_list if c.args[0] == old], "not while running")
            m.restore_all()
        self.assertFalse(os.path.exists(old), "the folder is back by exit: ours there is erased")

    def test_keeping_a_folders_own_data_without_saved_desktops_drops_the_old_save(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        m.save_desktops()
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        from src import datafolder
        with unittest.mock.patch.object(datafolder, "kept_new", True):
            m.relocate(other)
        self.assertFalse(os.path.exists(os.path.join(other, LAYOUT_FILE)), "nothing written into it")
        self.assertEqual(m._layout._entries, [])

    def test_an_unplugged_displays_hidden_palettes_go_to_the_main_display(self):
        main = self.win.add("Editor", 1, rect=(2000, 100, 2800, 700))
        palette = self.win.add("Tools", 1, rect=(2850, 100, 3050, 400), anchor=main, exstyle=N.WS_EX_TOOLWINDOW)
        m = self.manager()
        m.sync()
        self.win.cursor_device = D2
        self.new_desktop(m)
        self.assertFalse(self.visible(palette))
        del self.win.screen_list[1]
        m.sync()
        self.assertTrue(self.visible(main) and self.visible(palette))
        self.assertEqual(self.win.monitor_device_of_window(palette), D1, "the palette came along")

    def test_load_journals_its_hidden_windows_in_one_write(self):
        wins = [self.win.add(f"W{i}") for i in range(5)]
        m = self.manager()
        m.sync()
        m.save_desktops()
        for h in wins:
            m.move_window_to_desktop(h, D1, 0)
        saved = json.loads(read_text(self.layout_file))
        for e in saved["Windows"]:
            e["Desktop"] = 1
        with open(self.layout_file, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        m._layout._load_file()
        with unittest.mock.patch("src.desktop_manager.write_json", wraps=__import__("src.paths", fromlist=["x"]).write_json) as wj, \
                mock_no_switch(m):
            m.load_desktops()
        journal_writes = [c for c in wj.call_args_list if c.args[0].endswith(JOURNAL_FILE)]
        self.assertTrue(all(not self.visible(h) for h in wins))
        self.assertLessEqual(len(journal_writes), 2, "one ahead of the pass, one after (not one per window)")


class SeventhReview(Base):
    def test_the_journal_lists_a_desktop_before_any_of_it_is_hidden(self):
        wins = [self.win.add(f"W{i}") for i in range(4)]
        m = self.manager()
        m.sync()
        seen = []
        real = self.win.show_window

        def hide(h, cmd):
            if cmd == N.SW_HIDE:
                seen.append(set(r["Handle"] for r in (self.journal or {"Windows": []})["Windows"]))
            return real(h, cmd)
        self.win.show_window = hide
        self.new_desktop(m)
        self.assertTrue(seen and all(s >= set(wins) for s in seen),
                        "at every hide, the journal already lists the whole desktop (a hang + kill mid-switch)")

    def test_a_journal_delete_refused_for_a_moment_pauses_nothing(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        real = os.remove
        busy = [1]

        def remove(p):
            if p.endswith(JOURNAL_FILE) and busy:
                busy.pop()
                raise PermissionError(13, "in use", None, 32)
            return real(p)
        with unittest.mock.patch.object(os, "remove", remove):
            m.switch_relative(-1)  # nothing hidden: the delete is refused once
        self.assertFalse(paths.paused(self.dir), "the folder is not paused (the next journal must be written)")
        self.assertIsNotNone(self.journal)
        m.sync()
        self.assertIsNone(self.journal, "deleted on the next sync")

    def test_reading_back_our_own_journal_never_deletes_it(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)  # a hidden: our journal in self.dir
        with unittest.mock.patch("src.desktop_manager.write_json", return_value=False):
            self.assertFalse(m.relocate(self.dir))  # the same folder again; the rewrite fails
        self.assertIsNotNone(self.journal, "our journal is still there: the only record of the hidden window")

    def test_load_moves_floating_palettes_with_their_window_to_another_monitor(self):
        main = self.win.add("Editor", 0, rect=(100, 100, 900, 700))
        palette = self.win.add("Tools", 0, rect=(950, 100, 1150, 400), anchor=main, exstyle=N.WS_EX_TOOLWINDOW)
        m = self.manager()
        m.sync()
        self.win.cursor_device = D2
        self.new_desktop(m)  # D2 shows desktop 2
        PlacedWhileHidden.saved_layout(self, 0, "MON-B", 1, (2000, 100, 2800, 700))  # editor on D2 desktop 1
        with open(self.layout_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        saved["Windows"][0]["Exe"], saved["Windows"][0]["TitleHash"] = self.win.windows[main].exe, \
            __import__("src.layout", fromlist=["title_hash"]).title_hash("Editor")
        with open(self.layout_file, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        m._layout._load_file()
        with mock_no_switch(m):
            m.load_desktops()
        self.assertEqual(self.win.monitor_device_of_window(main), D2)
        self.assertEqual(self.win.monitor_device_of_window(palette), D2, "the palette went with it")


class mock_no_switch:
    """Load without switching to the saved shown desktop (so a window stays on a hidden one)."""

    def __init__(self, m):
        self.m = m

    def __enter__(self):
        self.saved = self.m._layout._showing
        self.m._layout._showing = {}

    def __exit__(self, *a):
        self.m._layout._showing = self.saved


class FolderTrouble(Base):
    def test_a_journal_that_isnt_an_object_with_windows_is_reported(self):
        for bad in ("[]", "{}", '{"Windows": 5}'):
            with open(os.path.join(self.dir, JOURNAL_FILE), "w") as fh:
                fh.write(bad)
            self.assertEqual(self.manager().recovery_problem, "damaged", bad)

    def test_an_offline_folder_is_not_touched_on_every_sync(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m.switch_relative(-1)  # nothing hidden: the journal would be removed
        paths._failed[os.path.normcase(os.path.normpath(self.dir))] = float("inf")
        self.addCleanup(paths.forget_failures)
        removes = []
        real = os.remove
        with unittest.mock.patch.object(os, "remove", side_effect=lambda p: removes.append(p) or real(p)), \
                unittest.mock.patch.object(paths, "offline", return_value=True):
            self.new_desktop(m)
            m.switch_relative(-1)
            for _ in range(3):
                m.sync()
            m.restore_all()
        self.assertEqual([p for p in removes if p.endswith(JOURNAL_FILE)], [], "never on the offline folder")

    def test_at_exit_a_paused_but_reachable_folders_journal_is_erased(self):
        from src import paths
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        paths._failed[os.path.normcase(os.path.normpath(self.dir))] = float("inf")  # (a refused write)
        self.addCleanup(paths.forget_failures)
        m.restore_all()
        self.assertIsNone(self.journal, "every window is shown: no journal left to re-show them later")

    def test_places_read_late_bring_back_the_desktops_and_places(self):
        from src import paths
        a, b = self.win.add("A"), self.win.add("B")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        self.new_desktop(m)
        m.move_window_to_desktop(b, D1, 1)
        m.snapshot()
        m.end()
        m.restore_all()
        with UnreadableAtStart.held(self, PLACES_FILE):
            m2 = self.manager()
            m2.sync()
        self.assertEqual(len(m2.desktops_of(D1)), 1, "(not read yet)")
        paths._read_next.clear()
        m2.snapshot()  # (a write: the late read)
        m2.sync()
        self.assertEqual(len(m2.desktops_of(D1)), 3, "its desktops back")
        self.assertIn(b, m2.desktops_of(D1)[1], "and the window adopted meanwhile to its place")

    def test_restore_hidden_windows_leaves_out_windows_that_dont_respond(self):
        ok = self.win.add("Fine", visible=False)
        hung = self.win.add("Busy", visible=False, hung=True)
        m = self.manager()
        m.sync()
        self.assertEqual(m.hidden_windows(), [ok])
        self.assertEqual(m.restore_windows([ok, hung, 0x7777]), 1, "only the shown one counts")

    def test_places_are_written_to_a_new_folder_and_at_the_last_snapshot(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        new = os.path.join(self.dir, "new")
        os.makedirs(new)
        m.relocate(new)
        self.assertIn(PLACES_FILE, os.listdir(new), "written at once, unchanged or not")
        os.remove(os.path.join(new, PLACES_FILE))
        m.snapshot()
        self.assertIn(PLACES_FILE, os.listdir(new), "the last snapshot is always written")

    def test_an_unplugged_displays_hidden_maximized_window_moves_its_frame(self):
        a = self.win.add("Chat", 1, rect=(2000, 100, 2800, 700))
        self.win.show_window(a, N.SW_MAXIMIZE)
        m = self.manager()
        m.sync()
        self.win.windows[a].visible = False  # its app hid it
        del self.win.screen_list[1]
        m.sync()
        self.assertEqual(self.win.monitor_device_of_window(a), D1, "maximized frame on the main display")
        self.assertFalse(self.visible(a))


class Relocate(Base):
    def test_moving_from_an_unreadable_folder_writes_everything_known_to_the_new_one(self):
        a = self.win.add("A")
        m = self.manager()
        m.sync()
        self.new_desktop(m)
        m.save_desktops()
        new = os.path.join(self.dir, "new")
        os.makedirs(new)
        m.relocate(new)  # (nothing was copied: the old folder could not be read)
        names = set(os.listdir(new))
        self.assertTrue({JOURNAL_FILE, LAYOUT_FILE} <= names, names)
        with open(os.path.join(new, JOURNAL_FILE), encoding="utf-8") as fh:
            self.assertEqual([r["Handle"] for r in json.load(fh)["Windows"]], [a])


if __name__ == "__main__":
    unittest.main()
