"""Desktop lifecycle tests without Tk windows, UAC prompts, or game access."""
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion import desktop
from companion import maps
from companion import replay_capture
from companion import replay_evidence
from companion.config import ConfigError, decode_config, encode_config, example_config, ENVELOPE_PREFIX
from companion.desktop import (CaptureController, DesktopApp, DesktopError, LocalReportInventory, StatusModel,
                               elevation_command, launch, prepare_config, request_elevation,
                               _version_tuple)


class LocalReportInventoryTests(unittest.TestCase):
    def _local(self, base, name, generation=1, rows=None, folder="unbound-replays"):
        value = {"schema": "sd2-local-unbound-task-report-v4", "report": {
            "game_build": 168061, "report_generation": generation,
            "native_players": [{"player_user_id": "1", "alliance": 0,
                                "official_kill_points": 0, "official_loss_points": 0}],
            "rows": rows or [], "report_data": {"identity_source": "unavailable",
                 "identity_status": "unavailable", "matched_rows": 0, "total_rows": len(rows or [])}}}
        path = base / folder / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return value["report"]

    def _export(self, base, name, report, submission=None):
        value = {"schema": "sd2-task-report-v4", "submission_id": submission or name,
                 "game_build": report["game_build"], "report_generation": report["report_generation"],
                 "native_players": report["native_players"], "rows": report["rows"]}
        path = base / "exports" / "replay-sha" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_five_captured_reports_with_three_exports_count_five_analyzed_two_unbound(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for index in range(5):
                report = self._local(base, str(index), index + 1)
                if index < 3:
                    self._export(base, str(index), report)
            inventory = LocalReportInventory(base)
            self.assertEqual(inventory.poll(), {"event": "local_report_inventory", "analyzed": 5,
                                                "unbound": 2, "bound": 3, "inventory_errors": 0})
            # The files are retained as two representations, not two analyses.
            self.assertEqual(len(list(base.rglob("*.json"))), 8)
            self._export(base, "new-binding", report)
            self.assertEqual(inventory.poll()["analyzed"], 5)
            self.assertEqual(inventory.poll()["unbound"], 1)

    def test_identical_report_values_preserve_capture_multiplicity_and_distinct_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            report = self._local(base, "capture-one", rows=[{"time_seconds": 2}, {"time_seconds": 2}])
            self._local(base, "capture-two", rows=report["rows"], folder="unbound-live")
            self._export(base, "submission-one", report)
            # A copied export with the same submission ID does not add a capture.
            self._export(base, "copied-export", report, submission="submission-one")
            inventory = LocalReportInventory(base)
            self.assertEqual((inventory.poll()["analyzed"], inventory.poll()["unbound"]), (2, 1))
            self._export(base, "submission-two", report)
            self.assertEqual((inventory.poll()["analyzed"], inventory.poll()["unbound"]), (2, 0))

    def test_export_without_original_snapshot_and_invalid_files_are_accounted_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            report = self._local(base, "original")
            self._export(base, "retained-export", report)
            (base / "unbound-replays" / "original.json").unlink()
            bad = base / "unbound-replays" / "broken.json"
            bad.write_text("{unfinished", encoding="utf-8")
            inventory = LocalReportInventory(base)
            self.assertEqual(inventory.poll(), {"event": "local_report_inventory", "analyzed": 1,
                                                "unbound": 0, "bound": 1, "inventory_errors": 1})
            bad.unlink()
            self._local(base, "fresh-cloud", generation=2)
            self.assertEqual((inventory.poll()["analyzed"], inventory.poll()["inventory_errors"]), (2, 0))

    def test_background_inventory_loads_history_and_refreshes_cloud_report_without_queue_event(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._local(base, "historical")
            def runner(config, *, emit_fn, stop_event):
                stop_event.wait(5)
                return 0
            controller = CaptureController(SimpleNamespace(data_directory=base), runner)
            controller.start()
            def inventory_event():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    event = controller.events.get(timeout=3)
                    if event["event"] == "local_report_inventory":
                        return event
                self.fail("inventory refresh missing")
            try:
                self.assertEqual(inventory_event()["analyzed"], 1)
                self._local(base, "new-cloud", generation=2)
                event = inventory_event()
                self.assertEqual((event["analyzed"], event["unbound"]), (2, 2))
            finally:
                controller.request_stop()
                controller.thread.join(timeout=3)
            self.assertFalse(controller.is_alive())
            self.assertFalse(controller.inventory_thread.is_alive())

    def test_upload_counts_never_replace_analyzed_count_or_current_status_after_inventory(self):
        model = StatusModel()
        model.update({"event": "replay_report_waiting_for_matching_file"})
        status = model.status
        model.update({"event": "local_report_inventory", "analyzed": 5, "unbound": 2, "bound": 3,
                      "inventory_errors": 0, "path": "private-path"})
        self.assertEqual((model.saved, model.unbound, model.bound), (5, 2, 3))
        self.assertEqual(model.status, status)
        self.assertEqual(model.code, "replay_report_waiting_for_matching_file")
        model.update({"event": "upload_acknowledged", "total": 3, "sent": 2, "pending": 1})
        self.assertEqual((model.saved, model.queued, model.sent, model.pending), (5, 3, 2, 1))
        model.update({"event": "replay_report_queued", "rows": 9})
        self.assertEqual(model.saved, 5)
        model.update({"event": "local_report_inventory", "analyzed": 6, "unbound": 2, "bound": 4})
        self.assertEqual(model.saved, 6)

    def test_report_directory_button_opens_root_containing_cloud_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._local(base, "cloud")
            app = object.__new__(DesktopApp)
            app.config = SimpleNamespace(data_directory=base)
            with patch("companion.desktop.os.startfile", create=True) as startfile:
                app.open_exports()
            startfile.assert_called_once_with(str(base))

    def test_previous_protocol_files_are_not_loaded_or_migrated_into_new_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._local(base, "new-report")
            path = base / "unbound-replays" / "old-report.json"
            content = json.dumps({"schema": "sd2-local-unbound-task-report-v1", "report": {"rows": []}})
            path.write_text(content, encoding="utf-8")
            self.assertEqual(LocalReportInventory(base).poll(), {"event": "local_report_inventory", "analyzed": 1,
                              "unbound": 1, "bound": 0, "inventory_errors": 0})
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_captured_report_quality_is_independent_of_binding_and_upload_counts(self):
        model = StatusModel()
        model.update({"event": "task_report_captured", "rows": 3,
                      "official_score_players": 20, "total_players": 20,
                      "resolved_actors": 4, "total_actors": 6})
        self.assertEqual((model.official_score_players, model.total_players, model.resolved_actors, model.total_actors),
                         (20, 20, 4, 6))
        self.assertEqual((model.pending, model.sent), (0, 0))
        model.update({"event": "local_report_inventory", "analyzed": 1, "unbound": 1, "bound": 0})
        model.update({"event": "upload_acknowledged", "total": 0, "sent": 0, "pending": 0})
        self.assertEqual((model.saved, model.unbound, model.resolved_actors, model.total_actors), (1, 1, 4, 6))

    def test_capture_quality_queue_rejects_arbitrary_metadata_and_invalid_coverage(self):
        controller = CaptureController(object(), Mock())
        controller.publish({"event": "task_report_captured", "official_score_players": 20, "total_players": 20,
                            "resolved_actors": 4, "total_actors": 6, "address": 123, "name": "private"})
        self.assertEqual(controller.drain(), [{"event": "task_report_captured", "official_score_players": 20,
                         "total_players": 20, "resolved_actors": 4, "total_actors": 6}])
        controller.publish({"event": "task_report_captured", "official_score_players": True,
                            "resolved_actors": -1, "total_actors": "private"})
        self.assertEqual(controller.drain(), [{"event": "task_report_captured"}])


class DesktopInventoryEntryTests(unittest.TestCase):
    """The results list: local replay rows and online rows, one per analysis."""

    def _local(self, base, name, players=2, rows=None, map_name=None, folder="unbound-replays"):
        report = {"game_build": 168061, "report_generation": 1,
                  "native_players": [{"player_user_id": str(index), "name": "P%d" % index, "alliance": index % 2,
                                      "official_kill_points": 100, "official_loss_points": 50}
                                     for index in range(players)],
                  "rows": rows if rows is not None else [{"game_time_seconds": 12.5}]}
        if map_name:
            report["map"] = map_name
        path = base / folder / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "sd2-local-unbound-task-report-v4", "report": report}),
                        encoding="utf-8")
        return path, report

    def _export(self, base, name, report, map_name="Mogilev #10vs10", server="[ASIA-JP] 1 服"):
        payload = {"schema": "sd2-task-report-v4", "submission_id": name, "game_build": report["game_build"],
                   "report_generation": report["report_generation"],
                   "native_players": report["native_players"], "rows": report["rows"],
                   "map": map_name, "server_name": server}
        path = base / "exports" / "live" / "digest" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_local_and_online_rows_keep_the_columns_the_window_shows(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            # A capture without a replay digest is a server match (live or spectated).
            spectated, _ = self._local(base, "spectated", players=20)
            os.utime(spectated, (1_700_000_000, 1_700_000_000))
            # Only a record bound to a local .rpl3 file counts as a local replay.
            digest = "d" * 64
            bound_report = {"game_build": 168061, "report_generation": 3,
                            "native_players": [{"player_user_id": "7", "name": "P7", "alliance": 1,
                                                "official_kill_points": 20, "official_loss_points": 10}],
                            "rows": []}
            bound = self._export(base, "bound", bound_report)
            bound_payload = json.loads(bound.read_text(encoding="utf-8"))
            bound_payload["client_replay_sha256"] = digest
            bound_path = base / "exports" / digest / "bound.json"
            bound_path.parent.mkdir(parents=True, exist_ok=True)
            bound_path.write_text(json.dumps(bound_payload), encoding="utf-8")
            bound.unlink()
            os.utime(bound_path, (1_700_000_300, 1_700_000_300))
            online_report = {"game_build": 168061, "report_generation": 2,
                             "native_players": [{"player_user_id": "9", "name": "P9", "alliance": 0,
                                                 "official_kill_points": 10, "official_loss_points": 5}],
                             "rows": []}
            export = self._export(base, "online", online_report)
            os.utime(export, (1_700_000_500, 1_700_000_500))
            rows = LocalReportInventory(base).entries()
            self.assertEqual([row["kind"] for row in rows], ["online", "local", "online"])
            online_row, local_row, spectated_row = rows
            self.assertEqual(local_row["players"], 1)
            self.assertEqual(local_row["replay"], digest)
            self.assertEqual(spectated_row["players"], 20)
            self.assertEqual(spectated_row["ended_at"], 1_700_000_000)
            self.assertTrue(online_row["linked"])
            self.assertEqual(online_row["map"], "Mogilev #10vs10")
            self.assertEqual(online_row["server"], "[ASIA-JP] 1 服")
            self.assertEqual(online_row["players"], 1)

    def test_a_linked_report_stays_one_row_and_moves_the_analysis_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            replay, report = self._local(base, "same")
            os.utime(replay, (1_700_000_000, 1_700_000_000))
            inventory = LocalReportInventory(base)
            # Without a replay digest the capture is a server match, not a replay.
            self.assertEqual(inventory.entries()[0]["kind"], "online")
            digest = "e" * 64
            export = self._export(base, "same-report", report)
            payload = json.loads(export.read_text(encoding="utf-8"))
            payload["client_replay_sha256"] = digest
            bound = base / "exports" / digest / "same-report.json"
            bound.parent.mkdir(parents=True, exist_ok=True)
            bound.write_text(json.dumps(payload), encoding="utf-8")
            export.unlink()
            os.utime(bound, (1_700_000_900, 1_700_000_900))
            rows = inventory.entries()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["linked"])
            self.assertEqual(rows[0]["kind"], "local")
            self.assertEqual(rows[0]["ended_at"], 1_700_000_000)
            self.assertEqual(rows[0]["analyzed_at"], 1_700_000_900)

    def test_rows_are_newest_first_and_invalid_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            older, _ = self._local(base, "older", players=2)
            newer, _ = self._local(base, "newer", players=4)
            os.utime(older, (1_600_000_000, 1_600_000_000))
            os.utime(newer, (1_650_000_000, 1_650_000_000))
            (base / "unbound-replays" / "broken.json").write_text("{incomplete", encoding="utf-8")
            rows = LocalReportInventory(base).entries()
            self.assertEqual([row["players"] for row in rows], [4, 2])
            self.assertGreater(rows[0]["ended_at"], rows[1]["ended_at"])


