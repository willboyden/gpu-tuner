"""Thread-safe, process-local login attempt limiter."""

import threading
import time
from collections import deque


class LoginLimiter:
    def __init__(self, max_attempts=5, window_seconds=60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.attempts = deque()
        self.lock = threading.Lock()

    def allow(self):
        """Reserve an attempt, or reject when the window is full."""
        with self.lock:
            now = time.monotonic()

            while (
                self.attempts
                and now - self.attempts[0] >= self.window_seconds
            ):
                self.attempts.popleft()

            if len(self.attempts) >= self.max_attempts:
                return False

            self.attempts.append(now)
            return True
