"""Test helper: put the focused window on the next or previous desktop of its monitor and show that
desktop, using only what the app itself offers (the overview's "+", moving a window, switching).
Works with the simulated Windows (fakes.py) and the real one (live tests)."""
from __future__ import annotations

from src.desktop_manager import MAX_DESKTOPS_PER_MONITOR


def move_active(m, w, delta: int) -> None:
    fg = w.get_foreground()
    if fg:
        move_window(m, w, fg, delta)


def move_window(m, w, fg: int, delta: int) -> None:
    """The same for a given window. Live tests use this: they can't count on making their window
    the foreground window (Windows refuses that to a background process while the user is busy)."""
    dev = w.monitor_device_of_window(fg)
    if dev is None:
        return
    m.sync()
    target = m.current_of(dev) + delta
    if target < 0:
        return
    if target >= len(m.desktops_of(dev)):
        if delta <= 0 or len(m.desktops_of(dev)) >= MAX_DESKTOPS_PER_MONITOR:
            return
        m.create_desktop_and_switch(dev)  # "+": a new desktop, shown
        m.move_window_to_desktop(fg, dev, target)
    else:
        m.move_window_to_desktop(fg, dev, target)
        m.switch_to(dev, target)
    w.set_foreground(fg)
