"""Argon2id password hashing and opaque HttpOnly sessions."""

import threading
import time
from collections import OrderedDict, deque

from pwdlib import PasswordHash

PASSWORD_HASH = PasswordHash.recommended()
DUMMY_HASH = PASSWORD_HASH.hash("not-an-account-password")
COOKIE_NAME = "sphinx_session"


def valid_password(password: str):
    if not 12 <= len(password) <= 128:
        raise ValueError("Das Passwort muss zwischen 12 und 128 Zeichen lang sein.")


def verify(password: str, encoded: str):
    try:
        return PASSWORD_HASH.verify(password, encoded)
    except Exception:
        return False


class LoginLimiter:
    """Bounded in-memory throttling; exactly one API worker is used."""

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key: str, *, limit=10, period=60):
        now = time.monotonic()
        with self.lock:
            times = self.entries.setdefault(key, deque())
            self.entries.move_to_end(key)
            while times and times[0] <= now - period:
                times.popleft()
            while len(self.entries) > 1024:
                self.entries.popitem(last=False)
            if len(times) >= limit:
                return False
            times.append(now)
            return True
