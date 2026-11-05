"""Automatic setup and retry behavior, without real enrollment or credentials."""
import io
import json
import os
from email.message import Message
from pathlib import Path
import threading
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import uuid

from companion.bootstrap import (BootstrapClient, BootstrapError, REGISTRATION_URL,
                                 RegistrationWorker, can_register, check_connection)
from companion.cli import _activate_registration, _catalog, _run_auto, _start_registration, _uploader
from companion.config import Config, DEFAULT_CATALOG_URL, DEFAULT_UPLOAD_URL, from_dict
from companion.credentials import _installation_target, _target


INSTALLATION = "59deff03-6ffd-4b26-8778-806d8b348110"
TOKEN = "a" * 43


class Response(io.BytesIO):
    def __init__(self, value, status=201):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def getcode(self):
        return self.status


class Opener:
    def __init__(self, *, value=None, fail_once=False):
        self.value = value
        self.fail_once = fail_once
        self.requests = []

    def open(self, outgoing, **kwargs):
        self.requests.append(outgoing)
        if self.fail_once and len(self.requests) == 1:
            raise OSError("network offline")
        return Response(self.value if self.value is not None else {
            "schema": 1, "installation_id": INSTALLATION, "status": "registered"})


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = from_dict({"schema": 1})
        self.stored = {}
        self.writes = []

    def store(self, url, identity, token):
        self.stored[url, identity] = token
        self.writes.append((url, identity, token))

    def client(self, opener=None, **kwargs):
        return BootstrapClient(self.config, INSTALLATION, opener=opener or Opener(),
            read_token=lambda url, identity: self.stored.get((url, identity)),
            store_token=self.store, **kwargs)

    def test_first_launch_generates_and_stores_scoped_key_before_registration(self):
        opener = Opener()
        with patch("companion.bootstrap.secrets.token_urlsafe", return_value=TOKEN) as generate:
            token = self.client(opener).register()
        self.assertEqual(token, TOKEN)
        generate.assert_called_once_with(32)
        self.assertEqual(self.writes, [(DEFAULT_UPLOAD_URL, INSTALLATION, TOKEN)])
        outgoing = opener.requests[0]
        self.assertEqual(outgoing.full_url, REGISTRATION_URL)
        self.assertEqual(outgoing.method, "POST")
        self.assertNotIn("Authorization", outgoing.headers)
        self.assertEqual(json.loads(outgoing.data), {"schema": 1, "installation_id": INSTALLATION, "token": TOKEN})

    def test_lost_response_retries_with_identical_persisted_credential(self):
        opener = Opener(fail_once=True)
        client = self.client(opener)
        with self.assertRaisesRegex(BootstrapError, "automatic_registration_retry"):
            client.register()
        token = client.register()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(json.loads(opener.requests[0].data), json.loads(opener.requests[1].data))
        self.assertEqual(token, self.writes[0][2])

    def test_device_scope_is_independent_of_operator_and_other_installations(self):
        first = _installation_target(DEFAULT_UPLOAD_URL, INSTALLATION)
        second = _installation_target(DEFAULT_UPLOAD_URL, str(uuid.uuid4()))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, _target(DEFAULT_UPLOAD_URL))

    def test_custom_destinations_and_local_mode_never_get_automatic_credential(self):
        for changes in ({"online_enabled": False},
                        {"upload_url": "https://other.invalid/task-reports"},
                        {"catalog_url": "https://other.invalid/rooms"}):
            config = from_dict({"schema": 1, **changes})
            with self.subTest(changes=changes):
                self.assertFalse(can_register(config))
                with self.assertRaisesRegex(BootstrapError, "automatic_registration_unavailable"):
                    BootstrapClient(config, INSTALLATION)

    def test_credential_storage_failure_prevents_network_and_never_logs_secret(self):
        opener = Opener()
        client = self.client(opener)
        client._store = Mock(side_effect=OSError("private token " + TOKEN))
        with self.assertRaises(BootstrapError) as error:
            client.register()
        self.assertEqual(str(error.exception), "automatic_credential_storage_failed")
        self.assertEqual(opener.requests, [])
        self.assertNotIn(TOKEN, str(error.exception))

    def test_invalid_or_oversized_response_does_not_activate_uploads(self):
        for value in (b"x" * 17000, b"invalid json", {"schema": 2},
                      {"schema": 1, "installation_id": str(uuid.uuid4()), "status": "registered"},
                      {"schema": 1, "installation_id": INSTALLATION, "status": "denied"}):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(BootstrapError):
                self.client(Opener(value=value)).register()

    def test_conflict_does_not_replace_original_credential(self):
        self.stored[DEFAULT_UPLOAD_URL, INSTALLATION] = TOKEN
        opener = Mock()
        opener.open.side_effect = HTTPError(REGISTRATION_URL, 409, "conflict", {}, None)
        with self.assertRaisesRegex(BootstrapError, "installation_credential_conflict"):
            self.client(opener).register()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.stored[DEFAULT_UPLOAD_URL, INSTALLATION], TOKEN)

    def test_worker_retries_off_sampling_thread_and_emits_no_credential(self):
        main_thread = threading.get_ident()
        calls = []
        def register():
            calls.append(threading.get_ident())
            if len(calls) == 1:
                raise BootstrapError("automatic_registration_retry")
            return TOKEN
        client = SimpleNamespace(register=register)
        events = []
        worker = RegistrationWorker(client, emit=events.append, retry_seconds=0.01)
        worker.start()
        try:
            self.assertTrue(worker.ready.wait(1))
            self.assertEqual(worker.token_if_ready(), TOKEN)
            self.assertTrue(all(identity != main_thread for identity in calls))
            self.assertEqual(events[0], {"event": "client_registration_pending"})
            self.assertEqual(events[1]["event"], "client_registration_retry")
            self.assertNotIn(TOKEN, json.dumps(events))
        finally:
            worker.close()

    def test_failed_registration_worker_can_stop_immediately_without_waiting_for_retry(self):
        called = threading.Event()
        def fail():
            called.set()
            raise BootstrapError("automatic_registration_retry")
        worker = RegistrationWorker(SimpleNamespace(register=fail), retry_seconds=60)
        worker.start()
        self.assertTrue(called.wait(1))
        worker.close()
        self.assertFalse(worker.thread.is_alive())

    def test_success_enables_existing_catalog_and_delivery_without_restart(self):
        service = SimpleNamespace(uploader=None)
        catalog = SimpleNamespace(token=None, next_refresh=600)
        worker = SimpleNamespace(token_if_ready=lambda: TOKEN)
        events = []
        with patch("companion.cli.Uploader") as uploader, patch("companion.cli.DeliveryWorker") as delivery:
            active = _activate_registration(worker, self.config, catalog, service, None, events.append)
            again = _activate_registration(worker, self.config, catalog, service, active, events.append)
        self.assertIs(active, again)
        self.assertEqual(catalog.token, TOKEN)
        self.assertEqual(catalog.next_refresh, 0)
        uploader.assert_called_once()
        delivery.assert_called_once_with(service)
        active.start.assert_called_once()
        self.assertEqual(events, [{"event": "online_services_ready"}])

    def test_existing_operator_uploader_skips_automatic_registration(self):
        service = SimpleNamespace(uploader=object())
        with patch("companion.bootstrap.BootstrapClient") as client:
            worker = _start_registration(self.config, SimpleNamespace(installation_id=INSTALLATION), service, Mock())
        self.assertIsNone(worker)
        client.assert_not_called()

    def test_explicit_local_mode_gates_even_programmatically_configured_addresses(self):
        config = Config((), (), Path("unused"), upload_url=DEFAULT_UPLOAD_URL,
                        catalog_url=DEFAULT_CATALOG_URL, online_enabled=False)
        with patch("companion.cli.get_upload_token") as token:
            self.assertIsNone(_uploader(config))
            self.assertIsNone(_catalog(config))
        token.assert_not_called()

    def test_connection_probe_accepts_actual_uploader_http_400_error_code(self):
        from companion.upload import UploadError
        with patch("companion.outbox.Outbox", return_value=SimpleNamespace(installation_id=INSTALLATION)), \
                patch("companion.bootstrap.BootstrapClient") as client, \
                patch("companion.catalog.RemoteCatalog") as catalog, \
                patch("companion.upload.Uploader") as uploader, \
                patch("companion.credentials.get_upload_token", return_value="old-operator-token") as operator:
            client.return_value.register.return_value = TOKEN
            catalog.return_value.state = "ok"
            catalog.return_value.rooms = (object(), object())
            uploader.return_value.send.side_effect = UploadError("http_400", retryable=False, status_code=400)
            result = check_connection(self.config, fresh_installation=True)
        operator.assert_not_called()
        client.assert_called_once_with(self.config, INSTALLATION)
        self.assertTrue(result["automatic_registration"])
        self.assertTrue(result["upload_schema_check"])
        self.assertEqual(result["room_count"], 2)
        self.assertNotIn(TOKEN, json.dumps(result))

    @unittest.skipUnless(os.name == "nt", "Windows capture entry")
    def test_capture_runs_before_enrollment_and_delivery_activates_on_later_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            config = from_dict({"schema": 1, "data_directory": directory})
            stop = threading.Event()
            ready = threading.Event()
            worker = SimpleNamespace(token_if_ready=lambda: TOKEN if ready.is_set() else None, close=Mock())
            catalog = SimpleNamespace(token=None, next_refresh=600, rooms=(), state="no_credentials",
                                      last_success=None, last_error="catalog_token_missing")
            records = []
            class Service:
                def __init__(self, config, observer, outbox, *, uploader, emit):
                    self.uploader = uploader
                def tick(self):
                    records.append(self.uploader is not None)
                    if len(records) == 1:
                        ready.set()
                    if len(records) == 2:
                        stop.set()
                def close(self):
                    pass
            observer = SimpleNamespace(config=SimpleNamespace(poll_seconds=0))
            with patch("companion.cli._catalog", return_value=catalog), \
                    patch("companion.cli._capture_uploader", return_value=None), \
                    patch("companion.cli._start_registration", return_value=worker), \
                    patch("companion.live_runtime.AutomaticWindowsObserver", return_value=observer), \
                    patch("companion.live_capture.AutomaticCaptureService", Service), \
                    patch("companion.cli.DeliveryWorker") as delivery:
                self.assertEqual(_run_auto(config, emit_fn=Mock(), stop_event=stop), 0)
            self.assertEqual(records, [False, True])
            delivery.return_value.start.assert_called_once()
            delivery.return_value.close.assert_called_once()
            worker.close.assert_called_once()
            self.assertEqual(catalog.token, TOKEN)


if __name__ == "__main__":
    unittest.main()
