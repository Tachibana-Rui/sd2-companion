import json
import struct
import unittest

from replay_stream import ReplayStreamError, inspect_stream
from test_replay_orders import production


def block(tag, payload, reserved=0):
    return struct.pack(">4sQI", tag, len(payload), reserved) + payload


def star(tick, transitions=()):
    commands = b"".join(
        struct.pack("<IHiiiii", 5, 0, -1, before, after, -1, tick)
        for before, after in transitions
    )
    return block(b"star", struct.pack("<II", tick, len(transitions)) + commands)


def pact(tick, slot, commands=b"", count=0):
    return block(b"pact", struct.pack("<IIIH", tick, slot, count, len(commands)) + commands)


def endt(tick):
    return block(b"endt", struct.pack("<III", tick, 0, 0))


def footer(value=None):
    return block(b"rslt", json.dumps(value or {"result": {"Duration": "1800", "Victory": "4", "Score": "2000"}}).encode())


class StreamTests(unittest.TestCase):
    def assert_code(self, data, expected, offset=0):
        with self.assertRaises(ReplayStreamError) as captured:
            inspect_stream(data, offset, 10, 20)
        self.assertEqual(captured.exception.code, expected)

    def test_server_without_footer_is_valid_and_completed_by_native_states(self):
        data = b"preamble" + star(0, [(51, 52)]) + pact(0, 0) + endt(0)
        data += star(1, [(52, 53)]) + pact(1, 0, b"\x2a\x00", 1) + endt(1)
        data += star(2, [(53, 101)]) + endt(2)
        result = inspect_stream(data, 8, 10, 20)
        self.assertEqual(result["tick_count"], 3)
        self.assertEqual(result["frame_count"], 8)
        self.assertEqual(result["completion_evidence"], "native_state_53_to_101")
        self.assertEqual(result["battle_duration_seconds"], 0.1)
        self.assertIsNone(result["result"])
        self.assertEqual(result["player_slots"]["0"]["raw_command_count"], 1)
        self.assertEqual(result["player_slots"]["0"]["battle_packet_count"], 1)
        self.assertEqual(result["player_slots"]["0"]["battle_raw_command_count"], 1)
        self.assertEqual(result["parsed_bytes"], len(data) - 8)
        self.assertFalse(result["capabilities"]["actual_purchases"])
        self.assertFalse(result["capabilities"]["kill_attribution"])

    def test_eof_at_endt_does_not_prove_normal_completion(self):
        result = inspect_stream(star(0, [(52, 53)]) + endt(0), 0)
        self.assertEqual(result["framing_status"], "validated")
        self.assertEqual(result["completion_evidence"], "unverified")
        self.assertIsNone(result["recorded_timeline_seconds"])

    def test_client_footer_values_are_numeric_and_ignore_unknown_fields(self):
        data = star(0) + endt(0) + footer({"result": {"Duration": "1800.5", "Victory": 4, "Score": "2000", "name": "private"}})
        result = inspect_stream(data, 0, 10)
        self.assertEqual(result["completion_evidence"], "client_result_footer")
        self.assertEqual(result["result"], {"Duration": 1800.5, "Victory": 4, "Score": 2000})
        self.assertNotIn("private", json.dumps(result))

    def test_shutdown_system_sentinel_may_repeat(self):
        data = star(0) + pact(0, 0xFFFFFFFF) + pact(0, 0xFFFFFFFF) + endt(0)
        result = inspect_stream(data, 0)
        self.assertEqual(result["system_slot_orders"]["packet_count"], 2)
        self.assertEqual(result["player_slots"], {})

    def test_valid_changing_active_slots_and_endt_flags(self):
        data = star(0) + pact(0, 1) + endt(0) + star(1) + pact(1, 2)
        data += block(b"endt", struct.pack("<III", 1, 128, 0))
        result = inspect_stream(data, 0, expected_player_count=3)
        self.assertEqual(set(result["player_slots"]), {"1", "2"})
        self.assertEqual(result["last_tick"], 1)

    def test_unknown_system_kind_is_counted_without_invented_semantics(self):
        command = struct.pack("<IHiiiii", 123, 0, -1, 1, 2, 3, 0)
        data = block(b"star", struct.pack("<II", 0, 1) + command) + endt(0)
        result = inspect_stream(data, 0)
        self.assertEqual(result["unknown_system_command_count"], 1)
        self.assertEqual(result["game_state_transitions"], [])

    def test_raw_orders_are_counted_without_exposing_payload(self):
        private = b"private content"
        result = inspect_stream(star(0) + pact(0, 0, private, 1) + endt(0), 0)
        self.assertEqual(result["raw_command_bytes"], len(private))
        self.assertNotIn(private.decode(), json.dumps(result))

    def test_item_finished_before_production_does_not_confirm_spawn(self):
        item = b"\x11" + struct.pack("<6I", 0, 0xC0000020, 0, 0, 0, 50)
        data = star(0, [(51, 52)]) + pact(0, 0, item, 1) + endt(0)
        data += star(1) + pact(1, 0, production(), 1) + endt(1)
        result = inspect_stream(data, 0, 10)
        stats = result["action_stats"]
        self.assertTrue(stats["wire_decode_complete"])
        self.assertEqual(stats["item_finished"]["before_first_production_count"], 1)
        self.assertEqual(stats["item_finished"]["raw_value_overlap_with_prior_ground_groups"], 0)
        self.assertIsNone(stats["item_finished"]["verified_unit_completion_count"])
        self.assertIsNone(stats["unit_spawn_count"])
        self.assertEqual(result["item_finished_orders"][0]["stream_command_index"], 0)
        self.assertEqual(result["production_orders"][0]["stream_command_index"], 1)

    def test_partial_action_stream_does_not_report_zero_spawns_as_verified(self):
        result = inspect_stream(star(0) + pact(0, 0, b"\xff", 1) + endt(0), 0)
        self.assertFalse(result["action_stats"]["wire_decode_complete"])
        self.assertIsNone(result["action_stats"]["explicit_spawn_instruction_count"])

    def test_raw_id_overlap_is_not_a_verified_unit_completion(self):
        item = b"\x11" + struct.pack("<6I", 100, 0xC0000020, 0, 0, 0, 50)
        data = star(0) + pact(0, 0, production() + item, 2) + endt(0)
        result = inspect_stream(data, 0)
        stats = result["action_stats"]["item_finished"]
        self.assertEqual(stats["raw_value_overlap_with_prior_ground_groups"], 1)
        self.assertIsNone(stats["verified_unit_completion_count"])

    def test_truncated_payload(self):
        self.assert_code((star(0) + endt(0))[:-1], "truncated_block_payload")

    def test_truncated_header(self):
        self.assert_code(star(0) + endt(0) + b"x", "truncated_block_header")

    def test_oversized_block_is_rejected_before_allocation(self):
        self.assert_code(struct.pack(">4sQI", b"star", 2**63, 0), "block_too_large")

    def test_unknown_tag(self):
        self.assert_code(block(b"xxxx", b""), "unsupported_block_tag")

    def test_reserved_schema_mismatch(self):
        self.assert_code(block(b"star", b"", reserved=1), "unsupported_block_reserved")

    def test_nonsequential_tick(self):
        self.assert_code(star(0) + endt(0) + star(2) + endt(2), "nonsequential_tick")

    def test_pact_outside_tick(self):
        self.assert_code(pact(0, 0), "unexpected_or_short_pact")

    def test_missing_endt(self):
        self.assert_code(star(0) + star(1), "missing_endt")
        self.assert_code(star(0), "missing_final_endt")

    def test_star_count_mismatch(self):
        self.assert_code(block(b"star", struct.pack("<II", 0, 2)), "invalid_system_command_length")

    def test_system_repeated_tick_mismatch(self):
        command = struct.pack("<IHiiiii", 5, 0, -1, 52, 53, -1, 1)
        self.assert_code(block(b"star", struct.pack("<II", 0, 1) + command), "system_command_tick_mismatch")

    def test_state_transition_sequence_is_consistent(self):
        self.assert_code(star(0, [(51, 52)]) + endt(0) + star(1, [(53, 101)]) + endt(1), "inconsistent_game_state_transition")

    def test_player_command_size_and_count_mismatch(self):
        bad = block(b"pact", struct.pack("<IIIH", 0, 0, 1, 7) + b"xx")
        self.assert_code(star(0) + bad, "invalid_player_command_length")
        self.assert_code(star(0) + pact(0, 0, b"xx", 0), "invalid_player_command_count")

    def test_pact_and_endt_ticks_match_star(self):
        self.assert_code(star(0) + pact(1, 0), "pact_tick_mismatch")
        self.assert_code(star(0) + endt(1), "endt_tick_mismatch")

    def test_player_slot_bounds_and_duplicates(self):
        self.assert_code(star(0) + pact(0, 20), "player_slot_out_of_range")
        self.assert_code(star(0) + pact(0, 0) + pact(0, 0), "duplicate_player_slot_in_tick")

    def test_result_must_be_after_closed_tick_and_last(self):
        self.assert_code(star(0) + footer(), "unexpected_result")
        self.assert_code(star(0) + endt(0) + footer() + star(1), "data_after_result")

    def test_result_payload_and_shape_fail_safely(self):
        self.assert_code(star(0) + endt(0) + block(b"rslt", b"broken"), "invalid_result_json")
        self.assert_code(star(0) + endt(0) + footer({"else": 1}), "invalid_result_shape")
        self.assert_code(star(0) + endt(0) + footer({"result": {"Duration": "nan", "Victory": 1, "Score": 1}}), "invalid_result_value")

    def test_bad_arguments(self):
        data = star(0) + endt(0)
        for value in (-1, len(data), True, "0"):
            with self.assertRaises(ValueError):
                inspect_stream(data, value)
        for rate in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                inspect_stream(data, 0, rate)


if __name__ == "__main__":
    unittest.main()
