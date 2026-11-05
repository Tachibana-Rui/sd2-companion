"""Windows observer lifecycle with fake APIs; never opens a real process."""

from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from companion.config import Config, Endpoint, Room
from companion.native_report import NativeReport, UnsupportedBuildError
from companion.runtime import WindowsObserver
from companion.windows import ProcessInfo, TcpConnection


def report(generation=1):
    return NativeReport(generation=generation, result_address=0x200000 + generation * 0x1000,
                        variant_address=0x100000, rows=[])


class _FakeCatalog:
    """Stand-in for RemoteCatalog: no network, fixed table and tunables."""

    def __init__(self, rooms=(), settings=None):
        self.rooms, self.settings = rooms, dict(settings or {})

    def refresh_if_due(self, now):
        return False


class FakeMemory:
    def __init__(self, process):
        self.process = process
        self.close_count = 0
        self.reset_count = 0

    def close(self):
        self.close_count += 1

    def reset_budget(self):
        self.reset_count += 1


class FakeReader:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.baseline_ready = False
        self.poll_count = 0
        self.memory = None

    def poll(self):
        self.poll_count += 1
        if not self.steps:
            return None
        ready, result = self.steps.pop(0)
        self.baseline_ready = ready
        return result


class WindowsObserverTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(rooms=(Room("room-one", (Endpoint("fixture.invalid", 7552),), ("Fixture Room",)),),
                             replay_directories=(), data_directory=Path("unused-fixture-data"),
                             game_executable=Path("unused-fixture-game.exe"), process_poll_seconds=1)
        self.first = ProcessInfo(101, 10000, "unused-fixture-game.exe", "a" * 64, 0x140000000)
        self.second = ProcessInfo(101, 20000, "unused-fixture-game.exe", "a" * 64, 0x140000000)
        self.connection = TcpConnection("ipv4", "127.0.0.1", 55000, "127.0.0.1", 7552, 5, 101)
        self.memories = []
        self.readers = []
        self.reader_queue = []
        self.resolve = self.start_patch("companion.runtime.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 7552))])
        self.discover = self.start_patch("companion.windows.list_game_processes", return_value=[self.first])
        self.tcp = self.start_patch("companion.windows.tcp_connections", return_value=[self.connection])
        self.start_patch("companion.windows.WindowsMemory", side_effect=self.make_memory)
        self.start_patch("companion.native_report.NativeReportReader", side_effect=self.make_reader)

    def start_patch(self, name, **options):
        patcher = patch(name, **options)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def make_memory(self, process):
        memory = FakeMemory(process)
        self.memories.append(memory)
        return memory

    def make_reader(self, memory):
        reader = self.reader_queue.pop(0)
        if isinstance(reader, Exception):
            raise reader
        reader.memory = memory
        self.readers.append(reader)
        return reader

    def observer(self, *readers):
        self.reader_queue.extend(readers)
        observer = WindowsObserver(self.config)
        self.addCleanup(observer.close)
        return observer

    def test_the_served_catalog_supplies_rooms_and_tunables(self):
        catalog = _FakeCatalog(rooms=(Room("catalog-room", (Endpoint("catalog.invalid", 7552),), ("Served",)),),
                              settings={"process_poll_seconds": 2.0})
        self.reader_queue.append(FakeReader((False, None)))
        observer = WindowsObserver(self.config, catalog=catalog)
        self.addCleanup(observer.close)
        observation = observer.poll(10)
        self.assertEqual(observation["room_id"], "catalog-room")
        self.assertEqual(observer.config.process_poll_seconds, 2.0)
        self.assertEqual(observer.config.poll_seconds, self.config.poll_seconds)

    def test_an_empty_remote_catalog_does_not_fall_back_to_old_disk_rooms_or_stop_capture(self):
        self.reader_queue.append(FakeReader((False, None)))
        observer = WindowsObserver(self.config, catalog=_FakeCatalog())
        self.addCleanup(observer.close)
        observation = observer.poll(10)
        self.assertIsNone(observation["room_id"])
        self.assertNotIn("error", observation)
        self.assertEqual(observer.rooms(), ())
        self.resolve.assert_not_called()
        self.assertEqual(self.readers[0].poll_count, 1)

    def test_a_name_that_stops_resolving_keeps_the_previous_addresses(self):
        reader = FakeReader((False, None), (False, None))
        observer = self.observer(reader)
        self.assertEqual(observer.poll(10)["room_id"], "room-one")
        self.resolve.side_effect = socket.gaierror(11001, "no such host")
        observer.next_dns_refresh = 0
        observation = observer.poll(11)
        self.assertNotIn("error", observation)
        self.assertEqual(observation["room_id"], "room-one")
        self.assertEqual(observer.resolve_error, "target_endpoint_resolution_failed")
        # Retry sooner than the normal minute while a name is failing.
        self.assertLessEqual(observer.next_dns_refresh - 11, 10)

    def test_partial_controller_scan_does_not_claim_baseline_absence(self):
        reader = FakeReader((False, None), (False, None), (True, None))
        observer = self.observer(reader)
        for now in (10, 10.25):
            observation = observer.poll(now)
            self.assertEqual(observation["room_id"], "room-one")
            self.assertFalse(observation["baseline_ready"])
            self.assertNotIn("report", observation)
            self.assertFalse(observer.baseline_ready)
        observation = observer.poll(10.5)
        self.assertTrue(observation["baseline_ready"])
        self.assertIsNone(observation["baseline_report"])
        self.assertNotIn("report", observation)
        self.assertEqual(self.discover.call_count, 1)
        self.assertEqual(self.memories[0].reset_count, 3)

    def test_existing_report_is_initial_baseline_only_then_new_report_is_emitted(self):
        old, new = report(1), report(2)
        observer = self.observer(FakeReader((True, old), (True, None), (True, new)))
        first = observer.poll(10)
        self.assertTrue(first["baseline_ready"])
        self.assertIs(first["baseline_report"], old)
        self.assertNotIn("report", first)
        self.assertNotIn("report", observer.poll(10.25))
        third = observer.poll(10.5)
        self.assertIs(third["report"], new)
        self.assertIs(third["baseline_report"], old)

    def test_first_report_after_confirmed_absence_is_emitted_normally(self):
        fresh = report()
        observer = self.observer(FakeReader((True, None), (True, fresh)))
        first = observer.poll(10)
        self.assertTrue(first["baseline_ready"])
        self.assertIsNone(first["baseline_report"])
        second = observer.poll(10.5)
        self.assertIs(second["report"], fresh)
        self.assertIsNone(second["baseline_report"])

    def test_tcp_api_failure_is_unknown_connection_not_confirmed_disconnect(self):
        reader = FakeReader((True, None), (True, None))
        observer = self.observer(reader)
        self.tcp.side_effect = [[self.connection], OSError("private fixture OS error"), []]
        connected = observer.poll(10)
        self.assertTrue(connected["connection_observed"])
        self.assertEqual(connected["room_id"], "room-one")
        failed = observer.poll(10.25)
        self.assertFalse(failed["connection_observed"])
        self.assertEqual(failed["process_key"], connected["process_key"])
        self.assertIn("error", failed)
        self.assertNotIn("private fixture", str(failed))
        self.assertEqual(reader.poll_count, 1)
        self.assertEqual(self.memories[0].close_count, 0)
        # A successful API query returning no target socket is distinguishable.
        disconnected = observer.poll(10.5)
        self.assertTrue(disconnected["connection_observed"])
        self.assertIsNone(disconnected["room_id"])
        self.assertNotIn("error", disconnected)
        self.assertEqual(reader.poll_count, 2)

    def test_process_exit_releases_old_memory_and_reader(self):
        observer = self.observer(FakeReader((True, None)))
        self.discover.side_effect = [[self.first], []]
        observer.poll(10)
        old_memory = self.memories[0]
        self.assertEqual(observer.poll(11), {"error": "game_not_running"})
        self.assertEqual(old_memory.close_count, 1)
        self.assertIsNone(observer.memory)
        self.assertIsNone(observer.reader)
        self.assertIsNone(observer.process)
        self.assertFalse(observer.baseline_ready)
        observer.close()
        self.assertEqual(old_memory.close_count, 1)

    def test_reused_pid_new_creation_time_releases_old_reader_and_resets_baseline(self):
        old, new = report(1), report(20)
        observer = self.observer(FakeReader((True, old)), FakeReader((True, new)))
        self.discover.side_effect = [[self.first], [self.second]]
        first = observer.poll(10)
        second = observer.poll(11)
        self.assertNotEqual(first["process_key"], second["process_key"])
        self.assertEqual(self.memories[0].close_count, 1)
        self.assertEqual(self.memories[1].close_count, 0)
        self.assertIs(observer.reader, self.readers[1])
        self.assertIs(second["baseline_report"], new)
        self.assertNotIn("report", second)
        observer.close()
        self.assertEqual([memory.close_count for memory in self.memories], [1, 1])
        self.assertEqual(observer.poll(12), {"error": "observer_closed"})

    def test_unsupported_native_reader_closes_provisional_memory_and_returns_safe_code(self):
        observer = self.observer(UnsupportedBuildError())
        self.assertEqual(observer.poll(10), {"error": "unsupported_executable"})
        self.assertEqual(self.memories[0].close_count, 1)
        self.assertIsNone(observer.reader)
        self.assertIsNone(observer.memory)
        self.assertIsNone(observer.key)


if __name__ == "__main__":
    unittest.main()
