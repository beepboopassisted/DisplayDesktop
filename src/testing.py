"""Test-mode support (only active when DISPLAYDESKTOP_DATA is set, see paths.py).

DISPLAYDESKTOP_TEST_PIDS="123,456" restricts a test copy to the windows of those processes, so live
tests can switch desktops for real without hiding any of the user's own windows."""
from __future__ import annotations

import os

from . import native as N
from .paths import is_test_mode

PIDS_ENV = "DISPLAYDESKTOP_TEST_PIDS"


class ScopedWin:
    """The native module, but only windows of the given processes exist for enumeration and focus."""

    def __init__(self, pids: set[int]):
        self._pids = pids

    def __getattr__(self, name):
        return getattr(N, name)

    def _ours(self, h: int) -> bool:
        return bool(h) and N.get_pid(h) in self._pids

    def enum_windows(self) -> list[int]:
        return [h for h in N.enum_windows() if self._ours(h)]

    def get_foreground(self) -> int:
        h = N.get_foreground()
        return h if self._ours(h) else 0


def window_api():
    """The window API the app should use: the native module, or a scoped one in test mode."""
    raw = os.environ.get(PIDS_ENV, "")
    if is_test_mode() and raw:
        return ScopedWin({int(p) for p in raw.split(",") if p.strip()})
    return N
