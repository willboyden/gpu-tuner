"""Tests for independent password-authenticated browser sessions."""

import unittest
from unittest.mock import patch

from gpu_tuner.browser_sessions import BrowserSessions


class BrowserSessionsTests(unittest.TestCase):
    def test_unique_independent_sessions(self):
        sessions = BrowserSessions()
        first = sessions.create()
        second = sessions.create()

        self.assertNotEqual(first, second)
        self.assertTrue(sessions.valid(first))
        self.assertTrue(sessions.valid(second))
        self.assertFalse(sessions.valid("unknown-token"))

    def test_expiration(self):
        sessions = BrowserSessions(lifetime_seconds=60)

        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=100):
            token = sessions.create()

        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=159):
            self.assertTrue(sessions.valid(token))

        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=160):
            self.assertFalse(sessions.valid(token))

    def test_capacity_evicts_oldest_session(self):
        sessions = BrowserSessions(max_sessions=2)

        with patch("gpu_tuner.browser_sessions.time.monotonic", side_effect=[100, 101, 102]):
            first = sessions.create()
            second = sessions.create()
            third = sessions.create()

        self.assertFalse(sessions.valid(first))
        self.assertTrue(sessions.valid(second))
        self.assertTrue(sessions.valid(third))

    def test_revocation_is_individual(self):
        sessions = BrowserSessions()
        first = sessions.create()
        second = sessions.create()

        sessions.revoke(first)

        self.assertFalse(sessions.valid(first))
        self.assertTrue(sessions.valid(second))

    def test_invalid_token_types(self):
        sessions = BrowserSessions()

        self.assertFalse(sessions.valid(None))
        self.assertFalse(sessions.valid(123))

    def test_credential_change_revokes_session(self):
        sessions = BrowserSessions()
        token = sessions.create("original")
        self.assertTrue(sessions.valid(token, "original"))
        self.assertFalse(sessions.valid(token, "replacement"))
        self.assertFalse(sessions.valid(token, "original"))
        replacement = sessions.create("replacement")
        self.assertTrue(sessions.valid(replacement, "replacement"))

    def test_missing_credential_rejects_bound_session(self):
        sessions = BrowserSessions()
        token = sessions.create("original")
        self.assertFalse(sessions.valid(token))

    def test_remembered_lifetime_does_not_extend_default_sessions(self):
        sessions = BrowserSessions(lifetime_seconds=60)
        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=100):
            regular = sessions.create("credential")
            remembered = sessions.create("credential", lifetime_seconds=300)
        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=160):
            self.assertFalse(sessions.valid(regular, "credential"))
            self.assertTrue(sessions.valid(remembered, "credential"))
        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=400):
            self.assertFalse(sessions.valid(remembered, "credential"))


class PersistentBrowserSessionsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, "sessions.json")

    def store(self, **kwargs):
        return BrowserSessions(persistence_path=str(self.path), **kwargs)

    def test_remembered_session_survives_restart_without_expiry(self):
        sessions = self.store()
        token = sessions.create("credential", remember=True)
        regular = sessions.create("credential")
        with patch("gpu_tuner.browser_sessions.time.monotonic", return_value=10**15):
            restarted = self.store()
            self.assertTrue(restarted.valid(token, "credential"))
            self.assertTrue(restarted.remembered(token))
            self.assertFalse(restarted.valid(regular, "credential"))
        self.assertNotIn(token, self.path.read_text())
        self.assertNotIn("credential", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_revocation_and_password_changes_survive_restart(self):
        sessions = self.store()
        first = sessions.create("credential", remember=True)
        second = sessions.create("credential", remember=True)
        sessions.revoke(first)
        restarted = self.store()
        self.assertFalse(restarted.valid(first, "credential"))
        self.assertTrue(restarted.valid(second, "credential"))
        self.assertFalse(restarted.valid(second, "changed"))
        self.assertFalse(self.store().valid(second, "credential"))

    def test_capacity_eviction_survives_restart(self):
        sessions = self.store(max_sessions=2)
        first = sessions.create("credential", remember=True)
        second = sessions.create("credential", remember=True)
        third = sessions.create("credential", remember=True)
        restarted = self.store(max_sessions=2)
        self.assertFalse(restarted.valid(first, "credential"))
        self.assertTrue(restarted.valid(second, "credential"))
        self.assertTrue(restarted.valid(third, "credential"))

    def test_corrupt_store_fails_closed(self):
        for content in ("not json", "{}", "[[1,2]]"):
            self.path.write_text(content)
            self.assertEqual(self.store().sessions, {})

    def test_write_failure_does_not_issue_session(self):
        sessions = self.store()
        with patch("gpu_tuner.browser_sessions.os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                sessions.create("credential", remember=True)
        self.assertEqual(sessions.sessions, {})
        self.assertEqual(list(self.path.parent.glob(".sessions-*")), [])
