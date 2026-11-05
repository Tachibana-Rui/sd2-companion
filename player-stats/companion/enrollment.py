"""Installation-scoped credentials for companions that start without setup.

The client creates a random credential before registration so a lost response
can be retried safely. Only its digest is stored. Registration never grants the
operator token or permission to read raw reports or change server settings.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

INSTALLATIONS_PATH = "/api/companion/v3/installations"
MAX_REGISTRATION_BYTES = 4096
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")


class EnrollmentError(ValueError):
    def __init__(self, code, status_code=400):
        self.code, self.status_code = code, status_code
        super().__init__(code)


class _Limits:
    def __init__(self, clock=time.monotonic):
        self.clock, self.buckets, self.lock = clock, {}, threading.Lock()

    def allow(self, key, limit, window):
        now = self.clock()
        with self.lock:
            if key not in self.buckets and len(self.buckets) >= 4096:
                self.buckets = {k: v for k, v in self.buckets.items() if v and now - v[-1] < 3600}
                if len(self.buckets) >= 4096:
                    return False
            bucket = self.buckets.setdefault(key, deque())
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


def client_address(peer, forwarded=None):
    """Only a local reverse proxy may supply the cleaned X-Real-IP header."""
    try:
        direct = ipaddress.ip_address(peer)
        if direct.is_loopback and isinstance(forwarded, str):
            return str(ipaddress.ip_address(forwarded))
        return str(direct)
    except ValueError:
        return "unknown"


class InstallationRegistry:
    def __init__(self, path, *, clock=time.time, max_installations=10000):
        self.path, self.clock = Path(path).resolve(), clock
        self.max_installations = max_installations
        self.limits = _Limits()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS companion_installations (
                installation_id TEXT PRIMARY KEY,
                token_sha256 TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL)""")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, value, *, source="unknown"):
        if (not isinstance(value, dict) or set(value) != {"schema", "installation_id", "token"}
                or type(value["schema"]) is not int or value["schema"] != 1):
            raise EnrollmentError("invalid_installation_registration")
        identifier, token = value["installation_id"], value["token"]
        try:
            if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
                raise ValueError
        except (ValueError, AttributeError):
            raise EnrollmentError("invalid_installation_id") from None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise EnrollmentError("invalid_installation_credential")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT token_sha256 FROM companion_installations WHERE installation_id=?",
                                  (identifier,)).fetchone()
            if previous is not None:
                if not hmac.compare_digest(previous[0], digest):
                    raise EnrollmentError("installation_credential_conflict", 409)
                return False
            if (not self.limits.allow(("register", source), 30, 3600)
                    or not self.limits.allow(("register-global",), 300, 3600)):
                raise EnrollmentError("installation_registration_rate_limited", 429)
            if db.execute("SELECT COUNT(*) FROM companion_installations").fetchone()[0] >= self.max_installations:
                raise EnrollmentError("installation_registration_capacity", 503)
            try:
                db.execute("INSERT INTO companion_installations VALUES(?,?,?)", (identifier, digest, self.clock()))
            except sqlite3.IntegrityError:
                raise EnrollmentError("installation_credential_conflict", 409) from None
        return True

    def authenticate(self, token):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connection() as db:
            row = db.execute("SELECT installation_id FROM companion_installations WHERE token_sha256=?",
                             (digest,)).fetchone()
        return row[0] if row else None

    def check_submission(self, identifier, submission_id):
        if not self.limits.allow(("submit", identifier), 30, 60):
            raise EnrollmentError("installation_submission_rate_limited", 429)
        with self._connection() as db:
            # An idempotent retry remains possible when a quota is reached.
            if db.execute("SELECT 1 FROM submissions WHERE submission_id=?", (submission_id,)).fetchone():
                return
            daily = db.execute("SELECT COUNT(*) FROM submissions WHERE installation_id=? AND received_at>?",
                               (identifier, self.clock() - 86400)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM submissions WHERE installation_id=? AND status='pending_match' AND received_at>?",
                                 (identifier, self.clock() - 86400)).fetchone()[0]
            total_pending = db.execute("SELECT COUNT(*) FROM submissions WHERE status='pending_match' AND received_at>?",
                                       (self.clock() - 86400,)).fetchone()[0]
            if daily >= 200 or pending >= 20 or total_pending >= 2000:
                raise EnrollmentError("installation_submission_quota", 429)
