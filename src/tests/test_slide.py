"""The switch slide and the bounce at the first/last desktop, as pure motion math: no window is
created and the screen is never captured."""
from __future__ import annotations

import unittest

from src import slide as S

SAMPLES = [i / 200 for i in range(201)]


class Bounce(unittest.TestCase):
    def test_starts_and_ends_at_rest(self):
        for w in (1920, 3840):
            for d in (+1, -1):
                self.assertEqual(S.bounce_offset(0.0, w, d), 0)
                self.assertEqual(S.bounce_offset(1.0, w, d), 0)

    def test_pushes_a_little_toward_the_key_pressed(self):
        right = [S.bounce_offset(t, 3840, +1) for t in SAMPLES]
        left = [S.bounce_offset(t, 3840, -1) for t in SAMPLES]
        self.assertEqual(min(right), -134, "Ctrl+Alt+Right: pushed left by 3.5% of 3840 px")
        self.assertEqual(max(left), 134, "Ctrl+Alt+Left: pushed right just as far")
        self.assertEqual(left, [-x for x in right], "the two directions mirror each other")

    def test_size_follows_the_monitor_with_a_minimum(self):
        peak = lambda w: max(abs(S.bounce_offset(t, w, +1)) for t in SAMPLES)  # noqa: E731
        self.assertEqual(peak(1920), 67)
        self.assertEqual(peak(3840), 134)
        self.assertEqual(peak(300), S.BOUNCE_MIN_PX, "still visible on a small monitor")

    def test_springs_back_past_rest_a_little_then_settles(self):
        curve = [S.spring(t) for t in SAMPLES]
        peak_at = curve.index(max(curve))
        self.assertAlmostEqual(max(curve), 1.0, places=2)
        self.assertLess(SAMPLES[peak_at], 0.25, "the push comes right away")
        overshoot = min(curve)
        self.assertTrue(-0.3 < overshoot < -0.05, f"a small overshoot ({overshoot:.2f})")
        self.assertTrue(all(abs(v) < 0.05 for t, v in zip(SAMPLES, curve) if t >= 0.85), "settled")

    def test_moves_smoothly(self):
        # One refresh at 60 Hz is 16.7 ms of the 380 ms bounce. The push is quick (about half of it in
        # the first refresh) but no refresh moves more than 60% of the push.
        step = 16.7 / S.BOUNCE_MS
        t = 0.0
        while t + step <= 1.0:
            self.assertLess(abs(S.spring(t + step) - S.spring(t)), 0.6)
            t += step


class Slide(unittest.TestCase):
    def test_desktops_slide_side_by_side_like_a_comic_strip(self):
        w = 3840
        for direction, start, end in ((+1, (0, w), (-w, 0)), (-1, (0, -w), (w, 0))):
            self.assertEqual(S.strip_positions(0, w, direction), start, "old desktop on screen, new one next to it")
            self.assertEqual(S.strip_positions(w, w, direction), end, "moved by exactly one desktop")
            for offset in range(0, w + 1, 97):
                old_x, new_x = S.strip_positions(offset, w, direction)
                self.assertEqual(abs(new_x - old_x), w, "the two pictures always touch: no gap, no overlap")
                self.assertEqual(new_x - old_x, w * direction, "the new desktop is on the side moved to")

    def test_ease_goes_from_start_to_end_without_jumps(self):
        values = [S.ease(t) for t in SAMPLES]
        self.assertEqual((values[0], values[-1]), (0.0, 1.0))
        self.assertTrue(all(b >= a for a, b in zip(values, values[1:])), "never moves backwards")
        # gentle start: the first refresh (16.7 ms of 280 ms) moves less than 1% of the screen
        self.assertLess(S.ease(16.7 / S.DURATION_MS), 0.01)


if __name__ == "__main__":
    unittest.main()
