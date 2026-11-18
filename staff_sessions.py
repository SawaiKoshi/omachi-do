"""Opt-in, single-process browser sessions. No credentials or sessions in SQLite.

Only a separate HTTPS staff.localhost origin is supported by this development
server. Deployment on a shop network needs a separate deployment/auth review.
"""

from dataclasses import dataclass
from hashlib import sha256
import hmac
import secrets
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit


COOKIE_NAME = "__Host-omachi-staff"
ABSOLUTE_SECONDS = 3600
IDLE_SECONDS = 900
SHARED_PRINCIPAL = {"principal_kind": "shared_staff_token", "principal_id": "shared-staff"}


def browser_origin(value):
    if value is None:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "staff.localhost"
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or value != f"https://staff.localhost{':' + str(parsed.port) if parsed.port else ''}"):
        raise ValueError("Browser sessions require a separate https://staff.localhost[:port] origin.")
    return value


class SessionRejected(ValueError):
    pass


class SessionCapacity(RuntimeError):
    pass


@dataclass
class _Session:
    csrf: str
    created: float
    last_used: float


class StaffSessions:
    """Opaque random cookies, hashed lookup keys, fixed maximum and idle expiry.

    Restart revokes every session. Multi-worker use is deliberately unsupported;
    an unknown cookie is rejected, never converted into header authentication.
    """

    def __init__(self, *, clock=monotonic, capacity=256):
        self.clock = clock
        self.capacity = capacity
        self._sessions = {}
        self._login_attempts = []
        self._lock = RLock()

    @staticmethod
    def _key(cookie):
        if not isinstance(cookie, str) or len(cookie) != 43:
            raise SessionRejected("Invalid session.")
        return sha256(cookie.encode()).digest()

    def _expire(self, now):
        for key, session in list(self._sessions.items()):
            if (now - session.created >= ABSOLUTE_SECONDS
                    or now - session.last_used >= IDLE_SECONDS):
                del self._sessions[key]

    def allow_login(self):
        """Bound credential guessing and memory use, including failed logins."""
        with self._lock:
            now = self.clock()
            self._login_attempts = [at for at in self._login_attempts if now - at < 60]
            if len(self._login_attempts) >= 10:
                return False
            self._login_attempts.append(now)
            return True

    def create(self, previous=None):
        with self._lock:
            now = self.clock()
            self._expire(now)
            if previous is not None:
                self.revoke(previous)
            if len(self._sessions) >= self.capacity:
                raise SessionCapacity("Session capacity reached.")
            cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[self._key(cookie)] = _Session(csrf, now, now)
            return cookie, csrf

    def authenticate(self, cookie, *, csrf=None, require_csrf=False):
        with self._lock:
            now = self.clock()
            self._expire(now)
            session = self._sessions.get(self._key(cookie))
            if session is None:
                raise SessionRejected("Session expired or unknown.")
            if require_csrf and (not isinstance(csrf, str)
                                 or not hmac.compare_digest(csrf.encode(), session.csrf.encode())):
                raise SessionRejected("Invalid CSRF token.")
            session.last_used = now
            return dict(authenticated=True, csrf_token=session.csrf,
                        expires_in_seconds=max(0, int(ABSOLUTE_SECONDS - (now - session.created))),
                        idle_timeout_seconds=IDLE_SECONDS,
                        principal=dict(SHARED_PRINCIPAL))

    def revoke(self, cookie):
        with self._lock:
            try:
                self._sessions.pop(self._key(cookie), None)
            except SessionRejected:
                pass

    def clear(self):
        with self._lock:
            self._sessions.clear()
