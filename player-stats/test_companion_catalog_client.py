"""Client catalog bootstrapping, safe UI delivery, and offline capture policy."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.cli import _catalog, _catalog_event, _capture_uploader, _uploader
from companion.config import ConfigError, decode_config, encode_config, from_dict, load
from companion.desktop import CaptureController, StatusModel, catalog_text


UPLOAD = "https://example.invalid/api/companion/v3/task-reports"
ROOMS = "https://example.invalid/api/companion/v3/rooms"


class CatalogBootstrapTests(unittest.TestCase):
    def test_legacy_upload_configuration_derives_room_endpoint_without_rewriting_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.json"
            old = {"schema": 1, "rooms": [], "upload_url": UPLOAD, "catalog_url": None}
            original = encode_config(old)
            path.write_text(original, encoding="utf-8")
            config = load(path)
            self.assertEqual(config.catalog_url, ROOMS)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertIsNone(decode_config(original)["catalog_url"])

    def test_explicit_catalog_url_remains_compatible_and_local_only_has_no_network(self):
        address = "https://example.invalid/api/companion/v3/catalog"
        config = from_dict({"schema": 1, "upload_url": UPLOAD, "catalog_url": address})
        self.assertEqual(config.catalog_url, address)
        local = from_dict({"schema": 1, "online_enabled": False})
        self.assertIsNone(local.catalog_url)
        with patch("companion.cli.get_upload_token") as token:
            self.assertIsNone(_catalog(local))
        token.assert_not_called()

    def test_cli_can_bootstrap_programmatic_config_and_uses_upload_scoped_credential(self):
        config = replace(from_dict({"schema": 1, "upload_url": UPLOAD}), catalog_url=None)
        with patch("companion.cli.get_upload_token", return_value="fixture-secret") as token, \
                patch("companion.catalog.RemoteCatalog") as remote:
            _catalog(config)
        token.assert_called_once_with(UPLOAD, config.token_env)
        self.assertEqual(remote.call_args.args[0], ROOMS)
        self.assertEqual(remote.call_args.kwargs["token"], "fixture-secret")

    def test_missing_upload_credential_keeps_local_capture_but_manual_upload_still_fails(self):
        config = from_dict({"schema": 1, "upload_url": UPLOAD})
        emitted = []
        with patch("companion.cli.get_upload_token", return_value=None):
            self.assertIsNone(_capture_uploader(config, emitted.append))
            with self.assertRaisesRegex(ConfigError, "upload_token_environment_variable_missing"):
                _uploader(config)
        self.assertEqual(emitted, [{"event": "upload_token_environment_variable_missing"}])


class CatalogDesktopDeliveryTests(unittest.TestCase):
    def test_worker_status_survives_both_safe_event_passes_without_exposing_table(self):
        catalog = SimpleNamespace(state="ok", rooms=("not-exported.invalid",),
                                  last_success=1793810000.0, last_error=None)
        controller = CaptureController(object(), Mock())
        controller.publish({**_catalog_event(catalog), "rooms": ["private-address.invalid"],
                            "url": "https://private-address.invalid", "token": "fixture-secret"})
        event = controller.drain()[0]
        self.assertEqual(event["state"], "ok")
        self.assertEqual(event["room_count"], 1)
        self.assertEqual(event["last_success"], 1793810000.0)
        self.assertNotIn("invalid", json.dumps(event))
        self.assertNotIn("fixture-secret", json.dumps(event))
        model = StatusModel()
        previous_status = model.status
        model.update(event)
        self.assertEqual((model.catalog_state, model.catalog_rooms, model.catalog_last_success),
                         ("ok", 1, 1793810000.0))
        self.assertEqual(model.status, previous_status)
        self.assertIn("已同步", catalog_text(model, "zh"))
        self.assertIn("synced", catalog_text(model, "en"))

    def test_failed_refresh_reports_elapsed_minutes_and_keeps_verified_sync_time(self):
        model = StatusModel()
        model.update({"event": "catalog_state", "state": "ok", "room_count": 2,
                      "last_success": 1793810000.0})
        model.update({"event": "catalog_state", "state": "unreachable", "room_count": 2,
                      "last_success": 1793810000.0, "code": "catalog_unreachable"})
        self.assertIn("离线 3 分钟", catalog_text(model, "zh", now=1793810180))
        self.assertIn("offline 3 min", catalog_text(model, "en", now=1793810180))
        self.assertEqual(model.catalog_last_success, 1793810000.0)

    def test_not_configured_and_initial_failures_do_not_claim_a_cached_table(self):
        model = StatusModel()
        model.update(_catalog_event(None))
        self.assertIn("仅本地采集", catalog_text(model, "zh"))
        model.update({"event": "catalog_state", "state": "unreachable", "room_count": 0})
        self.assertIn("本地采集继续", catalog_text(model, "zh"))
        self.assertNotIn("last", catalog_text(model, "en"))

    def test_untrusted_status_fields_do_not_reach_the_model(self):
        controller = CaptureController(object(), Mock())
        for state, count, stamp in (("private-host.invalid", 99, float("nan")),
                                    ("https://private.invalid", -1, True), ([], 99, None)):
            controller.publish({"event": "catalog_state", "state": state, "room_count": count,
                                "last_success": stamp})
            self.assertEqual(controller.drain()[0], {"event": "catalog_state"})


if __name__ == "__main__":
    unittest.main()
