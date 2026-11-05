"""Byte-addressable memory fixtures for the native mission-report reader.

These are ABI fixtures, not exported JSON or claims of live-game validation.
All text, code bytes, interface pointers and localisation tables are read by
the public reader exactly as they would be through WindowsMemory.read().
"""
from dataclasses import replace
import struct
import unittest

from companion.native_report import (
    NativeReportError, NativeReportProfile, NativeReportReader,
    SUPPORTED_EXE_SHA256, UnstableSnapshotError, UnsupportedBuildError,
    native_time_display,
)


class ByteMemory:
    image_base = 0x140000000
    exe_sha256 = SUPPORTED_EXE_SHA256

    def __init__(self):
        self.segments = []
        self.next_address = 0x50000000
        self.regions = []
        self.reads = []
        self.before_read = None
        self.reset_count = 0

    def alloc(self, size, data=b""):
        address = self.next_address
        self.next_address += (size + 0xFFF) & ~0xFFF
        self.map(address, data + bytes(size - len(data)))
        return address

    def map(self, address, data):
        self.segments.append((address, bytearray(data)))

    def write(self, address, data):
        for base, segment in reversed(self.segments):
            if base <= address and address + len(data) <= base + len(segment):
                segment[address-base:address-base+len(data)] = data
                return
        raise OSError("fixture_write_unmapped")

    def u64(self, address, value):
        self.write(address, struct.pack("<Q", value))

    def u32(self, address, value):
        self.write(address, struct.pack("<I", value))

    def read(self, address, size):
        self.reads.append((address, size))
        if self.before_read is not None:
            self.before_read(address, size)
        for base, segment in reversed(self.segments):
            if base <= address and address + size <= base + len(segment):
                return bytes(segment[address-base:address-base+size])
        raise OSError("fixture_read_unmapped")

    def iter_readable_regions(self):
        yield from self.regions

    def reset_budget(self):
        self.reset_count += 1

    def string(self, value):
        encoded = value.encode("utf-16-le")
        length = len(encoded) // 2
        block = bytearray(0x30)
        struct.pack_into("<I", block, 0, 1)
        if length <= 7:
            block[0x10:0x10+len(encoded)+2] = encoded + b"\0\0"
            capacity = 7
        else:
            payload = self.alloc(len(encoded)+2, encoded+b"\0\0")
            struct.pack_into("<Q", block, 0x10, payload)
            capacity = length
        struct.pack_into("<QQ", block, 0x20, length, capacity)
        return self.alloc(0x30, block)

    def getter(self, rva, code):
        address = self.image_base + rva
        self.map(address, code + b"\xcc" * (16 - len(code)))
        return address


