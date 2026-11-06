"""Server-issued room catalog: parse rules, memory-only client, served endpoint."""

import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from companion.catalog import (CatalogError, RemoteCatalog, parse_catalog, parse_timestamp,
                               sync, with_tunables)
from companion.config import ConfigError, Endpoint, Room, from_dict, parse_rooms
from companion.receiver import CATALOG_PATH, ROOMS_PATH, CatalogSource, ReceiverStore, create_server


def rooms_document(*, rooms=None, settings=None, expires_at=None):
    document = {"schema": 1,
                "rooms": rooms if rooms is not None else [
                    {"id": "room-one", "endpoints": [{"host": "jp1.example.invalid", "port": 7552}],
                     "server_names": ["sd2-jp-test"]}]}
    if settings is not None:
        document["settings"] = settings
    if expires_at is not None:
        document["expires_at"] = expires_at
    return document


def response(status=200, body=None, etag=None):
    headers = {}
    if etag:
        headers["ETag"] = etag
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    return status, headers, payload


class ParseTests(unittest.TestCase):
    def test_a_room_table_is_accepted_and_normalised(self):
        rooms, settings, expires_at = parse_catalog(rooms_document(settings={"poll_seconds": 1.5}), now=0)
        self.assertEqual(rooms, (Room("room-one", (Endpoint("jp1.example.invalid", 7552),), ("sd2-jp-test",)),))
        self.assertEqual(settings, {"poll_seconds": 1.5})
        self.assertIsNone(expires_at)

    def test_only_known_tunables_inside_their_range_are_accepted(self):
        for settings in ({"upload_url": "https://evil.invalid/x"}, {"poll_seconds": 0},
                         {"poll_seconds": True}, {"process_poll_seconds": 5000}):
            with self.subTest(settings=settings), self.assertRaises(CatalogError) as caught:
                parse_catalog(rooms_document(settings=settings), now=0)
            self.assertFalse(caught.exception.retryable)

    def test_schema_rooms_and_expiry_are_enforced(self):
        cases = (({"schema": 2, "rooms": []}, "catalog_schema_unsupported"),
                 ({"schema": 1, "rooms": []}, "catalog_without_rooms"),
                 ({"schema": 1, "rooms": [{"id": "Bad Id"}]}, "catalog_invalid_room_id"),
                 (rooms_document(expires_at="2026-01-01T00:00:00Z"), "catalog_expired"))
        for document, code in cases:
            with self.subTest(code=code), self.assertRaises(CatalogError) as caught:
                parse_catalog(document, now=1793810000)
            self.assertEqual(caught.exception.code, code)

    def test_timestamps_need_an_explicit_zone(self):
        self.assertEqual(parse_timestamp("2026-11-05T00:00:00Z"), 1793836800.0)
        for value in ("2026-11-05T00:00:00", "2026-11-05", "yesterday", 1793836800):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                parse_timestamp(value)


