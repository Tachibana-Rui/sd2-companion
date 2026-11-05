import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from companion.config import ConfigError, example_config, from_dict
from companion.config import decode_config
from companion.desktop import DesktopApp, prepare_config
from companion.updater import (UPDATE_URL, Release, UpdateError, check_for_update, download_release,
                               install_on_exit, parse_manifest, version_tuple)


class Opener:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        return io.BytesIO(self.body)


def manifest(body=b"MZ" + b"x" * 1024, **changes):
    return {"schema": 1, "version": "0.6.0", "url": UPDATE_URL.rsplit("/", 1)[0] + "/0.6.0/SD2-Companion.exe",
            "sha256": hashlib.sha256(body).hexdigest(), "size": len(body), **changes}


class UpdateTests(unittest.TestCase):
    def test_checks_public_https_without_upload_credentials(self):
        opener = Opener(json.dumps(manifest()).encode())
        with patch.dict(os.environ, {"SD2_COMPANION_TOKEN": "private-test-token"}):
            release = check_for_update("0.5.0", opener=opener)
        self.assertEqual(release.version, "0.6.0")
        request = opener.requests[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("private-test-token", repr(request.headers))
        self.assertIsNone(check_for_update("0.6.0.0", opener=opener))
        self.assertIsNone(check_for_update("0.7.0", opener=opener))
        self.assertEqual(version_tuple("0.6.0"), version_tuple("0.6.0.0"))

    def test_invalid_and_cross_origin_manifests_are_rejected(self):
        for changes in ({"schema": 2}, {"version": "1.2"}, {"version": "1.2.3 & x"},
                        {"url": "http://sd2.rui-blog.link/file.exe"},
                        {"url": "https://evil.invalid/file.exe"},
                        {"url": "https://user@sd2.rui-blog.link/file.exe"},
                        {"sha256": "a" * 63}, {"size": True}, {"size": 999999999}, {"token": "x"}):
            with self.subTest(changes=changes), self.assertRaises(UpdateError):
                parse_manifest(manifest(**changes))

    def test_download_only_publishes_verified_complete_pe(self):
        body = b"MZ" + b"x" * 1024
        release = parse_manifest(manifest(body))
        with tempfile.TemporaryDirectory() as directory:
            target = download_release(release, directory, opener=Opener(body))
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(list(Path(directory).glob("*.part")), [])
            target.unlink()
            for corrupt in (body[:-1], body + b"extra", b"MZ" + b"y" * 1024):
                with self.subTest(length=len(corrupt)), self.assertRaises(UpdateError):
                    download_release(release, directory, opener=Opener(corrupt))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_oversized_and_unreadable_manifest_fail_safely(self):
        for value in (b"x" * 70000, b"not-json", b"\xff"):
            with self.assertRaises(UpdateError):
                check_for_update("0.5.0", opener=Opener(value))

    def test_automatic_check_defaults_and_configuration(self):
        config = from_dict(example_config())
        self.assertTrue(config.automatic_updates)
        self.assertEqual(config.update_url, UPDATE_URL)
        self.assertEqual(config.update_interval_seconds, 21600)
        self.assertFalse(from_dict({**example_config(), "automatic_updates": False}).automatic_updates)
        for changes in ({"automatic_updates": "yes"}, {"update_url": "http://example.test/update.json"},
                        {"update_interval_seconds": 0}):
            with self.assertRaises(ConfigError):
                from_dict({**example_config(), **changes})

    def test_gui_stores_token_in_windows_store_without_config_or_message_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.json"
            prepare_config(path)
            app = object.__new__(DesktopApp)
            app.config_path = path
            address, token = "https://example.test/api/companion/v3/task-reports", "private-upload-token"
            with patch("companion.credentials.store_upload_token") as store, patch("companion.desktop._message") as message:
                self.assertTrue(app._save_upload_settings(address, token))
                store.assert_called_once_with(address, token)
                self.assertNotIn(token, repr(message.call_args_list))
            raw = decode_config(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["upload_url"], address)
            self.assertEqual(raw["catalog_url"], "https://example.test/api/companion/v3/rooms")
            self.assertNotIn(token, json.dumps(raw))
            with patch("companion.credentials.store_upload_token") as store, patch("companion.desktop._message"):
                self.assertTrue(app._save_upload_settings("", ""))
                store.assert_not_called()
            with patch("companion.credentials.store_upload_token", side_effect=ValueError("safe_code")), patch("companion.desktop._message"):
                self.assertFalse(app._save_upload_settings(address, "new-token"))

    @unittest.skipUnless(os.name == "nt", "Windows update helper")
    def test_helper_replaces_only_exe_and_preserves_config(self):
        helper = Path(__file__).parent / "companion/install-update.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        real_exe = Path(os.environ["SystemRoot"]) / "System32/where.exe"
        with tempfile.TemporaryDirectory(prefix="sd2 update test ") as directory:
            root = Path(directory)
            target = root / "SD2-Companion.exe"
            target.write_bytes(b"old version")
            updates = root / ".sd2-companion-updates"
            updates.mkdir()
            staged = updates / "new.exe"
            shutil.copyfile(real_exe, staged)
            config = root / "companion.json"
            config.write_text("private configuration", encoding="utf-8")
            digest = hashlib.sha256(staged.read_bytes()).hexdigest()
            arguments = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(helper), "-Target", str(target), "-Staged", str(staged),
                         "-Sha256", digest, "-ParentProcessId", "2147483647", "-ConfigPath", str(config)]
            subprocess.run(arguments, check=True, timeout=30, capture_output=True)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest)
            self.assertEqual(Path(str(target) + ".previous").read_bytes(), b"old version")
            self.assertEqual(config.read_text(), "private configuration")


if __name__ == "__main__":
    unittest.main()