class DesktopUpdateTests(unittest.TestCase):
    def test_version_comparison_rejects_anything_but_dotted_numbers(self):
        self.assertEqual(_version_tuple("0.4.1"), (0, 4, 1))
        self.assertGreater(_version_tuple("0.5"), _version_tuple("0.4.9"))
        for value in ("", "v0.4.1", "0.4.1-beta", None, 4):
            with self.subTest(value=value):
                self.assertIsNone(_version_tuple(value))


class DesktopSettingsTests(unittest.TestCase):
    def test_settings_rewrite_keeps_the_envelope_and_never_prints_the_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.json"
            prepare_config(path)
            app = object.__new__(DesktopApp)
            app.config_path = path
            with patch("companion.desktop._message") as message:
                self.assertTrue(app._write_config(upload_url="https://upload.example.invalid/reports"))
            message.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(ENVELOPE_PREFIX))
            self.assertNotIn("upload.example.invalid", text)
            self.assertEqual(decode_config(text)["upload_url"], "https://upload.example.invalid/reports")
            with patch("companion.desktop._message") as message:
                self.assertTrue(app._write_config(upload_url=None))
            self.assertIsNone(decode_config(path.read_text(encoding="utf-8"))["upload_url"])
            message.assert_not_called()

    def test_settings_rewrite_reports_an_unreadable_file_instead_of_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.json"
            path.write_text("sd2c1:not-base64!", encoding="utf-8")
            app = object.__new__(DesktopApp)
            app.config_path = path
            with patch("companion.desktop._message") as message:
                self.assertFalse(app._write_config(upload_url="https://upload.example.invalid/reports"))
            message.assert_called_once()
            self.assertEqual(path.read_text(encoding="utf-8"), "sd2c1:not-base64!")


