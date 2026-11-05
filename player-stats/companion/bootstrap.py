"""Automatic installation enrollment, without a shared secret in the binary."""
from __future__ import annotations

import http.client
import json
import re
import secrets
import threading
from urllib import error, request
import uuid

from .config import DEFAULT_CATALOG_URL, DEFAULT_UPLOAD_URL
from .credentials import (CredentialError, read_installation_token,
                          store_installation_token)

REGISTRATION_URL = "https://sd2.rui-blog.link/api/companion/v3/installations"
MAX_RESPONSE_BYTES = 16 * 1024


class BootstrapError(ValueError):
    """Only controlled codes; credentials and response bodies never escape."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def can_register(config):
    """Automatic enrollment is limited to the built-in service's exact URLs."""
    return (getattr(config, "online_enabled", True)
            and config.upload_url == DEFAULT_UPLOAD_URL
            and config.catalog_url in (None, DEFAULT_CATALOG_URL,
                "https://sd2.rui-blog.link/api/companion/v3/catalog"))


class BootstrapClient:
    def __init__(self, config, installation_id, *, opener=None, read_token=None,
                 store_token=None, timeout=6.0):
        if not can_register(config):
            raise BootstrapError("automatic_registration_unavailable")
        try:
            if str(uuid.UUID(installation_id)) != installation_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise BootstrapError("invalid_installation_id") from None
        self.url = config.upload_url
        self.installation_id = installation_id
        self.timeout = timeout
        self._opener = opener or request.build_opener(_NoRedirect())
        self._read = read_token or read_installation_token
        self._store = store_token or store_installation_token

    def register(self):
        """Persist before POST so retries survive lost responses and restarts."""
        try:
            token = self._read(self.url, self.installation_id)
            if token is None:
                token = secrets.token_urlsafe(32)
                self._store(self.url, self.installation_id, token)
        except (OSError, CredentialError):
            raise BootstrapError("automatic_credential_storage_failed") from None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise BootstrapError("automatic_credential_invalid")
        body = json.dumps({"schema": 1, "installation_id": self.installation_id,
                           "token": token}, separators=(",", ":")).encode("utf-8")
        outgoing = request.Request(REGISTRATION_URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "SD2-Companion/1"})
        try:
            with self._opener.open(outgoing, timeout=self.timeout) as response:
                status = response.getcode()
                if response.headers.get_content_type() != "application/json":
                    raise BootstrapError("automatic_registration_invalid_response")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            code = ("installation_credential_conflict" if status == 409 else
                    "automatic_registration_retry")
            raise BootstrapError(code) from None
        except (OSError, http.client.HTTPException):
            raise BootstrapError("automatic_registration_retry") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BootstrapError("automatic_registration_invalid_response")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise BootstrapError("automatic_registration_invalid_response") from None
        if (status not in (200, 201) or not isinstance(document, dict)
                or document.get("schema") != 1
                or document.get("installation_id") != self.installation_id
                or document.get("status") != "registered"):
            raise BootstrapError("automatic_registration_invalid_response")
        return token


class RegistrationWorker:
    """Enrollment network calls never block native memory sampling."""
    def __init__(self, client, *, emit=None, retry_seconds=60.0):
        self.client = client
        self.emit = emit or (lambda event: None)
        self.retry_seconds = retry_seconds
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self._token = None
        self.state = "pending"
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="sd2-companion-registration", daemon=True)
        self.thread.start()

    def _run(self):
        self.emit({"event": "client_registration_pending"})
        while not self.stop_event.is_set():
            try:
                token = self.client.register()
            except BootstrapError as exc:
                self.state = "retry"
                self.emit({"event": "client_registration_retry", "code": str(exc)})
                if self.stop_event.wait(self.retry_seconds):
                    return
            else:
                self._token = token
                self.state = "ready"
                self.ready.set()
                return

    def token_if_ready(self):
        return self._token if self.ready.is_set() else None

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)


def check_connection(config, *, fresh_installation=False):
    """Exercise enrollment, room fetching and schema rejection without a report."""
    from . import __version__
    from .catalog import RemoteCatalog
    from .credentials import get_upload_token
    from .outbox import Outbox
    from .upload import Uploader, UploadError
    outbox = Outbox(config.data_directory / "outbox.sqlite3")
    token = None if fresh_installation else get_upload_token(config.upload_url, config.token_env)
    automatic = token is None
    if automatic:
        token = BootstrapClient(config, outbox.installation_id).register()
    catalog = RemoteCatalog(config.catalog_url or DEFAULT_CATALOG_URL, token=token)
    catalog.refresh()
    if catalog.state != "ok" or not catalog.rooms:
        raise BootstrapError("automatic_catalog_check_failed")
    uploader = Uploader(config.upload_url, token=token)
    try:
        uploader.send({"schema": "connection-check", "submission_id": "connection-check",
                       "installation_id": outbox.installation_id})
    except UploadError as exc:
        if exc.status_code != 400:
            raise BootstrapError("automatic_upload_check_failed") from None
    else:
        raise BootstrapError("automatic_upload_check_unexpected_acceptance")
    return {"version": __version__, "automatic_registration": automatic,
            "installation_id": outbox.installation_id, "room_count": len(catalog.rooms),
            "upload_schema_check": True, "game_process_accessed": False,
            "configuration_required": False}
