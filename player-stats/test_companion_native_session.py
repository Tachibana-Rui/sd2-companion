"""Native GameSetup ABI tests; no replay files, processes, UAC or networking."""
from dataclasses import asdict
import json
import struct
import unittest

from companion.native_report import NativeReportError, UnstableSnapshotError, UnsupportedBuildError
from companion.native_session import (
    GAME_BUILD, LAUNCH_MAP_HELPER_RVA, LAUNCH_MAP_HELPER_VTABLE_RVA,
    MAX_COMMON_SETTINGS, MAX_PLAYER_SETTINGS, MAX_PLAYERS, MAX_STRING_BYTES,
    NETWORK_MANAGER_VTABLE_RVAS, NETWORK_SOURCE_VTABLE_RVA, REPLAY_SOURCE_VTABLE_RVA,
    RUNTIME_CONTEXT_RVA, NativeSessionReader,
)
from test_companion_native_report import ByteMemory


class SessionFixture:
    def __init__(self, *, replay=False, ended=False, common=None, players=None):
        self.memory = m = ByteMemory()
        self.string_handles = {}
        self.private_value_pointer = 0x7ABCDEF00000  # Deliberately not mapped.
        if common is None:
            common = [("UniqueSessionId", "ABCD0123-4567-89AB-CDEF-0123456789AB"),
                      ("Map", "map_test_10v10"), ("Version", str(GAME_BUILD)),
                      ("ServerName", "云端房间 🚩"), ("Password", None)]
        common_header, self.common_data = self.settings(common)
        if players is None:
            players = [(3, [("PlayerUserId", "123"), ("PlayerName", "Alice"),
                            ("PlayerAlliance", "0"), ("PlayerObserver", "0"),
                            ("PlayerDeckContent", None)]),
                       (12, [("PlayerUserId", "18446744073709551614"), ("PlayerName", "红方 玩家🚩"),
                             ("PlayerAlliance", "1"), ("PlayerObserver", "0")])]
        rows = bytearray()
        self.player_settings_data = []
        for slot, values in players:
            header, data = self.settings(values)
            self.player_settings_data.append(data)
            rows.extend(struct.pack("<I4x", slot) + header)
        self.players_data = m.alloc(max(1, len(rows)), rows)
        setup_raw = struct.pack("<I4xQ", len(players) | 0x80000000, self.players_data) + common_header
        self.setup = m.alloc(0x20, setup_raw)
        self.owner = m.alloc(0x80)
        m.u64(self.owner, m.image_base + LAUNCH_MAP_HELPER_VTABLE_RVA)
        m.u64(self.owner + 0x78, self.setup)
        self.context = m.alloc(0xB8)
        self.manager = m.alloc(0xE8)
        self.source = m.alloc(8)
        m.u64(self.manager, m.image_base + NETWORK_MANAGER_VTABLE_RVAS[0])
        m.u64(self.manager + 0xE0, self.setup)
        m.u64(self.manager + 0x60, self.source)
        m.u64(self.source, m.image_base + (REPLAY_SOURCE_VTABLE_RVA if replay else NETWORK_SOURCE_VTABLE_RVA))
        m.write(self.manager + 0x75, bytes([int(replay)]))
        m.write(self.context + 0x59, bytes([int(ended)]))
        m.u64(self.context + 0xB0, self.manager)
        m.map(m.image_base + LAUNCH_MAP_HELPER_RVA, struct.pack("<Q", self.owner))
        m.map(m.image_base + RUNTIME_CONTEXT_RVA, struct.pack("<Q", self.context))

    def narrow(self, value, *, capacity=None):
        data = value.encode("utf-8")
        capacity = (15 if len(data) < 16 else len(data)) if capacity is None else capacity
        raw = bytearray(0x30)
        struct.pack_into("<I", raw, 0, 1)
        if capacity < 16:
            raw[0x10:0x10 + len(data)] = data
        else:
            address = self.memory.alloc(len(data) + 1, data + b"\0")
            struct.pack_into("<Q", raw, 0x10, address)
        struct.pack_into("<QQ", raw, 0x20, len(data), capacity)
        handle = self.memory.alloc(0x30, raw)
        self.string_handles.setdefault(value, []).append(handle)
        return handle

    def settings(self, values):
        rows = b"".join(struct.pack("<QQ", self.narrow(key),
                                    self.private_value_pointer if value is None else self.narrow(value))
                        for key, value in values)
        address = self.memory.alloc(max(1, len(rows)), rows)
        return struct.pack("<I4xQ", len(values) | 0x88000000, address), address

    def reader(self):
        return NativeSessionReader(self.memory)


