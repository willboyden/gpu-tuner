"""Thread-safe browser sessions with optional persistent remembered logins."""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BrowserSessions:
    def __init__(self, lifetime_seconds=12 * 60 * 60, max_sessions=32,
                 persistence_path=None):
        self.lifetime_seconds = lifetime_seconds
        self.max_sessions = max_sessions
        self.persistence_path = persistence_path
        self.sessions = {}
        self.lock = threading.Lock()
        if persistence_path:
            try:
                with open(persistence_path, encoding="ascii") as f:
                    doc = json.loads(f.read(65537))
                if not isinstance(doc, list) or len(doc) > max_sessions:
                    raise ValueError("invalid session store")
                for entry in doc:
                    if (not isinstance(entry, list) or len(entry) != 2
                            or not all(isinstance(v, str) and len(v) == 64
                                       and all(c in "0123456789abcdef" for c in v)
                                       for v in entry)):
                        raise ValueError("invalid session store")
                    self.sessions[entry[0]] = (None, entry[1])
            except (OSError, ValueError, UnicodeError):
                # An unavailable or corrupt store never authenticates a browser.
                self.sessions = {}

    def _commit(self, sessions):
        if self.persistence_path:
            directory = os.path.dirname(self.persistence_path)
            fd, temporary = tempfile.mkstemp(prefix=".sessions-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as f:
                    json.dump([[key, credential] for key, (expires, credential)
                               in sessions.items() if expires is None], f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, self.persistence_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        self.sessions = sessions

    def _prune(self, now):
        self.sessions = {token: entry for token, entry in self.sessions.items()
                         if entry[0] is None or entry[0] > now}

    def create(self, credential=None, lifetime_seconds=None, remember=False):
        """Remembered sessions have no server expiry; ordinary sessions expire."""
        if remember and not isinstance(credential, str):
            raise ValueError("Remembered sessions require a credential")
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            sessions = self.sessions.copy()
            if len(sessions) >= self.max_sessions:
                # Preserve the original oldest-issued capacity policy.
                del sessions[next(iter(sessions))]
            token = secrets.token_urlsafe(32)
            lifetime = self.lifetime_seconds if lifetime_seconds is None else lifetime_seconds
            sessions[_digest(token)] = (None if remember else now + lifetime,
                                        _digest(credential) if credential is not None else None)
            self._commit(sessions)
            return token

    def valid(self, token, credential=None):
        if not isinstance(token, str):
            return False
        with self.lock:
            self._prune(time.monotonic())
            key = _digest(token)
            entry = self.sessions.get(key)
            if entry is None:
                return False
            saved = entry[1]
            current = _digest(credential) if isinstance(credential, str) else None
            if (saved is None and credential is None) or (
                    saved is not None and current is not None
                    and hmac.compare_digest(saved, current)):
                return True
            sessions = self.sessions.copy()
            del sessions[key]
            self._commit(sessions)
            return False

    def remembered(self, token):
        with self.lock:
            entry = self.sessions.get(_digest(token))
            return entry is not None and entry[0] is None

    def revoke(self, token):
        with self.lock:
            sessions = self.sessions.copy()
            sessions.pop(_digest(token), None)
            self._commit(sessions)
