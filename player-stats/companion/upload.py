"""Bounded report submission with a dedicated bearer token and no redirects."""

from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import time
from urllib import error, request
from urllib.parse import urlsplit

from .http_transport import OfficialHTTPSHandler


MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
TOKEN_ENV = "SD2_COMPANION_TOKEN"
ACCEPTED_STATUSES = frozenset(("accepted", "pending_match", "duplicate"))
_SUBMISSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def payload_bytes(payload, max_bytes=MAX_PAYLOAD_BYTES):
    """Serialize a submission deterministically without accepting JSON NaNs."""
    if not isinstance(payload, dict):
        raise ValueError("invalid_submission")
    submission_id = payload.get("submission_id")
    if not isinstance(submission_id, str) or not _SUBMISSION_ID.fullmatch(submission_id):
        raise ValueError("invalid_submission_id")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_submission") from None
    if len(encoded) > max_bytes:
        raise ValueError("submission_too_large")
    return encoded


class UploadError(Exception):
    """Public failure details intentionally contain no URL, body, or token."""

    def __init__(self, code, *, retryable, status_code=None, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retryable = bool(retryable)
        self.status_code = status_code
        self.retry_after = retry_after


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a same-origin redirect must be configured as the final endpoint.
        # This also prevents bearer credentials from reaching another origin.
        return None


def _positive_integer(value, upper, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise ValueError(label)
    return value


def _validate_url(url, allow_loopback_http):
    if (not isinstance(url, str) or not url or "\\" in url
            or any(not 33 <= ord(char) <= 126 for char in url)):
        raise ValueError("invalid_upload_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("invalid_upload_url") from None
    if not host or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("invalid_upload_url")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid_upload_url")
    if parsed.scheme == "https":
        return url
    loopback = host.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "http" and allow_loopback_http and loopback:
        return url
    raise ValueError("https_required")


def _retry_after(value, now):
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdecimal():
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        if math.isfinite(seconds):
            return min(86400.0, max(0.0, seconds))
    except (ValueError, TypeError, OverflowError):
        pass
    return None


class Uploader:
    def __init__(self, url, token=None, *, token_env=TOKEN_ENV, timeout=15,
                 allow_loopback_http=False, max_payload_bytes=MAX_PAYLOAD_BYTES,
                 max_response_bytes=MAX_RESPONSE_BYTES):
        self.url = _validate_url(url, allow_loopback_http)
        if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
            raise ValueError("invalid_token_environment_name")
        if token is None:
            token = os.environ.get(token_env)
        if not isinstance(token, str) or not token:
            raise ValueError("missing_upload_token")
        if len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("invalid_upload_token")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("invalid_upload_timeout")
        self.timeout = float(timeout)
        self.max_payload_bytes = _positive_integer(max_payload_bytes, MAX_PAYLOAD_BYTES, "invalid_payload_limit")
        self.max_response_bytes = _positive_integer(max_response_bytes, MAX_RESPONSE_BYTES, "invalid_response_limit")
        self._token = token
        # A report endpoint is contacted directly. In particular, an HTTP test
        # endpoint must never be sent through a machine's configured proxy.
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(), OfficialHTTPSHandler())

    def __repr__(self):
        return "Uploader(token=<redacted>)"

    def send(self, payload):
        encoded = payload_bytes(payload, self.max_payload_bytes)
        submission_id = payload["submission_id"]
        outgoing = request.Request(self.url, data=encoded, method="POST", headers={
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Idempotency-Key": submission_id,
            "User-Agent": "SD2-Companion/1",
        })
        deadline = time.monotonic() + self.timeout
        try:
            with self._opener.open(outgoing, timeout=self.timeout) as response:
                code = response.getcode()
                if not 200 <= code < 300:
                    raise UploadError("unexpected_http_status", retryable=code >= 500,
                                      status_code=code)
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise UploadError("invalid_receipt_type", retryable=True, status_code=code)
                # read1 avoids waiting for a whole large buffer while a peer
                # trickles bytes. Check the operation budget between reads in
                # addition to the underlying socket's I/O timeout.
                chunks = []
                size = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise UploadError("upload_timeout", retryable=True, status_code=code)
                    chunk = response.read1(min(8192, self.max_response_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise UploadError("receipt_too_large", retryable=True, status_code=code)
                    chunks.append(chunk)
                body = b"".join(chunks)
        except error.HTTPError as exc:
            code = exc.code
            retry_after = _retry_after(exc.headers.get("Retry-After"), time.time()) if exc.headers else None
            exc.close()
            if 300 <= code < 400:
                raise UploadError("redirect_rejected", retryable=False, status_code=code) from None
            raise UploadError("http_" + str(code), retryable=code == 429 or code >= 500,
                              status_code=code, retry_after=retry_after) from None
        except error.URLError as exc:
            code = "upload_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network_error"
            raise UploadError(code, retryable=True) from None
        except (TimeoutError, socket.timeout):
            raise UploadError("upload_timeout", retryable=True) from None
        except OSError:
            raise UploadError("network_error", retryable=True) from None
        except http.client.HTTPException:
            raise UploadError("network_error", retryable=True) from None
        try:
            receipt = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise UploadError("invalid_receipt_json", retryable=True, status_code=code) from None
        if (not isinstance(receipt, dict) or receipt.get("submission_id") != submission_id
                or not isinstance(receipt.get("status"), str)
                or receipt["status"] not in ACCEPTED_STATUSES):
            raise UploadError("invalid_receipt", retryable=True, status_code=code)
        # Do not return arbitrary response messages that might echo credentials
        # or player data. These two fields are the complete acknowledgement.
        return {"submission_id": submission_id, "status": receipt["status"]}