class NativeSessionTests(unittest.TestCase):
    def test_network_session_reads_real_narrow_string_and_dense_maps(self):
        fixture = SessionFixture()
        snapshot = fixture.reader().poll(process_key="101:10000")
        self.assertEqual(snapshot.session_id, "ABCD0123-4567-89AB-CDEF-0123456789AB")
        self.assertEqual(snapshot.map, "map_test_10v10")
        self.assertEqual(snapshot.server_name, "云端房间 🚩")
        self.assertEqual(snapshot.game_build, GAME_BUILD)
        self.assertEqual(snapshot.source_kind, "network_live")
        self.assertFalse(snapshot.is_replay)
        self.assertFalse(snapshot.game_ended)
        self.assertEqual(snapshot.participants, [{"id": "123", "name": "Alice", "alliance": 0},
                                                {"id": "18446744073709551614", "name": "红方 玩家🚩", "alliance": 1}])
        self.assertEqual(snapshot.to_dict(), asdict(snapshot))
        self.assertEqual(len(snapshot.context_epoch), 64)
        self.assertNotIn("Password", json.dumps(snapshot.to_dict()))
        self.assertNotIn("PlayerDeckContent", json.dumps(snapshot.to_dict()))
        self.assertFalse(any(address == fixture.private_value_pointer for address, _ in fixture.memory.reads))
        self.assertFalse({"setup", "manager", "context", "owner", "source"} & snapshot.to_dict().keys())

    def test_replay_requires_positive_source_and_replay_flag(self):
        fixture = SessionFixture(replay=True, ended=True)
        snapshot = fixture.reader().poll()
        self.assertEqual(snapshot.source_kind, "replay")
        self.assertTrue(snapshot.is_replay)
        self.assertTrue(snapshot.game_ended)

    def test_core_manager_vtable_is_also_verified(self):
        fixture = SessionFixture()
        fixture.memory.u64(fixture.manager, fixture.memory.image_base + NETWORK_MANAGER_VTABLE_RVAS[1])
        self.assertEqual(fixture.reader().poll().source_kind, "network_live")

    def test_unfamiliar_source_never_falls_back_to_live(self):
        fixture = SessionFixture()
        fixture.memory.u64(fixture.source, fixture.memory.image_base + 0x123456)
        reader = fixture.reader()
        with self.assertRaisesRegex(NativeReportError, "native_session_source_unknown"):
            reader.poll()
        self.assertEqual(reader.status, "native_session_source_unknown")

    def test_source_replay_flag_conflicts_and_invalid_flags_reject(self):
        for replay, flag, ended in ((True, 0, 0), (False, 1, 0), (False, 2, 0), (False, 0, 2)):
            with self.subTest(replay=replay, flag=flag, ended=ended):
                fixture = SessionFixture(replay=replay)
                fixture.memory.write(fixture.manager + 0x75, bytes([flag]))
                fixture.memory.write(fixture.context + 0x59, bytes([ended]))
                with self.assertRaises(NativeReportError):fixture.reader().poll()

    def test_owner_or_manager_layout_is_not_guessed(self):
        for attribute, expected in (("owner", "unsupported_native_session_owner"),
                                    ("manager", "unsupported_native_session_manager")):
            with self.subTest(attribute=attribute):
                fixture = SessionFixture()
                fixture.memory.u64(getattr(fixture, attribute), fixture.memory.image_base + 123456)
                with self.assertRaisesRegex(NativeReportError, expected):fixture.reader().poll()

    def test_owner_and_runtime_manager_must_reference_identical_setup(self):
        fixture = SessionFixture()
        fixture.memory.u64(fixture.manager + 0xE0, fixture.setup + 0x1000)
        with self.assertRaisesRegex(NativeReportError, "native_session_setup_mismatch"):
            fixture.reader().poll()

    def test_missing_singletons_or_setup_wait_without_publishing_identity(self):
        for target in ("owner_global", "context_global", "owner_setup", "context_manager"):
            with self.subTest(target=target):
                fixture = SessionFixture()
                address = {"owner_global": fixture.memory.image_base + LAUNCH_MAP_HELPER_RVA,
                           "context_global": fixture.memory.image_base + RUNTIME_CONTEXT_RVA,
                           "owner_setup": fixture.owner + 0x78,
                           "context_manager": fixture.context + 0xB0}[target]
                fixture.memory.u64(address, 0)
                reader = fixture.reader()
                self.assertIsNone(reader.poll())
                self.assertEqual(reader.status, "waiting_for_native_session")

    def test_missing_session_or_map_waits_and_version_is_checked(self):
        for common in ([("Map", "map_test")], [("UniqueSessionId", "opaque-id")]):
            fixture = SessionFixture(common=common)
            reader = fixture.reader()
            self.assertIsNone(reader.poll())
            self.assertEqual(reader.status, "waiting_for_native_session_identity")
        fixture = SessionFixture(common=[("UniqueSessionId", "opaque-id"), ("Map", "map_test")])
        self.assertEqual(fixture.reader().poll().game_build, GAME_BUILD)
        fixture = SessionFixture(common=[("UniqueSessionId", "opaque-id"), ("Map", "map_test"), ("Version", "1")])
        with self.assertRaisesRegex(NativeReportError, "native_session_build_mismatch"):
            fixture.reader().poll()

    def test_uuid_and_opaque_session_ids_are_preserved_exactly(self):
        for identifier in ("ABCD0123-4567-89AB-CDEF-0123456789AB", "abcdef01234567890123456789abcdef", "native:opaque-session"):
            fixture = SessionFixture(common=[("UniqueSessionId", identifier), ("Map", "map_test")])
            self.assertEqual(fixture.reader().poll().session_id, identifier)

    def test_fingerprint_gate_is_shared_with_report_reader(self):
        fixture = SessionFixture()
        fixture.memory.exe_sha256 = "a" * 64
        with self.assertRaises(UnsupportedBuildError):fixture.reader()
        self.assertEqual(fixture.memory.reads, [])

    def test_sso_capacity_fifteen_and_heap_capacity_sixteen(self):
        for value, capacity in (("a" * 15, 15), ("b" * 15, 16), ("国" * 6, 18)):
            fixture = SessionFixture(common=[("UniqueSessionId", "opaque-id"), ("Map", "map_test")])
            name = fixture.narrow(value, capacity=capacity)
            extra_key = fixture.narrow("ServerName")
            raw = fixture.memory.read(fixture.common_data, 2 * 16) + struct.pack("<QQ", extra_key, name)
            data = fixture.memory.alloc(len(raw), raw)
            fixture.memory.write(fixture.setup + 0x10, struct.pack("<I4xQ", 3, data))
            self.assertEqual(fixture.reader().poll().server_name, value)

    def test_invalid_utf8_length_capacity_and_terminator_reject(self):
        for change, expected in (("utf8", "invalid_native_session_utf8"),
                                 ("length", "invalid_native_session_string_size"),
                                 ("capacity", "invalid_native_session_string_size"),
                                 ("terminator", "invalid_native_session_string_layout")):
            with self.subTest(change=change):
                fixture = SessionFixture()
                handle = fixture.string_handles["Alice"][0]
                if change == "utf8":fixture.memory.write(handle + 0x10, b"\xff")
                elif change == "length":fixture.memory.u64(handle + 0x20, 16)
                elif change == "capacity":fixture.memory.u64(handle + 0x28, MAX_STRING_BYTES + 1)
                else:fixture.memory.write(handle + 0x10 + 5, b"X")
                with self.assertRaisesRegex(NativeReportError, expected):fixture.reader().poll()

    def test_observers_are_not_participants_and_need_no_player_id(self):
        fixture = SessionFixture(players=[(0, [("PlayerObserver", "1")]),
                                          (9, [("PlayerUserId", "000123"), ("PlayerName", "Alice"),
                                               ("PlayerAlliance", "0")])])
        self.assertEqual(fixture.reader().poll().participants, [{"id": "123", "name": "Alice", "alliance": 0}])

    def test_names_preserve_utf8_emoji_joiners_without_deriving_identity(self):
        fixture = SessionFixture(players=[(0, [("PlayerUserId", "123"), ("PlayerName", "玩家 👩‍🔬"),
                                               ("PlayerAlliance", "0")])])
        self.assertEqual(fixture.reader().poll().participants,
                         [{"id": "123", "name": "玩家 👩‍🔬", "alliance": 0}])

    def test_read_budget_and_unreadable_data_are_explicit_not_stale_snapshots(self):
        fixture = SessionFixture()
        reader = fixture.reader()
        self.assertIsNotNone(reader.poll())
        fixture.memory.u64(fixture.setup + 0x18, fixture.private_value_pointer)
        with self.assertRaisesRegex(NativeReportError, "native_memory_unreadable"):
            reader.poll()
        self.assertEqual(reader.status, "native_memory_unreadable")

        fixture = SessionFixture()
        reader = fixture.reader()
        from dataclasses import replace
        reader._reader.profile = replace(reader._reader.profile, max_read_bytes_per_poll=32)
        with self.assertRaisesRegex(NativeReportError, "native_read_budget_exceeded"):
            reader.poll()

    def test_invalid_or_duplicate_native_player_identity_rejects(self):
        cases = [([(0, [("PlayerUserId", "18446744073709551615"), ("PlayerAlliance", "0")])], "integer"),
                 ([(0, [("PlayerUserId", "123"), ("PlayerAlliance", "2")])], "integer"),
                 ([(0, [("PlayerUserId", "123"), ("PlayerAlliance", "0")]),
                   (1, [("PlayerUserId", "123"), ("PlayerAlliance", "1")])], "duplicate_native_session_player_id"),
                 ([(0, [("PlayerUserId", "123"), ("PlayerAlliance", "0")]),
                   (0, [("PlayerUserId", "456"), ("PlayerAlliance", "1")])], "duplicate_native_session_slot")]
        for players, expected in cases:
            with self.subTest(expected=expected):
                fixture = SessionFixture(players=players)
                with self.assertRaisesRegex(NativeReportError, expected):fixture.reader().poll()

    def test_program_container_budgets_reject_oversized_counts(self):
        for target, count in (("common", MAX_COMMON_SETTINGS + 1), ("players", MAX_PLAYERS + 1),
                              ("settings", MAX_PLAYER_SETTINGS + 1)):
            with self.subTest(target=target):
                fixture = SessionFixture()
                address = {"common": fixture.setup + 0x10, "players": fixture.setup,
                           "settings": fixture.players_data + 8}[target]
                fixture.memory.u32(address, count)
                with self.assertRaisesRegex(NativeReportError, "native_session_container_limit"):
                    fixture.reader().poll()

    def test_duplicate_allowed_settings_and_control_text_reject(self):
        fixture = SessionFixture(common=[("UniqueSessionId", "first"), ("UniqueSessionId", "second"), ("Map", "map")])
        with self.assertRaisesRegex(NativeReportError, "duplicate_native_session_key"):fixture.reader().poll()
        for identifier in ("bad\nvalue", "bad\u202evalue", "a" * 129):
            fixture = SessionFixture(common=[("UniqueSessionId", identifier), ("Map", "map")])
            with self.assertRaisesRegex(NativeReportError, "invalid_native_session_text"):fixture.reader().poll()

    def test_repeated_poll_never_uses_stale_string_or_source_cache(self):
        fixture = SessionFixture()
        reader = fixture.reader()
        first = reader.poll(process_key="101:10000")
        self.assertEqual(first.context_epoch, reader.poll(process_key="101:10000").context_epoch)
        self.assertNotEqual(first.context_epoch, reader.poll(process_key="101:20000").context_epoch)
        handle = fixture.string_handles[first.session_id][0]
        payload = struct.unpack("<Q", fixture.memory.read(handle + 0x10, 8))[0]
        changed = "BBCD0123-4567-89AB-CDEF-0123456789AB"
        fixture.memory.write(payload, changed.encode() + b"\0")
        fixture.memory.write(fixture.context + 0x59, b"\1")
        second = reader.poll(process_key="101:10000")
        self.assertEqual(second.session_id, changed)
        self.assertNotEqual(first.context_epoch, second.context_epoch)
        self.assertTrue(second.game_ended)
        fixture.memory.u64(fixture.source, fixture.memory.image_base + REPLAY_SOURCE_VTABLE_RVA)
        fixture.memory.write(fixture.manager + 0x75, b"\1")
        self.assertEqual(reader.poll().source_kind, "replay")

    def test_all_session_anchors_headers_and_payloads_are_double_read(self):
        targets = ("owner_global", "owner_setup", "context_manager", "manager_setup", "manager_source",
                   "source_vtable", "replay_flag", "ended_flag", "players_header", "common_header",
                   "common_entries", "player_entries", "narrow_header", "narrow_payload")
        for target in targets:
            with self.subTest(target=target):
                fixture = SessionFixture()
                m = fixture.memory
                handle = fixture.string_handles["ABCD0123-4567-89AB-CDEF-0123456789AB"][0]
                payload = struct.unpack("<Q", m.read(handle + 0x10, 8))[0]
                addresses = {"owner_global": (m.image_base + LAUNCH_MAP_HELPER_RVA, 8),
                             "owner_setup": (fixture.owner + 0x78, 8),
                             "context_manager": (fixture.context + 0xB0, 8),
                             "manager_setup": (fixture.manager + 0xE0, 8),
                             "manager_source": (fixture.manager + 0x60, 8),
                             "source_vtable": (fixture.source, 8), "replay_flag": (fixture.manager + 0x75, 1),
                             "ended_flag": (fixture.context + 0x59, 1), "players_header": (fixture.setup, 16),
                             "common_header": (fixture.setup + 0x10, 16),
                             "common_entries": (fixture.common_data, 5 * 16),
                             "player_entries": (fixture.players_data, 2 * 0x18),
                             "narrow_header": (handle + 0x10, 0x20),
                             "narrow_payload": (payload, len("ABCD0123-4567-89AB-CDEF-0123456789AB") + 1)}
                address, size = addresses[target]
                old = m.read(address, size)
                calls = [0]
                def change_on_second_read(read_address, read_size):
                    if (read_address, read_size) == (address, size):
                        calls[0] += 1
                        if calls[0] == 2:m.write(address, bytes([old[0] ^ 1]) + old[1:])
                m.before_read = change_on_second_read
                with self.assertRaises(UnstableSnapshotError):fixture.reader().poll()
                self.assertEqual(calls[0], 2)


if __name__ == "__main__":unittest.main()