class DesktopConfigTests(unittest.TestCase):
    def test_first_launch_creates_an_encoded_config_that_needs_no_room_or_replay_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new-config" / "companion.json"
            config = prepare_config(path)
            self.assertEqual(config.collection_mode, "auto")
            self.assertEqual(config.rooms, ())
            self.assertEqual(config.replay_directories, ())
            self.assertEqual(config.upload_url, "https://sd2.rui-blog.link/api/companion/v3/task-reports")
            self.assertEqual(config.data_directory, path.parent / "companion-data-v3")
            text = path.read_text(encoding="utf-8")
            # The endpoints and the upload URL must never sit in the file as plain JSON.
            self.assertTrue(text.startswith(ENVELOPE_PREFIX))
            self.assertNotIn("{", text)
            self.assertEqual(decode_config(text), example_config())

    def test_encoded_config_round_trips_and_legacy_plain_json_still_loads(self):
        raw = {**example_config(), "upload_url": "https://upload.example.invalid/reports",
               "rooms": [{"id": "sd2-jp-test", "endpoints": [{"host": "203.0.113.10", "port": 7552}]}]}
        encoded = encode_config(raw)
        self.assertNotIn("203.0.113.10", encoded)
        self.assertNotIn("upload.example.invalid", encoded)
        self.assertEqual(decode_config(encoded), raw)
        # A plain document written by an older build stays readable and is never
        # rewritten behind the operator's back.
        plain = json.dumps(raw, indent=2)
        self.assertEqual(decode_config(plain), raw)
        for broken in ("", "sd2c1:not-base64!", "sd2c1:", "{incomplete"):
            with self.subTest(document=broken):
                with self.assertRaises(ConfigError):
                    decode_config(broken)

    def test_existing_live_config_is_only_overridden_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = {**example_config(), "collection_mode": "live",
                        "rooms": [{"id": "old-room", "endpoints": [{"host": "127.0.0.1", "port": 7552}]}],
                        "replay_directories": ["old-replays"], "upload_url": "https://example.invalid/reports"}
            encoded = json.dumps(original, indent=3).encode("utf-8")
            path.write_bytes(encoded)
            config = prepare_config(path)
            self.assertEqual(config.collection_mode, "auto")
            self.assertEqual(config.rooms[0].id, "old-room")
            self.assertEqual(config.upload_url, original["upload_url"])
            self.assertEqual(path.read_bytes(), encoded)

    def test_invalid_existing_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b"{incomplete json")
            with self.assertRaises(ValueError):
                prepare_config(path)
            self.assertEqual(path.read_bytes(), b"{incomplete json")


class DesktopElevationTests(unittest.TestCase):
    def test_elevation_uses_absolute_arguments_correctly_quoted_and_pythonw(self):
        with tempfile.TemporaryDirectory(prefix="SD2 Desktop 空格 ") as directory:
            base = Path(directory)
            executable = base / "Python Folder" / "python.exe"
            executable.parent.mkdir()
            executable.touch()
            pythonw = executable.with_name("pythonw.exe")
            pythonw.touch()
            entry = base / "Project Folder" / "player_companion.py"
            config = base / "Config Folder" / "companion.json"
            result, arguments, cwd = elevation_command(config, executable=executable, entrypoint=entry)
            self.assertEqual(result, pythonw.resolve())
            self.assertEqual(arguments, subprocess.list2cmdline([
                "-B", str(entry.resolve()), "desktop", "--config", str(config.resolve()), "--elevated"]))
            self.assertEqual(cwd, entry.parent.resolve())

    def test_frozen_build_relaunches_its_own_executable_for_elevation(self):
        with tempfile.TemporaryDirectory(prefix="SD2 Frozen ") as directory:
            base = Path(directory)
            executable = base / "SD2Companion.exe"
            executable.touch()
            config = base / "Profile Folder" / "companion.json"
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(executable)):
                result, arguments, cwd = elevation_command(config)
            self.assertEqual(result, executable.resolve())
            self.assertEqual(arguments, subprocess.list2cmdline(
                ["desktop", "--config", str(config.resolve()), "--elevated"]))
            self.assertEqual(cwd, config.resolve().parent)
            self.assertNotIn("player_companion.py", arguments)

    def test_shell_success_and_uac_cancel_have_distinct_results(self):
        with patch("companion.desktop._shell_execute", return_value=(33, 0)) as shell:
            request_elevation(Path("unused-settings.json"))
        self.assertEqual(shell.call_args.args[0], "runas")
        self.assertIn("--elevated", shell.call_args.args[2])
        for result, error, expected in ((5, 1223, "desktop_elevation_cancelled"),
                                        (5, 5, "desktop_elevation_failed"),
                                        (0, 0, "desktop_elevation_failed")):
            with self.subTest(result=result, error=error), \
                    patch("companion.desktop._shell_execute", return_value=(result, error)):
                with self.assertRaises(DesktopError) as caught:
                    request_elevation(Path("unused-settings.json"))
                self.assertEqual(caught.exception.code, expected)

    def test_non_admin_launch_requests_one_relaunch_and_does_not_start_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            with patch("companion.desktop._supported_platform", return_value=True), \
                    patch("companion.desktop._is_admin", return_value=False), \
                    patch("companion.desktop.request_elevation") as request, \
                    patch("companion.desktop.DesktopApp") as app, \
                    patch("companion.desktop._message") as message:
                self.assertEqual(launch(config), 0)
            self.assertTrue(config.is_file())
            request.assert_called_once_with(config.resolve())
            app.assert_not_called()
            message.assert_not_called()

    def test_elevated_flag_prevents_a_relaunch_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            with patch("companion.desktop._supported_platform", return_value=True), \
                    patch("companion.desktop._is_admin", return_value=False), \
                    patch("companion.desktop.request_elevation") as request, \
                    patch("companion.cli._run_auto", Mock(), create=True), \
                    patch("companion.desktop.DesktopApp") as app, \
                    patch("companion.desktop._message") as message:
                app.return_value.run.return_value = 0
                self.assertEqual(launch(config, elevated=True), 0)
            request.assert_not_called()
            self.assertIn("desktop_elevation_not_granted", message.call_args.args[0])
            self.assertEqual(app.call_args.args[1].collection_mode, "auto")

    def test_cancelled_uac_explains_replay_limit_and_keeps_auto_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            with patch("companion.desktop._supported_platform", return_value=True), \
                    patch("companion.desktop._is_admin", return_value=False), \
                    patch("companion.desktop.request_elevation", side_effect=DesktopError("desktop_elevation_cancelled")), \
                    patch("companion.cli._run_auto", Mock(), create=True), \
                    patch("companion.desktop.DesktopApp") as app, \
                    patch("companion.desktop._message") as message:
                app.return_value.run.return_value = 0
                self.assertEqual(launch(config), 0)
            self.assertEqual(app.call_args.args[1].collection_mode, "auto")
            self.assertIn("本地重放自动识别暂不可用", message.call_args.args[0])
            self.assertIn("实时完赛仍会尝试", message.call_args.args[0])
            self.assertIn("不需要本地录像", message.call_args.args[0])

    def test_admin_launch_starts_desktop_with_auto_config_and_no_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            runner = Mock()
            with patch("companion.desktop._supported_platform", return_value=True), \
                    patch("companion.desktop._is_admin", return_value=True), \
                    patch("companion.desktop.request_elevation") as request, \
                    patch("companion.cli._run_auto", runner, create=True), \
                    patch("companion.desktop.DesktopApp") as app:
                app.return_value.run.return_value = 7
                self.assertEqual(launch(path), 7)
            request.assert_not_called()
            self.assertEqual(app.call_args.args[0], path.resolve())
            self.assertEqual(app.call_args.args[1].collection_mode, "auto")
            self.assertIs(app.call_args.args[2], runner)

    def test_failed_elevation_also_keeps_live_memory_capture_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            runner = Mock()
            with patch("companion.desktop._supported_platform", return_value=True), \
                    patch("companion.desktop._is_admin", return_value=False), \
                    patch("companion.desktop.request_elevation", side_effect=DesktopError("desktop_elevation_failed")), \
                    patch("companion.cli._run_auto", runner, create=True), \
                    patch("companion.desktop.DesktopApp") as app, \
                    patch("companion.desktop._message") as message:
                app.return_value.run.return_value = 0
                self.assertEqual(launch(path), 0)
            self.assertIs(app.call_args.args[2], runner)
            self.assertEqual(app.call_args.args[1].collection_mode, "auto")
            self.assertIn("实时完赛不需要本地录像", message.call_args.args[0])

    def test_tk_unavailable_uses_message_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("companion.desktop._supported_platform", return_value=True), \
                    patch("companion.desktop._is_admin", return_value=True), \
                    patch("companion.cli._run_auto", Mock(), create=True), \
                    patch("companion.desktop.DesktopApp", side_effect=DesktopError("desktop_tk_unavailable")), \
                    patch("companion.desktop._message") as message:
                self.assertEqual(launch(path), 2)
            self.assertIn("tkinter", message.call_args.args[0])