class ReportFixture:
    def __init__(self, count=2):
        self.memory = m = ByteMemory()
        self.profile = NativeReportProfile()
        self.owner_victim = m.string("红方玩家")
        self.owner_killer = m.string("Long allied player 🚩")
        self.soldier_victim = m.string("Гвардия")
        self.soldier_killer = m.string("No. 45")
        self.translation_data = self.dictionary({0x10: "Т-34/85", 0x20: "Bf 109 G-6"})
        row = bytearray(0xB8)
        struct.pack_into("<d", row, 0, 60.099998474121094)
        struct.pack_into("<QQ", row, 0x08, 0x10, self.soldier_victim)
        struct.pack_into("<Q", row, 0x50, self.owner_victim)
        struct.pack_into("<III", row, 0x40, 11, 125, 2)
        struct.pack_into("<I", row, 0x58, 123)
        row[0x5C] = 1
        struct.pack_into("<QQ", row, 0x60, 0x20, self.soldier_killer)
        struct.pack_into("<Q", row, 0xA8, self.owner_killer)
        struct.pack_into("<III", row, 0x98, 22, 90, 1)
        struct.pack_into("<I", row, 0xB0, 456)
        self.rows_address = m.alloc(max(1, count) * 0xB8, bytes(row) * count)
        model = bytearray(0xBE0)
        struct.pack_into("<I", model, 0x3C, 2)
        struct.pack_into("<Q", model, 0x40, self.owner_victim)
        struct.pack_into("<Q", model, 0x40 + 8, 1234567890)
        struct.pack_into("<I", model, 0x40 + 0x30, 0)
        struct.pack_into("<Q", model, 0x40 + 0x88, self.owner_killer)
        struct.pack_into("<Q", model, 0x40 + 0x88 + 8, 18446744073709551614)
        struct.pack_into("<I", model, 0x40 + 0x88 + 0x30, 1)
        struct.pack_into("<QII", model, 0xB80, self.rows_address if count else 0, count, count)
        self.result = m.alloc(0xBE0, model)
        self.variant = m.alloc(0x20, struct.pack("<QI4xQ", 7, 1, self.result))
        self.manual_profile = replace(self.profile, explicit_variant_address=self.variant)

    def dictionary(self, mapping, *, group=None):
        m = self.memory
        resource_getter = m.getter(0x1010, b"\x48\x8b\x41\x20\xc3")
        table = bytearray(8 + 16 * len(mapping))
        struct.pack_into("<I", table, 4, len(mapping))
        for i, (key, value) in enumerate(sorted(mapping.items())):
            encoded = value.encode("utf-16-le")
            struct.pack_into("<QII", table, 8 + i * 16, key, len(table), len(encoded)//2)
            table.extend(encoded)
        data = m.alloc(len(table), table)
        vtable = m.alloc(0x60)
        m.u64(vtable + 0x48, resource_getter)
        resource = m.alloc(0x30)
        m.u64(resource, vtable)
        m.u64(resource + 0x20, data)
        layer = m.alloc(8, struct.pack("<Q", resource))
        layers = m.alloc(8, struct.pack("<Q", layer))
        if group is not None:
            m.write(group+0x30, struct.pack("<QII", layers, 1, 1))
            return data
        self.dictionary_group = m.alloc(0x40)
        m.write(self.dictionary_group+0x30, struct.pack("<QII", layers, 1, 1))
        manager = m.alloc(0x500)
        registry = m.alloc(16, struct.pack("<QI4x", 0xAABBCCDD, 0))
        m.write(manager+0xC0, struct.pack("<I4xQ", 1, registry))
        m.u64(manager+0xD8, self.dictionary_group)
        m.map(m.image_base+self.profile.localisation_manager_rva, struct.pack("<Q", manager))
        m.map(m.image_base+self.profile.unit_dictionary_token_rva, struct.pack("<Q", 0xAABBCCDD))
        return data

    def controller(self, *, variant=None, region_size=0x100, controller_offset=0):
        m = self.memory
        service_getter = m.getter(0x1020, b"\x48\x8b\x41\x10\xc3")
        result_getter = m.getter(0x1030, b"\x48\x8b\x41\x20\xc3")
        ivtable = m.alloc(0x80)
        m.u64(ivtable+0x70, result_getter)
        interface = m.alloc(0x28)
        m.u64(interface, ivtable)
        m.u64(interface+0x20, self.variant if variant is None else variant)
        svtable = m.alloc(0x10)
        m.u64(svtable+8, service_getter)
        service = m.alloc(0x18)
        m.u64(service, svtable)
        m.u64(service+0x10, interface)
        context = m.alloc(0x50)
        m.u64(context+0x48, service)
        region = m.alloc(region_size)
        controller = region + controller_offset
        m.u64(controller, m.image_base+self.profile.controller_vtable_rva)
        m.u64(controller+0x28, context)
        m.regions.append((region, region_size))
        return controller

    def reader(self):
        return NativeReportReader(self.memory, self.manual_profile)


class NativeReportTests(unittest.TestCase):
    def test_official_scores_are_independent_u32_fields_not_adjacent_counter_or_float(self):
        fixture = ReportFixture()
        row = fixture.result + 0x40
        fixture.memory.u32(row + 0x38, 0xffffffff)
        fixture.memory.u32(row + 0x3c, 0x12345678)
        fixture.memory.u32(row + 0x40, 130)
        fixture.memory.u32(row + 0x88 + 0x38, 1040)
        fixture.memory.u32(row + 0x88 + 0x40, 0)
        report = fixture.reader().poll()
        self.assertEqual((report.players[0]["official_kill_points"],
                          report.players[0]["official_loss_points"]), (0xffffffff, 130))
        self.assertEqual((report.players[1]["official_kill_points"],
                          report.players[1]["official_loss_points"]), (1040, 0))
        self.assertIsNone(report.rows[0]["victim"]["unit_guid"])
        self.assertEqual(report.rows[0]["victim"]["identity_status"], "unresolved")
        self.assertEqual(report.rows[0]["victim"]["player_user_id"], "1234567890")
        self.assertEqual(report.metadata["report_data"], {
            "identity_source": "unavailable", "identity_status": "unavailable",
            "matched_rows": 0, "total_rows": 2})

    def test_duplicate_roster_name_never_chooses_an_arbitrary_player_id(self):
        fixture = ReportFixture()
        fixture.memory.u64(fixture.result + 0x40 + 0x88, fixture.owner_victim)
        report = fixture.reader().poll()
        self.assertIsNone(report.rows[0]["victim"]["player_user_id"])
        self.assertEqual(len(report.rows), 2)
        self.assertEqual(report.rows[0]["game_time_seconds"], report.rows[1]["game_time_seconds"])

    def test_reads_native_rows_and_current_language_strings_in_original_order(self):
        f = ReportFixture()
        reader = f.reader()
        report = reader.poll()
        self.assertEqual((report.generation, report.game_build), (7, 168061))
        self.assertEqual([r["row_index"] for r in report.rows], [0, 1])
        self.assertEqual([r["game_time_seconds"] for r in report.rows], [60.099998474121094] * 2)
        self.assertEqual(report.rows[0]["time_display"], "01:00")
        self.assertEqual(report.rows[0]["victim"]["player_name"], "红方玩家")
        self.assertEqual(report.rows[0]["victim"]["unit_name"], "Т-34/85")
        self.assertEqual(report.rows[0]["victim"]["soldier_name"], "Гвардия")
        self.assertEqual(report.rows[0]["killer"]["player_name"], "Long allied player 🚩")
        self.assertEqual(report.rows[0]["killer"]["unit_name"], "Bf 109 G-6")
        self.assertEqual(report.rows[0]["victim"]["xp_texture_token"], 125)
        self.assertEqual(report.rows[0]["victim"]["display_veterancy"], 2)
        self.assertNotIn("experience", report.rows[0]["victim"])
        self.assertTrue(report.rows[0]["victim"]["is_ace"])
        self.assertEqual(report.players, [
            {"row_index": 0, "name": "红方玩家", "player_user_id": "1234567890", "alliance": 0, "alliance_display_value": 0, "official_kill_points": 0, "official_loss_points": 0},
            {"row_index": 1, "name": "Long allied player 🚩", "player_user_id": "18446744073709551614", "alliance": 1, "alliance_display_value": 1, "official_kill_points": 0, "official_loss_points": 0},
        ])
        self.assertIsNone(reader.poll())
        self.assertEqual(reader.status, "unchanged_report")
        self.assertEqual(report.to_dict()["metadata"]["source"], "published_native_mission_report")

    def test_zero_rows_is_valid_without_translation_dictionary_lookup(self):
        f = ReportFixture(count=0)
        report = f.reader().poll()
        self.assertEqual(report.rows, [])
        self.assertEqual(report.generation, 7)
        self.assertEqual(len(report.players), 2)
        self.assertNotIn((f.memory.image_base + f.profile.localisation_manager_rva, 8), f.memory.reads)

    def test_roster_reads_setup_user_id_without_account_authority_guess(self):
        f = ReportFixture(count=0)
        f.memory.u64(f.result+0x40+8, 0x123456789ABCDEF0)
        report = f.reader().poll()
        self.assertEqual(report.players[0]["name"], "红方玩家")
        self.assertEqual(report.players[0]["player_user_id"], str(0x123456789ABCDEF0))
        self.assertTrue(all("id" not in p and "player_id" not in p for p in report.players))
        self.assertTrue(all("steam_id" not in p and "eugen_id" not in p for p in report.players))

    def test_roster_uint64_max_is_invalid_but_zero_is_not_lost(self):
        f = ReportFixture(count=0)
        f.memory.u64(f.result+0x40+8, 0xFFFFFFFFFFFFFFFF)
        f.memory.u64(f.result+0x40+0x88+8, 0)
        report = f.reader().poll()
        self.assertIsNone(report.players[0]["player_user_id"])
        self.assertEqual(report.players[1]["player_user_id"], "0")

    def test_roster_user_id_change_during_snapshot_is_rejected(self):
        f = ReportFixture(count=0)
        reads = 0
        def mutation(address, size):
            nonlocal reads
            if address == f.result and size == 0xBE0:
                reads += 1
                if reads == 2:
                    f.memory.u64(f.result+0x40+8, 9876543210)
        f.memory.before_read = mutation
        reader = f.reader()
        with self.assertRaises(UnstableSnapshotError):
            reader.poll()
        f.memory.before_read = None
        self.assertEqual(reader.poll().players[0]["player_user_id"], "9876543210")

    def test_roster_preserves_unknown_alliance_display_without_claiming_team(self):
        f = ReportFixture(count=0)
        f.memory.u32(f.result+0x40+0x30, 4)
        report = f.reader().poll()
        self.assertIsNone(report.players[0]["alliance"])
        self.assertEqual(report.players[0]["alliance_display_value"], 4)

    def test_unsupported_roster_alliance_fails_closed(self):
        f = ReportFixture(count=0)
        f.memory.u32(f.result+0x40+0x30, 2)
        with self.assertRaisesRegex(NativeReportError, "unsupported_native_player_alliance"):
            f.reader().poll()

    def test_zero_roster_with_zero_timeline_requires_no_text_reads(self):
        f = ReportFixture(count=0)
        f.memory.u32(f.result+0x3C, 0)
        f.memory.segments = [segment for segment in f.memory.segments
                             if segment[0] in (f.variant, f.result)]
        report = f.reader().poll()
        self.assertEqual((report.rows, report.players), ([], []))

    def test_build_mismatch_is_rejected_before_memory_access(self):
        memory = ByteMemory()
        memory.exe_sha256 = "f" * 64
        with self.assertRaises(UnsupportedBuildError):
            NativeReportReader(memory)
        self.assertEqual(memory.reads, [])

    def test_profile_cannot_declare_an_unknown_build_supported(self):
        memory = ByteMemory()
        memory.exe_sha256 = "f" * 64
        with self.assertRaises(UnsupportedBuildError):
            NativeReportReader(memory, replace(NativeReportProfile(), exe_sha256="f" * 64))

    def test_controller_scan_resolves_real_getter_machine_code_without_calling(self):
        f = ReportFixture()
        f.controller()
        reader = NativeReportReader(f.memory)
        report = reader.poll()
        self.assertEqual(report.result_address, f.result)
        self.assertEqual(report.metadata["locator"], "native_report_controller")
        self.assertTrue(reader.baseline_ready)

    def test_partial_scan_does_not_claim_a_confirmed_baseline(self):
        f = ReportFixture()
        f.controller(controller_offset=0x80)
        profile = replace(f.profile, scan_chunk_bytes=63, scan_bytes_per_poll=63)
        reader = NativeReportReader(f.memory, profile)
        self.assertIsNone(reader.poll())
        self.assertFalse(reader.baseline_ready)
        self.assertEqual(reader.status, "locating_report_controller")
        self.assertIsNone(reader.poll())
        report = reader.poll()
        self.assertIsNotNone(report)
        self.assertTrue(reader.baseline_ready)

    def test_vtable_split_across_read_chunks_is_found(self):
        f = ReportFixture()
        f.controller(controller_offset=56)
        reader = NativeReportReader(f.memory, replace(f.profile, scan_chunk_bytes=63))
        self.assertEqual(len(reader.poll().rows), 2)

    def test_no_controller_scan_completes_with_confirmed_absence(self):
        memory = ByteMemory()
        address = memory.alloc(128)
        memory.regions.append((address, 128))
        reader = NativeReportReader(memory)
        self.assertIsNone(reader.poll())
        self.assertTrue(reader.baseline_ready)

    def test_peek_identity_does_not_consume_report(self):
        f = ReportFixture()
        reader = f.reader()
        self.assertEqual(reader.peek_identity(), (f.variant, 7, f.result))
        self.assertIsNotNone(reader.poll())

    def test_pending_variant_is_not_report_or_empty_success(self):
        f = ReportFixture()
        f.memory.u32(f.variant+8, 0)
        self.assertIsNone(f.reader().poll())
        f.memory.u32(f.variant+8, 1)
        f.memory.u64(f.variant, 0)
        self.assertIsNone(f.reader().poll())

    def test_missing_translation_rejected_and_same_generation_can_retry(self):
        f = ReportFixture()
        f.memory.u64(f.rows_address+8, 0x99)
        reader = f.reader()
        with self.assertRaisesRegex(NativeReportError, "native_unit_translation_missing"):
            reader.poll()
        f.dictionary({0x10: "Т-34/85", 0x20: "Bf 109 G-6", 0x99: "新译名"},
                     group=f.dictionary_group)
        self.assertEqual(reader.poll().rows[0]["victim"]["unit_name"], "新译名")

    def test_newest_dictionary_layer_overrides_older_translation(self):
        f = ReportFixture()
        m = f.memory
        old = m.read(f.dictionary_group+0x30, 0x10)
        old_layers = struct.unpack_from("<Q", old)[0]
        old_layer = struct.unpack("<Q", m.read(old_layers, 8))[0]
        f.dictionary({0x10: "override"}, group=f.dictionary_group)
        new_layers = struct.unpack("<Q", m.read(f.dictionary_group+0x30, 8))[0]
        new_layer = struct.unpack("<Q", m.read(new_layers, 8))[0]
        layers = m.alloc(16, struct.pack("<QQ", old_layer, new_layer))
        m.write(f.dictionary_group+0x30, struct.pack("<QII", layers, 2, 2))
        report = f.reader().poll()
        self.assertEqual(report.rows[0]["victim"]["unit_name"], "override")
        self.assertEqual(report.rows[0]["killer"]["unit_name"], "Bf 109 G-6")

    def test_unsupported_resource_getter_fails_closed(self):
        f = ReportFixture()
        f.memory.write(f.memory.image_base+0x1010, b"\xe8\x00\x00\x00\x00\xc3")
        with self.assertRaisesRegex(NativeReportError, "unsupported_native_getter"):
            f.reader().poll()

    def test_invalid_utf16_cannot_be_reported_as_decoded_text(self):
        f = ReportFixture()
        f.memory.write(f.owner_victim+0x10, b"\x00\xd8")
        with self.assertRaisesRegex(NativeReportError, "invalid_native_utf16"):
            f.reader().poll()

    def test_invalid_text_layout_and_terminator_rejected(self):
        f = ReportFixture()
        f.memory.u64(f.owner_victim+0x20, 9)
        with self.assertRaisesRegex(NativeReportError, "unsupported_native_text_layout"):
            f.reader().poll()
        f.memory.u64(f.owner_victim+0x20, 4)
        f.memory.write(f.owner_victim+0x18, b"\x01\x00")
        with self.assertRaisesRegex(NativeReportError, "invalid_native_text_terminator"):
            f.reader().poll()

    def test_in_place_row_change_is_detected_with_unchanged_header(self):
        f = ReportFixture()
        times = 0
        def mutation(address, size):
            nonlocal times
            if address == f.rows_address and size == 2 * 0xB8:
                times += 1
                if times == 2:
                    f.memory.write(f.rows_address+0x44, struct.pack("<I", 999))
        f.memory.before_read = mutation
        reader = f.reader()
        with self.assertRaises(UnstableSnapshotError):
            reader.poll()
        f.memory.before_read = None
        self.assertEqual(reader.poll().rows[0]["victim"]["xp_texture_token"], 999)

    def test_string_change_detected_even_with_unchanged_string_header(self):
        f = ReportFixture()
        times = 0
        def mutation(address, size):
            nonlocal times
            if address == f.owner_victim+0x10:
                times += 1
                if times == 2:
                    f.memory.write(f.owner_victim+0x10, "绿".encode("utf-16-le"))
        f.memory.before_read = mutation
        with self.assertRaises(UnstableSnapshotError):
            f.reader().poll()

    def test_generation_change_during_copy_is_detected(self):
        f = ReportFixture()
        times = 0
        def mutation(address, size):
            nonlocal times
            if address == f.variant:
                times += 1
                if times == 3:
                    f.memory.u64(f.variant, 8)
        f.memory.before_read = mutation
        with self.assertRaises(UnstableSnapshotError):
            f.reader().poll()

    def test_bounds_nonfinite_and_out_of_order_are_rejected(self):
        f = ReportFixture()
        f.memory.write(f.result+0xB88, struct.pack("<II", 3, 2))
        with self.assertRaisesRegex(NativeReportError, "invalid_native_timeline_size"):
            f.reader().poll()
        f.memory.write(f.result+0xB88, struct.pack("<II", 2, 2))
        f.memory.write(f.rows_address, struct.pack("<d", float("nan")))
        with self.assertRaisesRegex(NativeReportError, "invalid_native_timeline_time"):
            f.reader().poll()
        f.memory.write(f.rows_address, struct.pack("<d", 100.0))
        with self.assertRaisesRegex(NativeReportError, "unordered_native_timeline"):
            f.reader().poll()

    def test_report_copy_read_budget_fails_explicitly(self):
        f = ReportFixture()
        reader = NativeReportReader(f.memory, replace(f.manual_profile, max_read_bytes_per_poll=24))
        with self.assertRaisesRegex(NativeReportError, "native_read_budget_exceeded"):
            reader.poll()

    def test_small_scan_allowance_yields_and_resumes_without_losing_boundary(self):
        f = ReportFixture();f.controller()
        reader = NativeReportReader(f.memory, replace(f.profile, max_scan_bytes=1))
        for _ in range(7):
            self.assertIsNone(reader.poll())
            self.assertFalse(reader.baseline_ready)
        self.assertEqual(reader.poll().result_address, f.result)
        self.assertEqual(reader.diagnostics()['scan_bytes_total'], 8)

    def test_controller_after_two_gibibytes_is_reached_in_bounded_polls(self):
        f = ReportFixture()
        m = f.memory
        m.next_address = 0x180000000
        f.controller()
        sparse_base, sparse_size = 0x80000000, 2 * 1024**3 + 1024**2
        m.regions.insert(0, (sparse_base, sparse_size))
        original = m.read
        def sparse_read(address, size):
            if sparse_base <= address and address + size <= sparse_base + sparse_size:
                return bytes(size)
            return original(address, size)
        m.read = sparse_read
        reader = NativeReportReader(m)
        report = None
        for _ in range(70):
            report = reader.poll()
            self.assertLessEqual(reader.diagnostics()['read_bytes_last_poll'], reader.profile.max_read_bytes_per_poll)
            if report is not None:break
            self.assertFalse(reader.baseline_ready)
        self.assertIsNotNone(report)
        self.assertEqual(report.result_address, f.result)
        self.assertGreater(reader.diagnostics()['scan_bytes_total'], 2 * 1024**3)

    def test_completed_scan_reenumerates_and_finds_new_region(self):
        f = ReportFixture()
        m = f.memory
        original_region = m.alloc(64)
        m.regions.append((original_region, 64))
        reader = NativeReportReader(m, replace(f.profile, scan_bytes_per_poll=32))
        self.assertIsNone(reader.poll());self.assertFalse(reader.baseline_ready)
        self.assertIsNone(reader.poll());self.assertTrue(reader.baseline_ready)
        self.assertEqual(reader.diagnostics()['scan_rounds_completed'], 1)
        f.controller()
        self.assertIsNone(reader.poll())
        self.assertFalse(reader.baseline_ready)
        for _ in range(10):
            report = reader.poll()
            if report is not None:break
        self.assertIsNotNone(report)

    def test_candidate_budget_suspends_a_block_instead_of_skipping_controller(self):
        f = ReportFixture()
        controller = f.controller(controller_offset=0x80)
        m = f.memory
        region = controller - 0x80
        # An aligned but invalid candidate comes before the genuine object.
        m.u64(region, m.image_base + f.profile.controller_vtable_rva)
        reader = NativeReportReader(m, replace(f.profile, max_candidates=1))
        self.assertIsNone(reader.poll())
        self.assertFalse(reader.baseline_ready)
        self.assertEqual(reader.poll().result_address, f.result)
        self.assertEqual(reader.diagnostics()['candidates_seen'], 2)

    def test_scan_read_limit_resumes_cached_block_on_next_poll(self):
        f = ReportFixture();f.controller(region_size=0x1000, controller_offset=0xE00)
        reader = NativeReportReader(f.memory, replace(f.profile, scan_bytes_per_poll=0x1000,
                                                     max_read_bytes_per_poll=0x2000))
        # Force the scan to use all current read I/O, then let a fresh poll
        # validate its cached candidate with the normal report-copy budget.
        reader.profile = replace(reader.profile, max_read_bytes_per_poll=0x1000)
        self.assertIsNone(reader.poll());self.assertFalse(reader.baseline_ready)
        reader.profile = replace(reader.profile, max_read_bytes_per_poll=64 * 1024**2)
        self.assertEqual(reader.poll().result_address, f.result)

    def test_single_read_limit_and_unreadable_region_do_not_stall_scan(self):
        f = ReportFixture();f.controller(controller_offset=0x80)
        m = f.memory
        m.max_single_read = 63
        m.regions.insert(0, (0x10000000, 128))  # Unmapped/freed after enumeration.
        reader = NativeReportReader(m)
        self.assertIsNotNone(reader.poll())
        scan_reads = [size for address, size in m.reads
                      if 0x10000000 <= address < 0x10000080]
        self.assertTrue(scan_reads)
        self.assertLessEqual(max(scan_reads), 63)

    def test_diagnostics_contains_only_safe_progress_counters(self):
        f = ReportFixture();f.controller(controller_offset=0x80)
        reader = NativeReportReader(f.memory, replace(f.profile, scan_bytes_per_poll=16))
        self.assertIsNone(reader.poll())
        progress = reader.diagnostics()
        self.assertEqual(progress['scan_bytes_total'], 16)
        self.assertEqual(progress['scan_bytes_planned'], 0x100)
        self.assertEqual(progress['regions_total'], 1)
        self.assertEqual(progress['regions_completed'], 0)
        self.assertTrue(all(type(value) in (str, int, bool) for value in progress.values()))
        self.assertFalse(any(word in key for key in progress for word in ('address', 'pointer', 'name', 'path')))

    def test_original_minute_mode_time_format(self):
        self.assertEqual(native_time_display(59.999999999), "01:00")  # f32 rounds to60.
        self.assertEqual(native_time_display(60.9), "01:00")
        self.assertEqual(native_time_display(3600), "60:00")
        self.assertEqual(native_time_display(6000), "100:00")
        self.assertEqual(native_time_display(60000), "00:00")


if __name__ == "__main__":
    unittest.main()
