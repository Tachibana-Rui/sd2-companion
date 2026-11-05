"""Real orchestration invariants with fake observations, not a live game."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from companion.cli import RunLock, main
from companion.config import ConfigError, from_dict, load
from companion.getters import GetterError, resolve_getter
from companion.outbox import Outbox
from companion.replays import HeaderError, ReplayIdentity, ReplayWatcher, parse_header


def settings(base):
    return from_dict({"schema": 1, "rooms": [{"id": "room-one", "endpoints": [{"host": "127.0.0.1", "port": 7552}], "server_names": ["Test Room"]}],
                      "replay_directories": [str(base / "replays")], "data_directory": str(base / "data")}, base=base)


def replay(sid="native-session", *, name="Test Room"):
    raw = json.dumps({"game": {"UniqueSessionId": sid, "Version": "168061", "Map": "map-one", "ServerName": name},
                      "player_0": {"PlayerUserId": "123", "PlayerName": "测试玩家", "PlayerAlliance": "0"}}, ensure_ascii=False).encode()
    body = struct.pack(">4sQI", b"rply", len(raw), 0) + raw
    return struct.pack(">4sIII", b"ESAV", 3, 16 + len(body), 0) + body


def identity(sid="native-session", at=10, name="Test Room"):
    return parse_header(replay(sid, name=name), observed_at=at)


def report(generation=1, rows=None):
    return {"variant_address": 0x100000, "result_address": 0x200000 + generation * 0x1000,
            "generation": generation, "game_build": 168061,
            "rows": rows if rows is not None else [{"row_index": 0, "game_time_seconds": 2.0,
                    "killer": {"player_name": "甲", "unit_name": "Tank"},
                    "victim": {"player_name": "乙", "unit_name": "AT"}}]}


class ConfigAndHeaderTests(unittest.TestCase):
    def test_configuration_paths_are_relative_to_config_not_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = {"schema": 1, "rooms": [{"id": "room", "endpoints": [{"host": "::1", "port": 7552}]}],
                    "replay_directories": ["replays"], "data_directory": "data"}
            path = root / "config.json"; path.write_text(json.dumps(data))
            config = load(path)
            self.assertEqual(config.data_directory, root / "data")
            self.assertEqual(config.replay_directories, (root / "replays",))

    def test_credentials_unknown_options_and_shared_endpoints_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory))
            base = {"schema": 1, "rooms": [{"id": "one", "endpoints": [{"host": "localhost", "port": 1}]}], "replay_directories": [directory]}
            for additions in ({"token": "secret"}, {"upload_url": "https://user:password@example.test/post"}, {"poll_seconds": True}):
                with self.subTest(additions=list(additions)), self.assertRaises(ConfigError):
                    from_dict({**base, **additions})
            with self.assertRaises(ConfigError):
                from_dict({**base, "rooms": base["rooms"] + [{"id": "two", "endpoints": [{"host": "localhost", "port": 1}]}]})

    def test_header_identity_is_opaque_and_no_deck_exported(self):
        item = parse_header(replay("not-a-uuid:session"))
        self.assertEqual(item.session_id, "not-a-uuid:session")
        self.assertEqual(item.participants[0]["id"], "123")
        self.assertNotIn("deck", item.public_metadata())

    def test_header_observers_do_not_enter_report_participant_roster(self):
        data = bytearray(replay().replace(b'"player_0":', b'"player_1": {"PlayerObserver": "1"}, "player_0":'))
        struct.pack_into(">I", data, 8, len(data))
        struct.pack_into(">Q", data, 20, len(data) - 32)
        item = parse_header(bytes(data))
        self.assertEqual([p["id"] for p in item.participants], ["123"])

    def test_header_does_not_claim_growing_file_is_complete(self):
        data = bytearray(replay());struct.pack_into(">I", data, 8, 999999)
        self.assertEqual(parse_header(bytes(data)).session_id, "native-session")
        for bad in (b"pickle data", replay()[:40], replay().replace(b'"Version": "168061"', b'"Version": true')):
            with self.subTest(length=len(bad)), self.assertRaises(HeaderError):
                parse_header(bad)

    def test_watcher_baseline_growth_and_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);path = root / "Last_Replay.rpl3";path.write_bytes(replay("old"))
            watcher = ReplayWatcher([root]);watcher.baseline()
            self.assertEqual(watcher.poll(1), [])
            stamp = path.stat().st_mtime_ns
            path.write_bytes(replay("new"))
            os.utime(path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
            self.assertEqual([x.session_id for x in watcher.poll(2)], ["new"])
            # Header checksum/length updates and stream growth are not a new session.
            grown = bytearray(replay("new") + b"tail");struct.pack_into(">II", grown, 8, len(grown), 123)
            path.write_bytes(grown)
            os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
            self.assertEqual(watcher.poll(3), [])
            path.write_bytes(replay("next"))
            os.utime(path, ns=(stamp + 3_000_000_000, stamp + 3_000_000_000))
            self.assertEqual([x.session_id for x in watcher.poll(4)], ["next"])


class FakeMemory:
    def __init__(self): self.regions = {}
    def put(self, address, data): self.regions[address] = data
    def read(self, address, size):
        for base, data in self.regions.items():
            if base <= address and address + size <= base + len(data): return data[address - base:address - base + size]
        raise OSError("unreadable")


class GetterTests(unittest.TestCase):
    def test_real_localization_this_adjustor_resolves_without_execution(self):
        memory = FakeMemory();instance = 0x100000;function = 0x200000;leaf = function + 0x100
        data = bytearray(256)
        struct.pack_into('<Q', data, 0x80, 0x300000)
        struct.pack_into('<Q', data, 0x80 + 0x68, 0x400000)
        memory.put(instance, bytes(data))
        memory.put(0x300000, bytes(0x48) + struct.pack('<Q', leaf))
        memory.put(function, bytes.fromhex('4883e980488b0148ff6048').ljust(16, b'\xcc'))
        memory.put(leaf, bytes.fromhex('488b4168c3').ljust(16, b'\xcc'))
        self.assertEqual(resolve_getter(memory, instance, function), 0x400000)
        memory.put(0x300000, bytes(0x48) + struct.pack('<Q', function))
        with self.assertRaisesRegex(GetterError, 'getter_cycle'):
            resolve_getter(memory, instance, function)
        memory.put(function, bytes.fromhex('4883e980488b0148ff5048').ljust(16, b'\xcc'))
        with self.assertRaisesRegex(GetterError, 'getter_instructions_unsupported'):
            resolve_getter(memory, instance, function)

    def test_only_side_effect_free_getters_and_tail_jumps_are_interpreted(self):
        memory = FakeMemory();this=0x100000;fn=0x200000
        memory.put(this, b"\0" * 16 + struct.pack("<Q", 0x300000) + b"\0" * 16)
        for code, expected in ((b"\x48\x8b\x41\x10\xc3", 0x300000), (b"\x48\x8d\x81\x00\x01\0\0\xc3", this + 256)):
            memory.put(fn, code.ljust(16,b"\xcc"));self.assertEqual(resolve_getter(memory,this,fn),expected)
        memory.put(fn,b"\xe9"+struct.pack("<i",0x100-5)+b"\xcc"*11)
        memory.put(fn+256,b"\x48\x8b\xc1\xc3"+b"\xcc"*12)
        self.assertEqual(resolve_getter(memory,this,fn),this)

    def test_write_call_and_cycles_never_executed(self):
        memory=FakeMemory();fn=0x200000
        for code in (b"\x48\x89\x01\xc3", b"\xe8\0\0\0\0\xc3", b"\xe9\xfb\xff\xff\xff"):
            memory.put(fn,code.ljust(16,b"\xcc"))
            with self.assertRaises(GetterError):resolve_getter(memory,0x100000,fn)






class CliTests(unittest.TestCase):
    def test_frozen_windowless_launch_opens_the_desktop_window(self):
        from companion.cli import default_arguments
        self.assertEqual(default_arguments(None, stream=None, frozen=True, arguments=[]), ["desktop"])
        # An explicit subcommand, a real console, or a source run keep argparse in charge.
        self.assertIsNone(default_arguments(None, stream=None, frozen=True, arguments=["doctor"]))
        self.assertIsNone(default_arguments(None, stream=io.StringIO(), frozen=True, arguments=[]))
        self.assertIsNone(default_arguments(None, stream=None, frozen=False, arguments=[]))
        self.assertEqual(default_arguments(["run"], stream=None, frozen=True, arguments=[]), ["run"])

    def test_default_config_follows_the_frozen_executable(self):
        from companion.cli import default_config_path
        self.assertEqual(default_config_path().parent.parent, Path(Path(__file__).resolve().parents[0]))
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "SD2Companion.exe"
            executable.touch()
            with patch("companion.cli.sys.frozen", True, create=True), \
                    patch("companion.cli.sys.executable", str(executable)):
                frozen = default_config_path()
        self.assertEqual(frozen, executable.resolve().parent / "private-reports" / "companion.json")

    def test_v3_cli_exposes_only_automatic_capture_and_maintenance(self):
        from companion.cli import parser
        for command in ("capture", "bind"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parser().parse_args([command])
                self.assertEqual(error.exception.code, 2)

    def test_legacy_config_uses_auto_unless_live_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'legacy.json'
            config.write_text(json.dumps({'schema': 1, 'rooms': [{'id': 'old-room', 'endpoints': [
                {'host': '127.0.0.1', 'port': 7552}]}], 'replay_directories': ['replays']}), encoding='utf-8')
            for arguments, expected in (([], 'auto'), (['--mode', 'live'], 'live')):
                with self.subTest(mode=expected), patch('companion.cli._run', return_value=0) as run:
                    self.assertEqual(main(['run', '--config', str(config), '--once', *arguments]), 0)
                    self.assertEqual(run.call_args.args[0].collection_mode, expected)

    def test_missing_configuration_identifies_path_and_setup_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'not-created' / 'companion.json'
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = main(['run', '--config', str(path)])
            error = json.loads(stdout.getvalue())
            self.assertEqual(result, 3)
            self.assertEqual(error['code'], 'configuration_missing')
            self.assertEqual(error['config_path'], str(path.resolve()))
            self.assertIn('init', error['next'])
            self.assertFalse(path.exists())

    def test_configuration_requiring_host_edit_returns_launcher_setup_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'companion.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['init', '--output', str(path)]), 0)
            original = path.read_bytes()
            with patch('companion.cli._run', side_effect=ConfigError('configure_target_game_server_host_first')), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = main(['run', '--config', str(path)])
            error = json.loads(stdout.getvalue())
            self.assertEqual(result, 3)
            self.assertEqual(error['code'], 'configure_target_game_server_host_first')
            self.assertEqual(error['config_path'], str(path.resolve()))
            self.assertEqual(path.read_bytes(), original)

    def test_init_doctor_status_are_reviewable_without_process_access(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/'companion.json';stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(['init','--output',str(path)]),0)
                self.assertEqual(main(['init','--output',str(path)]),2)
                self.assertEqual(main(['doctor','--config',str(path)]),0)
                self.assertEqual(main(['status','--config',str(path)]),0)
            outputs=[json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertFalse(outputs[2]['game_process_accessed'])
            self.assertFalse(outputs[2]['target_hosts_configured'])

    def test_single_instance_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLock(directory):
                with self.assertRaises(ValueError):
                    with RunLock(directory):pass
            with RunLock(directory):pass



if __name__=='__main__':unittest.main()