class DesktopCaptureTests(unittest.TestCase):
    def test_capture_runs_off_ui_thread_and_stop_waits_for_cleanup(self):
        started, released = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def runner(config, *, emit_fn, stop_event):
            self.assertNotEqual(threading.get_ident(), main_thread)
            emit_fn({"event": "replay_file_identified", "replay_sha256": "c" * 64})
            started.set()
            stop_event.wait(2)
            released.set()
            return 0
        controller = CaptureController(object(), runner)
        controller.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertTrue(controller.is_alive())
            controller.request_stop()
            self.assertTrue(controller.finished.wait(1))
        finally:
            controller.request_stop()
            controller.thread.join(timeout=2)
        self.assertTrue(released.is_set())
        self.assertFalse(controller.is_alive())
        self.assertEqual(controller.result, 0)
        self.assertEqual(controller.drain()[0]["replay_sha256"], "c" * 64)

    def test_close_before_scheduled_start_does_not_start_capture(self):
        runner = Mock()
        controller = CaptureController(object(), runner)
        controller.request_stop()
        controller.start()
        runner.assert_not_called()
        self.assertTrue(controller.finished.is_set())
        self.assertFalse(controller.is_alive())

    def test_ui_queue_is_bounded_and_never_passes_paths_or_tokens(self):
        controller = CaptureController(object(), Mock())
        for index in range(1000):
            controller.publish({"event": "upload_acknowledged", "total": index,
                                "token": "private-token", "path": "private-user-path",
                                "player_name": "private-player", "replay_sha256": "not-a-hash"})
        events = controller.drain()
        self.assertEqual(len(events), 128)
        self.assertEqual(events[-1]["total"], 999)
        self.assertNotIn("private", json.dumps(events))

    def test_worker_failure_does_not_echo_arbitrary_exception_or_credentials(self):
        def runner(config, **kwargs):
            raise OSError("private-token in private-user-path")
        controller = CaptureController(object(), runner)
        controller.start()
        controller.thread.join(timeout=1)
        events = controller.drain()
        self.assertEqual(events[0], {"event": "companion_error", "code": "desktop_capture_failed"})
        self.assertNotIn("private", json.dumps(events))
        self.assertEqual(controller.result, 2)

    def test_status_preserves_hash_counts_and_upload_acknowledgement(self):
        model = StatusModel()
        model.update({"event": "replay_report_queued", "replay_sha256": "c" * 64, "rows": 42})
        self.assertEqual(model.saved, 1)
        self.assertEqual(model.rows, 42)
        self.assertEqual(model.replay_hash, "c" * 64)
        model.update({"event": "upload_acknowledged", "total": 3, "sent": 2, "pending": 1, "rejected": 0})
        self.assertEqual((model.saved, model.sent, model.pending), (3, 2, 1))
        self.assertIn("确认收件", model.status)
        self.assertEqual(model.replay_hash, "c" * 64)

    def test_upload_receipts_distinguish_received_from_database_match(self):
        model = StatusModel()
        model.update({"event": "upload_acknowledged", "receipt_status": "pending_match", "sent": 1})
        self.assertEqual(model.receipt_status, "pending_match")
        self.assertIn("正在等待匹配", model.status)
        self.assertNotIn("并已匹配", model.status)
        model.update({"event": "upload_acknowledged", "receipt_status": "accepted", "sent": 2})
        self.assertEqual(model.receipt_status, "accepted")
        self.assertIn("并已匹配", model.status)
        model.update({"event": "upload_acknowledged", "receipt_status": "duplicate", "sent": 2})
        self.assertEqual(model.receipt_status, "duplicate")
        self.assertIn("以接收端为准", model.status)
        self.assertNotIn("并已匹配", model.status)

    def test_queue_rejects_arbitrary_receipt_text(self):
        controller = CaptureController(object(), Mock())
        controller.publish({"event": "upload_acknowledged", "receipt_status": "private token echoed"})
        self.assertEqual(controller.drain(), [{"event": "upload_acknowledged"}])

    def test_native_session_id_is_bounded_and_has_no_control_characters(self):
        controller = CaptureController(object(), Mock())
        controller.publish({"event": "live_session_observed", "session_id": "native-session:123"})
        self.assertEqual(controller.drain()[0]["session_id"], "native-session:123")
        for invalid in ("", "a" * 129, "bad\nvalue", "bad\tvalue", "bad\x00value", "bad\x7fvalue",
                        "bad\x85value", "bad\u202evalue", "bad\ud800value", None, 123):
            with self.subTest(invalid=invalid):
                controller.publish({"event": "live_session_observed", "session_id": invalid})
                self.assertNotIn("session_id", controller.drain()[0])

    def test_live_capture_has_session_and_saved_report_without_replay_or_room(self):
        model = StatusModel()
        model.update({"event": "companion_started", "collection_mode": "auto"})
        self.assertEqual(model.collection_mode, "auto")
        self.assertIn("实时完赛 / 本地重放", model.status)
        model.update({"event": "live_session_observed", "session_id": "native-session"})
        self.assertEqual(model.session_id, "native-session")

        self.assertIn("游戏内存", model.status)
        model.update({"event": "live_report_queued", "session_id": "native-session", "rows": 15})
        self.assertEqual((model.saved, model.rows), (1, 15))
        self.assertEqual(model.replay_hash, "")
        self.assertEqual(model.replay_name, "")
        self.assertIn("实时任务报告", model.status)
        self.assertNotIn("失败", model.status)
        model.update({"event": "live_report_waiting_for_server"})
        self.assertIn("已收件不代表已经验收", model.status)
        self.assertEqual(model.session_id, "native-session")

    def test_native_session_and_local_replay_identity_remain_distinct(self):
        model = StatusModel()
        model.update({"event": "live_session_observed", "session_id": "native-session"})
        model.update({"event": "replay_file_identified", "replay_name": "played.rpl3", "replay_sha256": "a" * 64})
        self.assertEqual(model.session_id, "native-session")
        self.assertEqual((model.replay_name, model.replay_hash), ("played.rpl3", "a" * 64))

    def test_live_pending_states_do_not_require_sha_or_imply_report_success(self):
        for event in ("live_report_waiting_for_session", "live_report_waiting_for_server",
                      "live_report_source_unknown", "live_report_wrong_session"):
            with self.subTest(event=event):
                model = StatusModel()
                model.update({"event": event})
                self.assertNotIn("已保存", model.status)
                self.assertNotIn("SHA", model.status)
                self.assertEqual(model.saved, 0)
                self.assertIsNone(model.rows)
        model = StatusModel()
        model.update({"event": "file_monitor_requires_elevation"})
        self.assertIn("实时完赛仍继续监测", model.status)
        self.assertIn("不需要本地录像", model.status)
        model.update({"event": "file_monitor_events_lost"})
        self.assertIn("本地重放文件监控暂不可用", model.status)
        self.assertIn("实时完赛仍继续监测", model.status)
        self.assertNotIn("采集已停止", model.status)
        model.update({"event": "companion_error", "code": "file_monitor_trace_failed"})
        self.assertIn("实时完赛仍继续监测", model.status)
        self.assertNotIn("重新打开程序", model.status)

    def test_live_received_receipt_is_not_server_acceptance(self):
        model = StatusModel()
        model.update({"event": "live_session_observed", "session_id": "native-session"})
        model.update({"event": "upload_acknowledged", "receipt_status": "pending_match", "sent": 1})
        self.assertIn("已收件", model.status)
        self.assertIn("不代表服务器已验收", model.status)
        self.assertNotIn("并已匹配", model.status)
        self.assertNotIn("录像 SHA", model.status)
        self.assertEqual(model.session_id, "native-session")

    def test_window_shows_live_session_without_a_replay_hash_failure(self):
        app = object.__new__(DesktopApp)
        app.root = Mock()
        app.controller = Mock()
        app.controller.drain.return_value = [{"event": "live_session_observed", "session_id": "native-session"}]
        app.model = StatusModel()
        app.status_var = Mock();app.session_var = Mock();app.hash_var = Mock()
        app.counts_var = Mock();app.code_var = Mock()
        app.closing = False;app.destroyed = False
        app._pump()
        self.assertIn("Live session (game memory): native-session", app.session_var.set.call_args.args[0])
        self.assertIn("not needed for a live finish", app.hash_var.set.call_args.args[0])
        self.assertIn("Mode: live / replay", app.code_var.set.call_args.args[0])
        self.assertNotIn("fail", app.status_var.set.call_args.args[0].lower())
        self.assertIn("Analyzed:", app.counts_var.set.call_args.args[0])
        # The same instance in Chinese keeps the previous wording for operators.
        app.language = "zh"
        app._pump()
        self.assertIn("实时会话（游戏内存）：native-session", app.session_var.set.call_args.args[0])
        self.assertIn("已分析：", app.counts_var.set.call_args.args[0])

    def test_replay_name_is_basename_only_without_control_characters(self):
        controller = CaptureController(object(), Mock())
        controller.publish({"event": "replay_file_identified", "replay_name": "录像 🚩.rpl3"})
        self.assertEqual(controller.drain()[0]["replay_name"], "录像 🚩.rpl3")
        for name in ("", ".", "..", "D:/private/录像.rpl3", "D:\\private\\录像.rpl3",
                     "nested/录像.rpl3", "nested\\录像.rpl3", "bad\nname.rpl3", "bad\x00name.rpl3",
                     "bad\x7fname.rpl3", "bad\x85name.rpl3", "a" * 257, None, 123):
            with self.subTest(name=name):
                controller.publish({"event": "replay_file_identified", "replay_name": name})
                self.assertNotIn("replay_name", controller.drain()[0])

    def test_current_replay_name_and_hash_are_displayed_without_stale_filename(self):
        model = StatusModel()
        model.update({"event": "replay_file_identified", "replay_name": "first.rpl3", "replay_sha256": "a" * 64})
        self.assertEqual((model.replay_name, model.replay_hash), ("first.rpl3", "a" * 64))
        model.update({"event": "upload_acknowledged", "total": 1, "sent": 1})
        self.assertEqual(model.replay_name, "first.rpl3")
        model.update({"event": "replay_file_identified", "replay_sha256": "b" * 64})
        self.assertEqual(model.replay_name, "")
        model.update({"event": "replay_report_queued", "replay_name": "second.rpl3", "replay_sha256": "b" * 64})
        self.assertEqual(model.replay_name, "second.rpl3")

    def test_worker_finished_preserves_actual_error_code_and_explanation(self):
        model = StatusModel()
        model.update({"event": "companion_error", "code": "file_monitor_requires_elevation"})
        model.update({"event": "desktop_worker_finished", "exit_code": 2})
        self.assertEqual(model.code, "file_monitor_requires_elevation")
        self.assertIn("管理员权限", model.status)
        self.assertIn("采集已停止", model.status)
        generic = StatusModel()
        generic.update({"event": "desktop_worker_finished", "exit_code": 2})
        self.assertEqual(generic.code, "desktop_worker_finished")

    def test_scan_progress_survives_safe_queue_without_addresses_or_invalid_counts(self):
        controller = CaptureController(object(), Mock())
        controller.publish({"event": "locating_report_controller", "scan_mib": 2176,
                            "scan_total_mib": 8192, "scan_rounds_completed": 2,
                            "address": "private-pointer", "collection_mode": "replay"})
        event = controller.drain()[0]
        self.assertEqual(event, {"event": "locating_report_controller", "scan_mib": 2176,
                                 "scan_total_mib": 8192, "scan_rounds_completed": 2,
                                 "collection_mode": "replay"})
        for invalid in (-1, True, "128", 1.5, 1_000_000_001):
            with self.subTest(invalid=invalid):
                controller.publish({"event": "locating_report_controller", "scan_mib": invalid,
                                    "scan_total_mib": invalid, "scan_rounds_completed": invalid})
                self.assertEqual(controller.drain()[0], {"event": "locating_report_controller"})

    def test_scan_status_describes_progress_and_no_report_without_zero_row_success(self):
        model = StatusModel()
        model.update({"event": "locating_report_controller", "scan_mib": 2176,
                      "scan_total_mib": 8192, "scan_rounds_completed": 0})
        self.assertIn("正在定位任务报告", model.status)
        self.assertIn("2176/8192 MiB", model.status)
        self.assertIn("完成 0 轮", model.status)
        self.assertIsNone(model.rows)
        self.assertEqual(model.saved, 0)
        model.update({"event": "waiting_for_report_controller", "scan_mib": 8192,
                      "scan_total_mib": 8192, "scan_rounds_completed": 1})
        self.assertIn("当前未发现报告，继续监测", model.status)
        self.assertIn("任务报告界面停留", model.status)
        self.assertIn("完成 1 轮", model.status)
        self.assertIsNone(model.rows)
        self.assertEqual(model.saved, 0)
        self.assertNotIn("已保存", model.status)

    def test_legacy_scan_limit_error_is_actionable_and_survives_worker_exit(self):
        model = StatusModel()
        model.update({"event": "companion_error", "code": "native_scan_budget_exceeded"})
        self.assertIn("尚未读取报告", model.status)
        self.assertIn("更新程序", model.status)
        self.assertIn("关闭旧实例", model.status)
        self.assertIn("仍需确认", model.status)
        model.update({"event": "desktop_worker_finished", "exit_code": 2})
        self.assertEqual(model.code, "native_scan_budget_exceeded")
        self.assertIn("尚未读取报告", model.status)
        self.assertIn("采集已停止", model.status)
        self.assertIsNone(model.rows)

    def test_mode_is_explicit_and_unknown_mode_is_not_passed_to_ui(self):
        model = StatusModel()
        model.update({"event": "companion_started", "collection_mode": "live"})
        self.assertEqual(model.collection_mode, "live")
        self.assertIn("联机采集", model.status)
        model.update({"event": "companion_started", "collection_mode": "replay"})
        self.assertEqual(model.collection_mode, "replay")
        self.assertIn("自动重放采集", model.status)
        model.update({"event": "companion_started", "collection_mode": "private-token"})
        self.assertEqual(model.collection_mode, "replay")
        self.assertNotIn("private-token", model.status)

    def test_window_stays_open_until_capture_has_released_resources(self):
        app = object.__new__(DesktopApp)
        app.root = Mock()
        app.controller = Mock()
        app.controller.drain.return_value = []
        app.controller.is_alive.return_value = True
        app.model = StatusModel()
        app.status_var = Mock()
        app.buttons = [Mock(), Mock()]
        app.closing = False
        app.destroyed = False
        app.on_close()
        app.controller.request_stop.assert_called_once()
        app._pump()
        app.root.destroy.assert_not_called()
        self.assertTrue(app.closing)
        app.controller.is_alive.return_value = False
        app._pump()
        app.root.destroy.assert_called_once()
        self.assertTrue(app.destroyed)


