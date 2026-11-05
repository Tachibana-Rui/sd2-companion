"""Automatic observer tests with real native-session bytes and fake OS APIs."""
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock, patch

from companion.config import Config
from companion.file_access import FileMonitorError
from companion.live_runtime import AutomaticWindowsObserver
from companion.native_session import REPLAY_SOURCE_VTABLE_RVA, RUNTIME_CONTEXT_RVA
from companion.windows import ProcessInfo
from test_companion_native_session import SessionFixture
from test_companion_replay_capture import native_report
from test_companion_replay_runtime import FakeMonitor


class ReportDriver:
    """Only the report producer is faked; native session reads use its ABI."""
    def __init__(self):
        self.baseline_ready = True
        self.status = "waiting_for_published_report"
        self.value = None
        self.callback = None
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.callback is not None:self.callback()
        self.status = "native_report_read" if self.value is not None else "waiting_for_published_report"
        return self.value


class AutomaticRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SessionFixture()
        self.memory = self.fixture.memory
        self.memory.close = Mock()
        self.monitor = FakeMonitor()
        self.driver = ReportDriver()
        self.config = Config(rooms=(), replay_directories=(), data_directory=Path("unused"), collection_mode="auto")
        self.process = ProcessInfo(101, 10000, "SteelDivision2.exe", self.memory.exe_sha256, self.memory.image_base)
        self.discovery = self.start_patch("companion.windows.list_game_processes", return_value=[self.process])
        self.memory_factory = self.start_patch("companion.windows.WindowsMemory", return_value=self.memory)
        # NativeSessionReader imported by SessionFixture retains the real
        # budgeted reader; the observer receives only this report driver.
        self.report_factory = self.start_patch("companion.native_report.NativeReportReader", return_value=self.driver)
        self.start_patch("companion.windows.tcp_connections", side_effect=AssertionError("live identity does not need TCP"))
        self.observer = AutomaticWindowsObserver(self.config, monitor=self.monitor)
        self.addCleanup(self.observer.close)

    def start_patch(self, name, **kwargs):
        patcher = patch(name, **kwargs)
        value = patcher.start();self.addCleanup(patcher.stop)
        return value

    def baseline(self):
        observation = self.observer.poll(10)
        self.assertTrue(observation["baseline_ready"])
        self.assertIsNone(observation["baseline_report"])
        self.assertNotIn("report", observation)
        return observation

    def ready_report(self):
        self.driver.value = native_report()

    def test_real_memory_session_is_read_before_and_after_report_without_tcp(self):
        observation = self.baseline()
        self.assertTrue(observation["report_context_stable"])
        self.assertEqual(observation["native_session"]["source_kind"], "network_live")
        self.assertEqual(observation["native_session"]["session_id"], "ABCD0123-4567-89AB-CDEF-0123456789AB")
        self.assertEqual(observation["native_session_status"], "native_session_read")
        self.assertNotIn("room_id", observation)
        self.ready_report()
        observation = self.observer.poll(10.5)
        self.assertIs(observation["report"], self.driver.value)
        self.assertTrue(observation["report_context_stable"])
        # Each production session snapshot checks the owner link twice, and
        # the observer brackets its one report poll with two such snapshots.
        owner_address = self.memory.image_base + RUNTIME_CONTEXT_RVA
        self.assertGreaterEqual(self.memory.reads.count((owner_address, 8)), 8)

    def test_false_to_true_ended_transition_is_allowed_across_same_report_read(self):
        self.baseline();self.ready_report()
        self.driver.callback = lambda: self.memory.write(self.fixture.context + 0x59, b"\1")
        observation = self.observer.poll(10.5)
        self.assertTrue(observation["report_context_stable"])
        self.assertTrue(observation["native_session"]["game_ended"])

    def test_true_to_false_ended_transition_rejects_report_context(self):
        self.memory.write(self.fixture.context + 0x59, b"\1")
        self.baseline();self.ready_report()
        self.driver.callback = lambda: self.memory.write(self.fixture.context + 0x59, b"\0")
        observation = self.observer.poll(10.5)
        self.assertFalse(observation["report_context_stable"])
        self.assertIs(observation["report"], self.driver.value)

    def test_switching_to_replay_during_report_read_is_not_stable(self):
        self.baseline();self.ready_report()
        def switch_source():
            self.memory.u64(self.fixture.source, self.memory.image_base + REPLAY_SOURCE_VTABLE_RVA)
            self.memory.write(self.fixture.manager + 0x75, b"\1")
        self.driver.callback = switch_source
        observation = self.observer.poll(10.5)
        self.assertEqual(observation["native_session"]["source_kind"], "replay")
        self.assertFalse(observation["report_context_stable"])

    def test_same_roster_changed_session_during_read_changes_epoch_and_rejects(self):
        self.baseline();self.ready_report()
        identifier = "ABCD0123-4567-89AB-CDEF-0123456789AB"
        handle = self.fixture.string_handles[identifier][0]
        pointer = struct.unpack("<Q", self.memory.read(handle + 0x10, 8))[0]
        replacement = "BBCD0123-4567-89AB-CDEF-0123456789AB"
        self.driver.callback = lambda: self.memory.write(pointer, replacement.encode() + b"\0")
        observation = self.observer.poll(10.5)
        self.assertEqual(observation["native_session"]["session_id"], replacement)
        self.assertTrue(observation["native_context_changed"])
        self.assertFalse(observation["report_context_stable"])

    def test_map_or_player_identity_changes_are_detected_without_epoch_change(self):
        for value, replacement in (("map_test_10v10", "map_test_20v20"), ("123", "456")):
            with self.subTest(value=value):
                fixture = SessionFixture();memory = fixture.memory;memory.close = Mock()
                self.memory_factory.return_value = memory
                driver = ReportDriver();self.report_factory.return_value = driver
                observer = AutomaticWindowsObserver(self.config, monitor=FakeMonitor())
                try:
                    observer.poll(10)
                    handle = fixture.string_handles[value][0]
                    driver.value = native_report()
                    driver.callback = lambda: memory.write(handle + 0x10, replacement.encode() + b"\0")
                    observation = observer.poll(10.5)
                    self.assertFalse(observation["report_context_stable"])
                finally:observer.close()

    def test_unknown_source_during_report_read_preserves_raw_report_but_no_identity(self):
        self.baseline();self.ready_report()
        self.driver.callback = lambda: self.memory.u64(self.fixture.source, self.memory.image_base + 0x123456)
        observation = self.observer.poll(10.5)
        self.assertIsNone(observation["native_session"])
        self.assertEqual(observation["native_session_status"], "native_session_source_unknown")
        self.assertFalse(observation["report_context_stable"])
        self.assertIs(observation["report"], self.driver.value)

    def test_session_becoming_available_mid_read_does_not_keep_stale_error_status(self):
        self.memory.u64(self.memory.image_base + RUNTIME_CONTEXT_RVA, 0)
        self.driver.callback = lambda: self.memory.u64(self.memory.image_base + RUNTIME_CONTEXT_RVA, self.fixture.context)
        observation = self.observer.poll(10)
        self.assertIsNotNone(observation["native_session"])
        self.assertFalse(observation["report_context_stable"])
        self.assertEqual(observation["native_session_status"], "native_session_read")

    def test_file_monitor_requires_elevation_still_reads_native_session_and_reports(self):
        def denied():raise FileMonitorError("file_monitor_requires_elevation")
        self.monitor.start = denied
        observation = self.baseline()
        self.assertEqual(observation["file_monitor_error"], "file_monitor_requires_elevation")
        self.assertTrue(observation["access_uncertain"])
        self.assertNotIn("error", observation)
        self.assertEqual(observation["native_session"]["source_kind"], "network_live")
        self.ready_report()
        observation = self.observer.poll(10.5)
        self.assertIs(observation["report"], self.driver.value)
        self.assertTrue(observation["report_context_stable"])

    def test_live_only_observer_does_not_start_file_monitor(self):
        self.monitor.start = Mock(side_effect=AssertionError("must not start file monitoring"))
        observer = AutomaticWindowsObserver(self.config, monitor=self.monitor, with_replays=False)
        try:
            observation = observer.poll(10)
            self.assertEqual(observation["native_session"]["source_kind"], "network_live")
            self.assertNotIn("file_monitor_error", observation)
            self.monitor.start.assert_not_called()
        finally:observer.close()

    def test_process_exit_closes_memory_and_forgets_native_session_reader(self):
        self.baseline()
        self.discovery.return_value = []
        observation = self.observer.poll(14)
        self.assertEqual(observation["error"], "game_not_running")
        self.memory.close.assert_called_once()
        self.assertIsNone(self.observer.session_reader)
        self.assertIsNone(self.observer.session_reader_key)

    def test_reused_pid_with_new_creation_time_rebuilds_session_epoch_and_baseline(self):
        first = self.baseline()
        reader = self.observer.session_reader
        self.discovery.return_value = [ProcessInfo(101, 20000, "SteelDivision2.exe", self.memory.exe_sha256, self.memory.image_base)]
        second = self.observer.poll(14)
        self.assertNotEqual(first["process_key"], second["process_key"])
        self.assertIsNot(reader, self.observer.session_reader)
        self.assertNotEqual(first["native_session"]["context_epoch"], second["native_session"]["context_epoch"])
        self.assertTrue(second["baseline_ready"])
        self.assertNotIn("report", second)


if __name__ == "__main__":unittest.main()
