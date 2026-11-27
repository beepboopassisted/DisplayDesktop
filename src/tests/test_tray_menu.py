"""The tray menu: built without showing it (the Win32 menu calls are replaced), and each item's command
runs the right action."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src import tray as T


class TrayMenu(unittest.TestCase):
    def build(self, chosen: int = 0):
        """Runs TrayApp.show_menu on a stand-in app; returns the appended items, the commands run and
        the app (its menu painter is a stand-in)."""
        items, ran = [], []
        app = SimpleNamespace(hwnd=0, run_command=ran.append, _menu_painter=None)
        with mock.patch.object(T.G, "CreatePopupMenu", return_value=1),                 mock.patch.object(T.G, "AppendMenuW", side_effect=lambda m, fl, i, s: items.append((fl, i, s)) or 1),                 mock.patch.object(T.G, "TrackPopupMenu", return_value=chosen),                 mock.patch.object(T.G, "SetForegroundWindow"), mock.patch.object(T.G, "PostMessageW"),                 mock.patch.object(T.G, "DestroyMenu"), mock.patch.object(T.N, "cursor_pos", return_value=(0, 0)),                 mock.patch.object(T.TM, "MenuPainter") as painter:
            T.TrayApp.show_menu(app)
        self.painter = painter
        return items, ran, app

    def test_order_labels_and_dividers(self):
        menu = T.TrayApp.menu_items()
        self.assertEqual([it.id if it else None for it in menu],
                         [T.ID_OVERVIEW, T.ID_PREV, T.ID_NEXT, None, T.ID_LOAD, T.ID_SAVE, None,
                          T.ID_SETTINGS, None, T.ID_EXIT])
        shown = {it.id: (it.label, it.keys, it.arrow) for it in menu if it}
        self.assertEqual(shown[T.ID_OVERVIEW], ("Overview", "Ctrl+Alt+", "up"))
        self.assertEqual(shown[T.ID_PREV], ("Left", "Ctrl+Alt+", "left"))
        self.assertEqual(shown[T.ID_NEXT], ("Right", "Ctrl+Alt+", "right"))
        self.assertEqual(shown[T.ID_SETTINGS], ("Settings", "", ""))
        self.assertEqual(len(shown), len(menu) - 3, "menu IDs must be unique")
        self.assertTrue(all(it.label and not it.label.startswith("menu.") for it in menu if it),
                        "every label must come from strings.py")

    def test_every_item_and_divider_is_owner_drawn_in_menu_order(self):
        items, _, app = self.build()
        own = T.TM.MF_OWNERDRAW
        self.assertTrue(all(fl & own for fl, _, _ in items))
        layout = [None if fl & T.TM.MF_SEPARATOR else i for fl, i, _ in items]
        self.assertEqual(layout, [it.id if it else None for it in T.TrayApp.menu_items()])
        self.painter.return_value.close.assert_called_once()
        self.assertIsNone(app._menu_painter, "the painter is dropped when the menu closes")

    def test_chosen_item_is_run(self):
        _, ran, _ = self.build(chosen=T.ID_NEXT)
        self.assertEqual(ran, [T.ID_NEXT])
        _, ran, _ = self.build(chosen=0)  # menu dismissed
        self.assertEqual(ran, [])

    def test_left_right_switch_desktops(self):
        for cmd, delta in ((T.ID_PREV, -1), (T.ID_NEXT, +1)):
            mgr = mock.Mock()
            T.TrayApp.run_command(SimpleNamespace(exiting=False, mgr=mgr), cmd)
            mgr.switch_relative.assert_called_once_with(delta)

    def test_edge_bounces_without_touching_the_screen_here(self):
        # A stand-in animator: this only checks that the tray asks for a bounce, nothing is drawn
        for overview_open, expected in ((False, [mock.call("DEV", +1)]), (True, [])):
            anim = mock.Mock()
            with mock.patch.object(T.Overview, "is_open", return_value=overview_open),                     mock.patch.object(T.console, "log") as log:
                T.TrayApp._on_edge(SimpleNamespace(animator=anim), "DEV", +1)
            self.assertEqual(anim.bounce.call_args_list, expected,
                             "no bounce while the overview is open" if overview_open else "bounce")
            log.assert_called_once()

    def run_with_answer(self, cmd, answer):
        app = SimpleNamespace(exiting=False, mgr=mock.Mock(), exit=mock.Mock())
        app._confirm = lambda key: T.TrayApp._confirm(app, key)
        with mock.patch.object(T.G, "message_box", return_value=answer) as box, \
                mock.patch.object(T.console, "log"):
            T.TrayApp.run_command(app, cmd)
        return app, box

    def test_load_desktops_asks_first(self):
        app, box = self.run_with_answer(T.ID_LOAD, 7)
        self.assertIn("Load desktops?", box.call_args[0][0])
        self.assertTrue(box.call_args[0][1] & T.G.MB_DEFBUTTON2, "No is the default")
        app.mgr.load_desktops.assert_not_called()
        app, _ = self.run_with_answer(T.ID_LOAD, T.G.IDYES)
        app.mgr.load_desktops.assert_called_once()

    def test_exit_asks_if_you_are_sure(self):
        app, box = self.run_with_answer(T.ID_EXIT, 7)
        self.assertEqual(box.call_args[0][0], "Are you sure you want to quit DisplayDesktop?")
        self.assertTrue(box.call_args[0][1] & T.G.MB_DEFBUTTON2, "No is the default")
        app.exit.assert_not_called()
        app, _ = self.run_with_answer(T.ID_EXIT, T.G.IDYES)
        app.exit.assert_called_once_with()

    def order_of(self, run, during_box=None):
        """Runs an exit path on a stand-in app; returns what happened, in order. `during_box(app)`
        runs while the error message is open (its message loop still delivers timers and keys)."""
        calls = []
        mgr = mock.Mock()
        mgr.ended = False

        def end():
            if not mgr.ended:
                calls.append("end (windows shown)")
            mgr.ended = True
        mgr.snapshot.side_effect = lambda **kw: None if mgr.ended else calls.append("snapshot")  # (no-op once ended)
        mgr.write_snapshot.side_effect = lambda: calls.append("snapshot written")
        mgr.end.side_effect = end
        mgr.sync.side_effect = lambda: calls.append("sync")
        app = SimpleNamespace(exiting=False, _ended=False, mgr=mgr, hwnd=0, _stop_watch=0, _icon=0, _unreadable_skip=set(), _asking_unreadable=False,
                              _fatal=False, animator=mock.Mock(), destroy=mock.Mock(),
                              _remove_tray_icon=mock.Mock(), thread_error=None,
                              kill_timer=lambda tid: calls.append("timer stopped"))
        app.exit = lambda: T.TrayApp.exit(app)
        app.on_fatal = lambda exc: T.TrayApp.on_fatal(app, exc)
        app._stop_and_restore = lambda: T.TrayApp._stop_and_restore(app)
        app._on_sync_timer = lambda: T.TrayApp._on_sync_timer(app)
        app.on_hotkey = lambda hid: T.TrayApp.on_hotkey(app, hid)

        def box(*a):
            calls.append("error message")
            if during_box:
                during_box(app)

        with mock.patch.object(T.G, "message_box", side_effect=box), mock.patch.object(T.G, "PostQuitMessage"), \
                mock.patch.object(T.N, "UnregisterHotKey", side_effect=lambda h, i: calls.append("keys off") or 1), \
                mock.patch.object(T.console, "log"), mock.patch.object(T.console, "close_console"), \
                mock.patch.object(T.console, "closed_by_user", return_value=False):
            run(app)
        return calls

    def test_every_exit_stops_everything_then_snapshots_then_shows_the_windows(self):
        paths = {
            "tray Exit / run stop": lambda app: T.TrayApp.exit(app),
            "Windows shutting down": lambda app: T.TrayApp.on_message(app, T.G.WM_ENDSESSION, 1, 0),
            "an error": lambda app: T.TrayApp.on_fatal(app, RuntimeError("test")),
            "a background thread's error": lambda app: (setattr(app, "thread_error", RuntimeError("thread")),
                                                        T.TrayApp.on_message(app, T.WM_APP_QUIT, 0, 0)),
        }
        for name, run in paths.items():
            calls = self.order_of(run)
            self.assertEqual("error message" in calls, "error" in name, f"{name}: told only for an error")
            self.assertIn("timer stopped", calls, name)
            self.assertEqual(calls.count("snapshot"), 1, f"{name}: one snapshot only ({calls})")
            self.assertEqual(calls.count("end (windows shown)"), 1, name)
            self.assertLess(calls.index("timer stopped"), calls.index("snapshot"), f"{name}: {calls}")
            self.assertLess(calls.index("keys off"), calls.index("snapshot"), f"{name}: {calls}")
            self.assertLess(calls.index("snapshot"), calls.index("end (windows shown)"), f"{name}: {calls}")
            if "snapshot written" in calls:  # (exit: written once the windows are shown)
                self.assertLess(calls.index("end (windows shown)"), calls.index("snapshot written"), name)

    def test_an_error_message_appears_only_after_everything_stopped_and_nothing_runs_meanwhile(self):
        def while_open(app):
            app._on_sync_timer()            # the 600 ms timer firing inside the message box's loop
            T.TrayApp.on_message(app, T.N.WM_HOTKEY, T.HK_NEXT, 0)  # Ctrl+Alt+Right meanwhile
        calls = self.order_of(lambda app: T.TrayApp.on_fatal(app, RuntimeError("test")), while_open)
        box = calls.index("error message")
        for step in ("timer stopped", "keys off", "snapshot", "end (windows shown)"):
            self.assertLess(calls.index(step), box, f"{step} before the message: {calls}")
        self.assertNotIn("sync", calls, "no sync after the windows were restored")
        self.assertEqual(calls.count("snapshot"), 1, "the restored state is never recorded over the snapshot")

    def test_an_error_while_exiting_still_shows_the_windows_before_the_error_message(self):
        """Exit closes the Overview; an error in its WM_DESTROY reaches on_fatal (gui._dispatch) while
        exit() is still in its first steps, before the snapshot and before the windows are shown."""
        holder = {}

        class FailingOverview:
            def close(self):
                T.TrayApp.on_fatal(holder["app"], RuntimeError("error in WM_DESTROY"))

        def run(app):
            holder["app"] = app
            with mock.patch.object(T.Overview, "current", return_value=FailingOverview()):
                T.TrayApp.exit(app)

        calls = self.order_of(run)
        self.assertIn("error message", calls)
        self.assertLess(calls.index("end (windows shown)"), calls.index("error message"),
                        f"the box says the windows are back: they must be ({calls})")
        self.assertLess(calls.index("snapshot"), calls.index("end (windows shown)"), calls)
        self.assertEqual(calls.count("snapshot"), 1, calls)

    def test_options_of_a_folder_unreadable_at_the_move_are_applied_once_readable(self):
        app = SimpleNamespace(exiting=False, mgr=mock.Mock(recovery_problem=None, late_problem=False), hwnd=0,
                              _options_owed=False, animator=mock.Mock(active_count=0),
                              _apply_options=mock.Mock(), _icon=0, _unreadable_skip=set(), _asking_unreadable=False)
        readable = [False]
        with mock.patch.object(T.datafolder, "kept_new", True), mock.patch.object(T.settings, "forget"), \
                mock.patch.object(T.settings, "readable", side_effect=lambda: readable[0]), \
                mock.patch.object(T.settings, "flush"), mock.patch.object(T.Overview, "is_open", return_value=False), \
                mock.patch.object(T.console, "closed_by_user", return_value=False):
            T.TrayApp.folder_changed(app, r"D:\Elsewhere")
            app._apply_options.assert_not_called()  # (never from defaults)
            T.TrayApp._on_sync_timer(app)
            app._apply_options.assert_not_called()
            readable[0] = True
            T.TrayApp._on_sync_timer(app)
            T.TrayApp._on_sync_timer(app)
        app._apply_options.assert_called_once_with()

    def test_settings_opens_the_dialog_with_its_recovery_button(self):
        app = SimpleNamespace(exiting=False, mgr=mock.Mock(recovery_problem=None), _icon=7, run_command=mock.Mock(),
                              hwnd=0, _unreadable_skip=set(), _asking_unreadable=False, open_recovery=mock.Mock())
        app.open_restore = lambda: T.TrayApp.open_restore(app)
        app.folder_changed = lambda folder: T.TrayApp.folder_changed(app, folder)
        app._apply_options = lambda: T.TrayApp._apply_options(app)
        app.set_encryption, app.set_remember = mock.Mock(), mock.Mock()
        with mock.patch.object(T.SettingsWindow, "show") as show:
            T.TrayApp.run_command(app, T.ID_SETTINGS)
        values = {"UseGpu": False, "StartMenu": True, "ShowConsole": False}
        for not_copied, kept_new in ((False, False), (True, False), (False, True)):
            app.mgr.reset_mock()
            with mock.patch.object(T.settings, "get", side_effect=values.get), \
                    mock.patch.object(T.datafolder, "settings_not_copied", not_copied), \
                    mock.patch.object(T.datafolder, "kept_new", kept_new), \
                    mock.patch.object(T.settings, "forget") as forget, \
                    mock.patch.object(T.settings, "readable", return_value=True), \
                    mock.patch.object(T.settings, "write_if_missing") as write, \
                    mock.patch.object(T.gpu, "set_enabled") as use_gpu, \
                    mock.patch.object(T.shortcut, "set_enabled") as start_menu, \
                    mock.patch.object(T.console, "is_open", return_value=False):
                show.call_args[1]["on_folder"](r"D:\Elsewhere")  # Settings Folder moved the data
            app.mgr.relocate.assert_called_once_with(r"D:\Elsewhere")
            self.assertEqual(write.call_count, 1 if not_copied else 0,
                             "the settings known are written only when they could not be copied")
            self.assertEqual(forget.call_count, 1 if kept_new else 0,
                             "the folder's own data chosen: its settings are read afresh")
            use_gpu.assert_called_once_with(False)
            start_menu.assert_called_once_with(True)
        show.assert_called_once()
        self.assertEqual(show.call_args[0], (7,))
        show.call_args[1]["on_recovery"]()  # the dialog's Recovery button
        app.open_recovery.assert_called_once_with(forced=True, can_quit=False)  # (Close, never Quit)

    def test_folder_problems_open_recovery_mode_with_both_ways_out(self):
        app = SimpleNamespace(exiting=False, mgr=mock.Mock(journal_problems={}), _icon=7, run_command=mock.Mock(),
                              hwnd=9, _asking_unreadable=False, _unreadable_skip=set(), folder_changed=mock.Mock())
        app.open_restore = lambda: T.TrayApp.open_restore(app)
        app.problem_text = lambda kind: T.TrayApp.problem_text(app, kind)
        app.open_recovery = lambda kind=0, forced=False, can_quit=True: T.TrayApp.open_recovery(app, kind, forced,
                                                                                              can_quit)
        app._recovery_done = lambda result, can_quit=True: T.TrayApp._recovery_done(app, result, can_quit)
        with mock.patch.object(T.datafolder, "unreachable", r"Q:\Gone"), \
                mock.patch.object(T.datafolder, "current", return_value=r"Q:\Gone"):
            texts = {k: app.problem_text(k) for k in (T.PROBLEM_UNREACHABLE, T.PROBLEM_SAVE_FAILED, T.PROBLEM_DAMAGED)}
        self.assertIn("can't be found", texts[T.PROBLEM_UNREACHABLE])
        self.assertIn(r"Q:\Gone", texts[T.PROBLEM_UNREACHABLE])
        self.assertIn("can't save", texts[T.PROBLEM_SAVE_FAILED])
        self.assertIn("can't be read", texts[T.PROBLEM_DAMAGED])
        folder = tempfile.mkdtemp(prefix="ws-test-")
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.object(T.RecoveryWindow, "show") as box, mock.patch.object(T.console, "log"), \
                mock.patch.object(T.datafolder, "current", return_value=folder), \
                mock.patch.object(T, "is_test_mode", return_value=True):
            T.TrayApp.on_message(app, T.WM_APP_PROBLEM, T.PROBLEM_DAMAGED, 0)
            T.TrayApp.on_message(app, T.WM_APP_RECOVERY, 0, 0)  # (`run recovery`)
        self.assertEqual(box.call_count, 2)
        report = box.call_args_list[0][0][0]
        self.assertIn(texts[T.PROBLEM_DAMAGED], report.summary)
        self.assertEqual(len(report.rows), len(T.paths.DATA_FILES), "every file, with its state")
        self.assertTrue(box.call_args_list[1][0][0].summary.startswith(T.L.t("recovery.none")))
        self.assertIs(box.call_args[1]["on_folder"], app.folder_changed, "its Settings Folder moves like Settings'")
        box.call_args[1]["on_clear"]()  # its Clear Desktops: the tray's Clear (asks first)
        app.run_command.assert_called_once_with(T.ID_CLEAR)
        with mock.patch.object(T.RestoreWindow, "show") as restore:
            box.call_args[1]["on_restore"]()
        restore.assert_called_once_with(app.mgr, 7)
        with mock.patch.object(T.G, "PostMessageW") as post:
            box.call_args[1]["on_done"]("continue")
            post.assert_not_called()
            box.call_args[1]["on_done"]("folder")
            post.assert_called_once_with(9, T.WM_APP_RECOVERY, 0, 0)  # (again, for the folder moved to)
            post.reset_mock()
            box.call_args[1]["on_done"]("quit")
        post.assert_called_once_with(9, T.WM_APP_QUIT, 0, 0)
        # Opened from Settings (its Recovery button): Close in Quit's place, it never ends the app
        with mock.patch.object(T.RecoveryWindow, "show") as box, mock.patch.object(T.console, "log"), \
                mock.patch.object(T.datafolder, "current", return_value=folder), \
                mock.patch.object(T, "is_test_mode", return_value=True):
            T.TrayApp.open_recovery(app, forced=True, can_quit=False)
        self.assertFalse(box.call_args[1]["can_quit"])
        with mock.patch.object(T.G, "PostMessageW") as post:
            box.call_args[1]["on_done"]("quit")
            post.assert_not_called()
            box.call_args[1]["on_done"]("folder")
            post.assert_called_once_with(9, T.WM_APP_RECOVERY, 1, 0)  # (reopened still without Quit)
        with mock.patch.object(T.RecoveryWindow, "show") as box, mock.patch.object(T.console, "log"), \
                mock.patch.object(T.datafolder, "current", return_value=folder), \
                mock.patch.object(T, "is_test_mode", return_value=True):
            T.TrayApp.on_message(app, T.WM_APP_RECOVERY, 1, 0)
        self.assertFalse(box.call_args[1]["can_quit"])
        app.exiting = True
        with mock.patch.object(T.RecoveryWindow, "show") as box:
            T.TrayApp.on_message(app, T.WM_APP_PROBLEM, T.PROBLEM_DAMAGED, 0)
        box.assert_not_called()

    def test_a_folder_problem_while_recovery_mode_asks_is_told_in_it_or_right_after(self):
        app = SimpleNamespace(exiting=False, _asking_unreadable=True, _pending_problem=0, hwnd=9)
        T.TrayApp.open_recovery(app, T.PROBLEM_UNREACHABLE)
        self.assertEqual(app._pending_problem, T.PROBLEM_UNREACHABLE, "kept, never lost")
        app = SimpleNamespace(exiting=False, _asking_unreadable=False, _pending_problem=T.PROBLEM_SAVE_FAILED, hwnd=9,
                              _icon=0, _unreadable_skip=set(), mgr=SimpleNamespace(journal_problems={}),
                              folder_changed=mock.Mock(), reread=mock.Mock())
        app.problem_text = lambda kind: T.TrayApp.problem_text(app, kind)
        with mock.patch.object(T, "resolve_unreadable_files", return_value=True) as resolve, \
                mock.patch.object(T.datafolder, "current", return_value=r"Q:\DD"), \
                mock.patch.object(T.G, "PostMessageW") as post:
            T.TrayApp.ask_unreadable(app)
        self.assertIn("can't save", resolve.call_args[0][5], "the problem that came first is told in it")
        post.assert_not_called()
        app._pending_problem = 0

        def came_meanwhile(*a, **k):
            app._pending_problem = T.PROBLEM_DAMAGED
            return True
        with mock.patch.object(T, "resolve_unreadable_files", side_effect=came_meanwhile), \
                mock.patch.object(T.datafolder, "current", return_value=r"Q:\DD"), \
                mock.patch.object(T.G, "PostMessageW") as post:
            T.TrayApp.ask_unreadable(app)
        post.assert_called_once_with(9, T.WM_APP_PROBLEM, T.PROBLEM_DAMAGED, 0)  # (told right after)

    def test_save_load_and_clear_wait_while_recovery_mode_asks(self):
        app = SimpleNamespace(exiting=False, _asking_unreadable=True, mgr=mock.Mock(), balloon=mock.Mock())
        for cmd in (T.ID_SAVE, T.ID_LOAD, T.ID_CLEAR):
            T.TrayApp.run_command(app, cmd)
        self.assertEqual(app.balloon.call_count, 3)
        app.mgr.save_desktops.assert_not_called()
        app.mgr.load_desktops.assert_not_called()
        app.mgr.clear_desktops.assert_not_called()

    def test_a_failed_save_in_the_settings_folder_is_reported_once_posted(self):
        app = SimpleNamespace(exiting=False, hwnd=5)
        with mock.patch.object(T.G, "PostMessageW") as post, \
                mock.patch.object(T.datafolder, "current", return_value=r"Q:\DD"):
            T.TrayApp._write_failed(app, r"Q:\DD")
            T.TrayApp._write_failed(app, r"C:\Elsewhere")  # not the settings folder
        post.assert_called_once_with(5, T.WM_APP_PROBLEM, T.PROBLEM_SAVE_FAILED, 0)

    def test_a_save_that_could_not_be_written_says_so(self):
        app = SimpleNamespace(exiting=False, mgr=mock.Mock(), exit=mock.Mock(), balloon=mock.Mock())
        app._confirm = lambda key: True
        app.mgr.save_desktops.return_value = -1
        with mock.patch.object(T.console, "log"):
            T.TrayApp.run_command(app, T.ID_SAVE)
        text = app.balloon.call_args[0][0]
        self.assertIn("not saved", text)
        self.assertTrue(app.balloon.call_args[1].get("warning"))
        app.mgr.save_desktops.return_value = 3
        with mock.patch.object(T.console, "log"):
            T.TrayApp.run_command(app, T.ID_SAVE)
        self.assertIn("Saved 3 windows", app.balloon.call_args[0][0])

    def test_clear_desktops_asks_first(self):
        app, box = self.run_with_answer(T.ID_CLEAR, 7)
        self.assertIn("Clear desktops?", box.call_args[0][0])
        app.mgr.clear_desktops.assert_not_called()
        app, _ = self.run_with_answer(T.ID_CLEAR, T.G.IDYES)
        app.mgr.clear_desktops.assert_called_once()

    def test_overview_item_toggles_the_overview(self):
        mgr = mock.Mock()
        with mock.patch.object(T.Overview, "toggle") as toggle:
            T.TrayApp.run_command(SimpleNamespace(exiting=False, mgr=mgr), T.ID_OVERVIEW)
        toggle.assert_called_once_with(mgr)

    def test_console_command_toggles_and_remembers(self):
        # Show Console lives in Settings; ID_CONSOLE stays a (test-mode) command using console.set_enabled
        def run(is_open, opens):
            app = SimpleNamespace(exiting=False, mgr=mock.Mock(), balloon=mock.Mock())
            with mock.patch.object(T.console, "is_open", return_value=is_open), \
                    mock.patch.object(T.console, "open_console", return_value=opens), \
                    mock.patch.object(T.console, "close_console") as close, \
                    mock.patch.object(T.console, "save_enabled") as save:
                T.TrayApp.run_command(app, T.ID_CONSOLE)
            return app, close, save
        app, close, save = run(is_open=False, opens=True)
        save.assert_called_once_with(True)
        app.balloon.assert_not_called()
        app, close, save = run(is_open=True, opens=True)
        close.assert_called_once()
        save.assert_called_once_with(False)
        app, close, save = run(is_open=False, opens=False)  # python.exe not found
        save.assert_not_called()
        self.assertTrue(app.balloon.call_args[1].get("warning"), "a failure shows a warning")

    def test_hotkeys_that_cannot_be_registered_are_reported(self):
        app = SimpleNamespace(hwnd=0, balloon=mock.Mock())
        taken = lambda h, hid, mods, vk: vk != T.N.VK_UP  # noqa: E731 - another app owns Ctrl+Alt+Up
        with mock.patch.object(T.N, "RegisterHotKey", side_effect=taken), mock.patch.object(T.console, "log"):
            T.TrayApp._register_hotkeys(app)
        text = app.balloon.call_args[0][0]
        self.assertIn("Ctrl+Alt+↑", text)
        self.assertNotIn("Ctrl+Alt+←", text)
        self.assertTrue(app.balloon.call_args[1].get("warning"))
        app = SimpleNamespace(hwnd=0, balloon=mock.Mock())
        with mock.patch.object(T.N, "RegisterHotKey", return_value=True):
            T.TrayApp._register_hotkeys(app)
        app.balloon.assert_not_called()

    def test_tray_icon_double_click_opens_overview_right_click_the_menu(self):
        app = SimpleNamespace(exiting=False, mgr=mock.Mock(), show_menu=mock.Mock())
        with mock.patch.object(T.Overview, "toggle") as toggle:
            T.TrayApp.on_message(app, T.WM_TRAY, 0, T.G.WM_LBUTTONDBLCLK)
            toggle.assert_called_once_with(app.mgr)
            T.TrayApp.on_message(app, T.WM_TRAY, 0, T.G.WM_RBUTTONUP)
            app.show_menu.assert_called_once()
            app.exiting = True  # nothing while shutting down
            T.TrayApp.on_message(app, T.WM_TRAY, 0, T.G.WM_LBUTTONDBLCLK)
            self.assertEqual(toggle.call_count, 1)

    def test_nothing_runs_while_exiting_even_from_the_test_only_commands(self):
        for cmd in (T.ID_NEXT, T.ID_CLEAR, T.ID_SETTINGS, T.ID_CONSOLE):
            mgr = mock.Mock()
            T.TrayApp.run_command(SimpleNamespace(exiting=True, mgr=mgr), cmd)
            self.assertEqual(mgr.mock_calls, [], cmd)


class TrayStartup(unittest.TestCase):
    """TrayApp.__init__ with every Win32 step replaced: the order of the steps and what a failure
    after the first sync does (nothing real is created)."""

    def start(self, fail_after_sync: bool = False, error_during_sync: bool = False, unreachable=None,
              recovery_quit: bool = False, values=None):
        order, seen_choice = [], []
        holder = {}
        mgr = mock.Mock()
        mgr.recovery_problem = None

        def sync():
            order.append("first sync")
            seen_choice.append(T.gpu.ENABLED)
            if error_during_sync:  # e.g. the slide's WM_PAINT raised: on_fatal ended the app meanwhile
                holder["app"].exiting = True
        mgr.sync.side_effect = sync
        mgr.end.side_effect = lambda: order.append("end: windows shown")
        added = {}

        def add_icon(app):
            added["icon"] = True
            app._icon_added = True

        def set_timer(app, *a):
            order.append("timer set")
            if fail_after_sync:
                raise RuntimeError("failed after the first sync")

        values = {"UseGpu": True} if values is None else values
        events = iter((11, 22))  # the quit event, then the event that stops the quit watcher
        animator = mock.Mock()
        animator.dispose.side_effect = lambda: order.append("slides stopped")
        self.animator = animator
        patches = [
            # (never the user's data folder, key or files: what start reads first)
            mock.patch.object(T, "unlock_at_start", return_value=True),
            mock.patch.object(T, "resolve_unreadable_files", return_value=not recovery_quit),
            mock.patch.object(T.paths, "data_dir", return_value=r"Q:\unit test"),
            mock.patch.object(T.settings, "readable", return_value=True),
            mock.patch.object(T.console, "capture_output"), mock.patch.object(T.startup, "refresh"),
            mock.patch.object(T.shortcut, "ensure", return_value=""),
            mock.patch.object(T, "DesktopManager", return_value=mgr),
            mock.patch.object(T.settings, "get", side_effect=lambda k: values.get(k, False)),
            mock.patch.object(T.gpu, "set_enabled", side_effect=lambda on: order.append("gpu set up")),
            mock.patch.object(T.gpu, "describe", return_value="the CPU"),
            mock.patch.object(T.G, "RegisterWindowMessageW", return_value=0),
            mock.patch.object(T.G, "set_fatal_handler"),
            mock.patch.object(T.TrayApp, "create", lambda app, *a, **k: order.append("window")),
            mock.patch.object(T, "SlideAnimator", return_value=animator),
            mock.patch.object(T, "create_app_icon", return_value=0),
            mock.patch.object(T.TrayApp, "_add_tray_icon", add_icon),
            mock.patch.object(T.TrayApp, "_remove_tray_icon", lambda app: order.append("tray icon removed")),
            mock.patch.object(T, "_CreateEventW", side_effect=lambda *a: next(events)),
            mock.patch.object(T, "SetEvent", side_effect=lambda h: order.append(f"event {h} set")),
            mock.patch.object(T.threading, "Thread"),
            mock.patch.object(T.TrayApp, "_register_hotkeys", lambda app: holder.setdefault("app", app)),
            mock.patch.object(T.datafolder, "unreachable", unreachable),
            mock.patch.object(T.G, "PostMessageW", side_effect=lambda *a: order.append(("posted", a[1:3]))),
            mock.patch.object(T.paths, "on_write_failed", None),
            mock.patch.object(T.TrayApp, "set_timer", set_timer),
            mock.patch.object(T.console, "log"), mock.patch.object(T.console, "load_enabled", return_value=False),
        ]
        for p in patches:
            p.start()
        try:
            try:
                app = T.TrayApp()
            except (RuntimeError, T.StartCancelled) as exc:
                return order, seen_choice, exc
            return order, seen_choice, app
        finally:
            for p in reversed(patches):
                p.stop()

    def test_quit_in_recovery_mode_at_start_still_shows_a_crashed_run_s_windows(self):
        order, _, result = self.start(recovery_quit=True)
        self.assertIsInstance(result, T.StartCancelled)
        self.assertEqual(order, ["end: windows shown"], "its journals read (the manager's start), then ended: "
                                                        "nothing synced nor hidden")

    def test_the_gpu_setting_is_applied_before_the_first_sync(self):
        # the first sync can already switch desktops (a desktop shown last time) and slide
        order, _, _ = self.start()
        self.assertLess(order.index("gpu set up"), order.index("first sync"), order)

    def test_restore_applications_on_restart_follows_the_setting(self):
        for on in (True, False):
            _, _, app = self.start(values={"UseGpu": True, "RestoreApps": on})
            self.assertIs(app.mgr.restore_apps, on)

    def test_a_failure_after_the_first_sync_shows_the_windows_again(self):
        order, _, result = self.start(fail_after_sync=True)
        self.assertIsInstance(result, RuntimeError, "main() still gets the error (it reports it)")
        self.assertLess(order.index("first sync"), order.index("end: windows shown"), order)
        self.assertIn("tray icon removed", order)
        # nothing of the failed copy keeps running while its error box is open
        self.assertIn("event 22 set", order, "the quit watcher is stopped (else it can take a stop meant "
                                             "for a new copy from the shared quit event)")
        self.assertNotIn("event 11 set", order, "the shared quit event itself is not set")
        self.assertIn("slides stopped", order, "a slide the first sync started is stopped")
        self.assertLess(order.index("end: windows shown"), order.index("event 22 set"), "windows first")

    def test_an_error_during_the_first_sync_stops_the_start(self):
        order, _, app = self.start(error_during_sync=True)
        self.assertNotIn("timer set", order, f"nothing more runs once the app has ended: {order}")

    def test_a_settings_folder_problem_at_start_opens_the_error_after_the_start(self):
        order, _, app = self.start(unreachable=r"Q:\Gone")
        self.assertIn(("posted", (T.WM_APP_PROBLEM, T.PROBLEM_UNREACHABLE)), order)
        self.assertLess(order.index("timer set"), order.index(("posted", (T.WM_APP_PROBLEM, T.PROBLEM_UNREACHABLE))))

    def test_a_normal_start_shows_nothing_again(self):
        order, _, app = self.start()
        self.assertNotIn("end: windows shown", order)
        self.assertFalse(app.exiting)


class OpeningWindows(unittest.TestCase):
    """The overview and Settings while Windows reports no display, and the overview counting as
    open while it builds (building it syncs, which can switch desktops)."""

    def tearDown(self):
        T.Overview._open = None
        T.SettingsWindow.current = None

    def test_no_display_reported_opens_nothing_and_does_not_fail(self):
        with mock.patch.object(T.N, "screens", return_value=[]), \
                mock.patch.object(T.Overview, "_setup", side_effect=AssertionError("must not open")), \
                mock.patch.object(T.SettingsWindow, "__init__", side_effect=AssertionError("must not open")):
            T.Overview.toggle(mock.Mock())
            self.assertFalse(T.Overview.is_open())
            self.assertIsNone(T.SettingsWindow.show())

    def test_the_overview_counts_as_open_while_it_builds(self):
        seen = []
        with mock.patch.object(T.Overview, "_setup", lambda ov, mgr, scr: seen.append(T.Overview.is_open())):
            T.Overview.toggle(mock.Mock())
        self.assertEqual(seen, [True], "no slide starts if building it switches desktops")
        self.assertTrue(T.Overview.is_open())

    def test_an_overview_that_fails_to_open_does_not_stay_open(self):
        with mock.patch.object(T.Overview, "_setup", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                T.Overview.toggle(mock.Mock())
        self.assertFalse(T.Overview.is_open())

    def test_the_screen_for_a_point(self):
        a = SimpleNamespace(bounds=(0, 0, 100, 100), primary=False)
        b = SimpleNamespace(bounds=(100, 0, 200, 100), primary=True)
        self.assertIs(T.N.pick_screen([a, b], (10, 10)), a)
        self.assertIs(T.N.pick_screen([a, b], (500, 500)), b, "outside every screen: the primary one")
        self.assertIs(T.N.pick_screen([a], None), a, "no primary: the first")
        self.assertIsNone(T.N.pick_screen([], (10, 10)))


class RestoreList(unittest.TestCase):
    """Restore Hidden Windows' list, built without showing the dialog (never on screen): every window in
    order with its title and program, each process asked for its program once, and a scroll bar once it
    holds more than fits (it is filled with redraw off, then on again)."""

    def test_every_window_listed_and_each_program_read_once(self):
        import ctypes
        from src import gui as G, native as N, restore_window as RW
        if N.pick_screen(N.screens(), N.cursor_pos()) is None:
            self.skipTest("no display")
        made = [G.CreateWindowExW(0, "STATIC", t, 0, 0, 0, 10, 10, 0, 0, 0, None) for t in ("Beta one", "Alpha two")]
        self.addCleanup(lambda: [G.DestroyWindow(h) for h in made])
        windows = made * 20  # (more than the list shows at once)
        with mock.patch.object(RW._Dialog, "_show"), \
                mock.patch.object(RW.N, "get_exe_path", wraps=N.get_exe_path) as exe:
            dlg = RW.RestoreWindow(SimpleNamespace(hidden_windows=lambda: windows))
        self.addCleanup(dlg.destroy)
        self.assertEqual(exe.call_count, 1, "one process: its program is read once for the whole list")
        lv = dlg._controls[RW.ID_LIST]
        self.assertEqual(G.SendMessageW(lv, RW.LVM_GETITEMCOUNT, 0, None), len(windows))
        texts, handles = [], []
        for i in range(len(windows)):
            buf = ctypes.create_unicode_buffer(512)
            item = RW._LVITEMW(RW.LVIF_TEXT | RW.LVIF_PARAM, i, 0, 0, 0, ctypes.cast(buf, ctypes.c_wchar_p), 512)
            G.SendMessageW(lv, 0x104B, 0, ctypes.addressof(item))  # LVM_GETITEMW
            texts.append(item.pszText)  # (the buffer, or the list's own text it may point to instead)
            handles.append(item.lParam)
        self.assertEqual(handles, windows)
        self.assertEqual(texts, [RW.label(h) for h in windows])
        self.assertTrue(texts[0].startswith("Beta one  (") and texts[0].endswith(".exe)"), texts[0])
        self.assertTrue(N.get_style(lv) & N.WS_VSCROLL, "the scroll bar is shown: redraw is on again")


class LazyUi(unittest.TestCase):
    """The Overview, Settings and the dialogs are imported only when first opened; an ending (sync
    ticks, closing what is open) never imports them, and tray.<Name> still gives each class. The app's
    start loads neither typing nor dataclasses (with what they import: ~1 MB), nor hashlib/OpenSSL,
    shutil (bz2, lzma), uuid, bcrypt.dll or crypt32.dll (only Encrypt Settings and a move need them),
    nor pdh.dll (only Auto with several GPUs samples their use); opening the UI loads neither typing
    nor dataclasses either (kept for the rest of the run)."""

    def test_loaded_on_first_use_only(self):
        import os
        import subprocess
        import sys
        code = ("import sys\n"
                "from types import SimpleNamespace as S\n"
                "import src.tray as T\n"
                "lazy = ('src.overview', 'src.settings_window', 'src.restore_window')\n"
                "heavy = [m for m in ('typing', 'dataclasses', 'inspect', 'hashlib', 'shutil', 'uuid', 'bz2', 'lzma')\n"
                "         if m in sys.modules]\n"
                "import ctypes\n"
                "loaded = ctypes.WinDLL('kernel32').GetModuleHandleW\n"
                "loaded.argtypes, loaded.restype = [ctypes.c_wchar_p], ctypes.c_void_p\n"
                "heavy += [d for d in ('libcrypto-1_1.dll', 'bcrypt.dll', 'crypt32.dll', '_lzma.pyd', 'pdh.dll')\n"
                "          if loaded(d)]\n"
                "start = [m for m in lazy if m in sys.modules] + heavy\n"
                "app = S(exiting=False, kill_timer=lambda t: None, _stop_watch=0, hwnd=0,\n"
                "        animator=S(dispose=lambda: None),\n"
                "        mgr=S(snapshot=lambda defer: None, end=lambda: None, write_snapshot=lambda: None))\n"
                "open_ = T._overview_open()\n"
                "T.TrayApp._stop_and_restore(app)\n"
                "end = [m for m in lazy if m in sys.modules]\n"
                "names = [getattr(T, n).__name__ for n in ('Overview', 'SettingsWindow', 'RestoreWindow',\n"
                "                                          'RecoveryWindow', 'KeyWindow')]\n"
                "ui_heavy = [m for m in ('typing', 'dataclasses', 'inspect') if m in sys.modules]\n"
                "print(start, open_, end, app.exiting, names, sorted(m for m in lazy if m in sys.modules), ui_heavy)\n")
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=repo, capture_output=True, text=True,
                             timeout=60, creationflags=0x08000000)  # CREATE_NO_WINDOW
        self.assertEqual(out.stderr, "")
        self.assertEqual(out.stdout.strip(), "[] False [] True ['Overview', 'SettingsWindow', 'RestoreWindow', "
                                             "'RecoveryWindow', 'KeyWindow'] ['src.overview', "
                                             "'src.restore_window', 'src.settings_window'] []")
        with self.assertRaises(AttributeError):
            T.NoSuchName  # noqa: B018


if __name__ == "__main__":
    unittest.main()
