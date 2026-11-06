"""Server-issued room catalog and remote settings; memory only, never on disk.

The companion no longer needs the game endpoints in its local configuration:
the server hands out the room table (and whitelisted tunables) over the same
authenticated HTTPS endpoint used for uploads, so an address change is one
edit on the server instead of a rewritten client file. Nothing received here
is ever written next to the configuration file.
"""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
import re
import socket
import time
from urllib import error, request
from urllib.parse import urlsplit

from .config import ConfigError, parse_rooms
from .http_transport import ServiceOpener


CATALOG_PATH = "/api/companion/v3/catalog"
ROOMS_PATH = "/api/companion/v3/rooms"
MAX_CATALOG_BYTES = 256 * 1024
DEFAULT_INTERVAL_SECONDS = 600.0
# Only tunables that the capture loop already validates may be pushed.
TUNABLES = {"poll_seconds": (0.1, 60.0), "process_poll_seconds": (0.5, 120.0)}
_TOKEN_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")


class CatalogError(Exception):
    """Failure codes only: never local paths, endpoints, tokens, or bodies."""

    def __init__(self, code, *, retryable=True, status_code=None):
        super().__init__(code)
        self.code = code
        self.retryable = bool(retryable)
        self.status_code = status_code


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The catalog endpoint must be configured as the final HTTPS address.
        return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("catalog_duplicate_property", retryable=False)
        result[key] = value
    return result


def parse_timestamp(value):
    """ISO 8601 with an explicit zone; anything else is not a timestamp."""
    if not isinstance(value, str) or not _TIMESTAMP.match(value):
        raise CatalogError("catalog_timestamp_invalid", retryable=False)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CatalogError("catalog_timestamp_invalid", retryable=False) from None
    if parsed.tzinfo is None:
        raise CatalogError("catalog_timestamp_invalid", retryable=False)
    return parsed.astimezone(timezone.utc).timestamp()


def parse_catalog(document, *, now):
    """Return ``(rooms, settings, expires_at)`` or raise :class:`CatalogError`."""
    if not isinstance(document, dict):
        raise CatalogError("catalog_invalid", retryable=False)
    if document.get("schema") != 1:
        raise CatalogError("catalog_schema_unsupported", retryable=False)
    try:
        rooms = tuple(parse_rooms(document.get("rooms")))
    except ConfigError as exc:
        raise CatalogError("catalog_" + str(exc), retryable=False) from None
    if not rooms:
        raise CatalogError("catalog_without_rooms", retryable=False)
    settings = {}
    raw_settings = document.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise CatalogError("catalog_settings_invalid", retryable=False)
    if set(raw_settings) - set(TUNABLES):
        raise CatalogError("catalog_settings_unsupported", retryable=False)
    for key, value in raw_settings.items():
        low, high = TUNABLES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise CatalogError("catalog_settings_invalid", retryable=False)
        settings[key] = float(value)
    raw_expiry = document.get("expires_at")
    expires_at = parse_timestamp(raw_expiry) if raw_expiry is not None else None
    if expires_at is not None and expires_at <= now:
        raise CatalogError("catalog_expired", retryable=False)
    return rooms, settings, expires_at


def _validate_url(url, allow_loopback_http):
    from ipaddress import ip_address
    if (not isinstance(url, str) or not url or "\\" in url
            or any(not 33 <= ord(char) <= 126 for char in url)):
        raise CatalogError("catalog_url_invalid", retryable=False)
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise CatalogError("catalog_url_invalid", retryable=False) from None
    if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise CatalogError("catalog_url_invalid", retryable=False)
    if port is not None and not 1 <= port <= 65535:
        raise CatalogError("catalog_url_invalid", retryable=False)
    if parsed.scheme == "https":
        return url
    loopback = host.lower() == "localhost"
    if not loopback:
        try:
            loopback = ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "http" and allow_loopback_http and loopback:
        return url
    raise CatalogError("catalog_url_requires_https", retryable=False)


def sync(catalog, now):
    """Fetch when due. Returns True when the served table or tunables changed."""
    return False if catalog is None else catalog.refresh_if_due(now)


def with_tunables(config, settings):
    """Apply pushed tunables to a frozen config; ranges are checked on parse."""
    if not settings:
        return config
    from dataclasses import replace
    changes = {key: value for key, value in settings.items()
               if getattr(config, key, None) != value}
    return replace(config, **changes) if changes else config


