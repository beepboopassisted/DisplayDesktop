"""app.py: the background-thread error hook (nothing is started)."""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from src import app as A
from src import gui as G


def thread_error(exc: BaseException):
    """What threading.excepthook receives for an exception raised in a thread."""
    try:
        raise exc
    except BaseException as e:  # noqa: BLE001
        return SimpleNamespace(exc_type=type(e), exc_value=e, exc_traceback=e.__traceback__,
                               thread=SimpleNamespace(name="quit-watcher"))


class SharedObjects(unittest.TestCase):
    def test_the_mutex_and_quit_event_are_opened_by_a_normal_console_too(self):
        # (an elevated copy's: medium label, interactive users may wait on, query and set them)
        import ctypes
        from src import native as N
        sid = A._user_sid()
        self.assertTrue(sid and sid.startswith("S-1-5-"), sid)
        self.assertIn("(A;;GA;;;{0})", A._SHARED_SDDL, "this user only, never every interactive user")
        self.assertNotIn(";IU)", A._SHARED_SDDL)
        self.assertIn("S:(ML;;NW;;;ME)", A._SHARED_SDDL)
        sa = A.shared_attributes()
        self.assertIsNotNone(sa)
        create = N._f(N.kernel32, "CreateEventW", N.H, ctypes.c_void_p, N.BOOL, N.BOOL, ctypes.c_wchar_p)
        name = "DisplayDesktop_unit_test_shared_event"
        h = create(sa, False, False, name)
        self.addCleanup(N.CloseHandle, h)
        opened = A._OpenEventW(A.EVENT_MODIFY_STATE, False, name)
        self.assertTrue(opened)
        N.CloseHandle(opened)


class ThreadErrors(unittest.TestCase):
    QUIT = 0x8002

    def test_logged_with_its_traceback_then_the_app_quits_through_its_ui_thread(self):
        app = SimpleNamespace(hwnd=42)
        with mock.patch("src.console.log") as log, mock.patch.object(G, "PostMessageW") as post:
            A.make_thread_excepthook(lambda: app, self.QUIT)(thread_error(RuntimeError("boom")))
        text = log.call_args[0][0]
        self.assertIn("quit-watcher", text)
        self.assertIn("Traceback", text)
        self.assertIn("RuntimeError: boom", text)
        post.assert_called_once_with(42, self.QUIT, 0, 0)

    def test_before_the_app_exists_python_prints_it(self):
        with mock.patch("src.console.log"), mock.patch.object(G, "PostMessageW") as post, \
                mock.patch.object(threading, "__excepthook__") as default:
            A.make_thread_excepthook(lambda: None, self.QUIT)(thread_error(ValueError("early")))
        default.assert_called_once()
        post.assert_not_called()

    def test_a_thread_ending_itself_is_not_an_error(self):
        with mock.patch("src.console.log") as log, mock.patch.object(G, "PostMessageW") as post:
            A.make_thread_excepthook(lambda: SimpleNamespace(hwnd=1), self.QUIT)(thread_error(SystemExit()))
        log.assert_not_called()
        post.assert_not_called()

    def test_the_app_is_handed_the_error_to_tell_the_user(self):
        app = SimpleNamespace(hwnd=42, thread_error=None)
        err = RuntimeError("boom")
        with mock.patch("src.console.log"), mock.patch.object(G, "PostMessageW"):
            A.make_thread_excepthook(lambda: app, self.QUIT)(thread_error(err))
            A.make_thread_excepthook(lambda: app, self.QUIT)(thread_error(ValueError("second")))
        self.assertIs(app.thread_error, err, "the first error is the one told")

    def test_the_error_message_says_where_it_happened(self):
        from src import strings as L

        def inner():
            raise ValueError("NULL pointer access")
        try:
            inner()
        except ValueError as exc:
            text = L.error_text(exc)
        lines = text.splitlines()
        self.assertEqual(lines[0], "ValueError: NULL pointer access")
        self.assertIn("test_app.py line", lines[3])
        self.assertTrue(lines[3].endswith(", inner"), "the last step first")
        self.assertIn("Ctrl+C", lines[-1])
        self.assertEqual(L.error_text(ValueError("bare")).splitlines()[0], "ValueError: bare")

    def test_a_failing_log_still_quits(self):
        with mock.patch("src.console.log", side_effect=OSError("no console")), \
                mock.patch.object(G, "PostMessageW") as post:
            A.make_thread_excepthook(lambda: SimpleNamespace(hwnd=7), self.QUIT)(thread_error(RuntimeError("x")))
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
