"""Tests for the browser login rate limiter."""

import unittest
from unittest.mock import patch

from gpu_tuner.login_limit import LoginLimiter


class LoginLimiterTests(unittest.TestCase):

    def test_five_attempts_allowed_then_blocked(self):
        limiter = LoginLimiter()

        for _ in range(5):
            self.assertTrue(limiter.allow())

        self.assertFalse(limiter.allow())

    def test_attempts_expire(self):
        limiter = LoginLimiter(max_attempts=2, window_seconds=60)

        with patch("gpu_tuner.login_limit.time.monotonic", return_value=100):
            self.assertTrue(limiter.allow())
            self.assertTrue(limiter.allow())
            self.assertFalse(limiter.allow())

        with patch("gpu_tuner.login_limit.time.monotonic", return_value=160):
            self.assertTrue(limiter.allow())

    def test_instances_are_independent(self):
        first = LoginLimiter(max_attempts=1)
        second = LoginLimiter(max_attempts=1)

        self.assertTrue(first.allow())
        self.assertFalse(first.allow())
        self.assertTrue(second.allow())


if __name__ == "__main__":
    unittest.main()