class RemoteCatalogTests(unittest.TestCase):
    def test_not_modified_updates_sync_time_but_never_extends_expired_body(self):
        value = rooms_document(expires_at="2026-11-05T00:10:00Z")
        catalog = self.catalog(lambda *args: response(body=value, etag='"abc"'))
        with patch("companion.catalog.time.time", return_value=1793836800):
            self.assertTrue(catalog.refresh(now=100))
        # Refresh leaves 30 seconds for a retry before expiry.
        self.assertEqual(catalog.next_refresh, 670)
        catalog._fetch = lambda *args: response(status=304)
        with patch("companion.catalog.time.time", return_value=1793837000):
            self.assertFalse(catalog.refresh(now=300))
        self.assertEqual(catalog.last_success, 1793837000)
        self.assertEqual(catalog.next_refresh, 670)
        with patch("companion.catalog.time.time", return_value=1793837400):
            self.assertFalse(catalog.refresh(now=700))
        self.assertEqual(catalog.last_error, "catalog_expired")
        self.assertIsNone(catalog.etag)
        self.assertEqual(catalog.next_refresh, 730)
        self.assertEqual(len(catalog.rooms), 1)

    def test_duplicate_response_properties_are_rejected(self):
        catalog = self.catalog(lambda *args: (200, {}, b'{"schema":1,"schema":2,"rooms":[]}'))
        catalog.refresh(now=1)
        self.assertEqual(catalog.last_error, "catalog_duplicate_property")

    def catalog(self, fetch, *, token="fixture-token", interval=600, clock=lambda: 1000.0):
        return RemoteCatalog("https://sd2.example.invalid/api/companion/v3/catalog", token=token,
                             interval=interval, fetch=fetch, clock=clock)

    def test_a_fresh_document_replaces_the_table(self):
        calls = []

        def fetch(url, token, timeout, etag):
            calls.append((url, token, etag))
            return response(body=rooms_document(settings={"poll_seconds": 2}), etag='"abc"')

        catalog = self.catalog(fetch)
        self.assertTrue(catalog.refresh(now=1000.0))
        self.assertEqual(catalog.state, "ok")
        self.assertEqual([room.id for room in catalog.rooms], ["room-one"])
        self.assertEqual(catalog.settings, {"poll_seconds": 2.0})
        self.assertEqual(catalog.etag, '"abc"')
        self.assertEqual(calls, [("https://sd2.example.invalid/api/companion/v3/catalog",
                                  "fixture-token", None)])

    def test_an_unchanged_document_is_not_reported_as_a_change(self):
        catalog = self.catalog(lambda *args: response(body=rooms_document()))
        self.assertTrue(catalog.refresh(now=1000.0))
        self.assertFalse(catalog.refresh(now=2000.0))

    def test_not_modified_keeps_the_previous_table_and_extends_the_interval(self):
        catalog = self.catalog(lambda *args: response(body=rooms_document(), etag='"abc"'))
        catalog.refresh(now=1000.0)
        seen = []

        def fetch(url, token, timeout, etag):
            seen.append(etag)
            return 304, {"ETag": '"abc"'}, b""

        catalog._fetch = fetch
        self.assertFalse(catalog.refresh(now=1200.0))
        self.assertEqual(seen, ['"abc"'])
        self.assertEqual(catalog.state, "ok")
        self.assertEqual(len(catalog.rooms), 1)
        self.assertEqual(catalog.next_refresh, 1200.0 + 600.0)

    def test_failures_keep_the_last_good_table_and_back_off(self):
        failures = {(401, b'{"error":"unauthorized"}'): ("unauthorized", "catalog_http_401"),
                    (503, b"{}"): ("unreachable", "catalog_http_503"),
                    (200, b"not json"): ("invalid", "catalog_invalid"),
                    (200, json.dumps({"schema": 9}).encode()): ("invalid", "catalog_schema_unsupported")}
        catalog = self.catalog(lambda *args: response(body=rooms_document()))
        catalog.refresh(now=1000.0)
        for (status, payload), expected in failures.items():
            with self.subTest(status=status):
                catalog._fetch = lambda *args, status=status, payload=payload: (status, {}, payload)
                catalog.refresh(now=2000.0)
                self.assertEqual((catalog.state, catalog.last_error), expected)
                self.assertEqual([room.id for room in catalog.rooms], ["room-one"])
                self.assertEqual(catalog.next_refresh, 2000.0 + (60.0 if status == 503 else 600.0))

    def test_a_network_error_and_a_missing_credential_never_raise(self):
        def boom(*args):
            raise CatalogError("catalog_unreachable")

        catalog = self.catalog(boom)
        catalog.refresh(now=1000.0)
        self.assertEqual((catalog.state, catalog.last_error), ("unreachable", "catalog_unreachable"))
        self.assertEqual(catalog.rooms, ())
        catalog = self.catalog(lambda *args: self.fail("no request may be made"), token=None)
        catalog.refresh(now=1000.0)
        self.assertEqual((catalog.state, catalog.last_error), ("no_credentials", "catalog_token_missing"))

    def test_the_url_must_be_https_unless_loopback_is_allowed(self):
        with self.assertRaises(CatalogError):
            RemoteCatalog("http://sd2.example.invalid/catalog", token="x")
        RemoteCatalog("http://127.0.0.1:9/catalog", token="x", allow_loopback_http=True)
        for suffix in ("?token=x", "#fragment"):
            with self.assertRaises(CatalogError):
                RemoteCatalog("https://sd2.example.invalid/catalog" + suffix, token="x")

    def test_refresh_if_due_only_fetches_when_the_interval_elapsed(self):
        calls = []

        def fetch(*args):
            calls.append(1)
            return response(body=rooms_document())

        catalog = self.catalog(fetch)
        self.assertTrue(sync(catalog, 1000.0))
        self.assertFalse(sync(catalog, 1100.0))
        self.assertEqual(len(calls), 1)
        sync(catalog, 1700.0)
        self.assertEqual(len(calls), 2)

    def test_pushed_tunables_replace_only_changed_fields(self):
        config = from_dict({"schema": 1, "rooms": [], "replay_directories": [], "poll_seconds": 0.5})
        updated = with_tunables(config, {"poll_seconds": 2.0})
        self.assertEqual(updated.poll_seconds, 2.0)
        self.assertIs(with_tunables(config, {}), config)
        self.assertIs(with_tunables(updated, {"poll_seconds": 2.0}), updated)


class ServedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd2-catalog-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "rooms.json"
        self.path.write_text(json.dumps(rooms_document(settings={"process_poll_seconds": 5})), encoding="utf-8")
        self.store = ReceiverStore(self.base / "inbox.sqlite3")
        self.server = create_server(self.store, "fixture-token", catalog=CatalogSource(self.path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, headers=None, path=CATALOG_PATH):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            reply = connection.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            connection.close()

    @property
    def authorized(self):
        return {"Authorization": "Bearer fixture-token"}

    def test_the_catalog_requires_the_upload_credential(self):
        self.assertEqual(self.get()[0], 401)
        status, headers, body = self.get(self.authorized)
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["rooms"][0]["id"], "room-one")
        self.assertEqual(document["settings"], {"process_poll_seconds": 5.0})
        self.assertTrue(document["generated_at"].endswith("Z"))
        self.assertTrue(document["expires_at"].endswith("Z"))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_the_designed_rooms_path_requires_the_same_upload_credential(self):
        self.assertEqual(self.get(path=ROOMS_PATH)[0], 401)
        status, headers, body = self.get(self.authorized, path=ROOMS_PATH)
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["rooms"][0]["id"], "room-one")
        self.assertNotIn("signature", value)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, _ = self.get({**self.authorized, "If-None-Match": headers["ETag"]}, path=ROOMS_PATH)
        self.assertEqual(status, 304)

    def test_old_unchanged_room_file_issues_a_fresh_document(self):
        # Regression: mtime + 600 expired every deployment after ten minutes.
        os.utime(self.path, (1, 1))
        now = 1793836800
        self.server.catalog._clock = lambda: now
        status, _, body = self.get(self.authorized)
        self.assertEqual(status, 200)
        value = json.loads(body)
        rooms, _, expires = parse_catalog(value, now=now)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(expires, now + 600)

    def test_expiry_window_reissues_document_and_etag_without_a_file_edit(self):
        now = [1793836800]
        self.server.catalog._clock = lambda: now[0]
        _, headers, original = self.get(self.authorized)
        original_etag = headers["ETag"]
        now[0] += 570
        status, headers, body = self.get({**self.authorized, "If-None-Match": original_etag})
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["ETag"], original_etag)
        value = json.loads(body)
        self.assertGreater(parse_timestamp(value["expires_at"]), parse_timestamp(json.loads(original)["expires_at"]))

    def test_client_loads_rooms_endpoint_over_authenticated_http(self):
        url = "http://127.0.0.1:%d%s" % (self.server.server_port, ROOMS_PATH)
        catalog = RemoteCatalog(url, token="fixture-token", allow_loopback_http=True)
        self.assertTrue(catalog.refresh(now=1))
        self.assertEqual(catalog.state, "ok")
        self.assertEqual(len(catalog.rooms), 1)

    def test_a_matching_etag_answers_304_and_an_edit_changes_it(self):
        status, headers, _ = self.get(self.authorized)
        etag = headers["ETag"]
        status, _, body = self.get({**self.authorized, "If-None-Match": etag})
        self.assertEqual((status, body), (304, b""))
        self.path.write_text(json.dumps(rooms_document(
            rooms=[{"id": "room-two", "endpoints": [{"host": "jp2.example.invalid", "port": 10804}],
                    "server_names": []}])), encoding="utf-8")
        status, headers, body = self.get({**self.authorized, "If-None-Match": etag})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rooms"][0]["id"], "room-two")
        self.assertNotEqual(headers["ETag"], etag)

    def test_a_client_picks_the_table_up_over_the_loopback_endpoint(self):
        url = "http://127.0.0.1:%d%s" % (self.server.server_port, CATALOG_PATH)
        catalog = RemoteCatalog(url, token="fixture-token", allow_loopback_http=True)
        self.assertTrue(catalog.refresh(now=1000.0))
        self.assertEqual(catalog.state, "ok")
        self.assertEqual([room.id for room in catalog.rooms], ["room-one"])
        self.assertEqual(catalog.settings, {"process_poll_seconds": 5.0})
        # A second pass with the stored ETag is a cheap 304 and keeps the table.
        self.assertFalse(catalog.refresh(now=1600.0))
        self.assertEqual((catalog.state, len(catalog.rooms)), ("ok", 1))

    def test_a_wrong_credential_is_reported_as_unauthorized(self):
        url = "http://127.0.0.1:%d%s" % (self.server.server_port, CATALOG_PATH)
        catalog = RemoteCatalog(url, token="wrong-token", allow_loopback_http=True)
        catalog.refresh(now=1000.0)
        self.assertEqual((catalog.state, catalog.last_error), ("unauthorized", "catalog_http_401"))
        self.assertEqual(catalog.rooms, ())

    def test_without_a_catalog_file_the_endpoint_reports_unavailable(self):
        source = CatalogSource(self.base / "missing.json")
        with self.assertRaises(Exception) as caught:
            source.document()
        self.assertEqual(getattr(caught.exception, "code", None), "catalog_unavailable")
        self.assertEqual(source.status(), {"configured": True, "available": False,
                                           "error": "catalog_unavailable"})

    def test_invalid_catalog_files_are_rejected_rather_than_served(self):
        self.path.write_text(json.dumps({"schema": 1, "rooms": []}), encoding="utf-8")
        with self.assertRaises(Exception) as caught:
            CatalogSource(self.path).document()
        self.assertEqual(getattr(caught.exception, "code", None), "catalog_without_rooms")

    def test_health_reports_the_served_table(self):
        status, _, body = self.get(path="/health")
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(document["catalog"]["room_count"], 1)
        self.assertTrue(document["catalog"]["available"])


class ConfigIntegrationTests(unittest.TestCase):
    def test_a_config_may_have_no_rooms_when_it_uses_the_catalog(self):
        config = from_dict({"schema": 1, "rooms": [], "replay_directories": [],
                            "catalog_url": "https://sd2.example.invalid/api/companion/v3/catalog"})
        self.assertEqual(config.rooms, ())
        self.assertEqual(config.catalog_interval_seconds, 600.0)

    def test_a_plain_http_catalog_url_needs_the_loopback_opt_in(self):
        with self.assertRaises(ConfigError):
            from_dict({"schema": 1, "catalog_url": "http://sd2.example.invalid/catalog"})
        config = from_dict({"schema": 1, "catalog_url": "http://127.0.0.1:8770/api/companion/v3/catalog",
                            "allow_loopback_http": True})
        self.assertIsNotNone(config.catalog_url)

    def test_the_interval_is_bounded(self):
        for value in (10, 100000):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                from_dict({"schema": 1, "catalog_interval_seconds": value})

    def test_rooms_shared_parser_rejects_incomplete_endpoints(self):
        with self.assertRaises(ConfigError):
            parse_rooms([{"id": "room-one", "endpoints": []}])


if __name__ == "__main__":
    unittest.main()
