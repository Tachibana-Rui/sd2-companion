"""Zero-setup clients get restricted credentials without sharing operator keys."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from companion.enrollment import EnrollmentError, InstallationRegistry, INSTALLATIONS_PATH, client_address
from companion.receiver import API_PATH, RECEIPTS_PATH, ArchiveIndex, CatalogSource, ReceiverStore, create_server
from test_companion_receiver import native_archive, payload


def registration():
    return {"schema": 1, "installation_id": str(uuid.uuid4()), "token": secrets.token_urlsafe(32)}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "inbox.sqlite3"
        self.store = ReceiverStore(self.path)
        self.registry = InstallationRegistry(self.path)

    def test_random_key_is_idempotent_and_only_its_hash_is_stored(self):
        value = registration()
        self.assertTrue(self.registry.register(value))
        self.assertFalse(self.registry.register(value))
        self.assertEqual(self.registry.authenticate(value["token"]), value["installation_id"])
        self.assertIsNone(self.registry.authenticate(secrets.token_urlsafe(32)))
        with closing(sqlite3.connect(self.path)) as db:
            stored = db.execute("SELECT token_sha256 FROM companion_installations").fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(value["token"].encode()).hexdigest())
        self.assertNotIn(value["token"].encode(), self.path.read_bytes())

    def test_knowing_an_installation_id_cannot_reset_its_key(self):
        value = registration()
        self.registry.register(value)
        impostor = {**value, "token": secrets.token_urlsafe(32)}
        with self.assertRaisesRegex(EnrollmentError, "credential_conflict"):
            self.registry.register(impostor)
        self.assertEqual(self.registry.authenticate(value["token"]), value["installation_id"])
        self.assertIsNone(self.registry.authenticate(impostor["token"]))

    def test_registration_has_bounded_capacity_and_new_source_rate(self):
        limited = InstallationRegistry(self.path, max_installations=1)
        value = registration()
        limited.register(value)
        with self.assertRaisesRegex(EnrollmentError, "capacity"):
            limited.register(registration())
        self.assertFalse(limited.register(value))
        for _ in range(30):
            self.registry.register(registration(), source="new-source")
        with self.assertRaisesRegex(EnrollmentError, "rate_limited"):
            self.registry.register(registration(), source="new-source")

    def test_registration_rejects_malformed_ids_short_keys_and_extra_fields(self):
        for changes in ({"installation_id": "not-uuid"}, {"token": "short"},
                        {"token": "a" * 42 + "\n"}, {"operator_token": "secret"}, {"schema": 2}):
            with self.subTest(changes=changes), self.assertRaises(EnrollmentError):
                self.registry.register({**registration(), **changes})

    def test_untrusted_network_peer_cannot_spoof_rate_limit_address(self):
        self.assertEqual(client_address("192.0.2.1", "198.51.100.2"), "192.0.2.1")
        self.assertEqual(client_address("127.0.0.1", "198.51.100.2"), "198.51.100.2")
        self.assertEqual(client_address("::1", "bad value"), "unknown")


class EnrollmentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ReceiverStore(self.root / "inbox.sqlite3")
        self.registry = InstallationRegistry(self.store.path)
        catalog = self.root / "rooms.json"
        catalog.write_text(json.dumps({"schema": 1, "rooms": [{"id": "test", "server_names": [],
            "endpoints": [{"host": "game.example.invalid", "port": 7552}]}]}), encoding="utf-8")
        self.server = create_server(self.store, "private-operator-key", installations=self.registry,
                                    catalog=CatalogSource(catalog))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.value = registration()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, path, *, value=None, token=None, headers=None):
        outgoing = {"Content-Type": "application/json", **(headers or {})}
        if token:
            outgoing["Authorization"] = "Bearer " + token
        request = Request(self.base + path, data=json.dumps(value).encode() if value is not None else None,
                          headers=outgoing)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, json.loads(response.read())

    def enroll(self):
        status, headers, body = self.request(INSTALLATIONS_PATH, value=self.value)
        self.assertEqual(status, 201)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, {"schema": 1, "installation_id": self.value["installation_id"], "status": "registered"})
        return self.value["token"]

    def test_new_client_registers_and_reads_rooms_without_operator_secret(self):
        token = self.enroll()
        self.assertEqual(self.request(INSTALLATIONS_PATH, value=self.value)[0], 200)
        for path in ("/api/companion/v3/rooms", "/api/companion/v3/catalog"):
            self.assertEqual(self.request(path, token=token)[0], 200)
            self.assertEqual(self.request(path)[0], 401)
        for path in (API_PATH + "/" + "a" * 64, "/api/companion/v3/combat/players/123"):
            self.assertEqual(self.request(path, token=token)[0], 401)

    def test_installed_client_can_submit_only_its_own_reports(self):
        token = self.enroll()
        report = payload()
        report["installation_id"] = self.value["installation_id"]
        status, _, receipt = self.request(API_PATH, value=report, token=token,
            headers={"Idempotency-Key": report["submission_id"]})
        self.assertEqual(status, 202)
        self.assertEqual(receipt["status"], "pending_match")
        other = {**report, "submission_id": "b" * 64, "installation_id": str(uuid.uuid4())}
        self.assertEqual(self.request(API_PATH, value=other, token=token,
            headers={"Idempotency-Key": other["submission_id"]})[0], 403)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)

    def test_schema_probe_is_rejected_without_recording_a_report(self):
        token = self.enroll()
        status, _, value = self.request(API_PATH, value={"schema": "probe", "submission_id": "probe"}, token=token,
            headers={"Idempotency-Key": "probe"})
        self.assertEqual((status, value.get("error")), (400, "unsupported_schema"))
        self.assertEqual(self.store.status()["submissions"]["total"], 0)

    def test_receipt_endpoint_is_owner_scoped_and_never_exposes_report_data(self):
        token = self.enroll()
        report = payload()
        report['installation_id'] = self.value['installation_id']
        self.request(API_PATH, value=report, token=token,
                     headers={'Idempotency-Key': report['submission_id']})
        path = RECEIPTS_PATH + '/' + report['submission_id']
        status, headers, receipt = self.request(path, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(receipt, {'submission_id': report['submission_id'], 'status': 'pending_match',
                                   'pending_reason': 'archive_not_configured'})
        other = registration()
        self.registry.register(other)
        self.assertEqual(self.request(path, token=other['token'])[0], 404)
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request(RECEIPTS_PATH + '/' + 'f' * 64, token=token)[0], 404)
        self.assertEqual(self.request(path, token='private-operator-key')[2], receipt)
        self.assertEqual(self.request(API_PATH + '/' + report['submission_id'], token=token)[0], 401)
        self.assertEqual(set(receipt), {'submission_id', 'status', 'pending_reason'})

    def test_owned_receipt_updates_after_late_archive_without_resending_report(self):
        token = self.enroll()
        report = payload()
        report['installation_id'] = self.value['installation_id']
        self.request(API_PATH, value=report, token=token,
                     headers={'Idempotency-Key': report['submission_id']})
        path = RECEIPTS_PATH + '/' + report['submission_id']
        self.assertEqual(self.request(path, token=token)[2]['status'], 'pending_match')
        archive = self.root / 'late-server.sqlite3'
        native_archive(archive, room='test-room')
        self.store.archive_index = ArchiveIndex(archive)
        self.store.reconcile()
        status, _, receipt = self.request(path, token=token)
        self.assertEqual((status, receipt), (200, {'submission_id': report['submission_id'], 'status': 'accepted'}))
        self.assertEqual(self.store.status()['submissions']['total'], 1)
        self.assertEqual(self.store.report(report['submission_id'])['report'], report)

    def test_oversized_registration_is_rejected_and_conflicting_key_preserved(self):
        self.enroll()
        conflict = {**self.value, "token": secrets.token_urlsafe(32)}
        self.assertEqual(self.request(INSTALLATIONS_PATH, value=conflict)[0], 409)
        self.assertEqual(self.request(INSTALLATIONS_PATH, value={"data": "x" * 5000})[0], 413)


if __name__ == "__main__":
    unittest.main()