class DesktopListFormatTests(unittest.TestCase):
    def test_duration_text_and_nickname_join(self):
        self.assertEqual(desktop._duration_text(0), "0:00")
        self.assertEqual(desktop._duration_text(59.6), "1:00")
        self.assertEqual(desktop._duration_text(1417.6), "23:38")
        self.assertEqual(desktop._duration_text(3671), "1:01:11")
        for value in (None, -1, True, "120", float("nan")):
            with self.subTest(value=value):
                self.assertEqual(desktop._duration_text(value), "—")
        self.assertEqual(desktop._names_text(("Alpha", "Bravo", "中文名")), "Alpha, Bravo, 中文名")
        self.assertEqual(desktop._names_text(()), "—")
        self.assertEqual(desktop._names_text(("Alpha", "", None)), "Alpha")

    def test_english_title_matches_the_product_name(self):
        self.assertEqual(desktop._TEXT["title"][0], "SD2 Online match / Local replay Analysis Tool")
        self.assertEqual([desktop._TEXT[key][0] for key in ("intro_1", "intro_2", "intro_3")],
                         ["Keep this window open while you play Steel Division 2.",
                          "When a match ends, the companion reads the debrief (game timeline) from game "
                          "memory and uploads matches played on the [ASIA-JP] server for analysis.",
                          "You can also play a local replay in game to analyse its kills and losses locally. "
                          "Use File → Link local replay to server match to submit that analysis for server matching."])
        # The body starts with the sentences: no separate heading label.
        self.assertFalse(any("heading" in key for key in desktop._TEXT))

    def test_entry_duration_and_names_come_from_the_captured_report(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path, _report = DesktopInventoryEntryTests()._local(
                base, "match", players=3,
                rows=[{"game_time_seconds": 12.5}, {"game_time_seconds": 900.4}])
            entry = LocalReportInventory(base).entries()[0]
            self.assertEqual(entry["duration"], 900)
            self.assertEqual(entry["names"], ("P0", "P1", "P2"))
            self.assertEqual(desktop._duration_text(entry["duration"]), "15:00")
            self.assertEqual(desktop._names_text(entry["names"]), "P0, P1, P2")

    def test_header_paragraphs_rewrap_with_the_window(self):
        app = object.__new__(DesktopApp)
        first, second = Mock(), Mock()
        app.intro_labels = [first, second]
        app._fit_header(SimpleNamespace(width=1200))
        first.configure.assert_called_once_with(wraplength=1156)
        second.configure.assert_called_once_with(wraplength=1156)
        first.reset_mock()
        app._fit_header(SimpleNamespace(width=100))
        first.configure.assert_called_once_with(wraplength=240)


class DesktopServerAssociationTests(unittest.TestCase):
    def app(self, directory):
        app = object.__new__(DesktopApp)
        app.language = "en"
        app.root, app.status_var, app._refresh_entries = Mock(), Mock(), Mock()
        app.config = SimpleNamespace(data_directory=Path(directory), online_enabled=True,
                                     upload_url="https://example.invalid/api/companion/v3/task-reports")
        app.destroyed = app.closing = False
        return app

    def test_local_mode_and_cancel_do_not_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            with patch("tkinter.filedialog.askopenfilename", return_value="test.rpl3") as choose, \
                 patch("tkinter.messagebox.askyesno", return_value=False) as consent, \
                 patch("companion.desktop._read_local_report", return_value={}), \
                 patch("companion.desktop._message") as message, \
                 patch("companion.replay_association.enqueue_association") as enqueue:
                app.config.online_enabled = False
                app.link_replay_to_server({"path": "report.json"})
                choose.assert_not_called()
                self.assertIn("Enable online recording", message.call_args.args[0])
                app.config.online_enabled = True
                app.link_replay_to_server({"path": "report.json"})
                consent.assert_called_once()
                enqueue.assert_not_called()
                app.root.after.assert_not_called()

    def test_explicit_action_queues_same_report_and_only_claims_pending_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            report = {"rows": [], "native_players": []}
            # Run the background target synchronously, leaving the UI completion
            # callback separate as it is in the actual Tk event loop.
            def thread(*, target, **_kwargs):
                return SimpleNamespace(start=target)
            with patch("tkinter.filedialog.askopenfilename", return_value="test.rpl3"), \
                 patch("tkinter.messagebox.askyesno", return_value=True), \
                 patch("companion.desktop._read_local_report", return_value=report), \
                 patch("companion.desktop.threading.Thread", side_effect=thread), \
                 patch("companion.desktop._message") as message, \
                 patch("companion.replay_association.enqueue_association") as enqueue:
                app.link_replay_to_server({"path": "report.json", "session": "known-session"})
                self.assertEqual(enqueue.call_args.args[:3], (Path(directory), report, "test.rpl3"))
                self.assertTrue(enqueue.call_args.kwargs["user_selected"])
                self.assertEqual(enqueue.call_args.kwargs["session_id"], "known-session")
                self.assertEqual(enqueue.call_args.args[3].path, Path(directory).resolve() / "outbox.sqlite3")
                app.root.after.call_args.args[1]()
                app._refresh_entries.assert_called_once()
                self.assertIn("Queued for upload", message.call_args.args[0])
                self.assertIn("before linking", message.call_args.args[0])
                self.assertIn("waiting for server verification", app.status_var.set.call_args.args[0])


class DesktopSortingTests(unittest.TestCase):
    """Exercise real, hidden Tk tables and their actual heading callbacks."""

    def setUp(self):
        try:
            import tkinter as tk
            from tkinter import ttk
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:
            self.skipTest("Tk display unavailable: " + type(exc).__name__)
        self.tk, self.ttk = tk, ttk

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def click(self, table, column):
        self.root.tk.call(table.heading(column)["command"])

    def app(self):
        app = object.__new__(DesktopApp)
        app.root, app.tk, app.ttk = self.root, self.tk, self.ttk
        app.language = "en"
        app.controller = SimpleNamespace(inventory=None)
        app.config = SimpleNamespace(data_directory=Path("unused"))
        app.entries, app.visible_entries = [], []
        app._build_list()
        return app

    def test_main_default_is_time_descending_and_every_header_toggles(self):
        app = self.app()
        older = {"fingerprint": "older", "kind": "local", "analyzed_at": 10,
                 "duration": 9, "map": "Alpha", "names": ("Alpha",)}
        newer = {"fingerprint": "newer", "kind": "online", "analyzed_at": 100,
                 "duration": 100, "map": "Zulu", "names": ("Zulu",)}
        app.entries = [older, newer]
        app._refresh_entries()
        self.assertEqual([row["fingerprint"] for row in app.visible_entries], ["newer", "older"])
        self.assertIn("▼", app.tree.heading("analyzed")["text"])
        # Select the older analysis, then reverse order: the selection follows
        # the report, not the numeric position that happens to render it.
        app.tree.selection_set("1")
        self.click(app.tree, "analyzed")
        self.assertEqual([row["fingerprint"] for row in app.visible_entries], ["older", "newer"])
        self.assertEqual(app.tree.selection(), ("0",))
        self.assertIn("▲", app.tree.heading("analyzed")["text"])
        for column, *_ in app.COLUMNS:
            if column == "analyzed":
                continue
            self.click(app.tree, column)
            self.assertTrue(app.sort_reverse)
            self.assertEqual(app.visible_entries[0]["fingerprint"], "newer")
            self.click(app.tree, column)
            self.assertFalse(app.sort_reverse)
            self.assertEqual(app.visible_entries[0]["fingerprint"], "older")
            selected = int(app.tree.selection()[0])
            self.assertEqual(app.visible_entries[selected]["fingerprint"], "older")
        # A background inventory refresh inserts a row but retains both sort
        # direction and the selected report.
        app.controller.inventory = SimpleNamespace(entries=lambda: [newer, older,
            {**newer, "fingerprint": "newest", "analyzed_at": 1000, "names": ("Beta",)}])
        app._refresh_entries()
        selected = int(app.tree.selection()[0])
        self.assertEqual(app.visible_entries[selected]["fingerprint"], "older")
        self.assertEqual(app.sort_column, "players")
        self.assertFalse(app.sort_reverse)

    def test_missing_values_stay_last_in_both_directions(self):
        app = self.app()
        app.entries = [{"fingerprint": "missing", "duration": None},
                       {"fingerprint": "short", "duration": 9},
                       {"fingerprint": "long", "duration": 100}]
        self.click(app.tree, "duration")
        self.assertEqual([row["fingerprint"] for row in app.visible_entries], ["long", "short", "missing"])
        self.click(app.tree, "duration")
        self.assertEqual([row["fingerprint"] for row in app.visible_entries], ["short", "long", "missing"])

    def test_analysis_uses_kill_counts_then_sorts_raw_numbers_and_ratios(self):
        app = self.app()
        def player(name, kills, points, units):
            return {"name": name, "official_kill_points": points, "official_loss_points": points,
                    "economy_by_scope": {"all_components": {"kill_count": kills, "K": points, "L": points}},
                    "units": [{"role_scope": "all_components", **unit} for unit in units]}
        players = [player("Nine", 9, 100, [{"name": "Wrong player's unit", "kill_count": 99}]),
                   player("Twelve", 12, 9, [
                       {"name": "One", "kill_count": 1, "P_est": 100, "K": 9, "L": 100,
                        "conversion_ratio": 9.5, "exchange_ratio": None},
                       {"name": "Twenty", "kill_count": 20, "P_est": 9, "K": 100, "L": 9,
                        "conversion_ratio": 10.25, "exchange_ratio": 1.5}])]
        with patch("companion.desktop._read_local_report", return_value={}), \
             patch("companion.desktop.replay_evidence.load", return_value={}), \
             patch("companion.desktop._local_analysis", return_value=({"players": players}, "")):
            app.show_match_details({"path": "unused", "linked": True})
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        player_table, model_table = [widget for widget in descendants(self.root)
                                     if isinstance(widget, self.ttk.Treeview) and widget is not app.tree]
        self.assertEqual(player_table.get_children(), ("1", "0"))
        self.assertEqual(player_table.selection(), ("1",))
        self.assertEqual([model_table.set(item, "model") for item in model_table.get_children()], ["Twenty", "One"])
        self.click(player_table, "official_k")
        self.assertEqual(player_table.get_children(), ("0", "1"))
        self.assertEqual(player_table.selection(), ("1",))
        self.click(player_table, "official_k")
        self.assertEqual(player_table.get_children(), ("1", "0"))
        self.click(model_table, "conversion")
        self.assertEqual([model_table.set(item, "model") for item in model_table.get_children()], ["Twenty", "One"])
        self.click(model_table, "conversion")
        self.assertEqual([model_table.set(item, "model") for item in model_table.get_children()], ["One", "Twenty"])
        self.click(model_table, "exchange")
        self.assertEqual([model_table.set(item, "model") for item in model_table.get_children()], ["Twenty", "One"])
        self.click(model_table, "exchange")
        self.assertEqual([model_table.set(item, "model") for item in model_table.get_children()], ["Twenty", "One"])
        # Changing player keeps the chosen model sort and uses that player's
        # stable record id even after the player table has been reordered.
        player_table.selection_set("0")
        self.root.update()
        self.assertEqual(model_table.set(model_table.get_children()[0], "model"), "Wrong player's unit")
        self.assertIn("▲", model_table.heading("exchange")["text"])


class DesktopMapLabelTests(unittest.TestCase):
    """A match must never show a blank map when any source knows it."""

    def _local(self, base, name, wrapper=None, folder="unbound-replays", map_name=None):
        report = {"game_build": 168061, "report_generation": 1,
                  "native_players": [{"player_user_id": "1", "name": "P0", "alliance": 0}],
                  "rows": [{"game_time_seconds": 60.0}]}
        if map_name:
            report["map"] = map_name
        value = {"schema": "sd2-local-unbound-task-report-v4", "report": report,
                 "status": "local_only_not_session_verified", **(wrapper or {})}
        path = base / folder / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _export(self, base, name, digest, map_id=None, map_name=None):
        payload = {"schema": "sd2-task-report-v4", "submission_id": name, "game_build": 168061,
                   "report_generation": 1, "native_players": [{"player_user_id": "1", "name": "P0", "alliance": 0}],
                   "rows": [], "client_replay_sha256": digest}
        if map_id:
            payload["map"] = map_id
        if map_name:
            payload["map_name"] = map_name
        path = base / "exports" / digest / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_local_snapshot_wrapper_supplies_the_map(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._local(base, "live", wrapper={"map": "_3x2_Mogilev_LD_10v10", "server_name": "[ASIA-JP] 1"})
            entry = LocalReportInventory(base).entries()[0]
            # The raw id is translated through the same table the website uses.
            self.assertEqual(entry["map"], maps.display_name("_3x2_Mogilev_LD_10v10"))
            self.assertEqual(entry["server"], "[ASIA-JP] 1")

    def test_map_table_falls_back_to_the_identifier(self):
        self.assertEqual(maps.display_name("_3x2_Mogilev_LD_10v10"), "Mogilev #10vs10")
        self.assertEqual(maps.display_name("_3x2_Mogilev_LD_10v10_BKT"), "Mogilev #10vs10")
        self.assertEqual(maps.display_name("unknown-map-id"), "unknown-map-id")
        self.assertIsNone(maps.display_name(None))

    def test_replay_evidence_turns_a_map_id_into_the_battlefield_name(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            digest = "a" * 64
            self._export(base, "replay", digest, map_id="_3x2_Mogilev_LD_10v10")
            evidence = replay_evidence.evidence_path(base, digest)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(json.dumps(replay_evidence.compact({
                "match": {"version": 168061, "map": "_3x2_Mogilev_LD_10v10", "map_name": "Mogilev #10vs10"}})),
                encoding="utf-8")
            entry = LocalReportInventory(base).entries()[0]
            self.assertEqual(entry["map"], "Mogilev #10vs10")

    def test_evidence_wins_over_a_raw_map_id_but_not_over_a_real_name(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            digest = "b" * 64
            self._export(base, "named", digest, map_name="Slutsk 4v4")
            evidence = replay_evidence.evidence_path(base, digest)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(json.dumps(replay_evidence.compact(
                {"match": {"version": 168061, "map_name": "Mogilev #10vs10"}})), encoding="utf-8")
            entry = LocalReportInventory(base).entries()[0]
            self.assertEqual(entry["map"], "Slutsk 4v4")

    def test_replay_identity_hint_only_uses_a_single_candidate(self):
        identity = SimpleNamespace(map="map-one", server_name="Test Room", session_id="session-one")
        candidate = SimpleNamespace(snapshot=SimpleNamespace(identity=identity))
        state = SimpleNamespace(candidates={"a": candidate, "b": SimpleNamespace(snapshot=None)})
        self.assertEqual(replay_capture.ReplayCaptureService._identity_hint(state),
                         {"map": "map-one", "server_name": "Test Room", "session_id": "session-one"})
        # Two different replays could belong to different maps: never guess.
        other = SimpleNamespace(map="map-two", server_name="Test Room", session_id="session-two")
        ambiguous = SimpleNamespace(candidates={"a": candidate,
                                                "b": SimpleNamespace(snapshot=SimpleNamespace(identity=other))})
        self.assertEqual(replay_capture.ReplayCaptureService._identity_hint(ambiguous), {})
        # Two candidates that agree on the same header still give one answer.
        same = SimpleNamespace(candidates={"a": candidate,
                                           "b": SimpleNamespace(snapshot=SimpleNamespace(identity=identity))})
        self.assertEqual(replay_capture.ReplayCaptureService._identity_hint(same)["map"], "map-one")


    def test_scope_picks_all_components_when_roles_are_unknown(self):
        unknown_roles = {"main": {"kill_count": 0, "K": None, "unknown_role_count": 9},
                         "all_components": {"kill_count": 12, "K": 480}}
        self.assertEqual(desktop._scope_metrics({"economy_by_scope": unknown_roles})[0], "all_components")
        self.assertEqual(desktop._scope_metrics({"economy_by_scope": unknown_roles})[1]["K"], 480)
        # A fully classified match prefers the same scope the website defaults to.
        classified = {"main": {"kill_count": 5, "K": 200}, "all_components": {"kill_count": 5, "K": 200}}
        self.assertEqual(desktop._scope_metrics({"economy_by_scope": classified})[0], "all_components")
        # Older payloads without scopes fall back to the single economy metric.
        self.assertEqual(desktop._scope_metrics({"economy": {"K": 10}})[1]["K"], 10)
        self.assertEqual(desktop._scope_metrics({})[0], "main")


class DesktopLanguageTests(unittest.TestCase):
    def test_every_string_exists_in_both_languages(self):
        for key, texts in desktop._TEXT.items():
            with self.subTest(key=key):
                self.assertEqual(len(texts), 2)
                self.assertTrue(texts[0].strip() and texts[1].strip())

    def test_english_is_the_default_and_chinese_is_explicit(self):
        self.assertEqual(desktop._language(None), "en")
        self.assertEqual(desktop._language("zh"), "zh")
        self.assertEqual(desktop._language("fr"), "en")
        example = example_config()
        self.assertEqual(example["language"], "en")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.json"
            self.assertEqual(prepare_config(path).language, "en")
            state = {"status": "实时会话：等待识别", "code": "live_report_waiting_for_session"}
            model = SimpleNamespace(**state)
            self.assertIn("Waiting for the live session", desktop._status_text(model, "en"))
            self.assertEqual(desktop._status_text(model, "zh"), "实时会话：等待识别")

    def test_switching_language_rewrites_the_config_and_rebuilds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.json"
            prepare_config(path)
            app = object.__new__(DesktopApp)
            app.config_path = path
            app.language = "en"
            app.root = Mock()
            app._build_ui = Mock()
            app.set_language("zh")
            self.assertEqual(app.language, "zh")
            app._build_ui.assert_called_once()
            self.assertEqual(decode_config(path.read_text(encoding="utf-8"))["language"], "zh")
            app._build_ui.reset_mock()
            app.set_language("zh")
            app._build_ui.assert_not_called()

    def test_config_rejects_an_unknown_language(self):
        with self.assertRaises(ConfigError):
            from companion.config import from_dict
            from_dict({**example_config(), "language": "fr"})


class DesktopEvidenceTests(unittest.TestCase):
    def test_a_replay_export_is_a_local_replay_and_a_live_export_is_an_online_match(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            digest = "a" * 64
            replay_payload = {"schema": "sd2-task-report-v4", "submission_id": "s1", "game_build": 168061,
                              "report_generation": 1, "native_players": [], "rows": [],
                              "client_replay_sha256": digest, "map": "Mogilev #10vs10"}
            path = base / "exports" / digest / "s1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(replay_payload), encoding="utf-8")
            entry = LocalReportInventory(base).entries()[0]
            self.assertEqual(entry["replay"], digest)
            self.assertEqual(entry["kind"], "local")
            # A live export carries no replay digest and belongs to the server list.
            live_payload = {key: value for key, value in replay_payload.items() if key != "client_replay_sha256"}
            live_payload.update(submission_id="s2", session_id="session-one", report_generation=2)
            live_path = base / "exports" / "live" / "digest" / "s2.json"
            live_path.parent.mkdir(parents=True, exist_ok=True)
            live_path.write_text(json.dumps(live_payload), encoding="utf-8")
            kinds = {row["replay"] or row["fingerprint"][:8]: row["kind"]
                     for row in LocalReportInventory(base).entries()}
            self.assertEqual(sorted(kinds.values()), ["local", "online"])
            self.assertEqual(kinds.get(digest), "local")

    def test_captures_without_a_local_replay_file_are_online_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = {"schema": "sd2-local-unbound-task-report-v4",
                        "report": {"game_build": 168061, "report_generation": 1, "native_players": [], "rows": []},
                        "status": "local_only_not_session_verified",
                        "map": "_3x2_Mogilev_LD_10v10", "session_id": "session-one"}
            live = base / "unbound-live" / "one.json"
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text(json.dumps(snapshot), encoding="utf-8")
            # A spectated server match is captured from the game, but it has no local
            # .rpl3 binding: it belongs to the online list, not to local replays.
            spectated = {"schema": "sd2-local-unbound-task-report-v4",
                         "report": {"game_build": 168061, "report_generation": 2,
                                    "native_players": [], "rows": []}}
            spectated_path = base / "unbound-replays" / "one.json"
            spectated_path.parent.mkdir(parents=True, exist_ok=True)
            spectated_path.write_text(json.dumps(spectated), encoding="utf-8")
            rows = LocalReportInventory(base).entries()
            self.assertEqual([row["kind"] for row in rows], ["online", "online"])
            by_map = {row["map"]: row for row in rows}
            self.assertEqual(by_map[maps.display_name("_3x2_Mogilev_LD_10v10")]["linked"], False)
            self.assertFalse(any(row["replay"] for row in rows))

    def test_detail_analysis_uses_stored_evidence_and_reports_its_absence(self):
        report = {"game_build": 168061, "report_generation": 1,
                  "native_players": [{"player_user_id": "1", "name": "A", "alliance": 0}], "rows": []}
        with patch("companion.aggregation.derive_match") as derive:
            derive.return_value = {"players": []}
            desktop._local_analysis(report, {"identity_binding": {"validated": True}})
            self.assertEqual(derive.call_args.args[1], {"identity_binding": {"validated": True}})
            desktop._local_analysis(report)
            self.assertEqual(derive.call_args.args[1], {})


if __name__ == "__main__":
    unittest.main()