class RemoteCatalog:
    """Bounded catalog client that keeps the last good table in memory."""

    def __init__(self, url, *, token=None, token_env="SD2_COMPANION_TOKEN",
                 interval=DEFAULT_INTERVAL_SECONDS, timeout=15.0,
                 allow_loopback_http=False, opener=None, fetch=None, clock=time.monotonic):
        self.url = _validate_url(url, allow_loopback_http)
        if not isinstance(token_env, str) or not _TOKEN_ENV.match(token_env):
            raise CatalogError("catalog_token_env_invalid", retryable=False)
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 30 <= interval <= 86400:
            raise CatalogError("catalog_interval_invalid", retryable=False)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise CatalogError("catalog_timeout_invalid", retryable=False)
        self.token = token
        self.token_env = token_env
        self.interval = float(interval)
        self.timeout = float(timeout)
        self.allow_loopback_http = allow_loopback_http
        self._opener = opener or ServiceOpener(_NoRedirect)
        self._fetch = fetch or self._http_fetch
        self._clock = clock
        self.rooms = ()
        self.settings = {}
        self.state = "pending"
        self.last_error = None
        self.last_success = None
        self.etag = None
        self.expires_at = None
        self.next_refresh = 0.0

    def status(self):
        return {"state": self.state, "room_count": len(self.rooms), "settings": dict(self.settings),
                "last_error": self.last_error, "last_success": self.last_success,
                "next_refresh": self.next_refresh, "expires_at": self.expires_at}

    def refresh_if_due(self, now):
        """Fetch when due. Returns True when the room table or settings changed."""
        if now < self.next_refresh:
            return False
        before = (self.rooms, self.settings)
        self.refresh(now=now)
        return (self.rooms, self.settings) != before

    def refresh(self, *, now=None):
        """One bounded attempt. Returns True when the room table changed."""
        now = self._clock() if now is None else now
        wall = time.time()
        token = self.token if self.token is not None else os.environ.get(self.token_env)
        if not token:
            self.state = "no_credentials"
            self.last_error = "catalog_token_missing"
            self.next_refresh = now + self.interval
            return False
        try:
            status, headers, body = self._fetch(self.url, token, self.timeout, self.etag)
        except CatalogError as exc:
            self._fail(exc, now)
            return False
        except (OSError, http.client.HTTPException, ValueError):
            self._fail(CatalogError("catalog_unreachable"), now)
            return False
        if status == 304:
            if not self.rooms:
                self._fail(CatalogError("catalog_not_modified_without_content"), now)
                return False
            if self.expires_at is not None and self.expires_at <= wall:
                # A 304 does not extend a document's lifetime. Retry the
                # next pass without its ETag, keeping only the last good table.
                self.etag = None
                self._fail(CatalogError("catalog_expired"), now)
                self.next_refresh = now + 30.0
                return False
            self.state, self.last_error = "ok", None
            self.last_success = wall
            self.next_refresh = now + self._wait(wall, self.expires_at)
            return False
        try:
            if status != 200:
                raise CatalogError("catalog_http_" + str(status), retryable=status >= 500 or status == 429,
                                   status_code=status)
            document = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
            rooms, settings, expires_at = parse_catalog(document, now=wall)
        except CatalogError as exc:
            self._fail(exc, now)
            return False
        except (ValueError, UnicodeError, RecursionError):
            self._fail(CatalogError("catalog_invalid", retryable=False), now)
            return False
        changed = (rooms, settings) != (self.rooms, self.settings)
        self.rooms, self.settings = rooms, settings
        self.expires_at = expires_at
        self.etag = headers.get("ETag") if headers is not None else None
        self.state, self.last_error, self.last_success = "ok", None, wall
        self.next_refresh = now + self._wait(wall, expires_at)
        return changed

    def _wait(self, wall, expires_at=None):
        wait = self.interval
        if expires_at is not None:
            # Refresh before expiry, leaving room for one normal retry. Short
            # test lifetimes still refresh before their deadline, without spin.
            remaining = expires_at - wall
            wait = min(wait, max(1.0, remaining - min(30.0, remaining / 2)))
        return wait

    def _fail(self, exc, now):
        if exc.status_code in (401, 403):
            self.state = "unauthorized"
        elif exc.retryable:
            self.state = "unreachable"
        else:
            self.state = "invalid"
        self.last_error = exc.code
        # A short startup/network outage must not leave a running desktop
        # offline for the full ten-minute healthy refresh interval.
        self.next_refresh = now + (min(60.0, self.interval) if exc.retryable
                                   else max(300.0, self.interval))

    def _http_fetch(self, url, token, timeout, etag):
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
                   "User-Agent": "SD2-Companion/1"}
        if etag:
            headers["If-None-Match"] = etag
        outgoing = request.Request(url, method="GET", headers=headers)
        deadline = time.monotonic() + timeout
        try:
            with self._opener.open(outgoing, timeout=timeout) as response:
                status = response.getcode()
                if status == 304:
                    return 304, response.headers, b""
                if response.headers.get_content_type() != "application/json":
                    raise CatalogError("catalog_invalid_type", retryable=False, status_code=status)
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise CatalogError("catalog_timeout", status_code=status)
                    chunk = response.read1(min(8192, MAX_CATALOG_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_CATALOG_BYTES:
                        raise CatalogError("catalog_too_large", retryable=False, status_code=status)
                    chunks.append(chunk)
                return status, response.headers, b"".join(chunks)
        except error.HTTPError as exc:
            status = exc.code
            headers = exc.headers if exc.headers is not None else {}
            exc.close()
            if status == 304:
                return 304, headers, b""
            if 300 <= status < 400:
                raise CatalogError("catalog_redirect_rejected", retryable=False, status_code=status) from None
            raise CatalogError("catalog_http_" + str(status), retryable=status >= 500 or status == 429,
                               status_code=status) from None
        except error.URLError as exc:
            code = "catalog_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "catalog_unreachable"
            raise CatalogError(code) from None
        except (TimeoutError, socket.timeout):
            raise CatalogError("catalog_timeout") from None
