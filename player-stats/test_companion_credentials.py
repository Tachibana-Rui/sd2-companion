"""Windows credential ABI tests with no writes to the real credential manager."""
import ctypes
import os
import unittest
from unittest.mock import Mock, patch

from companion import credentials
from companion.cli import _uploader, main
from companion.config import Config, ConfigError
from pathlib import Path


URL = "https://example.invalid/api/companion/v1/task-reports"


class CredentialTests(unittest.TestCase):
    def test_endpoint_scope_rejects_credentials_queries_fragments_and_http(self):
        self.assertNotEqual(credentials._target(URL), credentials._target(URL + "/other"))
        for value in [URL + "?token=secret", URL + "#fragment", "https://user:pass@example.invalid/a",
                      "http://example.invalid/a", "https://example.invalid:65536/a", URL + "\n"]:
            with self.subTest(value=value), self.assertRaises(credentials.CredentialError):
                credentials._target(value)

    def test_environment_precedes_store(self):
        with patch.dict(os.environ, {"SD2_COMPANION_TOKEN": "environment-value"}), \
                patch.object(credentials, "read_upload_token") as read:
            self.assertEqual(credentials.get_upload_token(URL), "environment-value")
            read.assert_not_called()

    def test_https_fallback_and_loopback_environment_only(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(credentials, "read_upload_token", return_value="stored-value") as read:
            self.assertEqual(credentials.get_upload_token(URL), "stored-value")
            self.assertIsNone(credentials.get_upload_token("http://127.0.0.1:8770/reports"))
            read.assert_called_once_with(URL)

    def test_store_passes_exact_blob_user_scope_and_endpoint(self):
        api = Mock()
        api.CredWriteW.return_value = 1
        writes = []
        def write(pointer, flags):
            value = ctypes.cast(pointer, credentials._PCREDENTIALW).contents
            writes.append((value.Type, value.Persist, value.TargetName,
                           ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize), flags))
            return 1
        api.CredWriteW.side_effect = write
        with patch.object(credentials, "_windows_api", return_value=api):
            credentials.store_upload_token(URL, "stored-value")
        self.assertEqual(writes, [(1, 2, credentials._target(URL), b"stored-value", 0)])

    def test_store_validates_before_calling_windows(self):
        with patch.object(credentials, "_windows_api") as api:
            for token in ["", "has spaces", "line\nbreak", "x" * 2561, "非ASCII"]:
                with self.subTest(token_type=type(token).__name__), self.assertRaises(credentials.CredentialError):
                    credentials.store_upload_token(URL, token)
            api.assert_not_called()

    def _read_api(self, raw=b"stored-value", *, wrong_target=False):
        api = Mock()
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        value = credentials._CREDENTIALW(Type=1, TargetName="wrong" if wrong_target else credentials._target(URL),
                                         CredentialBlobSize=len(raw), CredentialBlob=blob)
        def read(target, kind, flags, result):
            ctypes.cast(result, ctypes.POINTER(credentials._PCREDENTIALW))[0] = ctypes.pointer(value)
            return 1
        api.CredReadW.side_effect = read
        # Keep backing storage alive for the whole read.
        api._storage = (blob, value)
        return api

    def test_read_returns_value_and_frees_windows_allocation(self):
        api = self._read_api()
        with patch.object(credentials, "_windows_api", return_value=api):
            self.assertEqual(credentials.read_upload_token(URL), "stored-value")
        api.CredFree.assert_called_once()

    def test_invalid_stored_bytes_and_target_still_free_allocation(self):
        for api in [self._read_api(b"\xff"), self._read_api(b"bad token"), self._read_api(wrong_target=True)]:
            with self.subTest(), patch.object(credentials, "_windows_api", return_value=api):
                with self.assertRaisesRegex(credentials.CredentialError, "invalid_upload_credential"):
                    credentials.read_upload_token(URL)
            api.CredFree.assert_called_once()

    def test_missing_credential_is_distinct_from_store_failure(self):
        api = Mock()
        api.CredReadW.return_value = 0
        with patch.object(credentials, "_windows_api", return_value=api), \
                patch.object(ctypes, "get_last_error", return_value=1168, create=True):
            self.assertIsNone(credentials.read_upload_token(URL))
        with patch.object(credentials, "_windows_api", return_value=api), \
                patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaisesRegex(credentials.CredentialError, "upload_credential_read_failed"):
                credentials.read_upload_token(URL)
        api.CredFree.assert_not_called()

    def test_store_write_failure_uses_safe_error_code(self):
        api = Mock()
        api.CredWriteW.return_value = 0
        with patch.object(credentials, "_windows_api", return_value=api):
            with self.assertRaisesRegex(credentials.CredentialError, "upload_credential_write_failed") as error:
                credentials.store_upload_token(URL, "secret-must-not-appear")
            self.assertNotIn("secret-must-not-appear", str(error.exception))

    def test_nonwindows_store_is_optional_for_read_and_unavailable_for_write(self):
        with patch.object(credentials, "_windows_api", return_value=None):
            self.assertIsNone(credentials.read_upload_token(URL))
            with self.assertRaisesRegex(credentials.CredentialError, "windows_credential_store_unavailable"):
                credentials.store_upload_token(URL, "stored-value")

    def test_cli_uses_stored_credential_and_missing_preserves_error(self):
        config = Config((), (), Path("unused"), upload_url=URL)
        with patch("companion.cli.get_upload_token", return_value="stored-value"), \
                patch("companion.cli.Uploader") as uploader:
            _uploader(config)
            uploader.assert_called_once_with(URL, token="stored-value", token_env="SD2_COMPANION_TOKEN",
                                             allow_loopback_http=False)
        with patch("companion.cli.get_upload_token", return_value=None):
            with self.assertRaisesRegex(ConfigError, "upload_token_environment_variable_missing"):
                _uploader(config)

    def test_doctor_reports_stored_token_presence_without_secret(self):
        config = Config((), (), Path("unused"), upload_url=URL)
        with patch("companion.cli.load", return_value=config), \
                patch("companion.cli.get_upload_token", return_value="stored-value"), \
                patch("companion.cli.emit") as emit:
            self.assertEqual(main(["doctor"]), 0)
            result = emit.call_args.args[0]
            self.assertTrue(result["token_present"])
            self.assertFalse(result["game_process_accessed"])
            self.assertNotIn("stored-value", str(result))


if __name__ == "__main__":
    unittest.main()
