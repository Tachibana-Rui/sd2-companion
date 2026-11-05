"""Ready-to-run defaults, explicit local mode and the replay folder menu."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.config import (ConfigError, DEFAULT_CATALOG_URL, DEFAULT_UPLOAD_URL,
                              decode_config, example_config, from_dict, load)
from companion.desktop import DesktopApp, StatusModel, _status_text, prepare_config


class DefaultConnectionTests(unittest.TestCase):
    def test_automatic_connection_retry_does_not_prompt_for_manual_credentials(self):
        from companion.cli import _catalog_event
        from companion.desktop import _safe_event, catalog_text
        remote = SimpleNamespace(state="no_credentials", rooms=(), last_success=None, last_error="catalog_token_missing")
        worker = SimpleNamespace(state="retry", token_if_ready=lambda: None)
        model = StatusModel()
        model.update(_safe_event(_catalog_event(remote, worker)))
        self.assertIn("自动重试", catalog_text(model, "zh"))
        self.assertNotIn("缺少上传凭证", catalog_text(model, "zh"))

    def test_automatic_retry_error_has_bilingual_status_and_keeps_its_diagnostic_code(self):
        model = StatusModel()
        model.update({"event": "client_registration_retry", "code": "automatic_registration_retry"})
        self.assertEqual(model.code, "automatic_registration_retry")
        self.assertIn("automatically", _status_text(model, "en"))
        self.assertIn("自动重试", _status_text(model, "zh"))

    def test_new_desktop_config_has_official_apis_and_no_game_addresses_or_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "companion.json"
            config = prepare_config(path)
            self.assertTrue(config.online_enabled)
            self.assertEqual(config.upload_url, DEFAULT_UPLOAD_URL)
            self.assertEqual(config.catalog_url, DEFAULT_CATALOG_URL)
            raw = decode_config(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["rooms"], [])
            self.assertNotIn("token", raw)
            self.assertEqual(raw, example_config())

    def test_legacy_null_defaults_upgrade_in_memory_and_keep_report_directory(self):
        for null_urls in ({}, {"upload_url": None, "catalog_url": None}):
            with self.subTest(null_urls=null_urls), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "companion.json"
                raw = {"schema": 1, "rooms": [], "data_directory": "existing-captures", **null_urls}
                original = json.dumps(raw)
                path.write_text(original, encoding="utf-8")
                config = prepare_config(path)
                self.assertEqual(config.upload_url, DEFAULT_UPLOAD_URL)
                self.assertEqual(config.catalog_url, DEFAULT_CATALOG_URL)
                self.assertEqual(config.data_directory, path.parent / "existing-captures")
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_explicit_local_mode_disables_both_apis_without_losing_custom_settings(self):
        raw = {"schema": 1, "online_enabled": False,
               "upload_url": "https://custom.invalid/api/task-reports",
               "catalog_url": "https://custom.invalid/api/rooms", "data_directory": "old-data"}
        local = from_dict(raw)
        self.assertFalse(local.online_enabled)
        self.assertIsNone(local.upload_url)
        self.assertIsNone(local.catalog_url)
        enabled = from_dict({**raw, "online_enabled": True})
        self.assertEqual(enabled.upload_url, raw["upload_url"])
        self.assertEqual(enabled.catalog_url, raw["catalog_url"])
        self.assertEqual(local.data_directory, enabled.data_directory)

    def test_custom_connection_and_legacy_catalog_are_retained(self):
        raw = {"schema": 1, "upload_url": "https://custom.invalid/v3/task-reports",
               "catalog_url": "https://custom.invalid/v3/catalog"}
        config = from_dict(raw)
        self.assertEqual(config.upload_url, raw["upload_url"])
        self.assertEqual(config.catalog_url, raw["catalog_url"])
        self.assertEqual(from_dict({"schema": 1, "upload_url": raw["upload_url"]}).catalog_url,
                         "https://custom.invalid/v3/rooms")

    def test_online_mode_rejects_non_boolean_values(self):
        for value in (0, "false", None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "invalid_online_enabled"):
                from_dict({"schema": 1, "online_enabled": value})


class ReplayFolderMenuTests(unittest.TestCase):
    def app(self):
        app = object.__new__(DesktopApp)
        app.language = "zh"
        return app

    def test_file_menu_registers_the_replay_folder_command(self):
        app = self.app()
        app.root = Mock()
        file_menu = Mock()
        app.tk = SimpleNamespace(Menu=Mock(side_effect=[Mock(), file_menu, Mock()]))
        app._add_command = Mock()
        app._build_menu()
        app._add_command.assert_any_call(file_menu, "打开 SD2 本地录像目录", app.open_replays_directory)

    def test_known_folder_path_is_opened_without_creating_or_editing_it(self):
        app = self.app()
        with tempfile.TemporaryDirectory() as temporary:
            relocated = Path(temporary) / "重定向 Saved Games" / "SteelDivision2"
            relocated.mkdir(parents=True)
            with patch("companion.desktop.default_replay_directories", return_value=(relocated,)), \
                    patch("companion.desktop.os.startfile", create=True) as start, \
                    patch("companion.desktop._message") as message:
                app.open_replays_directory()
            start.assert_called_once_with(str(relocated))
            message.assert_not_called()
            self.assertEqual(list(relocated.iterdir()), [])

    def test_missing_directory_shows_a_message_and_does_not_create_it(self):
        app = self.app()
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with patch("companion.desktop.default_replay_directories", return_value=(missing,)), \
                    patch("companion.desktop.os.startfile", create=True) as start, \
                    patch("companion.desktop._message") as message:
                app.open_replays_directory()
            start.assert_not_called()
            self.assertFalse(missing.exists())
            self.assertIn("尚不存在", message.call_args.args[0])

    def test_shell_failure_is_handled_in_the_interface_language(self):
        app = self.app()
        app.language = "en"
        with tempfile.TemporaryDirectory() as temporary:
            with patch("companion.desktop.default_replay_directories", return_value=(Path(temporary),)), \
                    patch("companion.desktop.os.startfile", side_effect=OSError, create=True), \
                    patch("companion.desktop._message") as message:
                app.open_replays_directory()
            self.assertIn("could not be opened", message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
