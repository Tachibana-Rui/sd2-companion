"""Durable retries and authenticated delivery, using loopback fixtures only."""

import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from companion.outbox import Outbox
from companion.upload import UploadError, Uploader


def submission(identifier="submission-1", **extra):
    return {"submission_id": identifier, "report": {"events": [], "scope": "whole_match"}, **extra}


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd2-companion-delivery-")
        self.path = Path(self.temporary.name) / "outbox.sqlite3"
        self.now = 1000.0
        self.box = Outbox(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temporary.cleanup()

    def test_installation_identity_and_immutable_payload_survive_restart(self):
        report = submission()
        self.assertTrue(self.box.enqueue(report))
        report["report"]["events"].append({"local_mutation": True})
        restored = Outbox(self.path, clock=lambda: self.now)
        self.assertEqual(restored.installation_id, self.box.installation_id)
        self.assertEqual(restored.due()[0]["payload"], submission())
        self.assertFalse(restored.enqueue(submission()))
        self.assertEqual(restored.status()["pending"], 1)

    def test_same_id_different_content_is_rejected_without_overwriting(self):
        self.box.enqueue(submission())
        with self.assertRaisesRegex(ValueError, "submission_id_content_conflict"):
            self.box.enqueue(submission(different=True))
        self.assertEqual(self.box.due()[0]["payload"], submission())

    def test_retry_schedule_and_server_retry_after_survive_restart(self):
        self.box.enqueue(submission())
        self.assertTrue(self.box.mark_retry("submission-1", "network_error", now=self.now))
        self.assertEqual(self.box.due(), [])
        self.assertEqual(self.box.due(now=1005)[0]["attempts"], 1)
        self.box.mark_retry("submission-1", "http_429", now=1005, retry_after=120)
        restored = Outbox(self.path, clock=lambda: self.now)
        self.assertEqual(restored.due(now=1124), [])
        self.assertEqual(restored.due(now=1125)[0]["attempts"], 2)

    def test_sent_and_rejected_are_persistent_and_never_requeued_by_duplicates(self):
        self.box.enqueue(submission("accepted"))
        self.box.enqueue(submission("rejected"))
        self.box.mark_sent("accepted", {"submission_id": "accepted", "status": "pending_match"})
        self.box.mark_rejected("rejected", "http_403")
        restored = Outbox(self.path, clock=lambda: self.now)
        self.assertFalse(restored.enqueue(submission("accepted")))
        self.assertFalse(restored.enqueue(submission("rejected")))
        self.assertEqual(restored.due(now=100000), [])
        self.assertEqual((restored.status()["sent"], restored.status()["rejected"]), (1, 1))
        self.assertFalse(restored.mark_retry("accepted", "network_error"))

    def test_receipt_must_acknowledge_exact_job(self):
        self.box.enqueue(submission())
        with self.assertRaisesRegex(ValueError, "invalid_receipt"):
            self.box.mark_sent("submission-1", {"submission_id": "another", "status": "accepted"})
        self.assertEqual(self.box.status()["pending"], 1)

    def test_bad_payload_never_enters_database(self):
        for invalid in ([], submission("bad\r\nid"), submission(value=float("nan"))):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    self.box.enqueue(invalid)
        self.assertEqual(self.box.status()["total"], 0)

    def test_saved_payload_fingerprint_is_checked_before_send(self):
        self.box.enqueue(submission())
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE companion_outbox SET payload=?", (json.dumps(submission(changed=True)),))
            db.commit()
        with self.assertRaisesRegex(ValueError, "invalid_outbox_payload"):
            self.box.due()

    def test_concurrent_enqueue_has_one_durable_submission(self):
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def enqueue():
            try:
                barrier.wait(timeout=3)
                results.append(self.box.enqueue(submission()))
            except Exception as exc:
                failures.append(type(exc).__name__)

        threads = [threading.Thread(target=enqueue) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.box.status()["total"], 1)

    def test_explicit_retry_preserves_content_identity_and_attempts_across_restart(self):
        for identifier in ("rejected-one", "rejected-two", "pending", "sent"):
            self.box.enqueue(submission(identifier))
        self.box.mark_retry("rejected-one", "network_error", now=self.now)
        self.box.mark_rejected("rejected-one", "http_401")
        self.box.mark_rejected("rejected-two", "http_404")
        self.box.mark_sent("sent", {"submission_id": "sent", "status": "accepted"})

        self.assertEqual(self.box.retry_rejected(limit=1, now=2000), 1)
        restored = Outbox(self.path, clock=lambda: self.now)
        self.assertEqual(restored.status()["rejected"], 1)
        self.assertEqual(restored.status()["sent"], 1)
        self.assertEqual([job["id"] for job in restored.due(now=1999)], ["pending"])
        retried = {job["id"]: job for job in restored.due(now=2000)}
        self.assertEqual(retried["rejected-one"]["attempts"], 2)
        self.assertEqual(retried["rejected-one"]["payload"], submission("rejected-one"))
        self.assertFalse(restored.enqueue(submission("rejected-one")))
        self.assertEqual(restored.retry_rejected(now=2001), 1)
        self.assertEqual(restored.retry_rejected(now=2001), 0)
        self.assertEqual(restored.status()["sent"], 1)

    def test_retry_rejected_validates_time_and_limit(self):
        for options in ({"limit": 0}, {"limit": True}, {"limit": 1001},
                        {"now": float("nan")}, {"now": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.box.retry_rejected(**options)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body)
        type(self).requests.append({"path": self.path, "body": payload,
                                    "authorization": self.headers.get("Authorization"),
                                    "idempotency": self.headers.get("Idempotency-Key")})
        status = 200
        headers = {}
        content_type = "application/json"
        receipt = {"submission_id": payload["submission_id"], "status": "accepted"}
        if self.path == "/pending":
            status, receipt["status"] = 202, "pending_match"
        elif self.path == "/duplicate":
            receipt["status"] = "duplicate"
        elif self.path == "/wrong_id":
            receipt["submission_id"] = "some-other-submission"
        elif self.path == "/wrong_status":
            receipt["status"] = "received_maybe"
        elif self.path == "/retry":
            status, headers["Retry-After"] = 429, "120"
        elif self.path == "/unavailable":
            status = 503
        elif self.path == "/rejected":
            status = 422
        elif self.path == "/unauthorized":
            status = 401
        elif self.path == "/redirect":
            status = 307
            headers["Location"] = "http://localhost:" + str(self.server.server_port) + "/redirect_destination"
        elif self.path == "/same_origin_redirect":
            status = 302
            headers["Location"] = "/redirect_destination"
        elif self.path == "/html":
            content_type = "text/html"
        encoded = json.dumps(receipt).encode("utf-8")
        if self.path == "/too_large":
            encoded = b" " * 2049
        elif self.path == "/malformed":
            encoded = b"{not JSON"
        elif self.path == "/echo":
            receipt["message"] = self.headers.get("Authorization")
            encoded = json.dumps(receipt).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)


class UploaderTests(unittest.TestCase):
    token = "test-companion-token-no-real-credential"

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        _Handler.requests = []

    def uploader(self, path="/accepted", **options):
        return Uploader(self.base + path, self.token, allow_loopback_http=True, **options)

    def test_accepted_receipt_and_required_authentication_headers(self):
        self.assertEqual(self.uploader().send(submission()), {"submission_id": "submission-1", "status": "accepted"})
        captured = _Handler.requests[-1]
        self.assertEqual(captured["authorization"], "Bearer " + self.token)
        self.assertEqual(captured["idempotency"], "submission-1")
        self.assertEqual(captured["body"], submission())

    def test_pending_and_duplicate_are_durable_successes(self):
        for endpoint, expected in (("/pending", "pending_match"), ("/duplicate", "duplicate")):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.uploader(endpoint).send(submission())["status"], expected)

    def test_dedicated_environment_token_and_redacted_repr(self):
        with patch.dict(os.environ, {"SD2_TEST_REPORT_TOKEN": self.token}):
            uploader = Uploader(self.base + "/accepted", token_env="SD2_TEST_REPORT_TOKEN", allow_loopback_http=True)
            self.assertEqual(uploader.send(submission())["status"], "accepted")
            self.assertNotIn(self.token, repr(uploader))

    def test_http_requires_explicit_loopback_test_opt_in(self):
        for endpoint, options in ((self.base, {}), ("http://example.com/upload", {"allow_loopback_http": True}),
                                  ("https://user:password@example.com/upload", {})):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    Uploader(endpoint, self.token, **options)
        self.assertEqual(_Handler.requests, [])

    def test_cross_origin_and_same_origin_redirects_never_forward_token(self):
        for endpoint in ("/redirect", "/same_origin_redirect"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(UploadError) as caught:
                    self.uploader(endpoint).send(submission())
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.code, "redirect_rejected")
        self.assertEqual([item["path"] for item in _Handler.requests], ["/redirect", "/same_origin_redirect"])

    def test_transient_and_permanent_http_failures(self):
        for endpoint, expected, retryable in (("/retry", 429, True), ("/unavailable", 503, True), ("/rejected", 422, False)):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(UploadError) as caught:
                    self.uploader(endpoint).send(submission())
                failure = caught.exception
                self.assertEqual(failure.status_code, expected)
                self.assertEqual(failure.retryable, retryable)
                self.assertNotIn(self.token, str(failure))
                if expected == 429:
                    self.assertEqual(failure.retry_after, 120)

    def test_wrong_receipt_cannot_mark_a_submission_delivered(self):
        for endpoint in ("/wrong_id", "/wrong_status", "/malformed", "/html"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(UploadError) as caught:
                    self.uploader(endpoint).send(submission())
                self.assertTrue(caught.exception.retryable)

    def test_bounded_response_and_request_size(self):
        with self.assertRaises(UploadError) as caught:
            self.uploader("/too_large", max_response_bytes=1024).send(submission())
        self.assertEqual(caught.exception.code, "receipt_too_large")
        requests_before = len(_Handler.requests)
        with self.assertRaisesRegex(ValueError, "submission_too_large"):
            self.uploader(max_payload_bytes=100).send(submission(large="x" * 1000))
        self.assertEqual(len(_Handler.requests), requests_before)

    def test_arbitrary_receipt_text_is_not_returned_or_logged(self):
        receipt = self.uploader("/echo").send(submission())
        self.assertEqual(set(receipt), {"submission_id", "status"})
        self.assertNotIn(self.token, json.dumps(receipt))

    def test_invalid_token_never_reaches_transport(self):
        for token in ("", "secret\r\nInjected: yes", "has a space"):
            with self.subTest(length=len(token)):
                with self.assertRaises(ValueError):
                    Uploader(self.base, token, allow_loopback_http=True)
        self.assertEqual(_Handler.requests, [])

    def test_transport_timeout_and_protocol_failure_are_redacted_retries(self):
        for failure in (URLError(socket.timeout(self.token)), http.client.IncompleteRead(b"", 10)):
            with self.subTest(failure=type(failure).__name__):
                uploader = self.uploader()
                with patch.object(uploader._opener, "open", side_effect=failure):
                    with self.assertRaises(UploadError) as caught:
                        uploader.send(submission())
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn(self.token, str(caught.exception))
        self.assertEqual(_Handler.requests, [])

    def test_response_operation_budget_is_checked_between_reads(self):
        uploader = self.uploader(timeout=1)
        with patch("companion.upload.time.monotonic", side_effect=[100, 102]):
            with self.assertRaises(UploadError) as caught:
                uploader.send(submission())
        self.assertEqual(caught.exception.code, "upload_timeout")
        self.assertTrue(caught.exception.retryable)

    def test_repaired_endpoint_can_deliver_explicitly_retried_rejection(self):
        with tempfile.TemporaryDirectory(prefix="sd2-companion-repaired-delivery-") as directory:
            box = Outbox(Path(directory) / "outbox.sqlite3")
            original = submission()
            box.enqueue(original)
            job = box.due()[0]
            with self.assertRaises(UploadError) as caught:
                self.uploader("/unauthorized").send(job["payload"])
            self.assertFalse(caught.exception.retryable)
            box.mark_rejected(job["id"], caught.exception.code)
            self.assertEqual(box.due(), [])

            self.assertEqual(box.retry_rejected(), 1)
            retried = box.due()[0]
            self.assertEqual(retried["payload"], original)
            self.assertEqual(retried["attempts"], 1)
            receipt = self.uploader().send(retried["payload"])
            box.mark_sent(retried["id"], receipt)
            self.assertEqual(box.status()["sent"], 1)
            self.assertEqual(box.retry_rejected(), 0)


if __name__ == "__main__":
    unittest.main()
